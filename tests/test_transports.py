import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

import client_config
import messages
import transports
from client_config import SshSettings

SETTINGS = SshSettings(gateway="gateway.example", host="labhost.example", user="",
                       port=22, receiver_command="python3 /srv/ssh_receiver.py /srv/pm.json")


def status_line(status):
    return json.dumps({"type": "status", "status": status}) + "\n"


class FakeProcess:
    """Stands in for a Popen object whose stdout yields pre-scripted lines."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeStopEvent:
    """A stop event whose wait() returns instantly and records the requested delays."""

    def __init__(self):
        self._set = False
        self.waits = []

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self._set


class ScriptedPopen:
    """Hands out one FakeProcess per call; sets the stop event when the script runs out."""

    def __init__(self, scripts, stop_event):
        self.scripts = list(scripts)
        self.stop_event = stop_event
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if not self.scripts:
            self.stop_event.set()
            return FakeProcess([])
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        if isinstance(script, dict):
            kwargs["stderr"].write(script["stderr"])
            script = script["stdout"]
        return FakeProcess(script)


class BackoffDelayTest(unittest.TestCase):
    def test_doubles_each_attempt_without_jitter(self):
        delays = [transports.backoff_delay(n, rng=lambda: 0.5) for n in range(4)]

        self.assertEqual(delays, [1, 2, 4, 8])

    def test_is_capped(self):
        self.assertEqual(transports.backoff_delay(20, rng=lambda: 0.5), 60)

    def test_jitter_stays_within_twenty_percent(self):
        self.assertAlmostEqual(transports.backoff_delay(3, rng=lambda: 0.0), 6.4)
        self.assertAlmostEqual(transports.backoff_delay(3, rng=lambda: 1.0), 9.6)


class BuildSshCommandTest(unittest.TestCase):
    def test_routes_through_gateway_to_host(self):
        command = transports.build_ssh_command(SETTINGS, "receiver watch")

        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[command.index("-J") + 1], "gateway.example")
        self.assertEqual(command[command.index("-p") + 1], "22")
        self.assertEqual(command[-2:], ["labhost.example", "receiver watch"])

    def test_username_is_applied_to_gateway_and_host(self):
        settings = SshSettings("gateway.example:2200", "labhost.example", "alice", 2222, "r")

        command = transports.build_ssh_command(settings, "r status")

        self.assertEqual(command[command.index("-J") + 1], "alice@gateway.example:2200")
        self.assertEqual(command[command.index("-p") + 1], "2222")
        self.assertEqual(command[-2], "alice@labhost.example")

    def test_never_prompts_and_detects_dead_connections(self):
        command = transports.build_ssh_command(SETTINGS, "r")
        options = [command[i + 1] for i, arg in enumerate(command) if arg == "-o"]

        self.assertIn("BatchMode=yes", options)
        self.assertIn("ServerAliveInterval=15", options)


class ParseStreamLineTest(unittest.TestCase):
    def test_status_line(self):
        self.assertEqual(transports.parse_stream_line(status_line({"lab1": None})),
                         ("status", {"lab1": None}))

    def test_heartbeat_line(self):
        self.assertEqual(transports.parse_stream_line('{"type": "heartbeat"}\n'),
                         ("heartbeat", None))

    def test_ssh_error_text(self):
        self.assertEqual(transports.parse_stream_line("Permission denied (publickey).\n"),
                         ("text", "Permission denied (publickey)."))

    def test_unrecognised_json_is_treated_as_text(self):
        self.assertEqual(transports.parse_stream_line('{"type": "status"}')[0], "text")

    def test_blank_line(self):
        self.assertEqual(transports.parse_stream_line("\n"), ("blank", None))


class SshSendRequestTest(unittest.TestCase):
    def test_sends_request_json_to_submit(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        request = messages.build_request("checkout", "alice", "lab1", "t1")
        transports.SshTransport(SETTINGS, run=fake_run).send_request(request)

        [(command, kwargs)] = calls
        self.assertEqual(command[-1], SETTINGS.receiver_command + " submit")
        self.assertEqual(json.loads(kwargs["input"]), request)

    def test_failure_raises_with_ssh_error_message(self):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 255, stdout="",
                                               stderr="Connection refused\n")

        transport = transports.SshTransport(SETTINGS, run=fake_run)

        with self.assertRaisesRegex(transports.TransportError, "Connection refused"):
            transport.send_request({})

    def test_timeout_raises_transport_error(self):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaisesRegex(transports.TransportError, "Timed out"):
            transports.SshTransport(SETTINGS, run=fake_run).send_request({})

    def test_missing_ssh_binary_raises_transport_error(self):
        def fake_run(command, **kwargs):
            raise FileNotFoundError("ssh")

        with self.assertRaisesRegex(transports.TransportError, "Could not start ssh"):
            transports.SshTransport(SETTINGS, run=fake_run).send_request({})


class SshWatchStatusTest(unittest.TestCase):
    def watch(self, scripts):
        stop = FakeStopEvent()
        popen = ScriptedPopen(scripts, stop)
        statuses, problems = [], []
        transport = transports.SshTransport(SETTINGS, popen=popen, rng=lambda: 0.5)
        transport.watch_status(statuses.append, problems.append, stop)
        return statuses, problems, stop, popen

    def test_delivers_each_snapshot_and_skips_heartbeats(self):
        statuses, _, _, popen = self.watch([[
            status_line({"lab1": None}),
            '{"type": "heartbeat"}\n',
            status_line({"lab1": "alice"}),
        ]])

        self.assertEqual(statuses, [{"lab1": None}, {"lab1": "alice"}])
        self.assertTrue(popen.commands[0][-1].endswith(" watch"))

    def test_reconnects_after_stream_ends(self):
        statuses, problems, _, _ = self.watch([
            [status_line({"lab1": None})],
            [status_line({"lab1": "alice"})],
        ])

        self.assertEqual(statuses, [{"lab1": None}, {"lab1": "alice"}])
        self.assertTrue(any("disconnected" in p for p in problems))

    def test_backs_off_while_connection_keeps_failing(self):
        _, _, stop, _ = self.watch([
            ["Connection timed out\n"],
            ["Connection timed out\n"],
            ["Connection timed out\n"],
        ])

        self.assertEqual(stop.waits, [2, 4, 8])

    def test_backoff_resets_after_a_healthy_connection(self):
        _, _, stop, _ = self.watch([
            ["Connection timed out\n"],
            [status_line({"lab1": None})],
        ])

        self.assertEqual(stop.waits, [2, 1])

    def test_ssh_error_text_is_reported(self):
        _, problems, _, _ = self.watch([["Permission denied (publickey).\n"]])

        self.assertIn("Permission denied (publickey).", problems)

    def test_ssh_error_output_is_reported(self):
        _, problems, _, _ = self.watch([
            {"stdout": [], "stderr": "Warning: added host key\nPermission denied (publickey).\n"},
        ])

        self.assertIn("Permission denied (publickey).", problems)

    def test_missing_ssh_binary_is_reported_and_retried(self):
        _, problems, stop, _ = self.watch([FileNotFoundError("ssh"), []])

        self.assertTrue(problems[0].startswith("Could not start ssh"))
        self.assertEqual(len(stop.waits), 2)

    def test_close_stops_the_running_stream(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingProcess(FakeProcess):
            def __init__(self):
                super().__init__([])
                self.stdout = self._lines()

            def _lines(self):
                yield status_line({"lab1": None})
                started.set()
                release.wait(5)

            def terminate(self):
                super().terminate()
                release.set()

        process = BlockingProcess()
        stop = threading.Event()
        transport = transports.SshTransport(SETTINGS, popen=lambda *a, **k: process)
        thread = threading.Thread(target=transport.watch_status,
                                  args=(lambda s: None, lambda p: None, stop))
        thread.start()
        self.assertTrue(started.wait(5))

        stop.set()
        transport.close()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertTrue(process.terminated)


class FileSystemTransportTest(unittest.TestCase):
    def setUp(self):
        self.root_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root_dir, ignore_errors=True)
        self.transport = transports.FileSystemTransport(self.root_dir, poll_interval=0)

    def test_send_request_writes_queue_file(self):
        request = messages.build_request("checkout", "alice", "lab1", "t1")

        self.transport.send_request(request)

        [name] = os.listdir(self.root_dir)
        with open(os.path.join(self.root_dir, name)) as f:
            self.assertEqual(json.load(f), request)

    def test_watch_reports_status_once_until_it_changes(self):
        with open(messages.status_path(self.root_dir), "w") as f:
            json.dump({"lab1": None}, f)

        stop = FakeStopEvent()
        statuses = []

        def stop_after_three_polls(timeout=None):
            stop.waits.append(timeout)
            if len(stop.waits) == 3:
                stop.set()

        stop.wait = stop_after_three_polls
        self.transport.watch_status(statuses.append, lambda p: None, stop)

        self.assertEqual(statuses, [{"lab1": None}])


class CreateTransportTest(unittest.TestCase):
    def test_existing_configs_get_the_filesystem_transport(self):
        transport = transports.create_transport({client_config.QUEUE_ROOT_KEY: "/q"})

        self.assertIsInstance(transport, transports.FileSystemTransport)

    def test_complete_ssh_config_gets_the_ssh_transport(self):
        transport = transports.create_transport({
            client_config.TRANSPORT_KEY: "ssh",
            client_config.SSH_GATEWAY_KEY: SETTINGS.gateway,
            client_config.SSH_HOST_KEY: SETTINGS.host,
            client_config.SSH_RECEIVER_COMMAND_KEY: SETTINGS.receiver_command,
        })

        self.assertIsInstance(transport, transports.SshTransport)

    def test_incomplete_ssh_config_is_rejected(self):
        with self.assertRaises(client_config.ConfigError):
            transports.create_transport({client_config.TRANSPORT_KEY: "ssh"})


if __name__ == "__main__":
    unittest.main()
