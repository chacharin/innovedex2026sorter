

import csv
import os
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import customtkinter as ctk
import zmq


# =========================================================
# CONFIG
# =========================================================
DATA_DIR = "data"
PORT_SERVO_CMD = 5555
TOPIC_SERVO_CMD = "servo_cmd"
ADDR_CONN = "tcp://localhost:{}"


# =========================================================
# MAIN GUI
# =========================================================
class CsvPoseTester:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("CSV Pose Tester")
        self.root.geometry("640x640")


        self.running = False
        self.current_rows = []

        # ZMQ
        self.ctx = zmq.Context.instance()
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.connect(ADDR_CONN.format(PORT_SERVO_CMD))

        self._build_ui()
        self._load_csv_files()

    # =====================================================
    # UI
    # =====================================================
    def _build_ui(self):
        ctk.CTkLabel(self.root, text="CSV Pose Tester",
                     font=("Tahoma", 22, "bold")).pack(pady=4)

        # ==========================
        # TOP CONTROL
        # ==========================
        top = ctk.CTkFrame(self.root, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=3)

        ctk.CTkLabel(top, text="File:", font=("Tahoma", 18, "bold")).pack(side="left")

        self.file_var = tk.StringVar()
        # Keep ttk.Combobox — <<ComboboxSelected>> binding is needed for file selection
        self.file_combo = ttk.Combobox(
            top, textvariable=self.file_var, state="readonly", width=26)
        self.file_combo.pack(side="left", padx=4)
        self.file_combo.bind("<<ComboboxSelected>>", lambda e: self.load_selected_csv())

        ctk.CTkButton(top, text="Refresh", width=70,
                      command=self._load_csv_files,
                      font=("Tahoma", 16)).pack(side="left", padx=4)

        # ==========================
        # DELAY
        # ==========================
        delay_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        delay_frame.pack(fill="x", padx=8, pady=1)

        ctk.CTkLabel(delay_frame, text="Delay (s):", font=("Tahoma", 18)).pack(side="left")
        self.delay_var = tk.DoubleVar(value=1.0)
        # CTkEntry supports textvariable= for DoubleVar
        ctk.CTkEntry(delay_frame, textvariable=self.delay_var, width=60,
                     font=("Tahoma", 16)).pack(side="left", padx=4)

        # ==========================
        # TABLE — keep ttk.Treeview (no CTk equivalent)
        # ==========================
        table_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        table_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = ("index", "s1", "s2", "s3", "s4")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)

        self.tree.heading("index", text="#")
        self.tree.heading("s1", text="S1")
        self.tree.heading("s2", text="S2")
        self.tree.heading("s3", text="S3")
        self.tree.heading("s4", text="S4")

        self.tree.column("index", width=35, anchor="center")
        self.tree.column("s1", width=80, anchor="center")
        self.tree.column("s2", width=80, anchor="center")
        self.tree.column("s3", width=80, anchor="center")
        self.tree.column("s4", width=80, anchor="center")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ==========================
        # BUTTONS
        # ==========================
        btn_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        btn_frame.pack(pady=5)

        ctk.CTkButton(btn_frame, text="TEST SELECTED", width=120,
                      font=("Tahoma", 18, "bold"),
                      command=self.test_selected_pose).grid(row=0, column=0, padx=3)

        ctk.CTkButton(btn_frame, text="PLAY ALL", width=120,
                      font=("Tahoma", 18, "bold"),
                      command=self.play_all).grid(row=0, column=1, padx=3)

        ctk.CTkButton(btn_frame, text="STOP", width=120,
                      fg_color="#d63031", text_color="white",
                      hover_color="#a82020",
                      font=("Tahoma", 18, "bold"),
                      command=self.stop_playback).grid(row=0, column=2, padx=3)

        # ==========================
        # STATUS — StringVar with trace so send_pose/stop_playback/_play_worker .set() works unchanged
        # ==========================
        self.status_var = tk.StringVar(value="Ready")
        self.status_lbl = ctk.CTkLabel(self.root, text=self.status_var.get(),
                                       font=("Consolas", 18, "bold"))
        self.status_lbl.pack(pady=2)
        self.status_var.trace_add("write",
                                  lambda *_: self.status_lbl.configure(
                                      text=self.status_var.get()))

        # ==========================
        # LOG — CTkTextbox replaces tk.Text
        # ==========================
        ctk.CTkLabel(self.root, text="Log", font=("Tahoma", 18, "bold")).pack(anchor="w", padx=8)
        self.log = ctk.CTkTextbox(self.root, height=100,
                                  font=("Consolas", 16))
        self.log.pack(fill="x", padx=8, pady=(0, 6))

    # =====================================================
    # CSV
    # =====================================================
    def _load_csv_files(self):
        os.makedirs(DATA_DIR, exist_ok=True)

        files = [
            f for f in os.listdir(DATA_DIR)
            if f.lower().endswith(".csv")
        ]

        files.sort()

        self.file_combo["values"] = files

        if files:
            self.file_var.set(files[0])
            self.load_selected_csv()

    def load_selected_csv(self):
        filename = self.file_var.get()

        if not filename:
            return

        path = os.path.join(DATA_DIR, filename)

        self.tree.delete(*self.tree.get_children())
        self.current_rows.clear()

        try:
            with open(path, newline="") as f:
                reader = csv.reader(f)

                try:
                    next(reader)
                except StopIteration:
                    return

                for idx, row in enumerate(reader, start=1):
                    if len(row) != 4:
                        continue

                    self.current_rows.append(row)

                    self.tree.insert(
                        "",
                        "end",
                        values=(idx, row[0], row[1], row[2], row[3])
                    )

            self._log(f"Loaded: {filename}")
            self.status_var.set(f"Loaded {len(self.current_rows)} poses")

        except Exception as e:
            messagebox.showerror("Error", str(e))

    # =====================================================
    # SEND COMMAND
    # =====================================================
    def send_pose(self, pose):
        msg = f"{TOPIC_SERVO_CMD} {pose[0]} {pose[1]} {pose[2]} {pose[3]}"

        try:
            self.pub.send_string(msg)
            self._log(f"SEND -> {pose}")
            self.status_var.set(f"Current pose: {pose}")

        except Exception as e:
            self._log(f"ERROR: {e}")

    # =====================================================
    # TEST SELECTED
    # =====================================================
    def test_selected_pose(self):
        selected = self.tree.selection()

        if not selected:
            messagebox.showwarning("Warning", "Please select a pose")
            return

        item = self.tree.item(selected[0])
        values = item["values"]

        pose = values[1:5]

        self.send_pose(pose)

    # =====================================================
    # PLAY ALL
    # =====================================================
    def play_all(self):
        if self.running:
            return

        if not self.current_rows:
            messagebox.showwarning("Warning", "No pose data")
            return

        self.running = True

        threading.Thread(target=self._play_worker, daemon=True).start()

    def _play_worker(self):
        delay = float(self.delay_var.get())

        self._log("PLAY ALL started")

        for idx, pose in enumerate(self.current_rows, start=1):
            if not self.running:
                self._log("PLAY ALL stopped")
                return

            self.root.after(
                0,
                lambda i=idx: self.status_var.set(f"Playing pose {i}")
            )

            self.root.after(0, lambda p=pose: self.send_pose(p))

            time.sleep(delay)

        self.running = False

        self.root.after(
            0,
            lambda: self.status_var.set("PLAY ALL finished")
        )

        self._log("PLAY ALL finished")

    # =====================================================
    # STOP
    # =====================================================
    def stop_playback(self):
        self.running = False
        self.status_var.set("Stopped")
        self._log("STOP pressed")

    # =====================================================
    # LOG
    # =====================================================
    def _log(self, text):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}\n"

        print(line, end="")

        self.log.insert("end", line)
        self.log.see("end")

    # =====================================================
    # CLOSE
    # =====================================================
    def shutdown(self):
        self.running = False

        try:
            self.pub.close(0)
        except Exception:
            pass


# =========================================================
# MAIN
# =========================================================
def main():
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()

    app = CsvPoseTester(root)

    def on_close():
        app.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
