"""GoldMind watchdog.

Runs every 30 minutes via Task Scheduler (see scripts/install.bat).
Scans live processes for `python.exe` running `main.py` from this project.
If none is found, launches `main.py` in a detached console so the bot
keeps running after this watchdog process exits.

Kept free of Engine/DB imports — a watchdog must not depend on the
subsystems it is trying to recover. All behavior is testable via
dependency injection (`process_lister` + `launcher`) so the test suite
runs cross-platform without real psutil or subprocess calls.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger("goldmind.watchdog")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
# Marker file written by a healthy Engine (see _update_heartbeat in future wiring);
# watchdog treats missing/stale markers as advisory, not fatal.
HEARTBEAT_FILE = PROJECT_ROOT / "data" / "heartbeat.txt"


# ----------------------------------------------------------------------
def _default_process_lister() -> list[dict[str, Any]]:
    """Yield processes with name + cmdline. Uses psutil when available."""
    try:
        import psutil  # type: ignore
    except ImportError:
        return []
    out: list[dict[str, Any]] = []
    for p in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            out.append({
                "pid": p.info["pid"],
                "name": (p.info["name"] or "").lower(),
                "cmdline": [str(c) for c in (p.info["cmdline"] or [])],
            })
        except Exception:  # noqa: BLE001
            continue
    return out


def _default_launcher(python_exe: str, script: Path, cwd: Path) -> None:
    """Start main.py in a detached new console (Windows DETACHED_PROCESS)."""
    flags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_CONSOLE = 0x00000010
        flags = DETACHED_PROCESS | CREATE_NEW_CONSOLE
    subprocess.Popen(  # noqa: S603
        [python_exe, str(script)],
        cwd=str(cwd),
        creationflags=flags,
        close_fds=True,
    )


# ----------------------------------------------------------------------
def is_bot_running(
    processes: Iterable[dict[str, Any]],
    main_script: Path = MAIN_SCRIPT,
) -> bool:
    target = str(main_script).lower()
    target_name = main_script.name.lower()
    for proc in processes:
        name = (proc.get("name") or "").lower()
        if "python" not in name:
            continue
        parts = [str(c).lower() for c in (proc.get("cmdline") or [])]
        if not parts:
            continue
        joined = " ".join(parts)
        if target in joined:
            return True
        # Path separators differ between platforms; also match by file name
        if any(Path(p).name.lower() == target_name for p in parts):
            return True
    return False


def watchdog_tick(
    *,
    process_lister: Callable[[], list[dict[str, Any]]] = _default_process_lister,
    launcher: Callable[[str, Path, Path], None] = _default_launcher,
    python_exe: str | None = None,
    main_script: Path = MAIN_SCRIPT,
    project_root: Path = PROJECT_ROOT,
) -> str:
    """One watchdog cycle. Returns a short verdict string (also logged)."""
    procs = process_lister()
    if is_bot_running(procs, main_script=main_script):
        verdict = "ok: bot process detected"
        logger.info(verdict)
        return verdict

    py = python_exe or _detect_python(project_root)
    logger.warning("bot process not found; restarting with %s", py)
    try:
        launcher(py, main_script, project_root)
    except Exception as exc:  # noqa: BLE001
        logger.exception("watchdog failed to launch: %s", exc)
        return f"error: launch failed: {exc}"
    return "restarted"


def _detect_python(project_root: Path) -> str:
    venv = project_root / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    return sys.executable or "python"


# ----------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    verdict = watchdog_tick()
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
