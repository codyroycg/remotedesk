#!/usr/bin/env python3
"""Build the support app into a single executable for THIS operating system.

    python build.py

The result goes to ../server/downloads/, where the server picks it up and
offers it on the customer page. Each download gets the server's address
embedded automatically, so one build works for any server.

PyInstaller can't cross-compile: build on Windows for the Windows app, and so
on (or use the GitHub Actions workflow in .github/workflows/build-agent.yml).
"""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "server" / "downloads"

system = platform.system()
name = {"Windows": "RemoteDesk-windows", "Darwin": "RemoteDesk-macos", "Linux": "RemoteDesk-linux"}[system]

# pynput picks its backend at runtime, so PyInstaller can't see it: list it explicitly.
backend = {"Windows": ["win32", "win32_vks"], "Darwin": ["darwin", "darwin_vks"], "Linux": ["xorg", "xorg_keysyms"]}[system]
short = backend[0]
hidden = [f"pynput.keyboard._{short}", f"pynput.mouse._{short}"] + [f"pynput._util.{m}" for m in backend]
extra = [arg for h in hidden for arg in ("--hidden-import", h)]
if system == "Linux":
    extra += ["--collect-submodules", "Xlib"]

subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt"), "pyinstaller"])
subprocess.check_call([
    sys.executable, "-m", "PyInstaller", str(HERE / "agent.py"),
    "--onefile", "--windowed", "--noconfirm", "--clean",
    "--name", name,
    "--collect-submodules", "pynput", *extra,
    "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build"), "--specpath", str(HERE / "build"),
])

built = HERE / "dist" / (name + (".exe" if system == "Windows" else ""))
OUT.mkdir(parents=True, exist_ok=True)
shutil.copy2(built, OUT / built.name)
print(f"\nBuilt {OUT / built.name}\nRestart the server (or just reload the customer page) to offer it.")
