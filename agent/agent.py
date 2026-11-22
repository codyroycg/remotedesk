#!/usr/bin/env python3
"""RemoteDesk support app (host agent).

Runs on the computer being supported. Connects out to the relay server,
streams the screen as JPEG frames and applies mouse/keyboard input sent by
the technician.

Attended mode (default): opens a small window showing a 6-digit code, asks
the user to Allow/Deny when the technician connects, and shows a "being
controlled" panel with chat and an End session button. Falls back to the
terminal when no GUI is available (or with --cli).

Unattended mode (--unattended --key ...): headless; registers in the
technician's machine list. Only install this on machines you administer.

The server address is found automatically, in this order:
  --server flag, address embedded in the downloaded app, remotedesk.json
  next to the app, REMOTEDESK_SERVER env var, or asked in the window.
"""
import argparse
import asyncio
import getpass
import hashlib
import io
import json
import os
import platform
import queue
import socket
import struct
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mss
import websockets
from PIL import Image

try:
    from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
    from pynput.mouse import Button, Controller as MouseController
except ImportError as e:  # e.g. no X display on Linux, or Wayland
    sys.exit(f"Cannot control the keyboard/mouse on this system: {e}")

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable if FROZEN else __file__).resolve().parent
TRAILER_MAGIC = b"RDCFG001"
CONSENT_TIMEOUT = 55

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




# ---------------------------------------------------------------- config
def normalize_server(addr):
    """Turn 'support.example.com', 'http://1.2.3.4:8080' etc. into a ws(s)://…/agent URL."""
    addr = addr.strip()
    if not addr:
        return ""
    if addr.startswith("https://"):
        addr = "wss://" + addr[8:]
    elif addr.startswith("http://"):
        addr = "ws://" + addr[7:]
    elif not addr.startswith(("ws://", "wss://")):
        host = addr.split("/")[0]
        hostname, _, port = host.partition(":")
        local = hostname == "localhost" or hostname.replace(".", "").isdigit()
        addr = ("ws://" if (local or (port and port not in ("443", "8443"))) else "wss://") + addr
    addr = addr.rstrip("/")
    if not addr.endswith("/agent"):
        addr += "/agent"
    return addr


def embedded_config():
    """Config appended to the executable by the server's /download endpoint."""
    if not FROZEN:
        return {}
    try:
        with open(sys.executable, "rb") as f:
            f.seek(-12, os.SEEK_END)
            tail = f.read(12)
            if tail[4:] != TRAILER_MAGIC:
                return {}
            (length,) = struct.unpack("<I", tail[:4])
            f.seek(-12 - length, os.SEEK_END)
            return json.loads(f.read(length))
    except (OSError, ValueError):
        return {}


def file_config():
    path = HERE / "remotedesk.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def resolve_server(cli_value):
    for candidate in (cli_value, embedded_config().get("server"), file_config().get("server"),
                      os.environ.get("REMOTEDESK_SERVER")):
        if candidate:
            return normalize_server(candidate)
    return ""


