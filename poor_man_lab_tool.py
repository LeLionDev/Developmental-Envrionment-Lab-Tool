import functools
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from tkinter import *

import client_config
import get_user
import messages
import ssh_setup_dialog
import transports
from _version import __version__

def app_dir():
    """Directory holding the script, or the .exe when running as a PyInstaller build."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(app_dir(), "poor_man.json")
_config = client_config.load_config(CONFIG_PATH)
TRANSPORT_MODE = client_config.get_transport_mode(_config)
USES_SSH = TRANSPORT_MODE == client_config.TRANSPORT_SSH

WINDOW_CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
DEFAULT_WINDOW_CORNER = "bottom_right"
WINDOW_CORNER = _config.get(client_config.WINDOW_CORNER_KEY, DEFAULT_WINDOW_CORNER)
if WINDOW_CORNER not in WINDOW_CORNERS:
    WINDOW_CORNER = DEFAULT_WINDOW_CORNER

def save_window_corner(corner):
    """Remember the chosen window corner in poor_man.json so it survives restarts."""
    _config[client_config.WINDOW_CORNER_KEY] = corner
    client_config.save_config(CONFIG_PATH, _config)

CURRENT_USER = get_user.get_display_name()

root = Tk(className="Lab Tools")
title = f"Lab Tools {__version__}"
root.title(title)

def prompt_and_save_ssh_connection():
    """Ask the user for gateway/host, save them to poor_man.json. Returns False if cancelled."""
    global _config
    current = client_config.get_ssh_settings(_config)
    entered = ssh_setup_dialog.ask_ssh_connection(root, current)
    if entered is None:
        return False
    _config = client_config.with_ssh_connection(_config, entered.gateway, entered.host, entered.user)
    client_config.save_config(CONFIG_PATH, _config)
    return True

# First launch in SSH mode: collect the connection details before showing the lab window.
if USES_SSH and client_config.validate_ssh_settings(client_config.get_ssh_settings(_config)):
    root.withdraw()
    if not prompt_and_save_ssh_connection():
        root.destroy()
        sys.exit(0)
    root.deiconify()

topmost_enabled = True

def reassert_topmost():
    """Periodically re-raise the window above others while Stay on Top is enabled."""
    if topmost_enabled:
        root.attributes("-topmost", False)
        root.attributes("-topmost", True)
    root.after(3000, reassert_topmost)

def apply_window_geometry():
    """Size the window to its contents and snap it to the configured screen corner."""
    root.update_idletasks()

    window_width = root.winfo_reqwidth()
    window_height = root.winfo_reqheight()

    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()

    # 10px margin on left/right/top edges; 50px on bottom to clear the taskbar
    x = 10 if "left" in WINDOW_CORNER else screen_width - window_width - 10
    y = 10 if "top" in WINDOW_CORNER else screen_height - window_height - 50

    root.geometry(f"{window_width}x{window_height}+{x}+{y}")

corner_picker = None

def close_corner_picker():
    """Close the corner picker popup if it is open."""
    global corner_picker
    if corner_picker is not None:
        corner_picker.destroy()
        corner_picker = None

def move_to_corner(corner):
    """Snap the window to a new corner and persist the choice."""
    global WINDOW_CORNER
    WINDOW_CORNER = corner
    save_window_corner(corner)
    close_corner_picker()
    apply_window_geometry()

def show_corner_picker():
    """Toggle the small popup that lets the user pick a screen corner."""
    global corner_picker
    if corner_picker is not None:
        close_corner_picker()
        return

    corner_picker = Toplevel(root)
    corner_picker.overrideredirect(True)
    corner_picker.attributes("-topmost", True)
    corner_picker.configure(bg="#2e2e2e")
    corner_picker.bind("<FocusOut>", lambda e: close_corner_picker())

    x = reset_btn.winfo_rootx() + reset_btn.winfo_width() // 2 - 45
    y = reset_btn.winfo_rooty() + reset_btn.winfo_height()
    corner_picker.geometry(f"90x60+{x}+{y}")

    button_grid = {
        "top_left": (0, 0), "top_right": (0, 1),
        "bottom_left": (1, 0), "bottom_right": (1, 1),
    }
    button_label = {
        "top_left": "↖", "top_right": "↗",
        "bottom_left": "↙", "bottom_right": "↘",
    }
    for corner, (row, col) in button_grid.items():
        btn = Button(corner_picker, text=button_label[corner],
                     command=functools.partial(move_to_corner, corner),
                     bg="#2e2e2e", fg="white", bd=0, highlightthickness=0,
                     activebackground="#4a4a4a", activeforeground="white")
        btn.grid(row=row, column=col, sticky="nsew")
        corner_picker.grid_columnconfigure(col, weight=1)
        corner_picker.grid_rowconfigure(row, weight=1)

    corner_picker.focus_set()

def run_background_command(args):
    """Run a shell command on a worker thread and print its output."""
    def _run():
        """Worker body: run the command without a console window and print the result."""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(args, capture_output=True, text=True, creationflags=creationflags)
        print((result.stdout or result.stderr).strip())
    threading.Thread(target=_run, daemon=True).start()

def kill_all_rdp():
    """Force-close every Remote Desktop (mstsc.exe) window. Windows only."""
    if not sys.platform.startswith("win32"):
        print("Kill All RDP: only supported on Windows")
        return
    run_background_command(["taskkill", "/IM", "mstsc.exe", "/F"])

def toggle_topmost():
    """Switch Stay on Top on or off and update the button label."""
    global topmost_enabled
    topmost_enabled = not topmost_enabled
    root.attributes("-topmost", topmost_enabled)
    topmost_btn.config(text=" 📌 Stay on Top: ON " if topmost_enabled else " 📌 Stay on Top: OFF ")

toolbar = Frame(root, bg="#2e2e2e", height=30)
toolbar.grid(row=0, column=0, columnspan=1, sticky="ew")  # widened to fit lab count once build_lab_widgets runs

reset_btn = Button(toolbar, text=" 🔄 Reset ", command=show_corner_picker, bg="#2e2e2e", fg="white", bd=0, highlightthickness=0, activebackground="#4a4a4a", activeforeground="white")
reset_btn.pack(side="right", fill="y", padx=2)

kill_rdp_btn = Button(toolbar, text=" 💀 Kill All RDP ", command=kill_all_rdp, bg="#2e2e2e", fg="white", bd=0, highlightthickness=0, activebackground="#4a4a4a", activeforeground="white")
kill_rdp_btn.pack(side="right", fill="y", padx=2)

topmost_btn = Button(toolbar, text=" 📌 Stay on Top: ON ", command=toggle_topmost, bg="#2e2e2e", fg="white", bd=0, highlightthickness=0, activebackground="#4a4a4a", activeforeground="white")
topmost_btn.pack(side="right", fill="y", padx=2)

LIVE_FG = "#66bb6a"
RECONNECTING_FG = "#ffa726"
connection_label = None

def edit_ssh_connection():
    """Let the user change gateway/host, then reconnect the status stream with the new values."""
    if prompt_and_save_ssh_connection():
        stop_status_watch()
        start_status_watch()

if USES_SSH:
    connection_btn = Button(toolbar, text=" ⚙ Connection ", command=edit_ssh_connection, bg="#2e2e2e", fg="white", bd=0, highlightthickness=0, activebackground="#4a4a4a", activeforeground="white")
    connection_btn.pack(side="right", fill="y", padx=2)

    # Fixed width so switching between "Live" and "Reconnecting" never resizes the toolbar.
    connection_label = Label(toolbar, text=" ● Connecting ", width=16, anchor="w", bg="#2e2e2e", fg=RECONNECTING_FG)
    connection_label.pack(side="left", fill="y", padx=2)

lab_widgets = {}

placeholder_label = Label(root, text="Waiting for server...", anchor="w")
placeholder_label.grid(row=1, column=0, padx=(15, 15), pady=8, sticky="w")

# Background threads (status watcher, request sender) never touch widgets directly --
# Tkinter is not thread-safe. They post (generation, kind, payload) events onto this queue,
# and process_ui_events drains it on the GUI thread.
ui_events = queue.Queue()
transport = None
watch_stop = None
watch_generation = 0
last_status = None

def start_status_watch():
    """Create the configured transport and start streaming status on a background thread."""
    global transport, watch_stop, watch_generation
    watch_generation += 1
    generation = watch_generation
    transport = transports.create_transport(_config)
    watch_stop = threading.Event()
    threading.Thread(
        target=transport.watch_status,
        args=(
            lambda status: ui_events.put((generation, "status", status)),
            lambda problem: ui_events.put((generation, "problem", problem)),
            watch_stop,
        ),
        daemon=True,
    ).start()

def stop_status_watch():
    """Stop the current status watcher and release its connection."""
    if watch_stop is not None:
        watch_stop.set()
    if transport is not None:
        transport.close()

def send_request(lab, action):
    """Deliver a checkout/release request; runs on a worker thread so the GUI never freezes."""
    request = messages.build_request(action, CURRENT_USER, lab, datetime.now().isoformat())
    try:
        transport.send_request(request)
    except Exception as e:
        ui_events.put((watch_generation, "send_failed", f"{action} {lab} failed: {e}"))

def handle_click(lab, action, button):
    """Disable the clicked button until the next status update, then send the request."""
    button.config(state="disabled")
    threading.Thread(target=send_request, args=(lab, action), daemon=True).start()

AVAILABLE_BG = "#2e7d32"
OCCUPIED_BG = "#c62828"

def build_lab_widgets(status):
    """Create one column of widgets per lab the first time a status snapshot arrives."""
    placeholder_label.grid_forget()
    toolbar.grid_configure(columnspan=len(status))
    for i, lab in enumerate(sorted(status)):
        name_label = Label(root, text=lab, font=("Arial", 10, "bold"), fg="white")
        name_label.grid(row=1, column=i, padx=(15, 15), pady=(8, 0), sticky="ew")

        occupant_label = Label(root, anchor="center")
        occupant_label.grid(row=2, column=i, padx=(15, 15), pady=(0, 4), sticky="ew")

        button = Button(root)
        button.grid(row=3, column=i, padx=(15, 15), pady=(0, 8), sticky="ew")

        lab_widgets[lab] = (name_label, occupant_label, button)

    update_lab_widgets(status)
    apply_window_geometry()

def update_lab_widgets(status):
    """Recolour and relabel each lab's widgets to match the snapshot."""
    for lab, (name_label, occupant_label, button) in lab_widgets.items():
        occupant = status.get(lab)

        if occupant is None:
            name_label.config(bg=AVAILABLE_BG)
            occupant_label.config(text="free")
            button.config(text="Check Out", state="normal",
                           command=functools.partial(handle_click, lab, "checkout", button))
        elif occupant == CURRENT_USER:
            name_label.config(bg=OCCUPIED_BG)
            occupant_label.config(text=occupant)
            button.config(text="Release", state="normal",
                           command=functools.partial(handle_click, lab, "release", button))
        else:
            name_label.config(bg=OCCUPIED_BG)
            occupant_label.config(text=occupant)
            button.config(text="Occupied", state="disabled")

