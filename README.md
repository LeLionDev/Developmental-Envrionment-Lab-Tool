# Poor Man's Lab Checkout Tool

![Tool screenshot](assets/GUI_demo.png)

A lab checkout/reservation tool built around a "poor man's message queue": instead of real
client-server networking (which is restricted in this environment), the server and clients
communicate entirely through a shared filesystem directory.

## How it works

- A **server** (`poor_man_server.py`) owns a local SQLite database tracking which labs are
  occupied by whom. It polls a shared directory for request files dropped in by clients,
  applies them to the database, and writes out a snapshot (`status.json`) for clients to read.
- A **client** (`poor_man_lab_tool.py`, a small always-on-top tkinter window) reads that
  snapshot to show live lab status, and lets you check out or release a lab by writing a
  request file into the same shared directory.
- The database itself never lives in the shared directory and is never written by more than
  one process -- only the server touches it. This avoids the file corruption you'd risk from
  multiple machines writing to the same SQLite file over a network share.

```
client  --writes req_*.json-->  shared dir  --polled by-->  server  --writes-->  local .db
client  <--reads status.json--  shared dir  <--exports----  server
```

## Requirements

Everything here is Python standard library only -- no `pip install` needed to run the server
or the client from source. Tested against Python 3.

## Setup

### 1. Pick a shared directory

Choose a directory both the server machine and all client machines can read/write -- typically
a network share. This is your **queue root**.

### 2. Create `init.json`

A one-time setup file listing the labs to track. It lives wherever you want (commonly right in
the queue root), and only needs to exist before the server's first run:

```json
{
    "labs": ["labA", "labB", "labC", "labD", "labE", "labF"]
}
```

Re-running the server against an updated `init.json` (e.g. to add a lab) is safe -- seeding is
idempotent and never touches an already-occupied lab.

### 3. Create `poor_man.json`

Copy `poor_man.json` (checked into this repo as a template) and fill in real, absolute paths
for your environment:

```json
{
    "poor_man_message_queue_root": "/path/to/your/shared/dir",
    "poor_man_message_init_config": "/path/to/your/shared/dir/init.json",
    "poor_man_db_path": "/path/to/a/local/dir/poor_man.db",
    "poor_man_window_corner": "bottom_right"
}
```

- `poor_man_message_queue_root` -- the shared directory from step 1.
- `poor_man_message_init_config` -- path to the `init.json` from step 2.
- `poor_man_db_path` -- **must be on local disk on the server machine**, not inside the shared
  directory. This is the one setting that matters most for correctness.
- `poor_man_window_corner` -- client-only; which screen corner the window snaps to
  (`top_left`, `top_right`, `bottom_left`, or `bottom_right`). Optional -- defaults to
  `bottom_right` if omitted, and the client rewrites this automatically whenever you use the
  Reset corner picker.

Keep a real, filled-in copy of this file out of version control (it's environment-specific) --
only the placeholder template belongs in the repo.

### 4. Run the server

```
python3 poor_man_server.py /path/to/your/poor_man.json
```

This runs forever: seeds the database from `init.json` on startup, then polls the queue
directory every few seconds, processing any pending requests and refreshing `status.json`.
Leave it running on one machine (it doesn't need to be a client machine).

### 5. Run the client

```
python3 poor_man_lab_tool.py /path/to/your/poor_man.json
```

Or, if a packaged `.exe` build is available (see below), just double-click it -- it looks for a
`poor_man.json` sitting next to it automatically, no argument needed.

A small always-on-top window appears showing each lab: green means free, red means occupied
(with the occupant's name shown underneath). Click a free lab's button to check it out; once
you hold it, the same button becomes "Release".

The toolbar above the labs has three buttons:

- **Reset** -- opens a small picker to snap the window to whichever screen corner you want
  (top-left, top-right, bottom-left, bottom-right). The chosen corner is remembered in
  `poor_man.json` and restored on the next launch.
- **Kill All RDP** -- force-closes all `mstsc.exe` (Remote Desktop) processes on the client
  machine. Windows only.
- **Stay on Top** -- toggles whether the window keeps reasserting itself above other windows.

## SSH connection test (experimental)

`ssh_connection_test.py` is a standalone script for checking that a lab machine is reachable
over SSH and accepts commands. Unlike the rest of the tool it needs
[paramiko](https://www.paramiko.org/) (`pip install paramiko`).

```
python3 ssh_connection_test.py HOST -u USER                        # runs "echo ssh-ok"
python3 ssh_connection_test.py HOST -u USER -c hostname -c uptime  # run specific commands
python3 ssh_connection_test.py HOST -u USER -i                     # interactive command prompt
```

Auth uses `--key`, then your SSH agent / `~/.ssh` keys, then a password (`SSH_PASSWORD` env var
or `--ask-password`). Unknown host keys are accepted with a warning unless `--strict-host-keys`
is passed. The exit code is the last command's exit status (2 = auth failed, 3 = couldn't
connect).

## Running the tests

```
python3 -m unittest tests.test_server -v
```

## Packaging the client as a standalone `.exe`

The client (not the server) can be packaged into a single-file Windows executable that needs
no Python installation to run, using [PyInstaller](https://pyinstaller.org/):

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "PoorManLabTool" poor_man_lab_tool.py
```

This has to be built on a Windows machine (PyInstaller doesn't cross-compile). The output,
`dist/PoorManLabTool.exe`, is fully self-contained -- distribute it alongside a filled-in
`poor_man.json` in the same folder, and nothing else needs to be installed on the machines
that run it. Building and running it both work without admin rights.

## Notes and known limitations (v1)

- Checkout is first-come-first-served; a lab can only be released by whoever currently holds
  it.
- "Kill All RDP" and window corner-snapping are Windows-tested; the corner picker also works
  on Linux, but window-manager behavior can vary (Wayland compositors especially).
- There's no client-driven setup flow; `init.json` is a static file the server reads at
  startup, not something sent from the GUI.
- The client lays labs out in a single row that grows with the lab count, with no wrapping or
  scrolling -- keep `init.json` to roughly **6-8 labs** for now. Beyond that the window can
  grow wider than the screen and push labs off the edge with no way to reach them.
