#!/usr/bin/env python3
"""FileDrop desktop app: scan the QR code with your phone to transfer files."""
import os
import shutil
import subprocess
import sys

import webview
from webview.dom import DOMEventHandler

import server

SHARE = os.path.join(os.path.expanduser("~"), "FileDrop")


def reveal(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


class Api:
    """Methods callable from the desktop page as window.pywebview.api.*"""

    def __init__(self):
        self.last_dir = os.path.expanduser("~/Downloads")

    def _src(self, name, folder=None, box="inbox"):
        base = server.STATE[box]
        sub = server.safe_name(folder) if folder else ""
        parts = [base] + ([sub] if sub else []) + ([server.safe_name(name)] if name and server.safe_name(name) else [])
        return os.path.join(*parts)

    def save(self, name, folder=None):
        """Save one file (asks for a file name) or a whole named folder (asks where to create it)."""
        win = webview.windows[0]
        if folder:
            src = self._src("", folder)
            if not os.path.isdir(src):
                return False
            chosen = win.create_file_dialog(webview.FOLDER_DIALOG, directory=self.last_dir)
            if not chosen:
                return False
            parent = chosen if isinstance(chosen, str) else chosen[0]
            self.last_dir = parent
            dest = os.path.join(parent, os.path.basename(src))
            os.makedirs(dest, exist_ok=True)
            for f in os.listdir(src):
                if not f.endswith(".part"):
                    shutil.move(os.path.join(src, f), server.unique_path(dest, f))
            shutil.rmtree(src, ignore_errors=True)
            return True
        src = self._src(name)
        if not os.path.isfile(src):
            return False
        dest = win.create_file_dialog(webview.SAVE_DIALOG, directory=self.last_dir, save_filename=name)
        if not dest:
            return False
        dest = dest if isinstance(dest, str) else dest[0]
        self.last_dir = os.path.dirname(dest)
        shutil.move(src, dest)
        return True

    def discard(self, name, folder=None):
        p = self._src(name, folder)
        if folder and not name:
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)

    def send_files(self, folder=None):
        paths = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        if not paths:
            return 0
        target = server.target_dir(server.STATE["outbox"], folder)
        for f in paths:
            shutil.copy(f, server.unique_path(target, os.path.basename(f)))
        return len(paths or [])

    def remove_outgoing(self, name, folder=None):
        p = self._src(name, folder, "outbox")
        if folder and not name:
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)

    def open_folder(self):
        reveal(SHARE)


def add_to_outbox(paths, folder):
    """Copy dropped files (or the files of dropped folders) into the 'For phone' box."""
    out = server.STATE["outbox"]
    for p in paths:
        if os.path.isdir(p):
            target = server.target_dir(out, folder or os.path.basename(p.rstrip("/")))
            names = [os.path.join(p, n) for n in os.listdir(p)]
            files = [f for f in names if os.path.isfile(f) and not os.path.basename(f).startswith(".")]
        else:
            target, files = server.target_dir(out, folder), [p]
        for f in files:
            shutil.copy(f, server.unique_path(target, os.path.basename(f)))


def on_drop(event):
    win = webview.windows[0]
    paths = [f["pywebviewFullPath"] for f in event["dataTransfer"].get("files", [])
             if f.get("pywebviewFullPath")]
    if paths:
        folder = (win.evaluate_js('document.getElementById("folder").value') or "").strip()
        add_to_outbox(paths, folder or None)
        win.evaluate_js("tick()")


def setup_drop(window):
    # Handle drops on the whole window; prevent_default stops WebKit from opening the file.
    window.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True)
    window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)


def main():
    _, port = server.start(SHARE)
    window = webview.create_window("FileDrop", f"http://127.0.0.1:{port}/desktop",
                                   js_api=Api(), width=460, height=780, min_size=(400, 640))
    window.events.loaded += lambda: setup_drop(window)
    webview.start()


if __name__ == "__main__":
    main()
