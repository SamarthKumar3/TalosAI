from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import httpx
from rich.console import Console

from talos.agent import TalosAgent
from talos.cli import extensions, phase3, phase4, phase5, phase6, phase7
from talos.cli.api_client import ApiTalosAgent
from talos.cli.arguments import _parser, _talos_parser
from talos.cli.bootstrap import ShellSettings, ensure_provider_credential
from talos.cli.environment import find_project_root, load_cli_environment
from talos.cli.prompting import PromptToolkitReader, resolve_prompt_name
from talos.cli.rendering import RichShellRenderer
from talos.cli.shell import TalosShell
from talos.config import TalosConfig
from talos.credentials import KeyringCredentialStore, migrate_legacy_credential_file
from talos.errors import ProviderError, TalosError
from talos.extensions import ExtensionManager
from talos.skills import Skill


async def _run_shell(*, no_history: bool, project_root: Path) -> int:
    console = Console()
    environment = load_cli_environment(project_root)
    settings = ShellSettings.from_environment(project_root, environment)
    config = TalosConfig.from_file(settings.config_path, environment=environment)
    store = KeyringCredentialStore(settings.credential_service_name)
    migrated = migrate_legacy_credential_file(settings.legacy_credential_path, store)
    if migrated:
        console.print(
            "[green]Migrated the legacy plaintext credential into the OS vault "
            "and removed the file.[/green]"
        )
    ensure_provider_credential(config, environment, store=store, console=console)
    command_context = phase3.CommandContext(project_root, environment, config, store)
    runtime_mode = environment.get("TALOS_SHELL_RUNTIME_MODE", "").strip()
    if runtime_mode == "service":
        if phase3.start_with_context(command_context) != 0:
            raise TalosError("Local services could not be started")
        api_token = phase3.local_api_token(
            command_context,
            create=False,
        )
        await phase3.wait_until_ready(environment)
        shell_agent = cast(
            TalosAgent,
            ApiTalosAgent(
                config=config,
                api_base=environment["TALOS_API_BASE_URL"],
                token=api_token,
                timeout_seconds=config.budgets.max_execution_seconds + 5,
            ),
        )
    elif runtime_mode == "direct":
        shell_agent = TalosAgent.from_config(
            str(settings.config_path),
            environment=environment,
        )
        ExtensionManager(project_root, environment, shell_agent).reload_tools()
    else:
        raise TalosError("TALOS_SHELL_RUNTIME_MODE must be service or direct")
    reader = PromptToolkitReader(
        history_path=settings.history_path,
        history_enabled=settings.history_enabled and not no_history,
    )

    async def update_skill(action: str, value: str) -> tuple[Skill, ...]:
        updated = await phase5.update_with_context(command_context, action, value)
        if isinstance(shell_agent, ApiTalosAgent):
            shell_agent.config = updated
        else:
            shell_agent.set_skills(updated.skills)
        return updated.skills

    selected_repository = {"path": "."}
    shell = TalosShell(
        agent=shell_agent,
        reader=reader,
        renderer=RichShellRenderer(console),
        prompt_name=resolve_prompt_name(environment, project_root=project_root),
        ui_opener=lambda: phase4.open_ui_with_context(command_context, ensure_core=True),
        knowledge_importer=lambda path: phase6.add_with_context(command_context, path),
        skill_updater=update_skill,
        repository_handler=(
            (
                lambda arguments, conversation_id: _shell_repository(
                    command_context, arguments, reader, selected_repository, conversation_id
                )
            )
            if runtime_mode == "service"
            else None
        ),
        artifacts_directory=project_root / ".talos" / "artifacts",
    )
    await shell.run()
    return 0


async def _shell_repository(
    context: phase3.CommandContext,
    arguments: tuple[str, ...],
    reader: PromptToolkitReader,
    selected_repository: dict[str, str],
    conversation_id: str,
) -> str:
    if not arguments or arguments[0] == "status":
        result = await phase7.status_with_context(
            context,
            job_id=arguments[1] if len(arguments) > 1 else None,
            path=selected_repository["path"],
        )
        return str(result)
    if arguments[0] in {"add", "index", "reindex"}:
        indexed = await _shell_index_repository(context, arguments, reader, selected_repository)
        if selected_repository.get("ready") == "true":
            await phase7.select_with_context(context, conversation_id, selected_repository["path"])
        return indexed
    if arguments[0] in {"ask", "hld"}:
        query = (
            " ".join(arguments[1:])
            if arguments[0] == "ask"
            else "Create a repository HLD as a Mermaid flowchart with cited explanations."
        )
        response = await phase7.ask_with_context(context, query, path=selected_repository["path"])
        if arguments[0] == "hld":
            from talos.repository_diagrams import write_hld

            target = write_hld(response, context.root / ".talos" / "artifacts")
            return f"{response}\nMermaid file: {target}"
        return response
    if arguments[0] == "search" and len(arguments) > 1:
        return str(
            await phase7.search_with_context(
                context, " ".join(arguments[1:]), path=selected_repository["path"]
            )
        )
    if arguments[0] == "cancel" and len(arguments) == 2:
        return str(await phase7.cancel_with_context(context, arguments[1]))
    if arguments[0] == "remove":
        return await _shell_remove_repository(context, reader, selected_repository["path"])
    return (
        "Use /repo [status [JOB_ID]|index [PATH_OR_URL]|ask QUESTION|"
        "hld|search QUERY|cancel JOB_ID|remove]"
    )