# ---------------------------------------------------------------- agent core
class Agent:
    """Network + capture + input. Talks to a UI object (TkUI or CliUI)."""

    def __init__(self, args, ui):
        self.args = args
        self.ui = ui
        self.ws = None
        self.session = False
        self.ack = asyncio.Event()
        self.quality = args.quality
        self.injector = InputInjector(args.monitor)
        self.stream_task = None

    async def send(self, obj):
        if self.ws:
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
        self.ui.status("Connecting to the support server…")
        async with websockets.connect(self.args.server, max_size=2**24, ping_interval=20,
                                      open_timeout=15) as ws:
            self.ws = ws
            await self.send(hello)
            try:
                async for raw in ws:
                    await self.on_message(json.loads(raw))
            finally:
                self.ws = None
                if self.session:
                    await self.stop_session("connection lost")

    async def on_message(self, msg):
        t = msg.get("type")
        if t == "code":
            self.ui.show_code(msg["code"])
        elif t == "registered":
            self.ui.status(f"Registered for unattended access as '{self.args.name}'.")
        elif t == "error":
            self.ui.error(msg["error"])
        elif t == "join_request":
            if self.args.auto_accept:
                ok = True
            else:
                try:
                    ok = await asyncio.wait_for(self.ui.ask_consent(msg["tech"]), CONSENT_TIMEOUT + 2)
                except asyncio.TimeoutError:
                    ok = False
            await self.send({"type": "consent", "accepted": bool(ok)})
            if not ok:
                self.ui.consent_denied()
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
            self.ui.chat_in(str(msg.get("text", ""))[:2000])

    async def start_session(self, tech):
        self.session = True
        self.ack.set()
        self.ui.session_started(tech)
        self.stream_task = asyncio.create_task(self.stream())

    async def stop_session(self, reason):
        if not self.session:
            return
        self.session = False
        if self.stream_task:
            self.stream_task.cancel()
        await asyncio.to_thread(self.injector.release_all)
        self.ui.session_ended(reason)
        if not self.args.unattended and self.ws:
            await self.ws.close()  # attended codes are one-time; the app is done

    async def chat_out(self, text):
        await self.send({"type": "chat", "text": text})

    async def end_by_user(self):
        if self.session:
            await self.send({"type": "end"})

    async def close(self):
        if self.ws:
            await self.ws.close()

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
            if jpeg is not None and self.session and self.ws:
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


