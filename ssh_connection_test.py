"""
Paramiko SSH connection test.

Connects to a remote host over SSH and runs commands against it, printing each
command's stdout/stderr and exit status. Useful for confirming a lab machine is
reachable and accepting SSH commands before wiring SSH into the lab tool.

Usage:
    # One-off connectivity check (runs a harmless default command)
    python3 ssh_connection_test.py HOST -u USER

    # Run specific commands, in order
    python3 ssh_connection_test.py HOST -u USER -c "hostname" -c "uptime"

    # Interactive: type commands at a prompt, blank line / "exit" to quit
    python3 ssh_connection_test.py HOST -u USER -i

Authentication tries, in order: an explicit --key file, the SSH agent / default
keys in ~/.ssh, then a password (from --password, the SSH_PASSWORD environment
variable, or an interactive prompt if --ask-password is given).
"""
import argparse
import getpass
import os
import socket
import sys
from typing import Optional, Tuple

import paramiko

DEFAULT_TEST_COMMAND = "echo ssh-ok"


def connect(
        host: str,
        port: int,
        username: str,
        password: Optional[str] = None,
        key_filename: Optional[str] = None,
        timeout: float = 10.0,
        strict_host_keys: bool = False,
) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if strict_host_keys:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        # Test script convenience: accept unknown hosts but say so.
        client.set_missing_host_key_policy(paramiko.WarningPolicy())

    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        key_filename=key_filename,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=key_filename is None,
        allow_agent=True,
    )
    return client


def run_command(client: paramiko.SSHClient, command: str, timeout: float = 30.0) -> Tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.close()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, out, err


def print_result(command: str, exit_status: int, out: str, err: str) -> None:
    print(f"$ {command}")
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print(err, end="" if err.endswith("\n") else "\n", file=sys.stderr)
    print(f"[exit status: {exit_status}]")


def interactive_loop(client: paramiko.SSHClient, timeout: float) -> int:
    print("Connected. Enter commands to run remotely (blank line or 'exit' to quit).")
    last_status = 0
    while True:
        try:
            command = input("ssh> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not command or command in ("exit", "quit"):
            break
        try:
            last_status, out, err = run_command(client, command, timeout)
            print_result(command, last_status, out, err)
        except (paramiko.SSHException, socket.timeout) as e:
            print(f"Command failed: {e}", file=sys.stderr)
            last_status = 1
    return last_status


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test an SSH connection with paramiko and run commands.")
    parser.add_argument("host", help="Hostname or IP to connect to")
    parser.add_argument("-p", "--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("-u", "--user", default=getpass.getuser(), help="Username (default: current user)")
    parser.add_argument("-k", "--key", help="Path to a private key file")
    parser.add_argument("--password", help="Password (prefer SSH_PASSWORD env var or --ask-password)")
    parser.add_argument("--ask-password", action="store_true", help="Prompt for a password")
    parser.add_argument("-c", "--command", action="append", dest="commands",
                        help=f"Command to run; repeatable (default: '{DEFAULT_TEST_COMMAND}')")
    parser.add_argument("-i", "--interactive", action="store_true", help="Read commands from a prompt")
    parser.add_argument("-t", "--timeout", type=float, default=10.0, help="Connect timeout in seconds")
    parser.add_argument("--command-timeout", type=float, default=30.0, help="Per-command timeout in seconds")
    parser.add_argument("--strict-host-keys", action="store_true",
                        help="Reject hosts not already in known_hosts")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    password = args.password or os.environ.get("SSH_PASSWORD")
    if args.ask_password and not password:
        password = getpass.getpass(f"Password for {args.user}@{args.host}: ")

    print(f"Connecting to {args.user}@{args.host}:{args.port} ...")
    try:
        client = connect(
            args.host, args.port, args.user,
            password=password,
            key_filename=args.key,
            timeout=args.timeout,
            strict_host_keys=args.strict_host_keys,
        )
    except paramiko.AuthenticationException as e:
        print(f"Authentication failed: {e}", file=sys.stderr)
        return 2
    except (paramiko.SSHException, socket.error) as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        return 3

    try:
        if args.interactive:
            return interactive_loop(client, args.command_timeout)

        last_status = 0
        for command in args.commands or [DEFAULT_TEST_COMMAND]:
            last_status, out, err = run_command(client, command, args.command_timeout)
            print_result(command, last_status, out, err)
        return last_status
    except (paramiko.SSHException, socket.timeout) as e:
        print(f"Command failed: {e}", file=sys.stderr)
        return 4
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
