import json
import os
import shutil
import tempfile
import unittest

import client_config
from client_config import SshSettings


def complete_settings(**overrides):
    values = dict(gateway="gateway.example", host="labhost.example", user="",
                  port=22, receiver_command="python3 /srv/ssh_receiver.py /srv/poor_man.json")
    values.update(overrides)
    return SshSettings(**values)


class TransportModeTest(unittest.TestCase):
    def test_defaults_to_filesystem_for_existing_configs(self):
        self.assertEqual(client_config.get_transport_mode({}), client_config.TRANSPORT_FILESYSTEM)

    def test_ssh_mode_is_recognised(self):
        config = {client_config.TRANSPORT_KEY: "ssh"}

        self.assertEqual(client_config.get_transport_mode(config), client_config.TRANSPORT_SSH)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(client_config.ConfigError):
            client_config.get_transport_mode({client_config.TRANSPORT_KEY: "carrier-pigeon"})


class GetSshSettingsTest(unittest.TestCase):
    def test_missing_keys_produce_blank_incomplete_settings(self):
        settings = client_config.get_ssh_settings({})

        self.assertEqual(settings, SshSettings())
        self.assertFalse(settings.is_complete())

    def test_values_are_read_and_trimmed(self):
        settings = client_config.get_ssh_settings({
            client_config.SSH_GATEWAY_KEY: " gw ",
            client_config.SSH_HOST_KEY: "host",
            client_config.SSH_USER_KEY: "alice",
            client_config.SSH_PORT_KEY: "2222",
            client_config.SSH_RECEIVER_COMMAND_KEY: "cmd",
        })

        self.assertEqual(settings, SshSettings("gw", "host", "alice", 2222, "cmd"))
        self.assertTrue(settings.is_complete())

    def test_non_numeric_port_is_rejected(self):
        with self.assertRaises(client_config.ConfigError):
            client_config.get_ssh_settings({client_config.SSH_PORT_KEY: "twenty-two"})


class ValidateSshSettingsTest(unittest.TestCase):
    def test_complete_settings_are_valid(self):
        self.assertEqual(client_config.validate_ssh_settings(complete_settings()), [])

    def test_gateway_may_include_a_port(self):
        settings = complete_settings(gateway="gateway.example:2200")

        self.assertEqual(client_config.validate_ssh_settings(settings), [])

    def test_ip_address_host_is_valid(self):
        self.assertEqual(client_config.validate_ssh_settings(complete_settings(host="10.0.0.5")), [])

    def test_missing_gateway_and_host_are_reported(self):
        problems = client_config.validate_ssh_settings(complete_settings(gateway="", host=""))

        self.assertEqual(len(problems), 2)

    def test_values_that_look_like_ssh_options_are_rejected(self):
        for field in ("gateway", "host", "user"):
            with self.subTest(field=field):
                settings = complete_settings(**{field: "-oProxyCommand=evil"})

                self.assertNotEqual(client_config.validate_ssh_settings(settings), [])

    def test_whitespace_inside_host_is_rejected(self):
        self.assertNotEqual(
            client_config.validate_ssh_settings(complete_settings(host="lab host")), [])

    def test_out_of_range_port_is_rejected(self):
        self.assertNotEqual(client_config.validate_ssh_settings(complete_settings(port=70000)), [])

    def test_missing_receiver_command_is_reported(self):
        problems = client_config.validate_ssh_settings(complete_settings(receiver_command=""))

        self.assertEqual(len(problems), 1)
        self.assertIn(client_config.SSH_RECEIVER_COMMAND_KEY, problems[0])


class SaveAndUpdateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.path = os.path.join(directory, "poor_man.json")

    def test_with_ssh_connection_preserves_other_settings(self):
        original = {
            client_config.WINDOW_CORNER_KEY: "top_left",
            client_config.SSH_RECEIVER_COMMAND_KEY: "cmd",
        }

        updated = client_config.with_ssh_connection(original, " gw ", "host", "alice")

        self.assertEqual(updated[client_config.WINDOW_CORNER_KEY], "top_left")
        self.assertEqual(updated[client_config.SSH_RECEIVER_COMMAND_KEY], "cmd")
        self.assertEqual(updated[client_config.SSH_GATEWAY_KEY], "gw")
        self.assertNotIn(client_config.SSH_GATEWAY_KEY, original)

    def test_save_then_load_round_trips(self):
        config = client_config.with_ssh_connection({"other": 1}, "gw", "host", "")

        client_config.save_config(self.path, config)

        self.assertEqual(client_config.load_config(self.path), config)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["poor_man.json"])

    def test_save_writes_readable_indented_json(self):
        client_config.save_config(self.path, {"a": 1})

        with open(self.path) as f:
            self.assertEqual(f.read(), json.dumps({"a": 1}, indent=4))


if __name__ == "__main__":
    unittest.main()
