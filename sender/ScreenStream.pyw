"""Simple window for running the sender without a terminal. Double-click to open.

Settings are saved to settings.json next to this file. The sender itself runs as
stream_server.py in the background; its status line is shown at the bottom.
"""
import json
import os
import secrets
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "settings.json"
REQUIREMENTS = HERE.parent / "requirements.txt"
DEFAULTS = {"token": "", "preset": "live", "budget": "", "fps": "", "monitor": "1", "port": "8080"}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def python_exe():
    # pythonw.exe has no console; the sender needs a normal python.exe to print status.
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    return str(exe)


def missing_packages():
    probe = subprocess.run([python_exe(), "-c", "import mss, numpy, PIL"], capture_output=True, creationflags=NO_WINDOW)
    return probe.returncode != 0


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        root.title("Roblox Screen Stream")
        root.resizable(False, False)

        saved = DEFAULTS | (json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {})
        if not saved["token"]:
            saved["token"] = secrets.token_urlsafe(12)
        self.vars = {k: tk.StringVar(value=str(v)) for k, v in saved.items()}

        frm = ttk.Frame(root, padding=12)
        frm.grid()
        rows = [
            ("Token (copy into StreamServer)", "token"),
            ("Preset", "preset"),
            ("Bandwidth cap KB/s (blank = preset)", "budget"),
            ("Frames per second (blank = 15)", "fps"),
            ("Monitor (1 = main)", "monitor"),
            ("Port", "port"),
        ]
        for r, (label, key) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2)
            if key == "preset":
                w = ttk.Combobox(frm, textvariable=self.vars[key], values=["live", "max"], state="readonly", width=28)
            else:
                w = ttk.Entry(frm, textvariable=self.vars[key], width=30)
            w.grid(row=r, column=1, pady=2)
        ttk.Button(frm, text="Copy", width=6, command=self.copy_token).grid(row=0, column=2, padx=(4, 0))
        ttk.Label(frm, text="live = safe for published games, max = Studio only", foreground="gray").grid(
            row=len(rows), column=0, columnspan=3, sticky="w")

        self.button = ttk.Button(frm, text="Start streaming", command=self.toggle)
        self.button.grid(row=len(rows) + 1, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.status = tk.StringVar(value="Stopped")
        ttk.Label(frm, textvariable=self.status, width=55).grid(row=len(rows) + 2, column=0, columnspan=3, sticky="w")
        root.protocol("WM_DELETE_WINDOW", self.close)

    def copy_token(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.vars["token"].get())
        self.status.set("Token copied")

    def save(self):
        SETTINGS.write_text(json.dumps({k: v.get().strip() for k, v in self.vars.items()}, indent=2))

    def toggle(self):
        if self.proc:
            self.stop()
            return
        v = {k: var.get().strip() for k, var in self.vars.items()}
        for key in ("budget", "fps", "monitor", "port"):
            if v[key] and not v[key].isdigit():
                messagebox.showerror("Screen Stream", f"{key} must be a whole number.")
                return
        if not v["token"]:
            messagebox.showerror("Screen Stream", "Enter a token.")
            return
        self.save()
        if missing_packages():
            if not messagebox.askyesno("Screen Stream", "Some Python packages are missing. Install them now?"):
                return
            self.status.set("Installing packages…")
            self.root.update()
            result = subprocess.run([python_exe(), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
                                    capture_output=True, text=True, creationflags=NO_WINDOW)
            if result.returncode != 0:
                messagebox.showerror("Screen Stream", "Install failed:\n" + result.stderr[-800:])
                self.status.set("Stopped")
                return
        args = [python_exe(), "-u", str(HERE / "stream_server.py"), "--token", v["token"], "--preset", v["preset"],
                "--monitor", v["monitor"] or "1", "--port", v["port"] or "8080"]
        if v["budget"]:
            args += ["--budget", v["budget"]]
        if v["fps"]:
            args += ["--fps", v["fps"]]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     creationflags=NO_WINDOW)
        self.stopping = False
        self.button.config(text="Stop streaming")
        self.status.set("Starting…")
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()

    def read_output(self, proc):
        last = ""
        for line in proc.stdout:
            last = line.strip() or last
            self.root.after(0, self.status.set, last)
        code = proc.wait()
        self.root.after(0, self.stopped, proc, code, last)

    def stopped(self, proc, code, last):
        if proc is not self.proc:
            return
        self.proc = None
        self.button.config(text="Start streaming")
        self.status.set("Stopped" if self.stopping else f"Stopped: {last}")

    def stop(self):
        if self.proc:
            self.stopping = True
            self.proc.terminate()

    def close(self):
        self.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
