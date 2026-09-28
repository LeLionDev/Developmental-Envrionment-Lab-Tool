import json
import os
import shutil
import tempfile
import unittest

try:
    import paramiko
    import poor_man_ssh
except ImportError:
    paramiko = None

from poor_man_server import PoorManServer


@unittest.skipIf(paramiko is None, "paramiko is not installed")
class SSHTransportTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.queue_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.queue_dir, ignore_errors=True)
        data_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)

        init_config_path = os.path.join(self.queue_dir, "init.json")
        with open(init_config_path, "w") as f:
            json.dump({"labs": ["lab1", "lab2"]}, f)
        config_path = os.path.join(data_dir, "poor_man.json")
        with open(config_path, "w") as f:
            json.dump({
                "poor_man_message_queue_root": self.queue_dir,
                "poor_man_message_init_config": init_config_path,
                "poor_man_db_path": os.path.join(data_dir, "poor_man.db"),
            }, f)

        self.server = PoorManServer(config_path)
        self.server.setup_db()
        self.ssh_server = poor_man_ssh.SSHRequestServer(
            self.server.handle_request, self.host_key, "lab_tool", "secret",
            address="127.0.0.1", port=0,
        ).start()
        self.addCleanup(self.ssh_server.stop)

    def make_client(self, password="secret", fingerprint=None):
        client = poor_man_ssh.SSHLabClient(
            "127.0.0.1", self.ssh_server.port, "lab_tool", password, host_key_fingerprint=fingerprint)
        self.addCleanup(client.close)
        return client

    def test_status(self):
        self.assertEqual(self.make_client().status(), {"lab1": None, "lab2": None})

    def test_checkout_and_release_round_trip(self):
        client = self.make_client()

        response = client.send({"action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], {"lab1": "alice", "lab2": None})

        response = client.send({"action": "checkout", "user": "bob", "lab": "lab1", "timestamp": "t2"})
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"]["lab1"], "alice")

        response = client.send({"action": "release", "user": "alice", "lab": "lab1", "timestamp": "t3"})
        self.assertTrue(response["ok"])
        self.assertEqual(client.status(), {"lab1": None, "lab2": None})

    def test_bad_json_is_rejected(self):
        client = self.make_client()
        client._client = client._connect()
        _, stdout, _ = client._client.exec_command("not json")
        response = json.loads(stdout.read())
        self.assertFalse(response["ok"])
        self.assertEqual(stdout.channel.recv_exit_status(), 1)

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(paramiko.AuthenticationException):
            self.make_client(password="wrong").status()

    def test_pinned_fingerprint(self):
        good = poor_man_ssh.key_fingerprint(self.host_key)
        self.assertEqual(self.make_client(fingerprint=good).status()["lab1"], None)

        with self.assertRaises(paramiko.SSHException):
            self.make_client(fingerprint="SHA256:not-the-right-key").status()

    def test_client_reconnects_after_server_restart(self):
        client = self.make_client()
        client.status()
        port = self.ssh_server.port
        self.ssh_server.stop()
        client._client.get_transport().close()  # simulate the dropped connection

        restarted = poor_man_ssh.SSHRequestServer(
            self.server.handle_request, self.host_key, "lab_tool", "secret",
            address="127.0.0.1", port=port,
        ).start()
        self.addCleanup(restarted.stop)

        self.assertEqual(client.status(), {"lab1": None, "lab2": None})


if __name__ == "__main__":
    unittest.main()
