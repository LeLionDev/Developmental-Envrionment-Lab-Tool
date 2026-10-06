"""Shared message protocol for the lab tool.

The client, the server and the SSH receiver all import from here, so the definition of
"what a request looks like" and "where the status snapshot lives" exists in exactly one
place. The filesystem path and the SSH path therefore cannot drift apart.
"""
import json
import os
import uuid

REQUEST_PREFIX = "req_"
STATUS_FILENAME = "status.json"
SUPPORTED_ACTIONS = ("checkout", "release")
REQUIRED_REQUEST_FIELDS = ("action", "user", "lab", "timestamp")


class InvalidRequestError(ValueError):
    """Raised when a request body does not match the expected shape."""


def build_request(action, user, lab, timestamp):
    """Assemble a request dict in the exact shape the server expects."""
    return {"action": action, "user": user, "lab": lab, "timestamp": timestamp}


def validate_request(request):
    """Check a request's shape and return a copy containing only the known fields.

    Every required field must be a non-empty string and the action must be supported.
    Unknown extra fields are dropped so nothing unexpected reaches the queue. Whether the
    lab exists or is free is deliberately not checked here -- that is the server's job.
    Raises InvalidRequestError describing the first problem found.
    """
    if not isinstance(request, dict):
        raise InvalidRequestError("request must be a JSON object")
    for field in REQUIRED_REQUEST_FIELDS:
        value = request.get(field)
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequestError(f"missing or empty field '{field}'")
    if request["action"] not in SUPPORTED_ACTIONS:
        raise InvalidRequestError(f"unsupported action '{request['action']}'")
    return {field: request[field] for field in REQUIRED_REQUEST_FIELDS}


def write_request_file(root_dir, request):
    """Drop a request into the queue directory atomically and return its final path.

    The body is written to a hidden temp file first and then renamed, so the server's
    poller can never pick up a half-written request.
    """
    uid = uuid.uuid4().hex
    tmp_path = os.path.join(root_dir, f".{REQUEST_PREFIX}{uid}.tmp")
    final_path = os.path.join(root_dir, f"{REQUEST_PREFIX}{uid}.json")
    with open(tmp_path, "w") as f:
        json.dump(request, f)
    os.replace(tmp_path, final_path)
    return final_path


def status_path(root_dir):
    """Return the path of the status snapshot inside the queue directory."""
    return os.path.join(root_dir, STATUS_FILENAME)


def read_status_file(root_dir):
    """Load the status snapshot, or return None if the server has not written one yet.

    May raise json.JSONDecodeError if the server is mid-write; callers should treat that
    as "try again shortly" rather than as a real failure.
    """
    path = status_path(root_dir)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)