# ---------------------------------------------------------------- terminal UI
class CliUI:
    def status(self, text):
        print(f"[remotedesk] {text}", flush=True)

    def show_code(self, code):
        print("\n" + "=" * 44)
        print(f"   Your support code:  {code[:3]} {code[3:]}")
        print("   Read this code to your technician.")
        print("=" * 44 + "\n", flush=True)

    def error(self, text):
        print(f"[remotedesk] error: {text}", flush=True)

    async def ask_consent(self, tech):
        answer = await asyncio.to_thread(
            input, f"Technician '{tech}' wants to view and control this computer. Allow? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    def consent_denied(self):
        self.status("Request denied.")

    def session_started(self, tech):
        self.status(f"*** {tech} is now connected to this computer. Press Ctrl+C to end. ***")

    def session_ended(self, reason):
        self.status(f"Session ended ({reason}).")

    def chat_in(self, text):
        print(f"[chat] {text}", flush=True)


def run_cli(args):
    ui = CliUI()
    while True:
        try:
            asyncio.run(Agent(args, ui).run())
        except KeyboardInterrupt:
            break
        except (OSError, websockets.WebSocketException) as e:
            ui.error(f"connection problem: {e}")
        if not args.unattended:
            break
        ui.status("Reconnecting in 5s…")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            break


def friendly_reason(reason):
    return {
        "remote user ended the session": "You ended the session.",
        "technician left": "Your technician ended the session.",
        "technician disconnected": "Your technician disconnected.",
        "technician switched sessions": "Your technician ended the session.",
        "connection lost": "The connection was lost.",
    }.get(reason, "The session has ended.")


# ---------------------------------------------------------------- window UI
class TkUI:
    """Tk window on the main thread; the Agent runs on an asyncio loop in a worker thread.
    Agent -> UI calls are queued and applied by a Tk timer (Tk isn't thread-safe)."""

    RED = "#dc2626"

    def __init__(self, args):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.args = args
        self.q = queue.Queue()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.agent = None
        self.consent_future = None
        self.tech = "Your technician"

        self.root = tk.Tk()
        self.root.title("Remote Support")
        self.root.geometry("400x430")
        self.root.minsize(340, 380)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.body = ttk.Frame(self.root, padding=20)
        self.body.pack(fill="both", expand=True)
        self.root.after(100, self._drain)

    # ----- thread-safe entry points used by Agent
    def _post(self, fn, *a):
        self.q.put((fn, a))

    def status(self, text):         self._post(self._view_status, text)
    def show_code(self, code):      self._post(self._view_code, code)
    def error(self, text):          self._post(self._view_error, text)
    def consent_denied(self):       self._post(self._view_code_again)
    def session_started(self, tech): self._post(self._view_session, tech)
    def session_ended(self, reason): self._post(self._view_ended, reason)
    def chat_in(self, text):        self._post(self._chat_append, self.tech, text)

    async def ask_consent(self, tech):
        self.consent_future = self.loop.create_future()
        self._post(self._view_request, tech)
        return await self.consent_future

    def _drain(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                fn(*a)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ----- views
    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.root.attributes("-topmost", False)

    def _title(self, text, color=None):
        lbl = self.tk.Label(self.body, text=text, font=("TkDefaultFont", 15, "bold"), anchor="w",
                            fg=color or None, justify="left", wraplength=340)
        lbl.pack(fill="x", pady=(0, 8))
        return lbl

    def _text(self, text, **kw):
        lbl = self.ttk.Label(self.body, text=text, wraplength=340, justify="left", **kw)
        lbl.pack(fill="x", pady=(0, 8))
        return lbl

    def _view_setup(self, message=""):
        self._clear()
        self._title("Remote Support")
        self._text(message or "Enter the support address your technician gave you:")
        entry = self.ttk.Entry(self.body)
        entry.pack(fill="x", pady=(0, 10))
        entry.insert(0, self.args.server.replace("/agent", "").replace("wss://", "").replace("ws://", "") if self.args.server else "")
        entry.focus_set()

        def go(_=None):
            server = normalize_server(entry.get())
            if server:
                self.args.server = server
                self.connect()
        entry.bind("<Return>", go)
        self.ttk.Button(self.body, text="Connect", command=go).pack(anchor="e")

    def _view_status(self, text):
        if self.agent and self.agent.session:
            return
        self._clear()
        self._title("Remote Support")
        self._text(text)

    def _view_code(self, code):
        self.code = code
        self._clear()
        self._title("Your support code")
        self.tk.Label(self.body, text=f"{code[:3]} {code[3:]}", font=("Courier", 40, "bold")).pack(pady=18)
        self._text("Read this code to your technician.\n\nKeep this window open. You'll be asked "
                   "before anyone can see or control your screen.")
        self.ttk.Button(self.body, text="Cancel", command=self.on_close).pack(side="bottom", anchor="e")

    def _view_code_again(self):
        if getattr(self, "code", None):
            self._view_code(self.code)

    def _view_request(self, tech):
        self.tech = tech
        self._clear()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.bell()
        self._title("Allow remote control?")
        self._text(f"{tech} wants to view and control this computer.\n\n"
                   "Only allow this if you're expecting their help.")
        countdown = self._text("", foreground="gray")
        btns = self.ttk.Frame(self.body)
        btns.pack(side="bottom", fill="x")
        fut = self.consent_future

        def answer(ok):
            if fut and not fut.done():
                self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(ok))
            if not ok and getattr(self, "code", None):
                self._view_code(self.code)

        self.ttk.Button(btns, text="Allow", command=lambda: answer(True)).pack(side="right")
        self.ttk.Button(btns, text="Deny", command=lambda: answer(False)).pack(side="right", padx=6)

        remaining = [CONSENT_TIMEOUT]

        def tick():
            if fut.done() or not countdown.winfo_exists():
                return
            remaining[0] -= 1
            if remaining[0] <= 0:
                answer(False)
                return
            countdown.config(text=f"Denied automatically in {remaining[0]}s")
            self.root.after(1000, tick)
        tick()

    def _view_session(self, tech):
        self.tech = tech
        self._clear()
        self.root.title("● Remote session active")
        bar = self.tk.Label(self.body, text=f"● {tech} is controlling this computer", bg=self.RED,
                            fg="white", font=("TkDefaultFont", 11, "bold"), pady=8, wraplength=340)
        bar.pack(fill="x", pady=(0, 10))
        self.ttk.Button(self.body, text="End session", command=self._end_clicked).pack(side="bottom", fill="x", pady=(10, 0))
        row = self.ttk.Frame(self.body)
        row.pack(side="bottom", fill="x")
        entry = self.ttk.Entry(row)
        entry.pack(side="left", fill="x", expand=True)

        def send(_=None):
            text = entry.get().strip()
            if text:
                self._call(self.agent.chat_out(text))
                self._chat_append("You", text)
                entry.delete(0, "end")
        entry.bind("<Return>", send)
        self.ttk.Button(row, text="Send", command=send).pack(side="left", padx=(6, 0))
        self.chat_log = self.tk.Text(self.body, height=8, wrap="word", state="disabled", relief="solid", borderwidth=1)
        self.chat_log.pack(fill="both", expand=True, pady=(0, 8))
        self._chat_append("", "Chat with your technician here.")

    def _chat_append(self, who, text):
        log = getattr(self, "chat_log", None)
        if not log or not log.winfo_exists():
            return
        log.config(state="normal")
        log.insert("end", f"{who}: {text}\n" if who else f"{text}\n")
        log.see("end")
        log.config(state="disabled")
        if who and who != "You":
            self.root.deiconify()
            self.root.lift()

    def _end_clicked(self):
        self._call(self.agent.end_by_user())

    def _view_ended(self, reason):
        self._clear()
        self.root.title("Remote Support")
        self._title("Session ended")
        self._text(f"{friendly_reason(reason)}\n\nYour technician can no longer see or control "
                   "this computer. You can close this window.")
        self.ttk.Button(self.body, text="Close", command=self.root.destroy).pack(side="bottom", anchor="e")
        self.ended = True

    def _view_error(self, text):
        if getattr(self, "ended", False):
            return
        self._clear()
        self._title("Couldn't connect", color=self.RED)
        self._text(text)
        btns = self.ttk.Frame(self.body)
        btns.pack(side="bottom", fill="x")
        self.ttk.Button(btns, text="Close", command=self.root.destroy).pack(side="right")
        self.ttk.Button(btns, text="Try again", command=self.connect).pack(side="right", padx=6)
        self.ttk.Button(btns, text="Change address", command=self._view_setup).pack(side="left")
        self.failed = True

    # ----- lifecycle
    def connect(self):
        self.ended = False
        self.failed = False
        self.code = None
        self.agent = Agent(self.args, self)
        fut = self._call(self.agent.run())

        def done(f):
            exc = f.exception()
            if exc is not None:
                self.error(f"Couldn't reach the support server at {self.args.server}.\n\n({exc})")
            elif not self.ended and not self.failed:
                self.error("The support server closed the connection.")
        fut.add_done_callback(done)

    def on_close(self):
        if self.agent:
            try:
                self._call(self.agent.end_by_user()).result(2)
                self._call(self.agent.close()).result(2)
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        if self.args.server:
            self.connect()
        else:
            self._view_setup()
        self.root.mainloop()


# ---------------------------------------------------------------- main
def gui_available():
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


def main():
    p = argparse.ArgumentParser(description="RemoteDesk support app")
    p.add_argument("--server", help="support server, e.g. support.example.com or ws://192.168.1.5:8080")
    p.add_argument("--name", default=socket.gethostname())
    p.add_argument("--monitor", type=int, default=1, help="monitor index (1 = primary, 0 = all monitors)")
    p.add_argument("--fps", type=float, default=15)
    p.add_argument("--quality", type=int, default=60)
    p.add_argument("--max-width", type=int, default=1920)
    p.add_argument("--unattended", action="store_true", help="register for unattended access (requires --key)")
    p.add_argument("--key", default=os.environ.get("REMOTEDESK_AGENT_KEY", ""))
    p.add_argument("--auto-accept", action="store_true", help="skip the consent prompt (testing / your own machines)")
    p.add_argument("--cli", action="store_true", help="use the terminal instead of a window")
    args = p.parse_args()
    args.server = resolve_server(args.server)

    if args.unattended or args.cli or not gui_available():
        if not args.server:
            p.error("no server address: pass --server (e.g. --server support.example.com)")
        run_cli(args)
    else:
        TkUI(args).run()


if __name__ == "__main__":
    main()
