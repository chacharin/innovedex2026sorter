"""Node 2 — Hardware Node.

Subscribes to `servo_cmd` (SUB binds on PORT_SERVO_CMD) so multiple publishers
(teaching_node, main.py) can connect in.

Publishes IDLE / BUSY on `servo_status` (PUB binds on PORT_SERVO_STATUS).

A single sweep worker continuously chases the most recent target so rapid
slider drags don't pile up — the latest command always wins.

Command formats received on servo_cmd:
    "servo_cmd <a1> <a2> <a3> <a4>"   -> sweep to those angles (clamped)
    "servo_cmd home"                  -> sweep to HOME_POSE
    "servo_cmd stop"                  -> emergency: force home, override abort
"""

import threading
import time
import tkinter as tk

import customtkinter as ctk
import zmq

try:
    import pyfirmata2
    _HAS_FIRMATA = True
except Exception:
    _HAS_FIRMATA = False

from utils.zmq_config import (
    PORT_SERVO_CMD, PORT_SERVO_STATUS,
    TOPIC_SERVO_CMD, TOPIC_SERVO_STATUS,
    STATUS_IDLE, STATUS_BUSY,
    CMD_HOME, CMD_STOP,
    HOME_POSE, SERVO_PINS, SERVO_LIMITS,
    SWEEP_STEP_DEG, SWEEP_TICK_SEC,
    ADDR_BIND,
)


