import glob
import json
import logging
import os
import sqlite3
import sys
import time

from messages import REQUEST_PREFIX, SUPPORTED_ACTIONS, status_path

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler("poor_man.log")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
logger.addHandler(file_handler)


class PoorManServer:
    """Owns the lab database and applies queued requests to it; the only writer of the db."""
    def __init__(self, config_path):
        """Load settings from poor_man.json; the db is not touched until setup_db."""
        self.config_path = config_path
        self.read_config()
        self._last_status = None

    def read_config(self):
        """Read the queue directory, init.json path and db path from poor_man.json."""
        with open(self.config_path) as f:
            config = json.load(f)
            logger.info(f"Read config from {config}")
        self.root_dir = config["poor_man_message_queue_root"]
        self.init_config_path = config["poor_man_message_init_config"]
        self.db_path = config["poor_man_db_path"]

    def setup_db(self):
        """Create the labs table if needed and add any labs from init.json (idempotent)."""
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
        """Return pending request files in the queue directory, oldest name first."""
        pattern = os.path.join(self.root_dir, f"{REQUEST_PREFIX}*.json")
        return sorted(glob.glob(pattern))

    def process_message(self, path):
        """Apply one request file to the db, log the outcome and delete the file."""
        with open(path) as f:
            message = json.load(f)

        action = message.get("action")
        user = message.get("user")
        lab = message.get("lab")

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT occupant FROM labs WHERE name = ?", (lab,)).fetchone()

            if row is None:
                logger.info(f"Rejected '{action}' for unknown lab '{lab}' from {user}")
            elif action not in SUPPORTED_ACTIONS:
                logger.info(f"Ignored unsupported action '{action}' from {user}")
            elif action == "checkout":
                cursor = conn.execute(
                    "UPDATE labs SET occupant = ?, checked_out_at = ? WHERE name = ? AND occupant IS NULL",
                    (user, message.get("timestamp"), lab),
                )
                if cursor.rowcount == 1:
                    logger.info(f"{user} checked out {lab}")
                else:
                    logger.info(f"{user} lost the race for {lab} (already occupied by {row[0]})")
            else:
                cursor = conn.execute(
                    "UPDATE labs SET occupant = NULL, checked_out_at = NULL WHERE name = ? AND occupant = ?",
                    (lab, user),
                )
                if cursor.rowcount == 1:
                    logger.info(f"{user} released {lab}")
                else:
                    logger.info(f"{user} could not release {lab} (not the current occupant)")

        conn.close()
        os.remove(path)

    def export_snapshot(self):
        """Write status.json for clients, skipping the write if nothing changed."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT name, occupant FROM labs").fetchall()
        conn.close()

        status = {name: occupant for name, occupant in rows}
        if status == self._last_status:
            return

        final_path = status_path(self.root_dir)
        with open(final_path, "w") as f:
            json.dump(status, f)

        self._last_status = status

    def run(self, poll_interval=3):
        """Seed the db, then process requests and export status forever."""
        self.setup_db()
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