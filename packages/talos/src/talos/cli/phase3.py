from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from rich.console import Console

from talos.cli.bootstrap import ShellSettings, ensure_provider_credential
from talos.cli.environment import find_project_root, load_cli_environment, required_project_path
from talos.config import TalosConfig
from talos.credentials import KeyringCredentialStore, migrate_legacy_credential_file
from talos.errors import ConfigurationError, ProviderError


@dataclass(slots=True)
class CommandContext:
    root: Path
    environment: dict[str, str]
    config: TalosConfig
    credential_store: KeyringCredentialStore


def load_context(*, require_provider: bool, interactive: bool) -> CommandContext:
    root = find_project_root(Path.cwd())
    environment = load_cli_environment(root)
    settings = ShellSettings.from_environment(root, environment)
    config = TalosConfig.from_file(settings.config_path, environment=environment)
    store = KeyringCredentialStore(settings.credential_service_name)
    migrate_legacy_credential_file(settings.legacy_credential_path, store)
    if require_provider:
        if interactive:
            ensure_provider_credential(
                config,
                environment,
                store=store,
                console=Console(stderr=True),
            )
        else:
            value = environment.get(config.provider.api_key_env) or store.get(
                config.provider.api_key_env
            )
            if not value:
                raise ConfigurationError(
                    f"{config.provider.api_key_env} is missing; run 'hi talos' to configure it"
                )
            environment[config.provider.api_key_env] = value
    return CommandContext(root, environment, config, store)


def ask(prompt: str, *, json_output: bool) -> int:
    context = load_context(require_provider=False, interactive=False)
    stdin_text = read_bounded_stdin(
        int(_required(context.environment, "TALOS_CLI_MAX_STDIN_BYTES"))
    )
    input_text = f"{stdin_text}\n\n{prompt}".strip() if stdin_text else prompt
    token = local_api_token(context, create=False)
    response = httpx.post(
        f"{_local_api_base_url(context.environment)}/api/v1/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"input": input_text},
        timeout=context.config.budgets.max_execution_seconds + 5,
    )
    if response.status_code >= 400:
        raise ProviderError(f"Local Talos API returned HTTP {response.status_code}")
    payload = response.json()
    if json_output:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        Console().print(str(payload["response"]), markup=False)
    return 0


def status() -> int:
    context = load_context(require_provider=False, interactive=False)
    return _compose(context, ["ps"])


def start() -> int:
    context = load_context(require_provider=True, interactive=sys.stdin.isatty())
    return start_with_context(context)


def start_with_context(context: CommandContext) -> int:
    local_api_token(context, create=True)
    return _compose(context, ["up", "-d", "postgres-dev", "redis-dev", "api", "worker"])


def stop() -> int:
    context = load_context(require_provider=False, interactive=False)
    result = _compose(context, ["stop"])
    if result == 0:
        from talos.cli.doctor_bridge import stop_doctor_host

        stop_doctor_host(context.root)
    return result


def restart() -> int:
    context = load_context(require_provider=True, interactive=sys.stdin.isatty())
    local_api_token(context, create=True)
    return _compose(context, ["restart", "api", "worker"])


def logs(since: str) -> int:
    context = load_context(require_provider=False, interactive=False)
    tail = _positive_integer(context.environment, "TALOS_LOG_TAIL_LINES")
    return _compose(
        context,
        ["logs", "--since", since, "--tail", str(tail), "api", "worker", "web"],
    )


def migrate() -> int:
    context = load_context(require_provider=False, interactive=False)
    return _compose(context, ["run", "--rm", "migrate"])


def start_web(context: CommandContext) -> int:
    local_api_token(context, create=True)
    # Core readiness is handled before this call. Avoid traversing and reconciling
    # api/worker dependencies a second time when only the Web adapter is missing.
    return _compose(context, ["up", "-d", "--no-deps", "web"])


def reset_ui_stack(context: CommandContext) -> int:
    """Rebuild the Talos Compose project without deleting named volumes."""
    local_api_token(context, create=True)
    stop_timeout = _positive_integer(context.environment, "TALOS_COMPOSE_STOP_TIMEOUT_SECONDS")
    stopped = _compose(
        context,
        ["down", "--remove-orphans", "--timeout", str(stop_timeout)],
    )
    if stopped != 0:
        return stopped
    return _compose(
        context,
        ["up", "-d", "--build", "postgres-dev", "redis-dev", "api", "worker", "web"],
    )


