import functools
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from tkinter import *

import get_user
from _version import __version__

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(app_dir(), "poor_man.json")
with open(CONFIG_PATH) as f:
    _config = json.load(f)
ROOT_DIR = _config["poor_man_message_queue_root"]
STATUS_PATH = os.path.join(ROOT_DIR, "status.json")
LAST_MODIFIED = None
LAST_STATUS = None

# When poor_man.json has SSH settings, requests and status go straight to the server over
# SSH; the shared directory is still used as a fallback if SSH is unavailable.
SSH_CLIENT = None
if _config.get("poor_man_ssh_host"):
    try:
        import poor_man_ssh
        SSH_CLIENT = poor_man_ssh.SSHLabClient.from_config(_config)
    except ImportError:
        print("paramiko is not installed; using the shared directory instead of SSH")

WINDOW_CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
DEFAULT_WINDOW_CORNER = "bottom_right"
WINDOW_CORNER = _config.get("poor_man_window_corner", DEFAULT_WINDOW_CORNER)
if WINDOW_CORNER not in WINDOW_CORNERS:
    WINDOW_CORNER = DEFAULT_WINDOW_CORNER

def save_window_corner(corner):
    _config["poor_man_window_corner"] = corner
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(_config, f, indent=4)
    os.replace(tmp_path, CONFIG_PATH)

CURRENT_USER = get_user.get_display_name()

root = Tk(className="Lab Tools")
title = f"Lab Tools {__version__}"
root.title(title)

topmost_enabled = True

def reassert_topmost():
    if topmost_enabled:
        root.attributes("-topmost", False)
        root.attributes("-topmost", True)
    root.after(3000, reassert_topmost)

def apply_window_geometry():
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
    global corner_picker
    if corner_picker is not None:
        corner_picker.destroy()
        corner_picker = None

def move_to_corner(corner):
    global WINDOW_CORNER
    WINDOW_CORNER = corner
    save_window_corner(corner)
    close_corner_picker()
    apply_window_geometry()

def show_corner_picker():
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
    def _run():
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(args, capture_output=True, text=True, creationflags=creationflags)
        print((result.stdout or result.stderr).strip())
    threading.Thread(target=_run, daemon=True).start()

def kill_all_rdp():
    if not sys.platform.startswith("win32"):
        print("Kill All RDP: only supported on Windows")
        return
    run_background_command(["taskkill", "/IM", "mstsc.exe", "/F"])

def toggle_topmost():
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

lab_widgets = {}

placeholder_label = Label(root, text="Waiting for server...", anchor="w")
placeholder_label.grid(row=1, column=0, padx=(15, 15), pady=8, sticky="w")

def load_status():
    if not os.path.exists(STATUS_PATH):
        return None

    with open(STATUS_PATH) as f:
        return json.load(f)

status_updates = queue.Queue()

def write_request(request):
    uid = uuid.uuid4().hex
    tmp_path = os.path.join(ROOT_DIR, f".req_{uid}.tmp")
    final_path = os.path.join(ROOT_DIR, f"req_{uid}.json")
    with open(tmp_path, "w") as f:
        json.dump(request, f)
    os.replace(tmp_path, final_path)

def submit_request(lab, action):
    request = {
        "action": action,
        "user": CURRENT_USER,
        "lab": lab,
        "timestamp": datetime.now().isoformat(),
    }
    if SSH_CLIENT is not None:
        try:
            response = SSH_CLIENT.send(request)
            print(response.get("message"))
            status_updates.put(response["status"])
            return
        except Exception as e:
            print(f"SSH request failed, falling back to shared directory: {e}")
    write_request(request)

def handle_click(lab, action, button):
    global LAST_STATUS
    button.config(state="disabled")
    # Force the next status to be applied even if unchanged, so a rejected request re-enables the button.
    LAST_STATUS = None
    threading.Thread(target=submit_request, args=(lab, action), daemon=True).start()

AVAILABLE_BG = "#2e7d32"
OCCUPIED_BG = "#c62828"

def build_lab_widgets(status):
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

def read_status_file_if_changed():
    global LAST_MODIFIED
    modified = os.path.getmtime(STATUS_PATH) if os.path.exists(STATUS_PATH) else None
    if modified == LAST_MODIFIED:
        return None
    status = load_status()
    LAST_MODIFIED = modified
    return status

def fetch_status():
    if SSH_CLIENT is not None:
        try:
            return SSH_CLIENT.status()
        except Exception as e:
            print(f"SSH status failed, falling back to shared directory: {e}")
    return read_status_file_if_changed()

def poll_status_forever():
    # Runs off the UI thread so a slow share or SSH timeout can't freeze the window.
    while True:
        try:
            status = fetch_status()
            if status is not None:
                status_updates.put(status)
        except Exception as e:
            print(f"refresh_status failed: {e}")
        time.sleep(3)

def refresh_status():
    global LAST_STATUS

    try:
        status = None
        while not status_updates.empty():
            status = status_updates.get_nowait()
        if status is not None and status != LAST_STATUS:
            if not lab_widgets:
                build_lab_widgets(status)
            else:
                update_lab_widgets(status)
                apply_window_geometry()
            LAST_STATUS = status
    except Exception as e:
        print(f"refresh_status failed: {e}")
    finally:
        root.after(200, refresh_status)


threading.Thread(target=poll_status_forever, daemon=True).start()
refresh_status()
apply_window_geometry()
reassert_topmost()

root.mainloop()
