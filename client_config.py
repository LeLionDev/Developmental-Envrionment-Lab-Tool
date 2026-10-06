"""Read, validate and save the client's settings in poor_man.json.

poor_man.json stays the single source of truth for configuration; this module is just the
code that reads and writes it, kept free of any GUI code so it can be unit tested.
"""
import json
import os
import re
from dataclasses import dataclass

TRANSPORT_KEY = "poor_man_transport"
QUEUE_ROOT_KEY = "poor_man_message_queue_root"
WINDOW_CORNER_KEY = "poor_man_window_corner"
SSH_GATEWAY_KEY = "poor_man_ssh_gateway"
SSH_HOST_KEY = "poor_man_ssh_host"
SSH_USER_KEY = "poor_man_ssh_user"
SSH_PORT_KEY = "poor_man_ssh_port"
SSH_RECEIVER_COMMAND_KEY = "poor_man_ssh_receiver_command"

TRANSPORT_FILESYSTEM = "filesystem"
TRANSPORT_SSH = "ssh"
TRANSPORTS = (TRANSPORT_FILESYSTEM, TRANSPORT_SSH)
DEFAULT_SSH_PORT = 22

# Hostnames and IPv4 addresses only. The first character must be alphanumeric so a value
# can never start with "-" and be parsed by ssh as a command-line option.
_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")
_GATEWAY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*(:[0-9]{1,5})?$")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._\-]*$")


class ConfigError(ValueError):
    """Raised when poor_man.json is missing a setting or holds an invalid one."""


@dataclass(frozen=True)
class SshSettings:
    """Everything needed to reach the receiving host through the gateway."""

    gateway: str = ""
    host: str = ""
    user: str = ""
    port: int = DEFAULT_SSH_PORT
    receiver_command: str = ""

    def is_complete(self):
        """True when the user-supplied connection details (gateway and host) are filled in."""
        return bool(self.gateway and self.host)


def load_config(path):
    """Read poor_man.json into a dict."""
    with open(path) as f:
        return json.load(f)


def save_config(path, config):
    """Write the config back atomically so a crash mid-save cannot corrupt poor_man.json."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(config, f, indent=4)
    os.replace(tmp_path, path)


def get_transport_mode(config):
    """Return which transport the client should use, defaulting to the shared filesystem.

    Defaulting to filesystem means existing poor_man.json files keep working unchanged.
    """
    mode = config.get(TRANSPORT_KEY, TRANSPORT_FILESYSTEM)
    if mode not in TRANSPORTS:
        raise ConfigError(f"'{TRANSPORT_KEY}' must be one of {TRANSPORTS}, got '{mode}'")
    return mode


def get_ssh_settings(config):
    """Build SshSettings from the config, treating missing keys as blank."""
    try:
        port = int(config.get(SSH_PORT_KEY) or DEFAULT_SSH_PORT)
    except (TypeError, ValueError):
        raise ConfigError(f"'{SSH_PORT_KEY}' must be a whole number")
    return SshSettings(
        gateway=str(config.get(SSH_GATEWAY_KEY) or "").strip(),
        host=str(config.get(SSH_HOST_KEY) or "").strip(),
        user=str(config.get(SSH_USER_KEY) or "").strip(),
        port=port,
        receiver_command=str(config.get(SSH_RECEIVER_COMMAND_KEY) or "").strip(),
    )


def validate_ssh_settings(settings):
    """Return a list of human-readable problems with the settings; empty means valid."""
    problems = []
    if not settings.gateway:
        problems.append("Gateway is required.")
    elif not _GATEWAY_PATTERN.match(settings.gateway):
        problems.append("Gateway must be a hostname, optionally followed by :port.")
    if not settings.host:
        problems.append("Receiving host is required.")
    elif not _HOSTNAME_PATTERN.match(settings.host):
        problems.append("Receiving host must be a plain hostname or IP address.")
    if settings.user and not _USERNAME_PATTERN.match(settings.user):
        problems.append("Username may only contain letters, digits, '.', '_' and '-'.")
    if not 1 <= settings.port <= 65535:
        problems.append("Port must be between 1 and 65535.")
    if not settings.receiver_command:
        problems.append(
            f"'{SSH_RECEIVER_COMMAND_KEY}' is not set in poor_man.json -- ask whoever "
            "runs the server for the correct value."
        )
    return problems


def with_ssh_connection(config, gateway, host, user):
    """Return a copy of the config with the user-entered connection details applied.

    Only the details the user types in (gateway, host, username) are touched; everything
    else, including the admin-provided receiver command, is preserved.
    """
    updated = dict(config)
    updated[SSH_GATEWAY_KEY] = gateway.strip()
    updated[SSH_HOST_KEY] = host.strip()
    updated[SSH_USER_KEY] = user.strip()
    return updated
