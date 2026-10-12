from __future__ import annotations

import argparse


def _parser(program: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=program, description="Open the Talos interactive shell")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("talos", "shell"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Disable persistent input history",
    )
    return parser


def _talos_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="talos", description="Talos automation and lifecycle CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize = subcommands.add_parser("init", help="Create portable SDK configuration files")
    initialize.add_argument("directory", nargs="?", default=".")
    shell = subcommands.add_parser("shell", help="Open the interactive shell")
    shell.add_argument("--no-history", action="store_true")
    ask = subcommands.add_parser("ask", help="Send a prompt through the local API")
    ask.add_argument("prompt")
    ask.add_argument("--json", action="store_true", dest="json_output")
    subcommands.add_parser("status", help="Show local service status")
    doctor = subcommands.add_parser("doctor", help="Run read-only diagnostics")
    doctor.add_argument("--json", action="store_true", dest="json_output")
    subcommands.add_parser("start", help="Start local services")
    subcommands.add_parser("stop", help="Stop local services without deleting data")
    subcommands.add_parser("restart", help="Restart API and worker services")
    logs = subcommands.add_parser("logs", help="Show bounded API and worker logs")
    logs.add_argument("--since", default="10m")
    subcommands.add_parser("migrate", help="Apply database migrations")
    subcommands.add_parser("ui", help="Open the optional Web UI")
    knowledge = subcommands.add_parser("knowledge", help="Manage local knowledge")
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_add = knowledge_commands.add_parser("add", help="Ingest a project text file")
    knowledge_add.add_argument("path")
    knowledge_status = knowledge_commands.add_parser("status", help="Inspect an ingestion job")
    knowledge_status.add_argument("job_id")
    knowledge_cancel = knowledge_commands.add_parser("cancel", help="Cancel an ingestion job")
    knowledge_cancel.add_argument("job_id")
    skills = subcommands.add_parser("skills", help="Manage project skills")
    skill_commands = skills.add_subparsers(dest="skill_command", required=True)
    for action in ("add", "enable", "disable"):
        entry = skill_commands.add_parser(action)
        entry.add_argument("value")
    tools = subcommands.add_parser("tools", help="Review consequential tool approvals")
    tool_commands = tools.add_subparsers(dest="tool_command", required=True)
    tool_add = tool_commands.add_parser("add", help="Import a loose YAML tool manifest")
    tool_add.add_argument("path")
    tool_add.add_argument("--function", help="Typed function in a reviewed Python file")
    tool_add.add_argument("--sha256", help="Exact reviewed Python source hash")
    tool_inspect = tool_commands.add_parser(
        "inspect", help="Show Python functions and SHA-256 without executing code"
    )
    tool_inspect.add_argument("path")
    tool_run = tool_commands.add_parser(
        "run", help="Invoke a managed tool; Python runs in the local Docker sandbox"
    )
    tool_run.add_argument("name")
    tool_run.add_argument("arguments", help="JSON object of named arguments")
    tool_run.add_argument(
        "--approve", action="store_true", help="Approve this exact local sandbox invocation"
    )
    tool_add.add_argument(
        "--reviewed-python",
        action="store_true",
        help="Confirm review of the exact SHA-256 in a Python sandbox manifest",
    )
    for action in ("approve", "reject", "status"):
        entry = tool_commands.add_parser(action)
        entry.add_argument("approval_id")
    rag = subcommands.add_parser("rag", help="Search indexed knowledge")
    rag.add_argument("query")
    repository = subcommands.add_parser("repo", help="Index and search a Git repository")
    repo_commands = repository.add_subparsers(dest="repo_command", required=True)
    for action in ("add", "index", "reindex"):
        entry = repo_commands.add_parser(action)
        entry.add_argument("path", nargs="?", default=".")
        entry.add_argument("--yes", action="store_true")
        entry.add_argument(
            "--private",
            action="store_true",
            help="Prompt privately for a GitHub token with Contents read access",
        )
    repo_status = repo_commands.add_parser("status")
    repo_status.add_argument("job_id", nargs="?")
    repo_status.add_argument("--path", default=".")
    repo_cancel = repo_commands.add_parser("cancel")
    repo_cancel.add_argument("job_id")
    repo_remove = repo_commands.add_parser("remove")
    repo_remove.add_argument("--yes", action="store_true")
    repo_remove.add_argument("--path", default=".")
    repo_search = repo_commands.add_parser("search")
    repo_search.add_argument("query")
    repo_search.add_argument("--path", default=".")
    repo_ask = repo_commands.add_parser("ask", help="Ask a question about an indexed repository")
    repo_ask.add_argument("query")
    repo_ask.add_argument("--path", default=".", help="Local path or GitHub repository URL")
    repo_hld = repo_commands.add_parser(
        "hld", help="Save a cited repository HLD as a Mermaid .mmd file"
    )
    repo_hld.add_argument("--path", default=".", help="Local path or GitHub repository URL")
    repo_hld.add_argument("--output", help="Directory for the Mermaid file and cited explanation")
    return parser
