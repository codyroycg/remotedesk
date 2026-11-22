#!/usr/bin/env python3
"""RemoteDesk host agent.

Runs on the computer being supported. Connects out to the relay server,
streams the screen as JPEG frames and applies mouse/keyboard input sent by
the technician.

Attended mode (default): prints a 6-digit code, asks the local user for
consent before anyone can connect, and shows an on-screen banner (with chat
and an "End session" button) for the whole session.

Unattended mode (--unattended --key ...): registers in the technician's
machine list so it can be accessed without someone at the keyboard. Only
install this on machines you own or administer.
"""
import argparse
import asyncio
import getpass
import hashlib
import io
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mss
import websockets
from PIL import Image
from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
from pynput.mouse import Button, Controller as MouseController

HERE = Path(__file__).resolve().parent
UI = HERE / "ui.py"

# ---------------------------------------------------------------- capture
_tls = threading.local()
_capture_pool = ThreadPoolExecutor(max_workers=1)  # mss objects are per-thread


def _sct():
    if not hasattr(_tls, "sct"):
        _tls.sct = (getattr(mss, "MSS", None) or mss.mss)()
    return _tls.sct


def monitor_geometry(index):
    mons = _sct().monitors
    return mons[index if index < len(mons) else 1]


def grab(index, last_hash, quality, max_width):
    """Return (hash, jpeg_bytes_or_None, (w, h)). jpeg is None if unchanged."""
    mon = monitor_geometry(index)
    shot = _sct().grab(mon)
    digest = hashlib.blake2b(shot.raw, digest_size=16).digest()
    if digest == last_hash:
        return digest, None, shot.size
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if max_width and img.width > max_width:
        h = round(img.height * max_width / img.width)
        img = img.resize((max_width, h), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=False)
    return digest, buf.getvalue(), img.size


# ---------------------------------------------------------------- input
SPECIAL_KEYS = {
    "Enter": Key.enter, "Backspace": Key.backspace, "Tab": Key.tab, "Escape": Key.esc,
    "Shift": Key.shift, "Control": Key.ctrl, "Alt": Key.alt, "Meta": Key.cmd,
    "ArrowUp": Key.up, "ArrowDown": Key.down, "ArrowLeft": Key.left, "ArrowRight": Key.right,
    "Delete": Key.delete, "Home": Key.home, "End": Key.end,
    "PageUp": Key.page_up, "PageDown": Key.page_down, "CapsLock": Key.caps_lock,
    " ": Key.space,
}
for _name in ("insert", "menu", "num_lock", "print_screen", "scroll_lock", "pause"):
    if hasattr(Key, _name):
        SPECIAL_KEYS[{"insert": "Insert", "menu": "ContextMenu", "num_lock": "NumLock",
                      "print_screen": "PrintScreen", "scroll_lock": "ScrollLock",
                      "pause": "Pause"}[_name]] = getattr(Key, _name)
for _i in range(1, 21):
    if hasattr(Key, f"f{_i}"):
        SPECIAL_KEYS[f"F{_i}"] = getattr(Key, f"f{_i}")

BUTTONS = {0: Button.left, 1: Button.middle, 2: Button.right}


