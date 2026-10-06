"""How the client talks to the server: either through the shared directory or over SSH.

Both transports expose the same three methods, so the GUI never needs to know which one
it is using:

    send_request(request)                           -- deliver one checkout/release request
    watch_status(on_status, on_problem, stop_event) -- block, calling on_status with every
                                                       new snapshot until stop_event is set
    close()                                         -- release any resources (e.g. kill ssh)

watch_status is meant to be run on a background thread. The callbacks are invoked from
that thread, so GUI code must hand the results over to its own thread (see the client).
"""
import json
import os
import random
import subprocess
import tempfile
import threading

import client_config
import messages

# Keeps a console window from flashing up on Windows when the packaged (windowed) client
# launches ssh. Zero, i.e. no effect, on other platforms.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# BatchMode: fail instead of prompting for a password, since there is no terminal to type
#   into. StrictHostKeyChecking=accept-new: trust a host key the first time it is seen,
#   refuse it if it later changes. ServerAlive*: if the server goes silent for ~45s, ssh
#   exits, which is how a silently dropped stream gets noticed and reconnected.
SSH_OPTIONS = (
    "BatchMode=yes",
    "StrictHostKeyChecking=accept-new",
    "ConnectTimeout=10",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=3",
)
SEND_TIMEOUT_SECONDS = 30


class TransportError(Exception):
    """Raised when a request could not be delivered to the server."""


def backoff_delay(attempt, base=1.0, cap=60.0, jitter=0.2, rng=random.random):
    """Seconds to wait before reconnect attempt number `attempt` (0-based).

    Doubles each attempt (1, 2, 4, ... capped at `cap`) and adds +/- `jitter` randomness so
    many clients that lost the connection together do not all reconnect at the same moment.
    """
    delay = min(cap, base * (2 ** attempt))
    return delay * (1 + jitter * (2 * rng() - 1))


class FileSystemTransport:
    """The original transport: requests and status go through the shared directory."""

    def __init__(self, root_dir, poll_interval=3.0):
        self.root_dir = root_dir
        self.poll_interval = poll_interval

    def send_request(self, request):
        """Write the request into the shared queue directory."""
        messages.write_request_file(self.root_dir, request)

    def watch_status(self, on_status, on_problem, stop_event):
        """Check status.json every poll_interval seconds and report it whenever it changes.

        A change is detected by the file's modified time, exactly as the client always has.
        If the read fails (e.g. the server is mid-write) the change is retried next poll.
        """
        last_modified = None
        while not stop_event.is_set():
            try:
                path = messages.status_path(self.root_dir)
                modified = os.path.getmtime(path) if os.path.exists(path) else None
                if modified != last_modified:
                    status = messages.read_status_file(self.root_dir)
                    if status is not None:
                        on_status(status)
                    last_modified = modified
            except Exception as e:
                on_problem(f"Could not read status: {e}")
            stop_event.wait(self.poll_interval)

    def close(self):
        """Nothing to release for the filesystem transport."""


def build_ssh_command(settings, remote_command):
    """Build the argument list for running `remote_command` on the receiving host.

    The command hops through the gateway with -J. Arguments are passed as a list (never
    through a local shell), and the hostnames were validated in client_config, so nothing
    the user typed can be interpreted as an extra ssh option.
    """
    prefix = f"{settings.user}@" if settings.user else ""
    command = ["ssh", "-J", prefix + settings.gateway, "-p", str(settings.port)]
    for option in SSH_OPTIONS:
        command += ["-o", option]
    command += [prefix + settings.host, remote_command]
    return command


def parse_stream_line(line):
    """Classify one line of `ssh_receiver.py watch` output.

    Returns ("status", dict) for a status snapshot, ("heartbeat", None) for a keep-alive,
    ("blank", None) for an empty line, and ("text", str) for anything else -- which is
    usually an ssh error message such as "Permission denied (publickey)".
    """
    text = line.strip()
    if not text:
        return "blank", None
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return "text", text
    if isinstance(message, dict):
        if message.get("type") == "status" and isinstance(message.get("status"), dict):
            return "status", message["status"]
        if message.get("type") == "heartbeat":
            return "heartbeat", None
    return "text", text