class HardwareNode:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("Arduino Node — Servo Driver")
        root.geometry("480x620")

        root.resizable(False, False)

        # Robot state
        self.current = [float(a) for a in HOME_POSE]
        self.target  = [float(a) for a in HOME_POSE]
        self.target_lock = threading.Lock()
        self.last_status = None
        self.connected = False
        self.board = None
        self.servos = []
        self.running = True
        self.emergency = False  # set when STOP command received

        # ZMQ
        self.ctx = zmq.Context.instance()
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.bind(ADDR_BIND.format(port=PORT_SERVO_CMD))
        self.sub.setsockopt_string(zmq.SUBSCRIBE, TOPIC_SERVO_CMD)
        self.sub.RCVTIMEO = 100  # ms

        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.bind(ADDR_BIND.format(port=PORT_SERVO_STATUS))

        self._build_ui()
        self._connect_arduino()

        threading.Thread(target=self._zmq_loop,   daemon=True).start()
        threading.Thread(target=self._sweep_loop, daemon=True).start()
        # initial heartbeat so subscribers know we're up
        self.root.after(300, lambda: self._publish_status(STATUS_IDLE))

    # ---------------- UI ----------------
    def _build_ui(self):
        top = ctk.CTkFrame(self.root, fg_color="transparent")
        top.pack(fill="x", pady=5, padx=8)

        self.lbl_conn = ctk.CTkLabel(top, text="● Arduino: Disconnected",
                                     font=("Tahoma", 18, "bold"))
        self.lbl_conn.pack(side="left")

        self.lbl_status = ctk.CTkLabel(top, text="STATUS: —",
                                       font=("Tahoma", 18, "bold"))
        self.lbl_status.pack(side="right")

        self.bars = []
        self.lbls = []
        self.tgt_lbls = []
        for i in range(4):
            lo, hi = SERVO_LIMITS[i]
            f = ctk.CTkFrame(self.root, corner_radius=6)
            f.pack(fill="x", padx=8, pady=2)

            title_lbl = ctk.CTkLabel(f,
                                     text=f"S{i+1}  pin {SERVO_PINS[i]}  [{lo}–{hi}]",
                                     font=("Tahoma", 16))
            title_lbl.grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 0))

            bar = ctk.CTkProgressBar(f, width=220, height=10, corner_radius=4)
            bar.set(HOME_POSE[i] / 180.0)
            bar.grid(row=1, column=0, columnspan=4, padx=6, pady=2, sticky="ew")

            ctk.CTkLabel(f, text="cur:",
                         font=("Tahoma", 16)).grid(row=2, column=0, sticky="e")
            lbl = ctk.CTkLabel(f, text=f"{HOME_POSE[i]}°", width=40,
                               font=("Consolas", 18, "bold"))
            lbl.grid(row=2, column=1, sticky="w")

            ctk.CTkLabel(f, text="tgt:",
                         font=("Tahoma", 16)).grid(row=2, column=2, sticky="e", padx=(8, 0))
            tlbl = ctk.CTkLabel(f, text=f"{HOME_POSE[i]}°", width=40,
                                font=("Consolas", 18, "bold"))
            tlbl.grid(row=2, column=3, sticky="w", pady=(0, 4))

            self.bars.append(bar)
            self.lbls.append(lbl)
            self.tgt_lbls.append(tlbl)

        info = (f"SUB :{PORT_SERVO_CMD} ({TOPIC_SERVO_CMD})   "
                f"PUB :{PORT_SERVO_STATUS} ({TOPIC_SERVO_STATUS})")
        ctk.CTkLabel(self.root, text=info,
                     font=("Consolas", 14)).pack(pady=2)

        # log label — dynamic via configure
        self.log_lbl = ctk.CTkLabel(self.root, text="Initialising…",
                                    wraplength=360, justify="left",
                                    font=("Tahoma", 16))
        self.log_lbl.pack(pady=2, padx=8)

    # ---------------- Hardware ----------------
    def _connect_arduino(self):
        if not _HAS_FIRMATA:
            self._log("pyfirmata2 not installed → DRY-RUN mode")
            return
        try:
            port = pyfirmata2.Arduino.AUTODETECT
            self.board = pyfirmata2.Arduino(port)
            self.servos = [self.board.get_pin(f'd:{p}:s') for p in SERVO_PINS]
            for i, ang in enumerate(HOME_POSE):
                self.servos[i].write(ang)
            self.connected = True
            self.lbl_conn.configure(text="● Arduino: CONNECTED", text_color="#2ecc71")
            self._log("Arduino connected. Homed.")
        except Exception as e:
            self.lbl_conn.configure(text="● Arduino: FAILED", text_color="#d63031")
            self._log(f"Arduino error: {e}\nRunning in DRY-RUN mode (no hardware).")

    # ---------------- Helpers ----------------
    def _log(self, msg: str):
        self.log_lbl.configure(text=msg)
        print(f"[hardware] {msg}")

    def _publish_status(self, status: str):
        if status == self.last_status:
            return
        self.last_status = status
        try:
            self.pub.send_string(f"{TOPIC_SERVO_STATUS} {status}")
        except Exception:
            pass
        color = "#2ecc71" if status == STATUS_IDLE else "#f39c12"
        self.root.after(0, lambda: self.lbl_status.configure(
            text=f"STATUS: {status}", text_color=color))

    def _set_target(self, new_target):
        with self.target_lock:
            for i in range(4):
                lo, hi = SERVO_LIMITS[i]
                self.target[i] = max(lo, min(hi, float(new_target[i])))
        self.root.after(0, self._refresh_targets)

    def _refresh_ui(self):
        for i in range(4):
            self.bars[i].set(self.current[i] / 180.0)
            self.lbls[i].configure(text=f"{int(self.current[i])}°")

    def _refresh_targets(self):
        for i in range(4):
            self.tgt_lbls[i].configure(text=f"{int(self.target[i])}°")

    # ---------------- ZMQ loop ----------------
    def _zmq_loop(self):
        while self.running:
            try:
                msg = self.sub.recv_string()
            except zmq.Again:
                continue
            except Exception:
                break
            try:
                parts = msg.split()
                if len(parts) == 2 and parts[1] == CMD_HOME:
                    self.emergency = False
                    self._set_target(list(HOME_POSE))
                    self._log(f"cmd: HOME -> {HOME_POSE}")
                elif len(parts) == 2 and parts[1] == CMD_STOP:
                    self.emergency = True
                    self._set_target(list(HOME_POSE))
                    self._log("cmd: EMERGENCY STOP -> forcing home")
                elif len(parts) == 5:
                    new_t = [float(x) for x in parts[1:5]]
                    self._set_target(new_t)
                    self._log(f"cmd: target -> {new_t}")
                else:
                    self._log(f"ignored: {msg!r}")
            except Exception as e:
                self._log(f"bad cmd '{msg}': {e}")

    # ---------------- Sweep loop ----------------
    def _sweep_loop(self):
        step = SWEEP_STEP_DEG
        tick = SWEEP_TICK_SEC
        while self.running:
            with self.target_lock:
                tgt = list(self.target)

            moving = any(abs(self.current[i] - tgt[i]) > 0.5 for i in range(4))
            if moving:
                self._publish_status(STATUS_BUSY)
                for i in range(4):
                    diff = tgt[i] - self.current[i]
                    if abs(diff) <= step:
                        self.current[i] = tgt[i]
                    else:
                        self.current[i] += step if diff > 0 else -step
                    if self.connected:
                        try:
                            self.servos[i].write(self.current[i])
                        except Exception:
                            pass
                self.root.after(0, self._refresh_ui)
                time.sleep(tick)
            else:
                self._publish_status(STATUS_IDLE)
                time.sleep(0.05)

    # ---------------- Lifecycle ----------------
    def shutdown(self):
        self.running = False
        try: self.pub.close(0)
        except Exception: pass
        try: self.sub.close(0)
        except Exception: pass
        if self.board:
            try: self.board.exit()
            except Exception: pass


def main():
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    node = HardwareNode(root)

    def on_close():
        node.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
