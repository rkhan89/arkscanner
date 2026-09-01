"""Start the scanner fully detached, so it survives the terminal closing.

    python tools/start_soak.py          start it
    python tools/start_soak.py --status is it alive?
    python tools/start_soak.py --stop   stop it cleanly

Detachment matters: a process started normally from a terminal is a child of
that terminal's console. Close the window and it goes with it. This launches with
DETACHED_PROCESS under pythonw.exe, so the scanner has no console and no live
parent, and the launcher exits immediately.

The trade-off is that a process with no console cannot be sent Ctrl+C or
Ctrl+Break, so --stop works by writing the STOP file the scanner watches for.
That is a clean shutdown: it flushes the credit meter and closes out the run row.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from solscanner import config

PROJ = config.PROJECT_ROOT
PIDFILE = PROJ / "soak.pid"
SOAK_LOG = config.LOG_DIR / "soak_console.log"
# pythonw has no console at all; falls back to python.exe if it is missing.
PYW = PROJ / "venv" / "Scripts" / "pythonw.exe"
PY = PROJ / "venv" / "Scripts" / "python.exe"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def is_running(pid: int) -> bool:
    """True if a process with this pid is alive and is our scanner."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return False
    return f'"{pid}"' in out and "python" in out.lower()


def read_pid() -> int | None:
    if not PIDFILE.exists():
        return None
    try:
        return int(PIDFILE.read_text().strip())
    except ValueError:
        return None


def start() -> int:
    existing = read_pid()
    if existing and is_running(existing):
        print(f"Already running as pid {existing}. Use --stop first.")
        return 1

    config.LOG_DIR.mkdir(exist_ok=True)
    if config.STOP_FILE.exists():
        config.STOP_FILE.unlink()

    exe = PYW if PYW.exists() else PY
    fh = open(SOAK_LOG, "a", encoding="utf-8")
    fh.write(f"\n{'=' * 70}\nsoak started {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'=' * 70}\n")
    fh.flush()

    proc = subprocess.Popen(
        [str(exe), "-u", "run_scanner.py"],
        cwd=str(PROJ),
        stdout=fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True,
    )
    PIDFILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"started detached, pid {proc.pid}")
    print(f"console output -> {SOAK_LOG}")
    print(f"scanner log    -> {config.LOG_PATH}")
    print(f"stop with      -> python tools/start_soak.py --stop")
    return 0


def status() -> int:
    pid = read_pid()
    if pid is None:
        print("no pid file; not started by this launcher")
        return 1
    alive = is_running(pid)
    print(f"pid {pid}: {'RUNNING' if alive else 'NOT RUNNING'}")
    return 0 if alive else 1


def stop() -> int:
    pid = read_pid()
    config.STOP_FILE.write_text("stop", encoding="utf-8")
    print(f"wrote {config.STOP_FILE}")
    if pid is None:
        print("no pid file; the scanner will stop at its next status tick")
        return 0
    deadline = time.time() + config.STATUS_INTERVAL_SECONDS + 30
    while time.time() < deadline:
        if not is_running(pid):
            print(f"pid {pid} exited cleanly")
            config.STOP_FILE.unlink(missing_ok=True)
            PIDFILE.unlink(missing_ok=True)
            return 0
        time.sleep(2)
    print(f"pid {pid} still running after "
          f"{config.STATUS_INTERVAL_SECONDS + 30}s; check {SOAK_LOG}")
    return 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--start"
    raise SystemExit({"--start": start, "--status": status, "--stop": stop}[arg]())
