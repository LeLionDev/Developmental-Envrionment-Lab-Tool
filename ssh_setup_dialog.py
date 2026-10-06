"""The "Connection settings" window where a user enters their gateway and receiving host.

This module only gathers input. Validation rules live in client_config (unit tested) and
saving is done by the caller, so the dialog stays a thin layer of widgets.
"""
from tkinter import Button, Entry, Frame, Label, StringVar, Toplevel

import client_config

_FIELDS = (
    ("gateway", "Gateway (host[:port])"),
    ("host", "Receiving host"),
    ("user", "Username (optional)"),
)


def ask_ssh_connection(parent, current):
    """Show a modal dialog prefilled from `current` (SshSettings).

    Returns the validated SshSettings the user entered, or None if they cancelled.
    The dialog stays open, showing the problems, until the input is valid or cancelled.
    """
    dialog = Toplevel(parent)
    dialog.title("Lab Tools - Connection settings")
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)

    values = {name: StringVar(value=getattr(current, name)) for name, _ in _FIELDS}
    result = {"settings": None}

    form = Frame(dialog, padx=12, pady=10)
    form.pack(fill="both")
    for row, (name, label) in enumerate(_FIELDS):
        Label(form, text=label, anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        entry = Entry(form, textvariable=values[name], width=32)
        entry.grid(row=row, column=1, padx=(8, 0), pady=2)
        if row == 0:
            entry.focus_set()

    error_label = Label(form, text="", fg="#c62828", justify="left", wraplength=320)
    error_label.grid(row=len(_FIELDS), column=0, columnspan=2, sticky="w", pady=(6, 0))

    def on_save():
        """Validate the entered values; close and return them only if they are valid."""
        candidate = client_config.SshSettings(
            gateway=values["gateway"].get().strip(),
            host=values["host"].get().strip(),
            user=values["user"].get().strip(),
            port=current.port,
            receiver_command=current.receiver_command,
        )
        problems = client_config.validate_ssh_settings(candidate)
        if problems:
            error_label.config(text="\n".join(problems))
            return
        result["settings"] = candidate
        dialog.destroy()

    buttons = Frame(dialog, padx=12)
    buttons.pack(fill="x", pady=(0, 10))
    Button(buttons, text="Cancel", width=10, command=dialog.destroy).pack(side="right")
    Button(buttons, text="Save", width=10, command=on_save).pack(side="right", padx=(0, 6))
    dialog.bind("<Return>", lambda e: on_save())
    dialog.bind("<Escape>", lambda e: dialog.destroy())

    dialog.grab_set()
    parent.wait_window(dialog)
    return result["settings"]
