import io
import json
import os
import shutil
import tempfile
import unittest

import messages
import ssh_receiver


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def read_lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class ReceiverTestCase(unittest.TestCase):
    def setUp(self):
        self.root_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root_dir, ignore_errors=True)

        config_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, config_dir, ignore_errors=True)
        self.config_path = os.path.join(config_dir, "poor_man.json")
        with open(self.config_path, "w") as f:
            json.dump({"poor_man_message_queue_root": self.root_dir}, f)

    def write_status(self, status, mtime):
        path = messages.status_path(self.root_dir)
        with open(path, "w") as f:
            json.dump(status, f)
        os.utime(path, (mtime, mtime))

    def run_main(self, *args, stdin_text=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = ssh_receiver.main(list(args), io.StringIO(stdin_text), stdout, stderr)
        return code, stdout, stderr

    def queued_requests(self):
        return [name for name in os.listdir(self.root_dir)
                if name.startswith(messages.REQUEST_PREFIX)]


class SubmitTest(ReceiverTestCase):
    def test_valid_request_is_queued_for_the_server(self):
        request = {"action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1"}

        code, stdout, _ = self.run_main(self.config_path, "submit", stdin_text=json.dumps(request))

        self.assertEqual(code, ssh_receiver.EXIT_OK)
        self.assertEqual(read_lines(stdout), [{"type": "accepted"}])
        [name] = self.queued_requests()
        with open(os.path.join(self.root_dir, name)) as f:
            self.assertEqual(json.load(f), request)

    def test_invalid_request_is_rejected_and_not_queued(self):
        request = {"action": "explode", "user": "alice", "lab": "lab1", "timestamp": "t1"}

        code, _, stderr = self.run_main(self.config_path, "submit", stdin_text=json.dumps(request))

        self.assertEqual(code, ssh_receiver.EXIT_INVALID_REQUEST)
        self.assertIn("explode", stderr.getvalue())
        self.assertEqual(self.queued_requests(), [])

    def test_malformed_json_is_rejected(self):
        code, _, stderr = self.run_main(self.config_path, "submit", stdin_text="{not json")

        self.assertEqual(code, ssh_receiver.EXIT_INVALID_REQUEST)
        self.assertIn("not valid JSON", stderr.getvalue())
        self.assertEqual(self.queued_requests(), [])


class StatusAndUsageTest(ReceiverTestCase):
    def test_status_prints_current_snapshot(self):
        self.write_status({"lab1": "alice"}, mtime=100)

        code, stdout, _ = self.run_main(self.config_path, "status")

        self.assertEqual(code, ssh_receiver.EXIT_OK)
        self.assertEqual(read_lines(stdout), [{"type": "status", "status": {"lab1": "alice"}}])

    def test_status_before_server_has_run_is_empty(self):
        _, stdout, _ = self.run_main(self.config_path, "status")

        self.assertEqual(read_lines(stdout), [{"type": "status", "status": {}}])

    def test_unknown_command_prints_usage(self):
        code, _, stderr = self.run_main(self.config_path, "delete-everything")

        self.assertEqual(code, ssh_receiver.EXIT_USAGE)
        self.assertIn("usage", stderr.getvalue())


class StatusStreamerTest(ReceiverTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.out = io.StringIO()
        self.streamer = ssh_receiver.StatusStreamer(
            self.root_dir, self.out, clock=self.clock, heartbeat_interval=15)

    def test_sends_initial_snapshot(self):
        self.write_status({"lab1": None}, mtime=100)

        self.assertEqual(self.streamer.tick(), "status")
        self.assertEqual(read_lines(self.out), [{"type": "status", "status": {"lab1": None}}])

    def test_sends_nothing_while_unchanged(self):
        self.write_status({"lab1": None}, mtime=100)
        self.streamer.tick()

        self.assertIsNone(self.streamer.tick())

    def test_sends_update_when_contents_change(self):
        self.write_status({"lab1": None}, mtime=100)
        self.streamer.tick()

        self.write_status({"lab1": "alice"}, mtime=101)

        self.assertEqual(self.streamer.tick(), "status")
        self.assertEqual(read_lines(self.out)[-1]["status"], {"lab1": "alice"})

    def test_rewrite_with_identical_contents_is_not_resent(self):
        self.write_status({"lab1": None}, mtime=100)
        self.streamer.tick()

        self.write_status({"lab1": None}, mtime=101)

        self.assertIsNone(self.streamer.tick())

    def test_partially_written_file_is_retried_next_tick(self):
        path = messages.status_path(self.root_dir)
        with open(path, "w") as f:
            f.write('{"lab1": ')
        os.utime(path, (100, 100))

        self.assertIsNone(self.streamer.tick())

        with open(path, "w") as f:
            json.dump({"lab1": None}, f)
        os.utime(path, (100, 100))

        self.assertEqual(self.streamer.tick(), "status")

    def test_heartbeat_after_quiet_interval(self):
        self.clock.now = 14
        self.assertIsNone(self.streamer.tick())

        self.clock.now = 15
        self.assertEqual(self.streamer.tick(), "heartbeat")
        self.assertEqual(read_lines(self.out), [{"type": "heartbeat"}])

    def test_status_update_postpones_heartbeat(self):
        self.clock.now = 10
        self.write_status({"lab1": None}, mtime=100)
        self.streamer.tick()

        self.clock.now = 20
        self.assertIsNone(self.streamer.tick())


class WatchLoopTest(ReceiverTestCase):
    def test_watch_exits_cleanly_when_client_disconnects(self):
        class ClosedPipe(io.StringIO):
            def write(self, text):
                raise BrokenPipeError()

        self.write_status({"lab1": None}, mtime=100)

        code = ssh_receiver.handle_watch(self.root_dir, ClosedPipe(), sleep=lambda s: None)

        self.assertEqual(code, ssh_receiver.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
