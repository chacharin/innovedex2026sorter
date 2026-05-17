"""Node 5 — Main Decision (Brain).

Integrates the CSV Player as a service thread.  State machine:

    IDLE
      |- (button N pressed) -->
    PLAYING_MAIN  (publishes each pose in record-N.csv with tunable delay)
      -->
    WAITING_IDLE  (waits BUSY -> IDLE on servo_status)
      -->
    CAPTURING     (publishes vision_cmd capture)
      -->
    WAITING_COLOR (waits color_result, with timeout)
      -->
    PLAYING_BRANCH (record-N{1|2|3|4}.csv based on color)
      -->
    WAITING_IDLE
      -->
    DONE -> IDLE

ALL RUN button:
    Loops button 1 -> 5 in order. When a button cannot determine a color
    (vision timeout or unknown class), it is SKIPPED safely (arm goes home
    + waits IDLE) and the loop continues with the next button. After the
    last button, the arm returns to HOME pose.

Emergency STOP -> publishes "servo_cmd stop" (hardware overrides any sweep
and forces home), aborts any running sequence (single or ALL RUN).
"""

import csv
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
import zmq

from utils.zmq_config import (
    PORT_SERVO_CMD, PORT_SERVO_STATUS,
    PORT_VISION_CMD, PORT_VISION_RESULT,
    TOPIC_SERVO_CMD, TOPIC_SERVO_STATUS,
    TOPIC_VISION_CMD, TOPIC_VISION_RESULT,
    STATUS_IDLE, STATUS_BUSY,
    CMD_CAPTURE, CMD_STOP, CMD_HOME,
    COLORS, DATA_DIR, ADDR_CONN,
)


# Per-button result codes used by _run_one
RES_DONE    = "DONE"
RES_SKIPPED = "SKIPPED"
RES_ABORTED = "ABORTED"
RES_ERROR   = "ERROR"


# Branching map: button N -> main file, then color -> branch file
BRANCH = {
    n: {
        "main":   f"record-{n}.csv",
        "red":    f"record-{n}1.csv",
        "blue":   f"record-{n}2.csv",
        "green":  f"record-{n}3.csv",
        "yellow": f"record-{n}4.csv",
    }
    for n in range(1, 6)
}


