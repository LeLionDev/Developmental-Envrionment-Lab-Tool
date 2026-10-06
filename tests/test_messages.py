import json
import os
import shutil
import tempfile
import unittest

import messages


def valid_request(**overrides):
    request = {"action": "checkout", "user": "alice", "lab": "lab1", "timestamp": "t1"}
    request.update(overrides)
    return request


class ValidateRequestTest(unittest.TestCase):
    def test_valid_request_is_returned(self):
        self.assertEqual(messages.validate_request(valid_request()), valid_request())

    def test_unknown_fields_are_dropped(self):
        result = messages.validate_request(valid_request(extra="ignored"))

        self.assertNotIn("extra", result)

    def test_missing_field_is_rejected(self):
        request = valid_request()
        del request["user"]

        with self.assertRaisesRegex(messages.InvalidRequestError, "user"):
            messages.validate_request(request)

    def test_blank_field_is_rejected(self):
        with self.assertRaisesRegex(messages.InvalidRequestError, "lab"):
            messages.validate_request(valid_request(lab="  "))

    def test_non_string_field_is_rejected(self):
        with self.assertRaises(messages.InvalidRequestError):
            messages.validate_request(valid_request(user=42))

    def test_unsupported_action_is_rejected(self):
        with self.assertRaisesRegex(messages.InvalidRequestError, "explode"):
            messages.validate_request(valid_request(action="explode"))

    def test_non_object_is_rejected(self):
        with self.assertRaises(messages.InvalidRequestError):
            messages.validate_request(["not", "a", "dict"])


class QueueFilesTest(unittest.TestCase):
    def setUp(self):
        self.root_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root_dir, ignore_errors=True)

    def test_write_request_file_leaves_only_the_final_request(self):
        path = messages.write_request_file(self.root_dir, valid_request())

        self.assertEqual(os.listdir(self.root_dir), [os.path.basename(path)])
        self.assertTrue(os.path.basename(path).startswith(messages.REQUEST_PREFIX))
        with open(path) as f:
            self.assertEqual(json.load(f), valid_request())

    def test_read_status_file_returns_none_when_missing(self):
        self.assertIsNone(messages.read_status_file(self.root_dir))

    def test_read_status_file_returns_snapshot(self):
        with open(messages.status_path(self.root_dir), "w") as f:
            json.dump({"lab1": None}, f)

        self.assertEqual(messages.read_status_file(self.root_dir), {"lab1": None})


if __name__ == "__main__":
    unittest.main()
