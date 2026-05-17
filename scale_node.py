"""Node 3 — Teaching Node.

4 sliders for manual jogging.  Every slider movement publishes a
`servo_cmd <a1> <a2> <a3> <a4>` message → the hardware_node sweeps to that
target, so you can position the arm in real time before recording a pose.

Also includes:
  • +/- 1 and +/- 10 nudge buttons per axis
  • Home button
  • Record button (appends current pose to selected CSV)
  • Clear-file button (with confirmation)
  • Live preview of the selected CSV
"""

import csv
import os
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk
import zmq

from utils.zmq_config import (
    PORT_SERVO_CMD, TOPIC_SERVO_CMD,
    HOME_POSE, SERVO_LIMITS, SERVO_PINS,
    DATA_DIR, RECORD_OPTIONS, ADDR_CONN,
)


class TeachingNode:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("Scale Node — Jog & Record")
        root.geometry("640x780")

        os.makedirs(DATA_DIR, exist_ok=True)

        self.ctx = zmq.Context.instance()
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.connect(ADDR_CONN.format(port=PORT_SERVO_CMD))

        self.scales = []
        self._build_ui()

    # ---------------- UI ----------------
    def _build_ui(self):
        ctk.CTkLabel(self.root, text="Servo Jog",
                     font=("Tahoma", 20, "bold")).pack(pady=4)

        for i in range(4):
            lo, hi = SERVO_LIMITS[i]
            f = ctk.CTkFrame(self.root, corner_radius=6)
            f.pack(fill="x", padx=8, pady=2)

            ctk.CTkLabel(f, text=f"S{i+1}  pin {SERVO_PINS[i]}  [{lo}–{hi}]",
                         font=("Tahoma", 16)).pack(side="left", padx=(6, 2))

            # Per-slider value label updated by the slider command
            val_lbl = ctk.CTkLabel(f, text=f"{HOME_POSE[i]}",
                                   width=36, font=("Consolas", 18, "bold"))
            val_lbl.pack(side="right", padx=(0, 6))

            sc = ctk.CTkSlider(f, from_=lo, to=hi,
                               number_of_steps=hi - lo,
                               width=220,
                               command=lambda v, lbl=val_lbl: (
                                   lbl.configure(text=f"{int(float(v))}"),
                                   self._publish()
                               ))
            sc.set(HOME_POSE[i])
            sc.pack(side="left", padx=4, pady=4)

            for txt, delta in [("-1", -1), ("+1", 1), ("-10", -10), ("+10", 10)]:
                ctk.CTkButton(f, text=txt, width=36,
                              font=("Tahoma", 16),
                              command=lambda i=i, d=delta: self._nudge(i, d)
                              ).pack(side="left", padx=1)

            self.scales.append(sc)

        # File select
        sel = ctk.CTkFrame(self.root, fg_color="transparent")
        sel.pack(pady=5)
        ctk.CTkLabel(sel, text="File: record-", font=("Tahoma", 18)).grid(row=0, column=0)
        self.file_var = tk.StringVar(value=RECORD_OPTIONS[0])
        # Keep ttk.Combobox for clean width control; file_var trace drives preview
        ttk.Combobox(sel, textvariable=self.file_var,
                     values=RECORD_OPTIONS, width=5,
                     state="readonly").grid(row=0, column=1)
        ctk.CTkLabel(sel, text=".csv", font=("Tahoma", 18)).grid(row=0, column=2)
        self.file_var.trace_add("write", lambda *a: self._load_preview())

        # Action buttons
        btns = ctk.CTkFrame(self.root, fg_color="transparent")
        btns.pack(pady=4)
        ctk.CTkButton(btns, text="Home", width=90,
                      font=("Tahoma", 18, "bold"),
                      command=self._home).grid(row=0, column=0, padx=3)
        ctk.CTkButton(btns, text="Record Pose", width=110,
                      font=("Tahoma", 18, "bold"),
                      command=self._record).grid(row=0, column=1, padx=3)
        ctk.CTkButton(btns, text="Clear File", width=90,
                      font=("Tahoma", 18, "bold"),
                      command=self._clear).grid(row=0, column=2, padx=3)

        # status StringVar — trace so _publish(), _record(), _clear() .set() works unchanged
        self.status = tk.StringVar(value="Idle")
        self.status_lbl = ctk.CTkLabel(self.root, text=self.status.get(),
                                       font=("Tahoma", 18))
        self.status_lbl.pack(pady=2)
        self.status.trace_add("write",
                              lambda *_: self.status_lbl.configure(
                                  text=self.status.get()))

        # Preview (CTkTextbox replaces tk.Text)
        ctk.CTkLabel(self.root, text="File preview:",
                     font=("Tahoma", 18, "bold")).pack(anchor="w", padx=10)
        self.preview = ctk.CTkTextbox(self.root, height=120, width=440,
                                      font=("Consolas", 16),
                                      state="disabled")
        self.preview.pack(pady=3, padx=8)

        info = f"PUB :{PORT_SERVO_CMD} ({TOPIC_SERVO_CMD})"
        ctk.CTkLabel(self.root, text=info,
                     font=("Consolas", 14)).pack(pady=2)

        self._load_preview()

    # ---------------- Helpers ----------------
    def _nudge(self, i, delta):
        self.scales[i].set(self.scales[i].get() + delta)
        # CTkSlider fires command on .set(), so _publish is triggered automatically.

    def _publish(self):
        vals = [self.scales[i].get() for i in range(4)]
        msg = f"{TOPIC_SERVO_CMD} {vals[0]} {vals[1]} {vals[2]} {vals[3]}"
        try:
            self.pub.send_string(msg)
        except Exception as e:
            print(f"[teach] pub err: {e}")
        self.status.set(f"Sent → {vals}")

    def _home(self):
        for i in range(4):
            self.scales[i].set(HOME_POSE[i])
        self._publish()

    def _path(self):
        return os.path.join(DATA_DIR, f"record-{self.file_var.get()}.csv")

    def _record(self):
        vals = [self.scales[i].get() for i in range(4)]
        path = self._path()
        new = not os.path.isfile(path)
        with open(path, 'a', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(["Servo1", "Servo2", "Servo3", "Servo4"])
            w.writerow(vals)
        self.status.set(f"Recorded {vals} → {os.path.basename(path)}")
        self._load_preview()

    def _clear(self):
        path = self._path()
        if not messagebox.askyesno("Confirm",
                                   f"Clear {os.path.basename(path)}?\n"
                                   "(file will be reset to header only)"):
            return
        with open(path, 'w', newline='') as f:
            csv.writer(f).writerow(["Servo1", "Servo2", "Servo3", "Servo4"])
        self.status.set(f"Cleared {os.path.basename(path)}")
        self._load_preview()

    def _load_preview(self):
        path = self._path()
        self.preview.configure(state="normal")
        self.preview.delete(1.0, "end")
        if os.path.isfile(path):
            with open(path) as f:
                self.preview.insert("end", f.read())
        else:
            self.preview.insert("end",
                "(file not found — will be created on Record)")
        self.preview.configure(state="disabled")

    # ---------------- Lifecycle ----------------
    def shutdown(self):
        try: self.pub.close(0)
        except Exception: pass


def main():
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    node = TeachingNode(root)

    def on_close():
        node.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
