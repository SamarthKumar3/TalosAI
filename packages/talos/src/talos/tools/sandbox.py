"""Fail-closed Python execution in an operator-built Docker image."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from talos.errors import ConfigurationError
from talos.tools.runtime import ToolError

IMAGE = "talos-tool-sandbox:1"
RUNNER = """import asyncio, contextlib, importlib.util, json, sys, types
shim = types.ModuleType("talos")
shim.tool = lambda function: function
sys.modules["talos"] = shim
request = json.loads(sys.stdin.read(16385))
spec = importlib.util.spec_from_file_location("extension", "/tool/tool.py")
module = importlib.util.module_from_spec(spec)
with open("/dev/null", "w") as quiet, contextlib.redirect_stdout(quiet):
    spec.loader.exec_module(module)
    result = getattr(module, request["function"])(**request["arguments"])
    if hasattr(result, "__await__"):
        result = asyncio.run(result)
if not isinstance(result, str):
    raise TypeError("Sandbox tools must return a string")
encoded = json.dumps(result, allow_nan=False)
if len(encoded.encode()) > 32768:
    raise ValueError("Tool output exceeds 32 KiB")
print(encoded)
"""


def reviewed_source(root: Path, manifest: dict[str, Any]) -> bytes:
    path = root / str(manifest["python_file"])
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve() / "tools")
    except ValueError as exc:
        raise ConfigurationError("Python source must be inside the managed tools folder") from exc
    if (
        path.absolute() != resolved
        or resolved.suffix != ".py"
        or not resolved.is_file()
        or resolved.stat().st_size > 262144
    ):
        raise ConfigurationError("Python source must be a regular .py file of at most 256 KiB")
    source = resolved.read_bytes()
    if hashlib.sha256(source).hexdigest() != manifest["sha256"]:
        raise ConfigurationError("Python source changed; review and register its new hash")
    return source


async def run_python(root: Path, manifest: dict[str, Any], arguments: dict[str, Any]) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise ToolError("Docker sandbox is unavailable. Python tools never run on the host.")
    source = reviewed_source(root, manifest)
    name = "talos-tool-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="talos-sandbox-") as directory:
        path = Path(directory) / "tool.py"
        path.write_bytes(source)
        path.chmod(0o444)
        command = [
            docker,
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "-i",
            "--network=none",
            "--read-only",
            "--user=65534:65534",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=32",
            "--cpus=1",
            "--memory=128m",
            "--memory-swap=128m",
            "--ulimit",
            "nofile=64:64",
            "--log-driver=none",
            "--tmpfs",
            # Docker tmpfs specification, not a host temporary directory.
            "/tmp:rw,noexec,nosuid,size=16m",  # nosec B108
            "--mount",
            f"type=bind,source={path},target=/tool/tool.py,readonly",
            IMAGE,
            "python",
            "-I",
            "-c",
            RUNNER,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=65536,
        )
        try:
            # Read bounded stdout; a malicious tool cannot make the host buffer unbounded output.
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                json.dumps(
                    {"function": manifest["function"], "arguments": arguments}, allow_nan=False
                ).encode()
            )
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(10):
                output = b""
                while chunk := await process.stdout.read(4096):
                    output += chunk
                    if len(output) > 32769:
                        raise ToolError("Sandbox output exceeds 32 KiB")
                await process.wait()
            if process.returncode:
                raise ToolError(
                    "Sandbox tool failed. Check the image, source and string return type."
                )
            result = json.loads(output)
            if not isinstance(result, str):
                raise ToolError("Sandbox returned an invalid result")
            return result
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise ToolError("Sandbox timed out or returned invalid output") from exc
        finally:
            # Stop the container on caller cancellation as well as on normal completion.
            cleanup = await asyncio.create_subprocess_exec(
                docker,
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(cleanup.wait(), 5)
            except TimeoutError:
                cleanup.kill()
            if process.returncode is None:
                process.kill()
                await process.wait()