def doctor() -> int:
    report = doctor_report()
    console = Console()
    for check in report["checks"]:
        color = "green" if check["status"] == "ok" else "red"
        console.print(f"[{color}]{check['status'].upper()}[/{color}] {check['message']}")
    return 0 if report["healthy"] else 1


def doctor_report() -> dict[str, Any]:
    """Read-only diagnostics shared by the terminal and browser host bridge."""
    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, message: str) -> None:
        checks.append({"name": name, "status": "ok" if ok else "error", "message": message})

    try:
        context = load_context(require_provider=False, interactive=False)
        add("configuration", True, "Configuration is valid")
        add("observability", True, "Tracing and usage settings are valid")
        credential = bool(
            context.environment.get(context.config.provider.api_key_env)
            or context.credential_store.get(context.config.provider.api_key_env)
        )
        add(
            "model_key",
            credential,
            "Provider key is present"
            if credential
            else "Provider key is missing; run 'hi talos' to add it",
        )
    except (ConfigurationError, OSError):
        add(
            "configuration",
            False,
            "Configuration or credential storage is unavailable; check your local settings",
        )
        return {"healthy": False, "checks": checks}
    for label, arguments in (
        ("Docker", ["docker", "--version"]),
        ("Docker Compose", ["docker", "compose", "version"]),
        ("Docker engine", ["docker", "info"]),
    ):
        result = _run(
            arguments,
            cwd=context.root,
            environment=context.environment,
            capture=True,
            timeout_seconds=5,
        )
        add(
            label,
            result.returncode == 0,
            f"{label} is available"
            if result.returncode == 0
            else f"{label} unavailable: install or start Docker Desktop",
        )
    return {"healthy": all(item["status"] == "ok" for item in checks), "checks": checks}


def read_bounded_stdin(maximum_bytes: int) -> str:
    if sys.stdin.isatty():
        return ""
    payload = sys.stdin.buffer.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise ConfigurationError("stdin exceeds TALOS_CLI_MAX_STDIN_BYTES")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("stdin must be UTF-8 text") from exc


def _compose(context: CommandContext, arguments: list[str]) -> int:
    compose_file = required_project_path(
        context.root,
        context.environment,
        "TALOS_COMPOSE_FILE",
    )
    project_name = _required(context.environment, "TALOS_COMPOSE_PROJECT_NAME")
    command_timeout = _positive_integer(
        context.environment, "TALOS_COMPOSE_COMMAND_TIMEOUT_SECONDS"
    )
    result = _run(
        [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "-f",
            str(compose_file),
            *arguments,
        ],
        cwd=context.root,
        environment=context.environment,
        capture=False,
        timeout_seconds=command_timeout,
    )
    return result.returncode


def local_api_token(context: CommandContext, *, create: bool) -> str:
    name = "TALOS_LOCAL_API_TOKEN"
    value = context.environment.get(name)
    if not value or value == "replace-with-random-local-token":
        value = context.credential_store.get(name)
    if value == "replace-with-random-local-token":
        value = None
    if not value and create:
        value = secrets.token_urlsafe(32)
        context.credential_store.set(name, value)
    if not value:
        raise ConfigurationError("Local API token is missing; run 'talos start'")
    context.environment[name] = value
    return value


async def wait_until_ready(environment: dict[str, str]) -> None:
    api_base = _local_api_base_url(environment)
    timeout = _positive_integer(environment, "TALOS_API_STARTUP_TIMEOUT_SECONDS")
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{api_base}/health/ready")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise ConfigurationError("Local Talos API did not become ready before the configured timeout")


def _local_api_base_url(environment: dict[str, str]) -> str:
    value = _required(environment, "TALOS_API_BASE_URL").rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("TALOS_API_BASE_URL must be a valid HTTP(S) URL")
    remote_enabled = environment.get("TALOS_REMOTE_ACCESS_ENABLED", "false").strip().lower()
    if remote_enabled == "false" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigurationError(
            "TALOS_API_BASE_URL must use a loopback host in local mode; "
            "Docker service names such as 'api' are only reachable inside Compose"
        )
    return value


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    capture: bool,
    timeout_seconds: int = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            cwd=cwd,
            env={**os.environ, **environment},
            check=False,
            capture_output=capture,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(arguments, 1, "", str(exc))


def _required(environment: dict[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    return value


def _positive_integer(environment: dict[str, str], name: str) -> int:
    try:
        value = int(_required(environment, name))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value