async def _shell_index_repository(
    context: phase3.CommandContext,
    arguments: tuple[str, ...],
    reader: PromptToolkitReader,
    selected_repository: dict[str, str],
) -> str:
    choice = await reader.read("Read and index safe repository files? [y/N] ")
    if choice.strip().lower() not in {"y", "yes"}:
        return "Repository indexing was not confirmed."
    path = arguments[1] if len(arguments) > 1 else selected_repository["path"]
    result = await phase7.index_with_context(context, path, yes=True)
    selected_repository["path"] = path
    selected_repository["ready"] = "false"
    await phase7.wait_for_index(context, str(result["job_id"]))
    selected_repository["ready"] = "true"
    return str(result)


async def _shell_remove_repository(
    context: phase3.CommandContext, reader: PromptToolkitReader, path: str
) -> str:
    choice = await reader.read("Remove indexed repository data? Source files stay. [y/N] ")
    if choice.strip().lower() not in {"y", "yes"}:
        return "Repository removal was not confirmed."
    return str(await phase7.remove_with_context(context, yes=True, path=path))


def _entry(program: str, arguments: Sequence[str]) -> int:
    options = _parser(program).parse_args(arguments)
    try:
        project_root = find_project_root(Path.cwd())
        return asyncio.run(_run_shell(no_history=options.no_history, project_root=project_root))
    except (TalosError, OSError, KeyError) as exc:
        Console(stderr=True).print(f"[red]Talos could not start:[/red] {exc}")
        return 2
    except KeyboardInterrupt:
        Console().print("Goodbye.")
        return 130


def hi_main() -> None:
    raise SystemExit(_entry("hi", sys.argv[1:]))


def talos_main() -> None:
    options = _talos_parser().parse_args(sys.argv[1:])
    try:
        code = _dispatch_talos(options)
    except ProviderError as exc:
        Console(stderr=True).print(f"provider_error: {exc}", markup=False)
        code = 4
    except (TalosError, OSError, KeyError, httpx.HTTPError) as exc:
        Console(stderr=True).print(f"Talos command failed: {exc}", markup=False)
        code = 2
    raise SystemExit(code)


def _dispatch_talos(options: argparse.Namespace) -> int:
    if options.command == "doctor" and options.json_output:
        import json

        report = phase3.doctor_report()
        sys.stdout.write(json.dumps(report) + "\n")
        return 0 if report["healthy"] else 1
    if options.command == "init":
        from talos.cli.scaffold import initialize_project

        destination = initialize_project(Path(options.directory).resolve())
        Console().print(f"SDK configuration created in {destination}", markup=False)
        Console().print(
            "For the complete CLI/Web workspace, use a repository checkout with Docker."
        )
        return 0
    simple_commands = {
        "status": phase3.status,
        "doctor": phase3.doctor,
        "start": phase3.start,
        "stop": phase3.stop,
        "restart": phase3.restart,
        "migrate": phase3.migrate,
    }
    if handler := simple_commands.get(options.command):
        return handler()
    if options.command == "shell":
        return _entry("talos shell", ["--no-history"] if options.no_history else [])
    if options.command == "ask":
        return phase3.ask(options.prompt, json_output=options.json_output)
    if options.command == "logs":
        return phase3.logs(options.since)
    if options.command == "ui":
        context = phase3.load_context(require_provider=True, interactive=sys.stdin.isatty())
        return 0 if asyncio.run(phase4.open_ui_with_context(context, ensure_core=True)) else 2
    if options.command == "knowledge":
        return _dispatch_knowledge(options)
    if options.command == "skills":
        return phase5.update(options.skill_command, options.value)
    if options.command == "tools":
        return _dispatch_tool(options)
    if options.command == "rag":
        return phase6.search(options.query)
    if options.command == "repo":
        return phase7.command(
            options.repo_command,
            _repository_value(options),
            yes=getattr(options, "yes", False),
            path=getattr(options, "path", "."),
            private=getattr(options, "private", False),
            output=getattr(options, "output", None),
        )
    return 2


def _dispatch_knowledge(options: argparse.Namespace) -> int:
    if options.knowledge_command == "add":
        return phase6.add(options.path)
    if options.knowledge_command == "status":
        return phase6.status(options.job_id)
    if options.knowledge_command == "cancel":
        return phase6.cancel(options.job_id)
    return 2


def _dispatch_tool(options: argparse.Namespace) -> int:
    if options.tool_command == "add":
        return extensions.add(
            options.path,
            reviewed_python=options.reviewed_python,
            function=options.function,
            sha256=options.sha256,
        )
    if options.tool_command == "inspect":
        return extensions.inspect_python(options.path)
    if options.tool_command == "run":
        return extensions.run(options.name, options.arguments, approve=options.approve)
    if options.tool_command == "approve":
        return phase5.decide_tool(options.approval_id, approve=True)
    if options.tool_command == "reject":
        return phase5.decide_tool(options.approval_id, approve=False)
    if options.tool_command == "status":
        return phase5.tool_status(options.approval_id)
    return 2


def _repository_value(options: argparse.Namespace) -> str | None:
    if options.repo_command in {"add", "index", "reindex"}:
        return _optional_text(options.path)
    if options.repo_command in {"status", "cancel"}:
        return _optional_text(options.job_id)
    if options.repo_command in {"search", "ask"}:
        return _optional_text(options.query)
    return None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "hi":
        raise SystemExit(_entry("hi", arguments[1:]))
    sys.argv = [sys.argv[0], *arguments]
    talos_main()
