import glob
import json
import logging
import os
import sqlite3
import sys
import threading
import time

logger = logging.getLogger("poor_man_server")
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler("poor_man.log")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
logger.addHandler(file_handler)


class PoorManServer:
    def __init__(self, config_path):
        self.config_path = config_path
        self.read_config()
        self._last_status = None
        self._snapshot_lock = threading.Lock()
        self.ssh_server = None

    def read_config(self):
        with open(self.config_path) as f:
            config = json.load(f)
            redacted = {k: ("***" if k == "poor_man_ssh_password" else v) for k, v in config.items()}
            logger.info(f"Read config from {redacted}")
        self.root_dir = config["poor_man_message_queue_root"]
        self.init_config_path = config["poor_man_message_init_config"]
        self.db_path = config["poor_man_db_path"]
        self.config = config

    def setup_db(self):
        with open(self.init_config_path) as f:
            init_config = json.load(f)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS labs (
                    name TEXT PRIMARY KEY,
                    occupant TEXT,
                    checked_out_at TEXT
                )
                """
            )
            for lab in init_config["labs"]:
                conn.execute(
                    "INSERT OR IGNORE INTO labs (name, occupant, checked_out_at) VALUES (?, NULL, NULL)",
                    (lab,),
                )
        conn.close()
        logger.info(f"Set up db with labs: {init_config['labs']}")

    def poll_root(self):
        pattern = os.path.join(self.root_dir, "req_*.json")
        return sorted(glob.glob(pattern))

    def process_message(self, path):
        with open(path) as f:
            message = json.load(f)
        self.apply_request(message)
        os.remove(path)

    def apply_request(self, message):
        """Apply a checkout/release request to the db. Returns {"ok": bool, "message": str}."""
        action = message.get("action")
        user = message.get("user")
        lab = message.get("lab")

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT occupant FROM labs WHERE name = ?", (lab,)).fetchone()

            if row is None:
                ok, result = False, f"Rejected '{action}' for unknown lab '{lab}' from {user}"
            elif action not in ("checkout", "release"):
                ok, result = False, f"Ignored unsupported action '{action}' from {user}"
            elif action == "checkout":
                cursor = conn.execute(
                    "UPDATE labs SET occupant = ?, checked_out_at = ? WHERE name = ? AND occupant IS NULL",
                    (user, message.get("timestamp"), lab),
                )
                if cursor.rowcount == 1:
                    ok, result = True, f"{user} checked out {lab}"
                elif row[0] == user:
                    ok, result = True, f"{user} already holds {lab}"
                else:
                    ok, result = False, f"{user} lost the race for {lab} (already occupied by {row[0]})"
            else:
                cursor = conn.execute(
                    "UPDATE labs SET occupant = NULL, checked_out_at = NULL WHERE name = ? AND occupant = ?",
                    (lab, user),
                )
                if cursor.rowcount == 1:
                    ok, result = True, f"{user} released {lab}"
                else:
                    ok, result = False, f"{user} could not release {lab} (not the current occupant)"

        conn.close()
        logger.info(result)
        return {"ok": ok, "message": result}

    def handle_request(self, message):
        """Entry point for SSH requests: apply it, then return the result plus current status."""
        if message.get("action") == "status":
            response = {"ok": True, "message": "status"}
        else:
            response = self.apply_request(message)
            try:
                # Keep status.json current for clients still reading the shared directory.
                self.export_snapshot()
            except Exception:
                logger.exception("Failed to export snapshot after SSH request")
        response["status"] = self.read_status()
        return response

    def read_status(self):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT name, occupant FROM labs").fetchall()
        conn.close()
        return {name: occupant for name, occupant in rows}

    def start_ssh_server(self):
        """Start the SSH request listener if poor_man.json configures one."""
        if not self.config.get("poor_man_ssh_host"):
            return None

        import poor_man_ssh

        host_key_path = self.config.get(
            "poor_man_ssh_host_key",
            os.path.join(os.path.dirname(os.path.abspath(self.db_path)), "poor_man_host_key"),
        )
        self.ssh_server = poor_man_ssh.SSHRequestServer(
            self.handle_request,
            poor_man_ssh.load_or_create_host_key(host_key_path),
            self.config.get("poor_man_ssh_username", poor_man_ssh.DEFAULT_SSH_USERNAME),
            self.config.get("poor_man_ssh_password", ""),
            address=self.config.get("poor_man_ssh_listen_address", "0.0.0.0"),
            port=int(self.config.get("poor_man_ssh_port", poor_man_ssh.DEFAULT_SSH_PORT)),
        ).start()
        return self.ssh_server

    def export_snapshot(self):
        # Called from both the poll loop and SSH request threads.
        with self._snapshot_lock:
            status = self.read_status()
            if status == self._last_status:
                return

            final_path = os.path.join(self.root_dir, "status.json")
            with open(final_path, "w") as f:
                json.dump(status, f)

            self._last_status = status

    def run(self, poll_interval=3):
        self.setup_db()
        self.start_ssh_server()
        while True:
            for path in self.poll_root():
                try:
                    self.process_message(path)
                except Exception:
                    logger.exception(f"Failed to process {path}, will retry next poll")
            try:
                self.export_snapshot()
            except Exception:
                logger.exception("Failed to export snapshot, will retry next poll")
            time.sleep(poll_interval)


if __name__ == "__main__":
    PoorManServer(sys.argv[1]).run()