class InputInjector:
    def __init__(self, monitor_index):
        self.mouse = MouseController()
        self.kb = KeyboardController()
        self.monitor_index = monitor_index
        self.pressed = {}  # browser code/key -> pynput key
        self.buttons = set()

    def _xy(self, msg):
        mon = _capture_pool.submit(monitor_geometry, self.monitor_index).result()
        x = mon["left"] + float(msg.get("x", 0)) * (mon["width"] - 1)
        y = mon["top"] + float(msg.get("y", 0)) * (mon["height"] - 1)
        return int(round(x)), int(round(y))

    def handle_mouse(self, msg):
        action = msg.get("action")
        self.mouse.position = self._xy(msg)
        if action == "down":
            b = BUTTONS.get(msg.get("button", 0), Button.left)
            self.mouse.press(b)
            self.buttons.add(b)
        elif action == "up":
            b = BUTTONS.get(msg.get("button", 0), Button.left)
            self.mouse.release(b)
            self.buttons.discard(b)
        elif action == "wheel":
            self.mouse.scroll(int(msg.get("dx", 0)), int(msg.get("dy", 0)))

    def handle_key(self, msg):
        key = msg.get("key", "")
        ident = msg.get("code") or key
        if msg.get("action") == "down":
            k = SPECIAL_KEYS.get(key)
            if k is None and len(key) == 1:
                k = KeyCode.from_char(key)
            if k is None:
                return
            self.pressed[ident] = k
            self.kb.press(k)
        else:
            k = self.pressed.pop(ident, None) or SPECIAL_KEYS.get(key)
            if k is None and len(key) == 1:
                k = KeyCode.from_char(key)
            if k is not None:
                try:
                    self.kb.release(k)
                except Exception:
                    pass

    def release_all(self):
        for k in list(self.pressed.values()):
            try:
                self.kb.release(k)
            except Exception:
                pass
        self.pressed.clear()
        for b in list(self.buttons):
            try:
                self.mouse.release(b)
            except Exception:
                pass
        self.buttons.clear()


# ---------------------------------------------------------------- local UI
def gui_available():
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


