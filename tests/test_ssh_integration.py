"""End-to-end test: SshTransport talking to the real ssh_receiver.py in real processes.

No network is involved. A stand-in `ssh` script ignores the connection arguments and runs
the remote command locally, which exercises everything except the network hop itself:
process startup and shutdown, pipes, the line protocol and the receiver's file handling.
POSIX only, because the stand-in is a shell script.
"""
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import messages
import transports
from client_config import SshSettings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_SSH = """#!/bin/sh
# Keep only the last argument (the remote command) and run it like sshd would.
for last; do :; done
exec sh -c "exec $last"
"""


@unittest.skipUnless(os.name == "posix", "stand-in ssh is a POSIX shell script")
class SshIntegrationTest(unittest.TestCase):
    def setUp(self):
        workdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        self.queue_dir = os.path.join(workdir, "queue")
        os.mkdir(self.queue_dir)

        config_path = os.path.join(workdir, "poor_man.json")
        with open(config_path, "w") as f:
            json.dump({"poor_man_message_queue_root": self.queue_dir}, f)

        self.fake_ssh = os.path.join(workdir, "ssh")
        with open(self.fake_ssh, "w") as f:
            f.write(FAKE_SSH)
        os.chmod(self.fake_ssh, os.stat(self.fake_ssh).st_mode | stat.S_IEXEC)

        receiver = os.path.join(REPO_ROOT, "ssh_receiver.py")
        command = " ".join(shlex.quote(p) for p in (sys.executable, receiver, config_path))
        settings = SshSettings("gateway.example", "labhost.example", "", 22, command)
        self.transport = transports.SshTransport(
            settings,
            run=lambda cmd, **kw: subprocess.run([self.fake_ssh] + cmd[1:], **kw),
            popen=lambda cmd, **kw: subprocess.Popen([self.fake_ssh] + cmd[1:], **kw),
        )

    def write_status(self, status):
        tmp = messages.status_path(self.queue_dir) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, messages.status_path(self.queue_dir))

    def wait_for(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def test_submit_queues_a_request_file(self):
        request = messages.build_request("checkout", "alice", "lab1", "t1")

        self.transport.send_request(request)

        [name] = os.listdir(self.queue_dir)
        with open(os.path.join(self.queue_dir, name)) as f:
            self.assertEqual(json.load(f), request)

    def test_invalid_submit_reports_the_receivers_reason(self):
        with self.assertRaisesRegex(transports.TransportError, "unsupported action"):
            self.transport.send_request({"action": "explode", "user": "a", "lab": "l", "timestamp": "t"})

    def test_stream_delivers_changes_and_shuts_down_cleanly(self):
        self.write_status({"lab1": None})
        statuses = []
        stop = threading.Event()
        thread = threading.Thread(target=self.transport.watch_status,
                                  args=(statuses.append, lambda p: None, stop))
        thread.start()

        self.assertTrue(self.wait_for(lambda: statuses == [{"lab1": None}]))
        time.sleep(0.05)  # make sure the rewrite gets a different modified time
        self.write_status({"lab1": "alice"})
        self.assertTrue(self.wait_for(lambda: statuses[-1] == {"lab1": "alice"}))

        stop.set()
        self.transport.close()
        thread.join(10)

        self.assertFalse(thread.is_alive(), "watch thread did not stop after close()")


if __name__ == "__main__":
    unittest.main()
