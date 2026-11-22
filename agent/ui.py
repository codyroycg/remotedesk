"""Small Tk windows for the agent, run as subprocesses so they own their main thread.

  ui.py consent <tech>   -> exit 0 if the user clicks Allow, 1 otherwise
  ui.py banner  <tech>   -> always-on-top session window with chat.
                            stdin:  {"chat": "..."} lines from the technician
                            stdout: {"chat": "..."} / {"end": true} lines
"""
import json
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk


def consent(tech):
    root = tk.Tk()
    root.title("Remote support request")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    result = {"ok": False}

    frm = ttk.Frame(root, padding=20)
    frm.pack()
    ttk.Label(frm, text="Remote support request", font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
    ttk.Label(frm, text=f"{tech} wants to view and control this computer.\n"
                        "Only allow this if you are expecting their help.",
              justify="left").pack(anchor="w", pady=(8, 16))
    countdown = ttk.Label(frm, text="")
    countdown.pack(anchor="w", pady=(0, 10))
    btns = ttk.Frame(frm)
    btns.pack(anchor="e")

    def allow():
        result["ok"] = True
        root.destroy()

    ttk.Button(btns, text="Deny", command=root.destroy).pack(side="left", padx=4)
    ttk.Button(btns, text="Allow", command=allow).pack(side="left")
    root.protocol("WM_DELETE_WINDOW", root.destroy)

    remaining = [55]

    def tick():
        remaining[0] -= 1
        if remaining[0] <= 0:
            root.destroy()
            return
        countdown.config(text=f"Request will be denied automatically in {remaining[0]}s")
        root.after(1000, tick)

    tick()
    root.mainloop()
    sys.exit(0 if result["ok"] else 1)


def banner(tech):
    root = tk.Tk()
    root.title("Remote session active")
    root.attributes("-topmost", True)
    root.geometry("320x260-20+20")

    top = tk.Frame(root, bg="#dc2626")
    top.pack(fill="x")
    tk.Label(top, text=f"● {tech} is controlling this computer", fg="white", bg="#dc2626",
             font=("TkDefaultFont", 10, "bold"), padx=8, pady=6).pack(side="left")

    log = tk.Text(root, height=8, wrap="word", state="disabled")
    log.pack(fill="both", expand=True, padx=6, pady=6)

    entry_row = ttk.Frame(root)
    entry_row.pack(fill="x", padx=6)
    entry = ttk.Entry(entry_row)
    entry.pack(side="left", fill="x", expand=True)

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def append(who, text):
        log.config(state="normal")
        log.insert("end", f"{who}: {text}\n")
        log.see("end")
        log.config(state="disabled")

    def send(_=None):
        text = entry.get().strip()
        if text:
            emit({"chat": text})
            append("You", text)
            entry.delete(0, "end")

    entry.bind("<Return>", send)
    ttk.Button(entry_row, text="Send", command=send).pack(side="left", padx=(4, 0))

    def end():
        emit({"end": True})
        root.destroy()

    ttk.Button(root, text="End session", command=end).pack(fill="x", padx=6, pady=6)
    root.protocol("WM_DELETE_WINDOW", end)

    incoming = queue.Queue()

    def reader():
        for line in sys.stdin:
            try:
                incoming.put(json.loads(line))
            except ValueError:
                pass
        incoming.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def poll():
        try:
            while True:
                msg = incoming.get_nowait()
                if msg is None:
                    root.destroy()
                    return
                if msg.get("chat"):
                    append(tech, msg["chat"])
                    root.deiconify()
                    root.lift()
        except queue.Empty:
            pass
        root.after(150, poll)

    poll()
    root.mainloop()


if __name__ == "__main__":
    mode, tech = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "Technician")
    {"consent": consent, "banner": banner}[mode](tech)