class SshTransport:
    """Talks to ssh_receiver.py on the receiving host, through the gateway.

    Requests are one short ssh call each. Status is streamed: one long-lived ssh process
    runs `ssh_receiver.py watch`, which prints a line every time status.json changes. If
    that process ends for any reason it is restarted with exponential backoff.

    `run` and `popen` default to the real subprocess functions and exist so tests can
    substitute fakes without opening real connections.
    """

    def __init__(self, settings, run=subprocess.run, popen=subprocess.Popen,
                 rng=random.random):
        self.settings = settings
        self._run = run
        self._popen = popen
        self._rng = rng
        self._lock = threading.Lock()
        self._process = None
        self._closed = False

    def _remote(self, subcommand):
        """Full remote command line for a receiver subcommand (submit/status/watch)."""
        return f"{self.settings.receiver_command} {subcommand}"

    def send_request(self, request):
        """Send one request to the receiver's `submit` command, raising TransportError on failure."""
        command = build_ssh_command(self.settings, self._remote("submit"))
        try:
            result = self._run(
                command,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=SEND_TIMEOUT_SECONDS,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise TransportError(f"Timed out after {SEND_TIMEOUT_SECONDS}s sending request")
        except OSError as e:
            raise TransportError(f"Could not start ssh: {e}")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise TransportError(detail or f"ssh exited with code {result.returncode}")

    def watch_status(self, on_status, on_problem, stop_event):
        """Keep a status stream open until stop_event is set, reconnecting when it drops.

        The backoff resets whenever a connection delivered at least one snapshot, so a
        healthy connection that later drops retries quickly, while a host that keeps
        refusing us is retried less and less often (up to once a minute).
        """
        attempt = 0
        while not stop_event.is_set():
            received = self._stream_once(on_status, on_problem)
            if stop_event.is_set():
                break
            attempt = 0 if received else attempt + 1
            delay = backoff_delay(attempt, rng=self._rng)
            on_problem(f"Status stream disconnected, retrying in {delay:.0f}s")
            stop_event.wait(delay)

    def _stream_once(self, on_status, on_problem):
        """Run one `watch` ssh process to completion. Returns True if any snapshot arrived.

        ssh's own error output (e.g. "Permission denied") goes to a temporary file rather
        than a pipe: helper processes such as the -J jump connection inherit it, and if it
        were a pipe they could keep it open after ssh is stopped and block shutdown. The
        last error line is reported through on_problem once the process ends.
        """
        command = build_ssh_command(self.settings, self._remote("watch"))
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as error_log:
            try:
                process = self._popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=error_log,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=_NO_WINDOW,
                )
            except OSError as e:
                on_problem(f"Could not start ssh: {e}")
                return False

            with self._lock:
                if self._closed:
                    _release(process)
                    return False
                self._process = process

            received = False
            last_text = None
            try:
                for line in process.stdout:
                    kind, payload = parse_stream_line(line)
                    if kind == "status":
                        received = True
                        on_status(payload)
                    elif kind == "text":
                        last_text = payload
            finally:
                with self._lock:
                    self._process = None
                _release(process)

            last_text = _last_line(error_log) or last_text

        if last_text:
            on_problem(last_text)
        return received

    def close(self):
        """Stop the status stream; any later watch attempt exits immediately."""
        with self._lock:
            self._closed = True
            process = self._process
        if process is not None:
            _terminate(process)


def _terminate(process):
    """Stop a child process if it is still running and reap it so no zombie is left.

    Deliberately does not close the process's stdout: close() calls this from the GUI
    thread while the watch thread may still be reading it. The reading thread closes the
    pipe itself (see _release) once it has stopped reading.
    """
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _release(process):
    """Stop the process and close its stdout. Only call from the thread reading stdout."""
    _terminate(process)
    if process.stdout is not None and hasattr(process.stdout, "close"):
        process.stdout.close()


def _last_line(file):
    """Return the last non-blank line written to a file, or None if it is empty."""
    file.seek(0)
    lines = [line.strip() for line in file.read().splitlines() if line.strip()]
    return lines[-1] if lines else None


def create_transport(config):
    """Build the transport poor_man.json asks for.

    Raises client_config.ConfigError if SSH is selected but its settings are incomplete or
    invalid, so the caller can prompt the user instead of failing later on a bad connection.
    """
    if client_config.get_transport_mode(config) == client_config.TRANSPORT_FILESYSTEM:
        return FileSystemTransport(config[client_config.QUEUE_ROOT_KEY])

    settings = client_config.get_ssh_settings(config)
    problems = client_config.validate_ssh_settings(settings)
    if problems:
        raise client_config.ConfigError(" ".join(problems))
    return SshTransport(settings)
