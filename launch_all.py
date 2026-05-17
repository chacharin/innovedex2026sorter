"""Launch all nodes at once.

Order matters — we start the BIND-side nodes first so the SUB/PUB sockets
they own are alive before the connect-side nodes try to publish.

Usage:
    python launch_all.py            # auto-activates ./venv if not already inside it
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")


def ensure_venv():
    """If a project venv exists and we are not already running from it,
    re-launch this script under the venv python and exit."""
    if not os.path.isfile(VENV_PY):
        print("  ⚠  No venv found at ./venv — using system Python.")
        return
    # Normalise so Windows drive-letter case differences don't matter
    if os.path.normcase(sys.executable) == os.path.normcase(VENV_PY):
        print(f"  ✓  venv active: {VENV_PY}")
        return
    print(f"  ↻  venv not active — re-launching under: {VENV_PY}")
    result = subprocess.run([VENV_PY] + sys.argv)
    sys.exit(result.returncode)


ensure_venv()

PY = VENV_PY if os.path.isfile(VENV_PY) else sys.executable

# (display name, script).  Hardware + Vision bind, so they go first.
NODES = [
    ("Arduino Node",   "arduino_node.py"),
    ("Camera Node",     "camera_node.py"),
    ("Scale Node",   "scale_node.py"),
    ("Main Decision",   "main_decision_node.py"),
    ("CSV Pose Tester", "csv_pose_tester.py"),
]


def launch_one(name, script):
    path = os.path.join(ROOT, script)
    if not os.path.isfile(path):
        print(f"  ✗ missing {script}")
        return None
    creationflags = 0
    if os.name == "nt":
        # Each node gets its own console so logs don't mix
        creationflags = subprocess.CREATE_NEW_CONSOLE
    print(f"  ▶ launching {name}: {script}")
    return subprocess.Popen([PY, path], cwd=ROOT, creationflags=creationflags)


def main():
    print(f"Using Python: {PY}")
    print(f"Project root: {ROOT}\n")
    procs = []
    for name, script in NODES:
        p = launch_one(name, script)
        if p:
            procs.append((name, p))
        # stagger so binds settle before connects
        time.sleep(1.0)

    print("\nAll nodes launched.  Press Ctrl+C in this window to terminate them all,")
    print("or simply close each node window individually.\n")

    try:
        for _, p in procs:
            p.wait()
    except KeyboardInterrupt:
        print("\nTerminating all nodes…")
        for _, p in procs:
            try: p.terminate()
            except Exception: pass


if __name__ == "__main__":
    main()
