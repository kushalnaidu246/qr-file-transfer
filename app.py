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
        """Path of a file or folder in a box, or None if the name is invalid (never the box itself)."""
        return server.resolve(server.STATE[box], name, folder)

    def save(self, name, folder=None):
        """Save one file (asks for a file name) or a whole named folder (asks where to create it)."""
        win = webview.windows[0]
        if folder:
            src = self._src("", folder)
            if not src or not os.path.isdir(src):
                return False
            if server.is_busy(src):
                return "busy"  # phone is still sending into this folder
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
            try:
                os.rmdir(src)  # only if empty: never delete a file that is still arriving
            except OSError:
                pass
            return True
        src = self._src(name)
        if not src or not os.path.isfile(src):
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
        if not p:
            return
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
            server.copy_in(f, target)
        return len(paths)

    def remove_outgoing(self, name, folder=None):
        p = self._src(name, folder, "outbox")
        if not p:
            return
        if folder and not name:
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)

    def open_folder(self):
        reveal(SHARE)


def on_drop(event):
    win = webview.windows[0]
    paths = [f["pywebviewFullPath"] for f in event["dataTransfer"].get("files", [])
             if f.get("pywebviewFullPath")]
    if paths:
        folder = (win.evaluate_js('document.getElementById("folder").value') or "").strip()
        server.add_to_outbox(paths, folder or None)
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
