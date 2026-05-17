"""Node 1 — Vision Node.

Real-time YOLOv8 preview, but only PUBLISHES a color result when explicitly
asked (via vision_cmd "capture"), or when the user clicks the local Test
Capture button.

When asked to capture: collects ~8 frames over ~0.5 s, picks the highest-
confidence detection in each frame whose class is in COLORS, and votes the
most common color → robust against single-frame false positives.

ZMQ:
    SUB binds  PORT_VISION_CMD     ("vision_cmd capture")
    PUB binds  PORT_VISION_RESULT  ("color_result <red|blue|green|yellow|none>")
"""

import threading
import time
from collections import Counter
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk
import cv2
from PIL import Image, ImageTk
import zmq
from ultralytics import YOLO

from utils.zmq_config import (
    PORT_VISION_CMD, PORT_VISION_RESULT,
    TOPIC_VISION_CMD, TOPIC_VISION_RESULT,
    CMD_CAPTURE, COLORS, ADDR_BIND,
    DEFAULT_CAMERA_INDEX, DEFAULT_CONFIDENCE,
)

MODEL_PATH = "4color-detection.pt"


class VisionNode:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("Camera Node — YOLOv8")
        root.geometry("560x540")


        self.model = YOLO(MODEL_PATH)
        self.cap = None
        self.cam_idx = DEFAULT_CAMERA_INDEX
        self.confidence = DEFAULT_CONFIDENCE
        self.running = True
        self.cap_lock = threading.Lock()

        # ZMQ
        self.ctx = zmq.Context.instance()
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.bind(ADDR_BIND.format(port=PORT_VISION_CMD))
        self.sub.setsockopt_string(zmq.SUBSCRIBE, TOPIC_VISION_CMD)
        self.sub.RCVTIMEO = 100

        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.bind(ADDR_BIND.format(port=PORT_VISION_RESULT))

        self._build_ui()
        self._open_camera(self.cam_idx)

        threading.Thread(target=self._zmq_loop, daemon=True).start()
        self._update_frame()

    # ---------------- UI ----------------
    def _build_ui(self):
        top = ctk.CTkFrame(self.root, fg_color="transparent")
        top.pack(fill="x", pady=4, padx=8)

        ctk.CTkLabel(top, text="Cam:", font=("Tahoma", 18)).pack(side="left")
        self.cam_var = tk.StringVar(value=str(self.cam_idx))
        # Keep ttk.Combobox for <<ComboboxSelected>> binding
        cam_dd = ttk.Combobox(top, textvariable=self.cam_var,
                              values=[str(i) for i in range(0, 6)],
                              width=3, state="readonly")
        cam_dd.pack(side="left", padx=3)
        cam_dd.bind("<<ComboboxSelected>>",
                    lambda e: self._open_camera(int(self.cam_var.get())))

        ctk.CTkLabel(top, text="  Conf:", font=("Tahoma", 18)).pack(side="left")
        self.conf_var = tk.DoubleVar(value=self.confidence)
        conf_slider = ctk.CTkSlider(top, from_=0.30, to=0.95,
                                    number_of_steps=65,
                                    variable=self.conf_var,
                                    width=140,
                                    command=self._on_conf)
        conf_slider.pack(side="left", padx=3)

        ctk.CTkButton(top, text="Test Capture", width=100,
                      font=("Tahoma", 18, "bold"),
                      command=self._manual_capture).pack(side="right", padx=4)

        # Video label kept as tk.Label for ImageTk.PhotoImage compatibility
        video_frame = ctk.CTkFrame(self.root, corner_radius=4)
        video_frame.pack(pady=4)
        self.lbl_video = tk.Label(video_frame, width=400, height=300)
        self.lbl_video.pack()

        # status_var — StringVar with trace so _publish_result's .set() works unchanged
        self.status_var = tk.StringVar(value="Detected color: —")
        self.lbl_status = ctk.CTkLabel(self.root, text=self.status_var.get(),
                                       font=("Tahoma", 22, "bold"))
        self.lbl_status.pack(pady=2)
        self.status_var.trace_add("write",
                                  lambda *_: self.lbl_status.configure(
                                      text=self.status_var.get()))

        # last_var — StringVar with trace so _update_frame's .set() works unchanged
        self.last_var = tk.StringVar(value="Live: scanning…")
        self.lbl_last = ctk.CTkLabel(self.root, text=self.last_var.get(),
                                     font=("Tahoma", 18))
        self.lbl_last.pack()
        self.last_var.trace_add("write",
                                lambda *_: self.lbl_last.configure(
                                    text=self.last_var.get()))

        info = (f"SUB :{PORT_VISION_CMD} ({TOPIC_VISION_CMD})   "
                f"PUB :{PORT_VISION_RESULT} ({TOPIC_VISION_RESULT})")
        ctk.CTkLabel(self.root, text=info,
                     font=("Consolas", 14)).pack(pady=2)

    def _on_conf(self, _val):
        self.confidence = float(self.conf_var.get())

    # ---------------- Camera ----------------
    def _open_camera(self, idx: int):
        with self.cap_lock:
            if self.cap is not None:
                try: self.cap.release()
                except Exception: pass
            self.cap = cv2.VideoCapture(idx)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.cam_idx = idx
            print(f"[vision] camera switched to index {idx}")

    def _read_frame(self):
        with self.cap_lock:
            if self.cap is None:
                return False, None
            return self.cap.read()

    # ---------------- Live preview ----------------
    def _update_frame(self):
        if not self.running:
            return
        ret, frame = self._read_frame()
        live_label = "Live: (no camera)"
        if ret:
            results = self.model(frame, verbose=False)[0]
            best_label = None
            best_conf = 0.0
            for box in results.boxes:
                conf = float(box.conf[0])
                if conf >= self.confidence:
                    cls_id = int(box.cls[0])
                    label = self.model.names[cls_id].lower()
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
                    cv2.putText(frame, f"{label} {conf:.2f}",
                                (x1, max(y1 - 8, 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                    if conf > best_conf and label in COLORS:
                        best_conf = conf
                        best_label = label
            live_label = (f"Live: {best_label} ({best_conf:.2f})"
                          if best_label else "Live: no color above threshold")
            img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            imgtk = ImageTk.PhotoImage(image=Image.fromarray(img).resize((400, 300)))
            self.lbl_video.imgtk = imgtk
            self.lbl_video.configure(image=imgtk)
        self.last_var.set(live_label)
        self.root.after(20, self._update_frame)

    # ---------------- Capture-on-request ----------------
    def _capture_and_vote(self, n_frames: int = 8, gap_s: float = 0.05) -> str | None:
        votes = []
        for _ in range(n_frames):
            ret, frame = self._read_frame()
            if not ret:
                continue
            results = self.model(frame, verbose=False)[0]
            best_label = None
            best_conf = 0.0
            for box in results.boxes:
                conf = float(box.conf[0])
                if conf >= self.confidence:
                    cls_id = int(box.cls[0])
                    label = self.model.names[cls_id].lower()
                    if label in COLORS and conf > best_conf:
                        best_conf = conf
                        best_label = label
            if best_label:
                votes.append(best_label)
            time.sleep(gap_s)
        if not votes:
            return None
        winner, _ = Counter(votes).most_common(1)[0]
        print(f"[vision] vote {dict(Counter(votes))} -> {winner}")
        return winner

    def _publish_result(self, color: str | None):
        result = color if color else "none"
        try:
            self.pub.send_string(f"{TOPIC_VISION_RESULT} {result}")
        except Exception as e:
            print(f"[vision] pub err: {e}")
        self.root.after(0, lambda c=result:
                        self.status_var.set(f"Detected color: {c}"))

    # ---------------- ZMQ + manual ----------------
    def _zmq_loop(self):
        while self.running:
            try:
                msg = self.sub.recv_string()
            except zmq.Again:
                continue
            except Exception:
                break
            parts = msg.split()
            if len(parts) >= 2 and parts[1] == CMD_CAPTURE:
                color = self._capture_and_vote()
                self._publish_result(color)

    def _manual_capture(self):
        threading.Thread(target=lambda: self._publish_result(self._capture_and_vote()),
                         daemon=True).start()

    # ---------------- Lifecycle ----------------
    def shutdown(self):
        self.running = False
        try:
            with self.cap_lock:
                if self.cap: self.cap.release()
        except Exception: pass
        for s in (self.sub, self.pub):
            try: s.close(0)
            except Exception: pass


def main():
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    node = VisionNode(root)

    def on_close():
        node.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
