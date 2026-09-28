import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from poor_man_server import PoorManServer


class PoorManServerTestCase(unittest.TestCase):
    def setUp(self):
        self.queue_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.queue_dir, ignore_errors=True)

        data_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)

        self.db_path = os.path.join(data_dir, "poor_man.db")
        init_config_path = os.path.join(self.queue_dir, "init.json")
        with open(init_config_path, "w") as f:
            json.dump({"labs": ["lab1", "lab2"]}, f)

        config_path = os.path.join(data_dir, "poor_man.json")
        with open(config_path, "w") as f:
            json.dump({
                "poor_man_message_queue_root": self.queue_dir,
                "poor_man_message_init_config": init_config_path,
                "poor_man_db_path": self.db_path,
            }, f)

        self.server = PoorManServer(config_path)
        self.server.setup_db()

    def write_request(self, name, body):
        path = os.path.join(self.queue_dir, name)
        with open(path, "w") as f:
            json.dump(body, f)
        return path

    def lab_row(self, lab):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT occupant, checked_out_at FROM labs WHERE name = ?", (lab,)
        ).fetchone()
        conn.close()
        return row

    def test_setup_db_seeds_labs_as_free(self):
        self.assertEqual(self.lab_row("lab1"), (None, None))
        self.assertEqual(self.lab_row("lab2"), (None, None))

    def test_setup_db_is_idempotent(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE labs SET occupant = 'alice' WHERE name = 'lab1'")
        conn.commit()
        conn.close()

        self.server.setup_db()

        self.assertEqual(self.lab_row("lab1"), ("alice", None))

    def test_poll_root_finds_only_request_files(self):
        self.write_request("req_a.json", {})
        self.write_request("req_b.json", {})
        open(os.path.join(self.queue_dir, "status.json"), "w").close()

        found = self.server.poll_root()

        self.assertEqual(
            sorted(os.path.basename(p) for p in found),
            ["req_a.json", "req_b.json"],
        )

    def test_checkout_succeeds_for_free_lab(self):
        path = self.write_request("req_1.json", {
            "action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1",
        })

        self.server.process_message(path)

        self.assertEqual(self.lab_row("lab1"), ("alice", "t1"))
        self.assertFalse(os.path.exists(path))

    def test_checkout_race_loser_does_not_overwrite_occupant(self):
        self.server.process_message(self.write_request("req_1.json", {
            "action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1",
        }))
        self.server.process_message(self.write_request("req_2.json", {
            "action": "checkout", "user": "bob", "lab": "lab1", "timestamp": "t2",
        }))

        self.assertEqual(self.lab_row("lab1"), ("alice", "t1"))

    def test_checkout_rejects_unknown_lab(self):
        path = self.write_request("req_1.json", {
            "action": "checkout", "user": "alice", "lab": "lab99", "timestamp": "t1",
        })

        self.server.process_message(path)

        self.assertFalse(os.path.exists(path))

    def test_unsupported_action_is_ignored(self):
        path = self.write_request("req_1.json", {
            "action": "explode", "user": "alice", "lab": "lab1", "timestamp": "t1",
        })

        self.server.process_message(path)

        self.assertEqual(self.lab_row("lab1"), (None, None))
        self.assertFalse(os.path.exists(path))

    def test_occupant_can_release_their_own_lab(self):
        self.server.process_message(self.write_request("req_1.json", {
            "action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1",
        }))

        self.server.process_message(self.write_request("req_2.json", {
            "action": "release", "user": "alice", "lab": "lab1", "timestamp": "t2",
        }))

        self.assertEqual(self.lab_row("lab1"), (None, None))

    def test_non_occupant_cannot_release_lab(self):
        self.server.process_message(self.write_request("req_1.json", {
            "action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1",
        }))

        self.server.process_message(self.write_request("req_2.json", {
            "action": "release", "user": "bob", "lab": "lab1", "timestamp": "t2",
        }))

        self.assertEqual(self.lab_row("lab1"), ("alice", "t1"))

    def test_export_snapshot_writes_status_json(self):
        self.server.process_message(self.write_request("req_1.json", {
            "action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1",
        }))

        self.server.export_snapshot()

        with open(os.path.join(self.queue_dir, "status.json")) as f:
            status = json.load(f)

        self.assertEqual(status, {"lab1": "alice", "lab2": None})

    def test_export_snapshot_leaves_no_temp_file_behind(self):
        self.server.export_snapshot()

        self.assertEqual(
            sorted(os.listdir(self.queue_dir)),
            ["init.json", "status.json"],
        )

    def test_apply_request_reports_result(self):
        result = self.server.apply_request({
            "action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1",
        })
        self.assertEqual(result, {"ok": True, "message": "alice checked out lab1"})

        result = self.server.apply_request({
            "action": "checkout", "user": "bob", "lab": "lab1", "timestamp": "t2",
        })
        self.assertFalse(result["ok"])

    def test_checkout_by_current_occupant_is_ok(self):
        request = {"action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1"}
        self.server.apply_request(request)

        self.assertTrue(self.server.apply_request(request)["ok"])
        self.assertEqual(self.lab_row("lab1"), ("alice", "t1"))

    def test_handle_request_returns_status_and_exports_snapshot(self):
        response = self.server.handle_request({
            "action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1",
        })

        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], {"lab1": "alice", "lab2": None})
        with open(os.path.join(self.queue_dir, "status.json")) as f:
            self.assertEqual(json.load(f), {"lab1": "alice", "lab2": None})

    def test_handle_request_status_action_changes_nothing(self):
        response = self.server.handle_request({"action": "status"})

        self.assertEqual(response, {"ok": True, "message": "status", "status": {"lab1": None, "lab2": None}})
        self.assertFalse(os.path.exists(os.path.join(self.queue_dir, "status.json")))


if __name__ == "__main__":
    unittest.main()