async def ask_consent(tech, auto_accept):
    if auto_accept:
        print(f"[agent] auto-accepting connection from {tech}")
        return True
    if gui_available():
        proc = await asyncio.create_subprocess_exec(sys.executable, str(UI), "consent", tech)
        try:
            return (await asyncio.wait_for(proc.wait(), 60)) == 0
        except asyncio.TimeoutError:
            proc.kill()
            return False
    # Terminal fallback
    answer = await asyncio.to_thread(input, f"Technician '{tech}' wants to view and control this computer. Allow? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


class Banner:
    """On-screen 'session active' window with chat + End session button."""

    def __init__(self, tech, on_chat, on_end):
        self.proc = None
        self.tech, self.on_chat, self.on_end = tech, on_chat, on_end

    async def start(self):
        if not gui_available():
            print(f"[agent] *** {self.tech} is now connected to this computer. Press Ctrl+C to end. ***")
            return
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, str(UI), "banner", self.tech,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        asyncio.create_task(self._read())

    async def _read(self):
        while self.proc and (line := await self.proc.stdout.readline()):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("chat"):
                await self.on_chat(msg["chat"])
            if msg.get("end"):
                await self.on_end()
        if self.proc:  # window was closed
            await self.on_end()

    def chat(self, text):
        if self.proc and self.proc.stdin:
            self.proc.stdin.write((json.dumps({"chat": text}) + "\n").encode())
        else:
            print(f"[chat] {self.tech}: {text}")

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            p.terminate()


# ---------------------------------------------------------------- agent
class Agent:
    def __init__(self, args):
        self.args = args
        self.ws = None
        self.session = False
        self.ack = asyncio.Event()
        self.quality = args.quality
        self.injector = InputInjector(args.monitor)
        self.banner = None
        self.stream_task = None

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def run(self):
        hello = {
            "type": "hello",
            "name": self.args.name,
            "os": f"{platform.system()} {platform.release()}",
            "user": getpass.getuser(),
            "unattended": self.args.unattended,
        }
        if self.args.unattended:
            hello["id"] = machine_id()
            hello["key"] = self.args.key
        async with websockets.connect(self.args.server, max_size=2**24, ping_interval=20) as ws:
            self.ws = ws
            await self.send(hello)
            async for raw in ws:
                await self.on_message(json.loads(raw))

    async def on_message(self, msg):
        t = msg.get("type")
        if t == "code":
            print("\n" + "=" * 44)
            print(f"   Your support code:  {msg['code'][:3]} {msg['code'][3:]}")
            print("   Read this code to your technician.")
            print("=" * 44 + "\n", flush=True)
        elif t == "registered":
            print(f"[agent] registered for unattended access as '{self.args.name}' ({msg['id']})")
        elif t == "error":
            print(f"[agent] server error: {msg['error']}")
        elif t == "join_request":
            ok = await ask_consent(msg["tech"], self.args.auto_accept)
            await self.send({"type": "consent", "accepted": ok})
        elif t == "tech_joined":
            await self.start_session(msg["tech"])
        elif t == "tech_left":
            await self.stop_session(msg.get("reason", ""))
        elif not self.session:
            return
        elif t == "ack":
            self.ack.set()
        elif t == "mouse":
            await asyncio.to_thread(self.injector.handle_mouse, msg)
        elif t == "key":
            await asyncio.to_thread(self.injector.handle_key, msg)
        elif t == "settings":
            self.quality = max(10, min(95, int(msg.get("quality", self.quality))))
        elif t == "chat":
            if self.banner:
                self.banner.chat(str(msg.get("text", ""))[:2000])

    async def start_session(self, tech):
        print(f"[agent] session started with {tech}")
        self.session = True
        self.ack.set()
        self.banner = Banner(tech, self._chat_out, self._user_end)
        await self.banner.start()
        self.stream_task = asyncio.create_task(self.stream())

    async def stop_session(self, reason):
        if not self.session:
            return
        print(f"[agent] session ended ({reason})")
        self.session = False
        if self.stream_task:
            self.stream_task.cancel()
        if self.banner:
            self.banner.stop()
            self.banner = None
        await asyncio.to_thread(self.injector.release_all)
        if not self.args.unattended:
            # Attended codes are one-time; exit after the session like ScreenConnect's guest client.
            await self.ws.close()

    async def _chat_out(self, text):
        await self.send({"type": "chat", "text": text})

    async def _user_end(self):
        if self.session:
            await self.send({"type": "end"})

    async def stream(self):
        loop = asyncio.get_running_loop()
        last_hash, last_sent, last_size = None, 0.0, None
        min_interval = 1.0 / self.args.fps
        while self.session:
            try:
                await asyncio.wait_for(self.ack.wait(), 3)
            except asyncio.TimeoutError:
                pass  # tech didn't ack; send anyway so a lost ack can't stall us
            started = time.monotonic()
            force = started - last_sent > 2  # keepalive frame
            digest, jpeg, size = await loop.run_in_executor(
                _capture_pool, grab, self.args.monitor, None if force else last_hash,
                self.quality, self.args.max_width)
            last_hash = digest
            if jpeg is not None and self.session:
                if size != last_size:
                    await self.send({"type": "screen", "width": size[0], "height": size[1]})
                    last_size = size
                self.ack.clear()
                await self.ws.send(jpeg)
                last_sent = time.monotonic()
            await asyncio.sleep(max(0, min_interval - (time.monotonic() - started)))


def machine_id():
    path = Path.home() / ".remotedesk_id"
    if path.exists():
        return path.read_text().strip()
    mid = str(uuid.uuid4())
    path.write_text(mid)
    return mid


def main():
    p = argparse.ArgumentParser(description="RemoteDesk host agent")
    p.add_argument("--server", default="ws://localhost:8080/agent", help="relay URL, e.g. wss://support.example.com/agent")
    p.add_argument("--name", default=socket.gethostname())
    p.add_argument("--monitor", type=int, default=1, help="monitor index (1 = primary, 0 = all monitors)")
    p.add_argument("--fps", type=float, default=15)
    p.add_argument("--quality", type=int, default=60)
    p.add_argument("--max-width", type=int, default=1920)
    p.add_argument("--unattended", action="store_true", help="register for unattended access (requires --key)")
    p.add_argument("--key", default=os.environ.get("REMOTEDESK_AGENT_KEY", ""))
    p.add_argument("--auto-accept", action="store_true", help="skip the consent prompt (testing / your own machines)")
    args = p.parse_args()

    agent = Agent(args)
    while True:
        try:
            asyncio.run(agent.run())
        except KeyboardInterrupt:
            break
        except (OSError, websockets.WebSocketException) as e:
            print(f"[agent] connection error: {e}")
        if not args.unattended:
            break
        print("[agent] reconnecting in 5s…")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