def apply_status(status):
    """Show a new snapshot: build the lab widgets the first time, update them afterwards."""
    global last_status
    last_status = status
    if not lab_widgets:
        build_lab_widgets(status)
    else:
        update_lab_widgets(status)
        apply_window_geometry()
    if connection_label is not None:
        connection_label.config(text=" ● Live ", fg=LIVE_FG)

def show_problem(message):
    """Report a connection problem without interrupting the user."""
    print(message)
    if connection_label is not None:
        connection_label.config(text=" ● Reconnecting ", fg=RECONNECTING_FG)
    if not lab_widgets:
        placeholder_label.config(text=f"Waiting for server... ({message})")

def handle_send_failure(message):
    """A request never reached the server: report it and re-enable the buttons."""
    print(message)
    if connection_label is not None:
        connection_label.config(text=" ⚠ Request failed ", fg=RECONNECTING_FG)
    if last_status is not None:
        update_lab_widgets(last_status)

def process_ui_events():
    """Apply every event posted by background threads, ignoring ones from a replaced watcher."""
    try:
        while True:
            generation, kind, payload = ui_events.get_nowait()
            if generation != watch_generation:
                continue
            try:
                if kind == "status":
                    apply_status(payload)
                elif kind == "problem":
                    show_problem(payload)
                elif kind == "send_failed":
                    handle_send_failure(payload)
            except Exception as e:
                print(f"Failed to handle {kind} event: {e}")
    except queue.Empty:
        pass
    finally:
        root.after(200, process_ui_events)

def on_close():
    """Close the status connection cleanly before the window goes away."""
    stop_status_watch()
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)
start_status_watch()
process_ui_events()
apply_window_geometry()
reassert_topmost()

root.mainloop()
