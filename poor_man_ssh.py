"""
SSH transport for lab requests.

Instead of dropping req_*.json files into the shared directory and waiting for the next
poll, a client opens an SSH connection to the server and sends each request as the exec
command, e.g.

    {"action": "checkout", "user": "Jane Doe", "lab": "labA", "timestamp": "..."}

The server applies it immediately and replies on stdout with one JSON line:

    {"ok": true, "message": "Jane Doe checked out labA", "status": {"labA": "Jane Doe", ...}}

Supported actions are "checkout", "release" and "status" (read-only). The exit status is
0 when "ok" is true, 1 otherwise. Clients keep one connection open and run each request
on a new channel, so status polling doesn't pay for a new handshake every time.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import threading

import paramiko

logger = logging.getLogger("poor_man_server")

DEFAULT_SSH_PORT = 2222
DEFAULT_SSH_USERNAME = "lab_tool"
COMMAND_WAIT_TIMEOUT = 10


def key_fingerprint(key):
    """OpenSSH-style SHA256 fingerprint, e.g. 'SHA256:abc...'."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def load_or_create_host_key(path):
    if os.path.exists(path):
        return paramiko.RSAKey(filename=path)
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(path)
    logger.info(f"Generated new SSH host key at {path}")
    return key


class _ConnectionHandler(paramiko.ServerInterface):
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.commands = {}
        self.command_ready = threading.Condition()

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        username_ok = hmac.compare_digest(username.encode(), self.username.encode())
        password_ok = hmac.compare_digest(password.encode(), self.password.encode())
        if username_ok and password_ok:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        with self.command_ready:
            self.commands[channel.get_id()] = command
            self.command_ready.notify_all()
        return True

    def wait_for_command(self, channel, timeout):
        with self.command_ready:
            self.command_ready.wait_for(
                lambda: channel.get_id() in self.commands or channel.closed, timeout)
            return self.commands.pop(channel.get_id(), None)


class SSHRequestServer:
    """Accepts SSH connections and hands each exec'd JSON request to handle_request."""

    def __init__(self, handle_request, host_key, username, password,
                 address="0.0.0.0", port=DEFAULT_SSH_PORT):
        if not password:
            raise ValueError("An SSH password is required to run the SSH request server")
        self.handle_request = handle_request
        self.host_key = host_key
        self.username = username
        self.password = password
        self.address = address
        self.port = port
        self._sock = None

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.address, self.port))
        self._sock.listen(50)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        logger.info(f"SSH request server listening on {self.address}:{self.port} "
                    f"(host key {key_fingerprint(self.host_key)})")
        return self

    def stop(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            # shutdown() wakes the accept() blocked in _accept_loop so the port is really freed.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _accept_loop(self):
        while self._sock is not None:
            try:
                sock, addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve_connection, args=(sock, addr), daemon=True).start()

    def _serve_connection(self, sock, addr):
        transport = paramiko.Transport(sock)
        transport.add_server_key(self.host_key)
        handler = _ConnectionHandler(self.username, self.password)
        try:
            transport.start_server(server=handler)
            while transport.is_active():
                channel = transport.accept(1)
                if channel is not None:
                    threading.Thread(target=self._serve_channel, args=(handler, channel), daemon=True).start()
        except (paramiko.SSHException, EOFError, OSError) as e:
            logger.info(f"SSH connection from {addr[0]} ended: {e}")
        finally:
            transport.close()

    def _serve_channel(self, handler, channel):
        try:
            command = handler.wait_for_command(channel, COMMAND_WAIT_TIMEOUT)
            if command is None:
                return
            try:
                message = json.loads(command.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("request must be a JSON object")
                response = self.handle_request(message)
            except ValueError as e:
                response = {"ok": False, "message": f"Bad request: {e}"}
            except Exception:
                logger.exception(f"Failed to handle SSH request {command!r}")
                response = {"ok": False, "message": "Server error"}
            channel.sendall((json.dumps(response) + "\n").encode("utf-8"))
            channel.send_exit_status(0 if response.get("ok") else 1)
        except (paramiko.SSHException, EOFError, OSError) as e:
            logger.info(f"SSH channel failed: {e}")
        finally:
            channel.close()


class SSHLabClient:
    """Sends lab requests to an SSHRequestServer over one reused SSH connection."""

    def __init__(self, host, port, username, password, host_key_fingerprint=None, timeout=5.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.host_key_fingerprint = host_key_fingerprint
        self.timeout = timeout
        self._client = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config):
        """Build a client from poor_man.json, or return None if SSH isn't configured."""
        host = config.get("poor_man_ssh_host")
        if not host:
            return None
        return cls(
            host,
            int(config.get("poor_man_ssh_port", DEFAULT_SSH_PORT)),
            config.get("poor_man_ssh_username", DEFAULT_SSH_USERNAME),
            config.get("poor_man_ssh_password", ""),
            host_key_fingerprint=config.get("poor_man_ssh_host_key_fingerprint"),
        )

    def _connect(self):
        client = paramiko.SSHClient()
        # Host identity is checked against the pinned fingerprint below when one is configured.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host, port=self.port, username=self.username, password=self.password,
            timeout=self.timeout, banner_timeout=self.timeout, auth_timeout=self.timeout,
            look_for_keys=False, allow_agent=False,
        )
        if self.host_key_fingerprint:
            actual = key_fingerprint(client.get_transport().get_remote_server_key())
            if actual != self.host_key_fingerprint:
                client.close()
                raise paramiko.SSHException(
                    f"Host key mismatch for {self.host}: expected {self.host_key_fingerprint}, got {actual}")
        return client

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def send(self, message):
        """Send one request and return the server's response dict."""
        with self._lock:
            reused = self._client is not None
            try:
                if self._client is None:
                    self._client = self._connect()
                return self._exec(message)
            except (paramiko.SSHException, EOFError, OSError):
                self.close()
                if not reused:
                    raise
            # The kept-alive connection had gone stale (e.g. server restart); retry once fresh.
            self._client = self._connect()
            return self._exec(message)

    def status(self):
        return self.send({"action": "status"})["status"]

    def _exec(self, message):
        _, stdout, _ = self._client.exec_command(json.dumps(message), timeout=self.timeout)
        body = stdout.read().decode("utf-8")
        if not body:
            raise paramiko.SSHException("Empty response from server")
        return json.loads(body)
