"""Central ZMQ + system configuration for The Sorter project.

ZMQ topology (each port has exactly ONE bind):
    PORT 5555  -- servo_cmd      : SUB binds on hardware_node, PUB connect from
                                    teaching_node + main.py
    PORT 5556  -- servo_status   : PUB binds on hardware_node, SUB connect from
                                    main.py  (IDLE / BUSY)
    PORT 5557  -- vision_cmd     : SUB binds on vision_node, PUB connect from
                                    main.py  (capture)
    PORT 5558  -- color_result   : PUB binds on vision_node, SUB connect from
                                    main.py
"""

# --- Ports ---
PORT_SERVO_CMD     = 5555
PORT_SERVO_STATUS  = 5556
PORT_VISION_CMD    = 5557
PORT_VISION_RESULT = 5558

# --- Address templates ---
ADDR_BIND = "tcp://*:{port}"
ADDR_CONN = "tcp://localhost:{port}"

# --- Topics (used as ZMQ subscription prefixes) ---
TOPIC_SERVO_CMD     = "servo_cmd"
TOPIC_SERVO_STATUS  = "servo_status"
TOPIC_VISION_CMD    = "vision_cmd"
TOPIC_VISION_RESULT = "color_result"

# --- Status values ---
STATUS_IDLE = "IDLE"
STATUS_BUSY = "BUSY"

# --- Commands ---
CMD_CAPTURE = "capture"
CMD_STOP    = "stop"
CMD_HOME    = "home"

# --- Robot defaults ---
HOME_POSE = [90, 90, 90, 50]                 # degrees per axis (s8, s9, s10, s11)
SERVO_PINS = [8, 9, 10, 11]                  # Arduino digital pins
SERVO_LIMITS = [(0, 180), (0, 180), (0, 180), (30, 75)]  # (min, max) per axis

# --- Vision ---
COLORS = ["red", "blue", "green", "yellow"]
DEFAULT_CAMERA_INDEX = 1
DEFAULT_CONFIDENCE = 0.75

# --- Sweep tuning ---
SWEEP_STEP_DEG  = 1.0
SWEEP_TICK_SEC  = 0.02

# --- Data dir ---
DATA_DIR = "./data"

# --- Recordable file numbers ---
RECORD_OPTIONS = (
    [str(n) for n in range(1, 6)] +
    [f"{a}{b}" for a in range(1, 6) for b in range(1, 5)]
)
