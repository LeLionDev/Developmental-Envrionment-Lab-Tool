"""Server-side entry point for clients that connect over SSH.

Clients run this script on the receiving host through ssh. It never touches the database:
it only converts SSH traffic into the same files the filesystem clients use, so the
existing server loop (poll req_*.json -> update db -> export status.json) is unchanged.

Usage:
    python3 ssh_receiver.py /path/to/poor_man.json submit   < request.json
    python3 ssh_receiver.py /path/to/poor_man.json status
    python3 ssh_receiver.py /path/to/poor_man.json watch

submit -- validate one JSON request from stdin and drop it into the queue as req_*.json.
status -- print the current status snapshot once.
watch  -- print a status line whenever status.json changes, plus periodic heartbeats,
          until the client disconnects.

Every line written to stdout is one JSON object:
    {"type": "status", "status": {"labA": null, "labB": "Alice"}}
    {"type": "heartbeat"}
"""
import json
import os
import sys
import time

import messages

EXIT_OK = 0
EXIT_INVALID_REQUEST = 2
EXIT_USAGE = 64

WATCH_POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0

USAGE = "usage: ssh_receiver.py <poor_man.json> {submit|status|watch}"


def load_queue_root(config_path):
    """Read the queue directory from the same poor_man.json the server uses."""
    with open(config_path) as f:
        return json.load(f)["poor_man_message_queue_root"]


def write_line(out, message):
    """Write one JSON message as a single line and flush so the client sees it immediately."""
    out.write(json.dumps(message) + "\n")
    out.flush()


def status_message(status):
    """Wrap a status snapshot in the stream envelope."""
    return {"type": "status", "status": status}


def handle_submit(root_dir, stdin, stdout, stderr):
    """Validate the request on stdin and queue it for the server. Returns an exit code."""
    try:
        request = messages.validate_request(json.load(stdin))
    except json.JSONDecodeError as e:
        stderr.write(f"Request is not valid JSON: {e}\n")
        return EXIT_INVALID_REQUEST
    except messages.InvalidRequestError as e:
        stderr.write(f"Invalid request: {e}\n")
        return EXIT_INVALID_REQUEST

    messages.write_request_file(root_dir, request)
    write_line(stdout, {"type": "accepted"})
    return EXIT_OK


def handle_status(root_dir, stdout):
    """Print the current status snapshot once (an empty snapshot if none exists yet)."""
    write_line(stdout, status_message(messages.read_status_file(root_dir) or {}))
    return EXIT_OK


class StatusStreamer:
    """Decides, on each tick, whether to send a status update or a heartbeat.

    Kept separate from the loop and the clock so its behaviour can be tested tick by tick.
    A status line is sent only when status.json was modified AND its contents differ from
    what was last sent. A heartbeat is sent when nothing has been written for
    `heartbeat_interval` seconds; besides reassuring the client, the write fails once the
    client has gone, which is how an abandoned `watch` process notices and exits.
    """

    def __init__(self, root_dir, out, clock=time.monotonic,
                 heartbeat_interval=HEARTBEAT_SECONDS):
        self.root_dir = root_dir
        self.out = out
        self.clock = clock
        self.heartbeat_interval = heartbeat_interval
        self._last_modified = None
        self._last_status = None
        self._last_write = clock()

    def tick(self):
        """Run one check; returns "status", "heartbeat" or None depending on what was sent."""
        status = self._read_if_changed()
        if status is not None and status != self._last_status:
            self._send(status_message(status))
            self._last_status = status
            return "status"
        if self.clock() - self._last_write >= self.heartbeat_interval:
            self._send({"type": "heartbeat"})
            return "heartbeat"
        return None

    def _read_if_changed(self):
        """Return the snapshot if status.json's modified time changed, else None.

        A failed read (usually the server mid-write) leaves the remembered modified time
        alone, so the same change is retried on the next tick.
        """
        path = messages.status_path(self.root_dir)
        try:
            modified = os.path.getmtime(path)
        except OSError:
            return None
        if modified == self._last_modified:
            return None
        try:
            status = messages.read_status_file(self.root_dir)
        except (OSError, json.JSONDecodeError):
            return None
        self._last_modified = modified
        return status

    def _send(self, message):
        """Write a message and remember when, for heartbeat timing."""
        write_line(self.out, message)
        self._last_write = self.clock()


def handle_watch(root_dir, stdout, sleep=time.sleep, poll_seconds=WATCH_POLL_SECONDS):
    """Stream status changes until the client disconnects (detected as a broken pipe)."""
    streamer = StatusStreamer(root_dir, stdout)
    try:
        while True:
            streamer.tick()
            sleep(poll_seconds)
    except (BrokenPipeError, ConnectionResetError):
        return EXIT_OK


def main(argv, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr):
    """Parse the command line and dispatch to the matching handler. Returns an exit code."""
    if len(argv) != 2 or argv[1] not in ("submit", "status", "watch"):
        stderr.write(USAGE + "\n")
        return EXIT_USAGE

    config_path, command = argv
    root_dir = load_queue_root(config_path)
    if command == "submit":
        return handle_submit(root_dir, stdin, stdout, stderr)
    if command == "status":
        return handle_status(root_dir, stdout)
    return handle_watch(root_dir, stdout)


def run_from_command_line():
    """Entry point when executed by ssh; silences the broken-pipe noise from a gone client.

    After the client disconnects, Python would otherwise print a BrokenPipeError while
    flushing stdout at exit. Pointing stdout at devnull first is the documented fix.
    """
    exit_code = main(sys.argv[1:])
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return exit_code


if __name__ == "__main__":
    sys.exit(run_from_command_line())
