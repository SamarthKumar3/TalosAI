"""Standalone stdlib-only helper. Requests contain no commands, arguments or secrets."""

import contextlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4


def serve(root: Path) -> None:
    directory = root / ".talos/doctor"
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        return
    with (root / ".talos/doctor-host.lock").open("a+b") as lock:
        try:
            if sys.platform == "win32":
                import msvcrt

                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        while not (root / ".talos/doctor-stop").exists():
            heartbeat = directory / f"heartbeat-{uuid4().hex}.tmp"
            with heartbeat.open("x", encoding="utf-8") as output:
                output.write("ready")
            heartbeat.replace(directory / "heartbeat")
            for request in directory.glob("*.request"):
                if request.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}\.request", request.name):
                    continue
                report = {
                    "healthy": False,
                    "checks": [
                        {
                            "name": "host",
                            "status": "error",
                            "message": "Host diagnostics did not complete. "
                            "Run talos doctor in your terminal.",
                        }
                    ],
                }
                try:
                    if request.stat().st_size != 0:
                        continue
                    process = subprocess.run(
                        [sys.executable, "-m", "talos.cli.main", "doctor", "--json"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=25,
                        shell=False,
                    )
                    if len(process.stdout) <= 65536:
                        report = json.loads(process.stdout)
                except (OSError, subprocess.TimeoutExpired, ValueError):
                    pass
                finally:
                    request.unlink(missing_ok=True)
                response = request.with_suffix(".response")
                temporary = request.with_suffix(".tmp")
                with temporary.open("x", encoding="utf-8") as output:
                    output.write(json.dumps(report))
                temporary.replace(response)
            for old in directory.glob("*.response"):
                if not old.is_symlink() and time.time() - old.stat().st_mtime > 60:
                    old.unlink(missing_ok=True)
            time.sleep(1)
        (directory / "heartbeat").unlink(missing_ok=True)


if __name__ == "__main__":
    with contextlib.suppress(OSError):
        serve(Path(sys.argv[1]).resolve())