class Brain:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("Main Decision — The Sorter")
        root.geometry("780x820")


        # ZMQ ----------------------------------------------------------
        self.ctx = zmq.Context.instance()

        self.cmd_pub = self.ctx.socket(zmq.PUB)
        self.cmd_pub.connect(ADDR_CONN.format(port=PORT_SERVO_CMD))

        self.status_sub = self.ctx.socket(zmq.SUB)
        self.status_sub.connect(ADDR_CONN.format(port=PORT_SERVO_STATUS))
        self.status_sub.setsockopt_string(zmq.SUBSCRIBE, TOPIC_SERVO_STATUS)
        self.status_sub.RCVTIMEO = 100

        self.vision_pub = self.ctx.socket(zmq.PUB)
        self.vision_pub.connect(ADDR_CONN.format(port=PORT_VISION_CMD))

        self.vision_sub = self.ctx.socket(zmq.SUB)
        self.vision_sub.connect(ADDR_CONN.format(port=PORT_VISION_RESULT))
        self.vision_sub.setsockopt_string(zmq.SUBSCRIBE, TOPIC_VISION_RESULT)
        self.vision_sub.RCVTIMEO = 100

        # State --------------------------------------------------------
        self.servo_status = STATUS_IDLE
        self.last_color   = None
        self.color_event  = threading.Event()
        self.running      = True
        self.busy         = False
        self.abort_flag   = threading.Event()

        self._build_ui()

        threading.Thread(target=self._status_loop, daemon=True).start()
        threading.Thread(target=self._vision_loop, daemon=True).start()

        self.root.after(400, lambda: self._log("Brain online. Waiting for commands."))

    # ===================================================================
    # UI
    # ===================================================================
    def _build_ui(self):
        ctk.CTkLabel(self.root, text="THE SORTER  —  Main Decision",
                     font=("Tahoma", 22, "bold")).pack(pady=4)

        # Tunables ------------------------------------------------------
        ctrl = ctk.CTkFrame(self.root, corner_radius=6)
        ctrl.pack(fill="x", padx=12)

        ctk.CTkLabel(ctrl, text="Timing", font=("Tahoma", 16, "bold")).grid(
            row=0, column=0, columnspan=6, sticky="w", padx=6, pady=(4, 0))

        ctk.CTkLabel(ctrl, text="Pose delay (s):",
                     font=("Tahoma", 16)).grid(row=1, column=0, padx=2, sticky="e")
        self.delay_var = tk.DoubleVar(value=2.0)
        ctk.CTkEntry(ctrl, textvariable=self.delay_var, width=50,
                     font=("Tahoma", 16)).grid(row=1, column=1)

        ctk.CTkLabel(ctrl, text="IDLE timeout (s):",
                     font=("Tahoma", 16)).grid(row=1, column=2, padx=4, sticky="e")
        self.idle_to = tk.DoubleVar(value=20.0)
        ctk.CTkEntry(ctrl, textvariable=self.idle_to, width=50,
                     font=("Tahoma", 16)).grid(row=1, column=3)

        ctk.CTkLabel(ctrl, text="Vision timeout (s):",
                     font=("Tahoma", 16)).grid(row=1, column=4, padx=4, sticky="e")
        self.vis_to = tk.DoubleVar(value=5.0)
        ctk.CTkEntry(ctrl, textvariable=self.vis_to, width=50,
                     font=("Tahoma", 16)).grid(row=1, column=5, pady=(0, 6))

        # 5 main buttons
        btn_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        btn_frame.pack(pady=6)
        self.buttons = []
        for n in range(1, 6):
            b = ctk.CTkButton(btn_frame, text=f"Route {n}",
                              width=90, height=50,
                              font=("Tahoma", 18, "bold"),
                              command=lambda n=n: self._start_sequence(n))
            b.grid(row=0, column=n-1, padx=3)
            self.buttons.append(b)

        # ALL RUN button ------------------------------------------------
        self.all_run_btn = ctk.CTkButton(
            self.root,
            text=">>  ALL RUN  (1→5, skip if no color, home at end)  >>",
            font=("Tahoma", 20, "bold"), height=32,
            command=self._start_all_run)
        self.all_run_btn.pack(fill="x", padx=12, pady=(4, 2))

        # STOP button ---------------------------------------------------
        self.stop_btn = ctk.CTkButton(self.root,
                                      text="!!!  EMERGENCY STOP  !!!",
                                      fg_color="#d63031", text_color="white",
                                      hover_color="#a82020",
                                      font=("Tahoma", 24, "bold"), height=32,
                                      command=self._emergency_stop)
        self.stop_btn.pack(fill="x", padx=12, pady=4)

        # Status panel --------------------------------------------------
        stat = ctk.CTkFrame(self.root, corner_radius=6)
        stat.pack(fill="x", padx=12)

        ctk.CTkLabel(stat, text="Status", font=("Tahoma", 16, "bold")).pack(anchor="w", padx=6, pady=(4, 0))

        # StringVars with trace so _status_loop/_vision_loop .set() calls work unchanged
        self.state_var = tk.StringVar(value="Idle, ready")
        self.lbl_state = ctk.CTkLabel(stat, text=self.state_var.get(),
                                      font=("Tahoma", 18, "bold"))
        self.lbl_state.pack(anchor="w", padx=6)
        self.state_var.trace_add("write",
                                 lambda *_: self.lbl_state.configure(
                                     text=self.state_var.get()))

        self.servo_var = tk.StringVar(value="Servo: IDLE")
        self.lbl_servo = ctk.CTkLabel(stat, text=self.servo_var.get(),
                                      font=("Tahoma", 18))
        self.lbl_servo.pack(anchor="w", padx=6)
        self.servo_var.trace_add("write",
                                 lambda *_: self.lbl_servo.configure(
                                     text=self.servo_var.get()))

        self.color_var = tk.StringVar(value="Color: --")
        self.lbl_color = ctk.CTkLabel(stat, text=self.color_var.get(),
                                      font=("Tahoma", 18))
        self.lbl_color.pack(anchor="w", padx=6, pady=(0, 4))
        self.color_var.trace_add("write",
                                 lambda *_: self.lbl_color.configure(
                                     text=self.color_var.get()))

        # Log -----------------------------------------------------------
        ctk.CTkLabel(self.root, text="Event log:",
                     font=("Tahoma", 18, "bold")).pack(anchor="w", padx=12, pady=(4, 0))
        self.log = ctk.CTkTextbox(self.root, height=160,
                                  font=("Consolas", 16))
        self.log.pack(fill="both", expand=True, padx=12, pady=3)

    # ===================================================================
    # ZMQ listeners
    # ===================================================================
    def _status_loop(self):
        while self.running:
            try:
                msg = self.status_sub.recv_string()
            except zmq.Again:
                continue
            except Exception:
                break
            parts = msg.split()
            if len(parts) >= 2:
                self.servo_status = parts[1]
                self.root.after(0, lambda s=self.servo_status:
                                self.servo_var.set(f"Servo: {s}"))

    def _vision_loop(self):
        while self.running:
            try:
                msg = self.vision_sub.recv_string()
            except zmq.Again:
                continue
            except Exception:
                break
            parts = msg.split()
            if len(parts) >= 2:
                self.last_color = parts[1].lower()
                self.root.after(0, lambda c=self.last_color:
                                self.color_var.set(f"Color: {c}"))
                self.color_event.set()

    # ===================================================================
    # Helpers
    # ===================================================================
    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        print(line, end="")
        def _append():
            self.log.insert("end", line)
            self.log.see("end")
        self.root.after(0, _append)

    def _set_state(self, s: str):
        self.root.after(0, lambda: self.state_var.set(s))

    def _set_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for b in self.buttons:
            self.root.after(0, lambda b=b, s=state: b.configure(state=s))
        if hasattr(self, "all_run_btn"):
            self.root.after(0, lambda s=state: self.all_run_btn.configure(state=s))

    # ===================================================================
    # Actions
    # ===================================================================
    def _emergency_stop(self):
        self.abort_flag.set()
        self._log("EMERGENCY STOP pressed - sending stop + homing")
        try:
            self.cmd_pub.send_string(f"{TOPIC_SERVO_CMD} {CMD_STOP}")
        except Exception as e:
            self._log(f"stop pub error: {e}")
        self._set_state("STOPPED - homing")

    def _start_sequence(self, n: int):
        if self.busy:
            messagebox.showwarning("Busy",
                "A sequence is already running. Press STOP to abort first.")
            return
        def _wrap():
            self.busy = True
            self.abort_flag.clear()
            self._set_buttons(False)
            try:
                self._run_one(n, allow_skip=False)
            finally:
                self.busy = False
                self._set_buttons(True)
        threading.Thread(target=_wrap, daemon=True).start()

    def _start_all_run(self):
        if self.busy:
            messagebox.showwarning("Busy",
                "A sequence is already running. Press STOP to abort first.")
            return
        threading.Thread(target=self._run_all_sequence, daemon=True).start()

    def _run_all_sequence(self):
        self.busy = True
        self.abort_flag.clear()
        self._set_buttons(False)
        results = {}
        try:
            self._log("====== ALL RUN started: button 1 -> 5 ======")
            for n in range(1, 6):
                if self.abort_flag.is_set():
                    self._log("ALL RUN aborted by STOP")
                    break
                self._set_state(f"ALL RUN  ->  button {n}/5")
                self._log(f"------ ALL RUN: starting button {n} ------")
                res = self._run_one(n, allow_skip=True)
                results[n] = res
                self._log(f"   button {n} result = {res}")
                if res == RES_ABORTED:
                    self._log("ALL RUN aborted")
                    break
                if res == RES_ERROR:
                    self._log(f"button {n} ERROR - stopping ALL RUN")
                    break
                # RES_DONE or RES_SKIPPED -> continue to next button

            # Final homing - only if not aborted by STOP (STOP already homes)
            if not self.abort_flag.is_set():
                self._log("ALL RUN finished - going home")
                self._set_state("ALL RUN - homing")
                try:
                    self.cmd_pub.send_string(f"{TOPIC_SERVO_CMD} {CMD_HOME}")
                except Exception as e:
                    self._log(f"home pub err: {e}")
                self._wait_idle()

            summary = "  |  ".join(f"#{n}:{results.get(n, '--')}" for n in range(1, 6))
            self._log(f"====== ALL RUN DONE  [{summary}] ======")
            self._set_state(f"ALL RUN DONE  [{summary}]")
        except Exception as e:
            self._log(f"Exception in ALL RUN: {e}")
            self._set_state(f"ERROR - {e}")
        finally:
            self.busy = False
            self._set_buttons(True)

    def _run_one(self, n: int, allow_skip: bool = False) -> str:
        """Run one button's sequence.

        Returns one of RES_DONE / RES_SKIPPED / RES_ABORTED / RES_ERROR.

        If allow_skip is True, vision-timeout or unknown-color -> SKIPPED
        (and the arm is sent home + waits IDLE before returning, so the
        next sequence starts from a known-safe pose).
        """
        self.color_event.clear()
        self.last_color = None
        try:
            files = BRANCH[n]
            main_file = os.path.join(DATA_DIR, files["main"])

            self._set_state(f"PLAYING_MAIN  ({files['main']})")
            self._log(f"Button {n}: playing {files['main']}")
            if not self._play_csv(main_file):
                return RES_ABORTED if self.abort_flag.is_set() else RES_ERROR

            self._set_state("WAITING_IDLE")
            if not self._wait_idle():
                return RES_ABORTED if self.abort_flag.is_set() else RES_ERROR

            self._set_state("CAPTURING vision")
            self._log("Requesting vision capture ...")
            self.color_event.clear()
            self.last_color = None
            try:
                self.vision_pub.send_string(f"{TOPIC_VISION_CMD} {CMD_CAPTURE}")
            except Exception as e:
                self._log(f"vision pub error: {e}")
                self._set_state("ERROR - vision pub")
                return RES_ERROR

            self._set_state("WAITING_COLOR")
            got = self.color_event.wait(timeout=float(self.vis_to.get()))
            if self.abort_flag.is_set():
                return RES_ABORTED
            color = (self.last_color or "").lower() if got else ""

            if not got:
                self._log(f"Vision timeout (button {n})" + (" - skipping" if allow_skip else ""))
                if allow_skip:
                    return self._safe_skip()
                self._set_state("ERROR - vision timeout")
                return RES_ERROR

            self._log(f"   detected color = {color or '(none)'}")
            if color not in COLORS:
                self._log(f"No usable color (got '{color}')" + (" - skipping" if allow_skip else ""))
                if allow_skip:
                    return self._safe_skip()
                self._set_state("ERROR - unknown color")
                return RES_ERROR

            branch_name = files[color]
            branch_file = os.path.join(DATA_DIR, branch_name)
            self._set_state(f"PLAYING_BRANCH  ({branch_name})")
            self._log(f"Branch by '{color}' -> {branch_name}")
            if not self._play_csv(branch_file):
                return RES_ABORTED if self.abort_flag.is_set() else RES_ERROR

            self._set_state("WAITING_IDLE")
            if not self._wait_idle():
                return RES_ABORTED if self.abort_flag.is_set() else RES_ERROR

            self._set_state(f"DONE button {n}, color {color}")
            self._log(f"Button {n} complete (color {color})")
            return RES_DONE
        except Exception as e:
            self._log(f"Exception in button {n}: {e}")
            self._set_state(f"ERROR - {e}")
            return RES_ERROR

    def _safe_skip(self) -> str:
        """Send arm home and wait IDLE before returning SKIPPED, so the next
        button starts from a known pose instead of mid-air."""
        self._set_state("SKIPPING - homing before next")
        self._log("   going home before next button")
        try:
            self.cmd_pub.send_string(f"{TOPIC_SERVO_CMD} {CMD_HOME}")
        except Exception as e:
            self._log(f"home pub err: {e}")
        self._wait_idle()
        if self.abort_flag.is_set():
            return RES_ABORTED
        return RES_SKIPPED

    def _play_csv(self, path: str) -> bool:
        if not os.path.isfile(path):
            self._log(f"Missing file: {path}")
            self._set_state(f"ERROR - missing {os.path.basename(path)}")
            return False
        delay = float(self.delay_var.get())
        with open(path) as f:
            reader = csv.reader(f)
            try:
                next(reader)  # header
            except StopIteration:
                self._log(f"Empty file: {os.path.basename(path)}")
                return True
            for row in reader:
                if self.abort_flag.is_set():
                    self._log("Aborted during playback")
                    return False
                if len(row) != 4:
                    continue
                msg = f"{TOPIC_SERVO_CMD} {row[0]} {row[1]} {row[2]} {row[3]}"
                try:
                    self.cmd_pub.send_string(msg)
                except Exception as e:
                    self._log(f"pub err: {e}")
                self._log(f"   pose -> [{row[0]}, {row[1]}, {row[2]}, {row[3]}]")
                if self.abort_flag.wait(timeout=delay):
                    self._log("Aborted between poses")
                    return False
        return True

    def _wait_idle(self) -> bool:
        timeout = float(self.idle_to.get())
        busy_deadline = time.time() + 1.5
        while time.time() < busy_deadline:
            if self.abort_flag.is_set():
                return False
            if self.servo_status == STATUS_BUSY:
                break
            time.sleep(0.05)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.abort_flag.is_set():
                return False
            if self.servo_status == STATUS_IDLE:
                return True
            time.sleep(0.05)
        self._log("IDLE timeout")
        self._set_state("ERROR - idle timeout")
        return False

    def shutdown(self):
        self.running = False
        for s in (self.cmd_pub, self.status_sub,
                  self.vision_pub, self.vision_sub):
            try: s.close(0)
            except Exception: pass


def main():
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    brain = Brain(root)

    def on_close():
        brain.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
