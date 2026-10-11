from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def start_doctor_host(root: Path) -> None:
    """Start a host helper without exposing the Docker socket to Web."""
    directory = root / ".talos/doctor"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise OSError("Diagnostic directory must stay inside the project")
    (root / ".talos/doctor-stop").unlink(missing_ok=True)
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("doctor_host.py")), str(root)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        start_new_session=sys.platform != "win32",
    )


def stop_doctor_host(root: Path) -> None:
    """Ask the helper to exit without accepting a process ID or shell command."""
    marker = root / ".talos/doctor-stop"
    if marker.parent.is_dir() and not marker.is_symlink():
        with marker.open("w", encoding="utf-8") as output:
            output.write("stop")
