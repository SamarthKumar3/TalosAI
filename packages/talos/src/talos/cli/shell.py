from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from talos.agent import TalosAgent
from talos.cli.rendering import ShellRenderer, safe_configuration_rows
from talos.cli.routing import COMMANDS, InputKind, RoutedInput, route_input
from talos.errors import RunCancelledError, TalosError
from talos.events import EventType
from talos.skills import Skill
from talos.why import parse_why_offset

_TOOLS_COMMAND = "/tools"
_SKILLS_COMMAND = "/skills"
_KNOWLEDGE_COMMAND = "/knowledge"
_REPOSITORY_COMMAND = "/repo"


class PromptReader(Protocol):
    async def read(self, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Exchange:
    prompt: str
    response: str


class TalosShell:
    def __init__(
        self,
        *,
        agent: TalosAgent,
        reader: PromptReader,
        renderer: ShellRenderer,
        prompt_name: str,
        ui_opener: Callable[[], Awaitable[bool]] | None = None,
        knowledge_importer: Callable[[str], Awaitable[str]] | None = None,
        skill_updater: Callable[[str, str], Awaitable[tuple[Skill, ...]]] | None = None,
        repository_handler: Callable[[tuple[str, ...]], Awaitable[str]] | None = None,
    ) -> None:
        self._agent = agent
        self._reader = reader
        self._renderer = renderer
        self._prompt_name = prompt_name
        self._ui_opener = ui_opener
        self._knowledge_importer = knowledge_importer
        self._skill_updater = skill_updater
        self._repository_handler = repository_handler
        self._conversation_id: str | None = None
        self._active_run_id: str | None = None
        self._history: list[Exchange] = []
        self._input_tokens = 0
        self._output_tokens = 0

    async def run(self) -> None:
        self._renderer.welcome(self._agent.config.agent_name, self._agent.config.model.model_id)
        while True:
            try:
                value = await self._reader.read(f"{self._prompt_name}> ")
            except EOFError:
                self._renderer.text("Goodbye.")
                return
            except KeyboardInterrupt:
                continue
            routed = route_input(value)
            if routed.kind is InputKind.EMPTY:
                continue
            if routed.kind is InputKind.INVALID:
                self._renderer.text(routed.error, style="yellow")
                continue
            if routed.kind is InputKind.COMMAND:
                if await self._run_command(routed):
                    return
                continue
            await self._run_message(routed.text)

    async def _run_message(self, prompt: str) -> None:
        response = ""
        try:
            async for event in self._agent.stream(
                prompt,
                conversation_id=self._conversation_id,
            ):
                if event.type is EventType.RUN_STARTED:
                    self._active_run_id = event.run_id
                    self._conversation_id = event.conversation_id
                elif event.type is EventType.RESPONSE_DELTA:
                    delta = str(event.payload["text"])
                    response += delta
                    self._renderer.delta(delta)
                elif event.type is EventType.RESPONSE_COMPLETED:
                    usage = event.payload["usage"]
                    if not isinstance(usage, dict):
                        raise TypeError("Runtime usage payload is invalid")
                    self._input_tokens += int(usage["input_tokens"])
                    self._output_tokens += int(usage["output_tokens"])
            self._renderer.end_response()
            self._history.append(Exchange(prompt, response))
        except KeyboardInterrupt:
            if self._active_run_id is not None:
                self._agent.cancel(self._active_run_id)
            self._renderer.end_response()
            self._renderer.text("Run cancelled. The shell is still active.", style="yellow")
        except RunCancelledError:
            self._renderer.end_response()
            self._renderer.text("Run cancelled. The shell is still active.", style="yellow")
        except TalosError as exc:
            self._renderer.end_response()
            self._renderer.text(f"{exc.code}: {exc}", style="red")
        finally:
            self._active_run_id = None

    async def _run_command(self, routed: RoutedInput) -> bool:
        command = routed.command
        commands_with_arguments = {
            _TOOLS_COMMAND,
            "/rag",
            "/why",
            "/history",
            _KNOWLEDGE_COMMAND,
            _SKILLS_COMMAND,
            _REPOSITORY_COMMAND,
        }
        if routed.arguments and command not in commands_with_arguments:
            self._renderer.text(f"{command} does not accept arguments yet.", style="yellow")
            return False
        if command == "/exit":
            self._renderer.text("Goodbye.")
            return True
        handlers: dict[str, Callable[[RoutedInput], Awaitable[None] | None]] = {
            "/new": self._command_new,
            "/history": self._command_history,
            "/clear": self._command_clear,
            "/status": self._command_status,
            "/config": self._command_config,
            "/usage": self._command_usage,
            "/trace": self._command_trace,
            "/why": self._command_why,
            "/help": self._command_help,
            "/ui": self._command_ui,
            _TOOLS_COMMAND: self._command_tools,
            _SKILLS_COMMAND: self._command_skills,
            _KNOWLEDGE_COMMAND: self._command_knowledge,
            "/rag": self._command_rag,
            _REPOSITORY_COMMAND: self._command_repository,
        }
        handler = handlers.get(command)
        if handler is not None:
            result = handler(routed)
            if inspect.isawaitable(result):
                await result
        return False

    async def _command_new(self, _: RoutedInput) -> None:
        conversation = await self._agent.new_conversation()
        self._conversation_id = conversation.id
        self._renderer.text("Started a new conversation.")

    async def _command_history(self, routed: RoutedInput) -> None:
        if routed.arguments:
            if len(routed.arguments) != 1:
                self._renderer.text(
                    "Use /history or /history CONVERSATION_ID to resume retained history."
                )
                return
            try:
                conversation = await self._agent.get_conversation(routed.arguments[0])
                self._conversation_id = conversation.id
                self._renderer.text(
                    f"Selected conversation {conversation.id}. Use /why to inspect it."
                )
            except TalosError as exc:
                self._renderer.text(str(exc), style="yellow")
            return
        self._render_history()

    def _command_clear(self, _: RoutedInput) -> None:
        self._renderer.clear()

    def _command_status(self, _: RoutedInput) -> None:
        self._renderer.table(
            "Status",
            (
                ("Runtime", "local SDK"),
                ("Conversation", self._conversation_id or "not started"),
                ("Provider", self._agent.config.provider.display_name),
                ("Model", self._agent.config.model.model_id),
            ),
        )

    def _command_config(self, _: RoutedInput) -> None:
        self._renderer.table("Configuration", safe_configuration_rows(self._agent.config.raw))

    async def _command_usage(self, _: RoutedInput) -> None:
        usage = await self._agent.usage_report()
        estimated_cost = usage.get("estimated_cost")
        currency = usage.get("currency")
        cost = (
            f"{estimated_cost} {currency}" if estimated_cost is not None and currency else "unknown"
        )
        self._renderer.table(
            "Workspace usage",
            (
                ("Runs", str(usage.get("runs", 0))),
                ("Model calls", str(usage.get("model_calls", 0))),
                ("Input tokens", str(usage.get("input_tokens", 0))),
                ("Output tokens", str(usage.get("output_tokens", 0))),
                ("Retrieval requests", str(usage.get("retrieval_requests", 0))),
                ("Tool executions", str(usage.get("tool_executions", 0))),
                ("Estimated cost", cost),
            ),
        )

    async def _command_trace(self, _: RoutedInput) -> None:
        trace = await self._agent.trace()
        if trace is None:
            self._renderer.text("No completed or active run trace is available.")
            return
        error = trace.get("error")
        self._renderer.table(
            "Latest run trace",
            (
                ("Run", str(trace["run_id"])),
                ("Trace", str(trace["trace_id"])),
                ("State", str(trace["state"])),
                ("Duration", f"{trace['duration_ms']} ms"),
                ("Retrieval results", str(trace["retrieval_count"])),
                ("Tool calls", str(trace["tool_calls"])),
                ("Retries", str(trace["retries"])),
                ("Error", str(error.get("code")) if isinstance(error, dict) else "none"),
            ),
        )
        self._renderer.table(
            "Trace spans",
            [
                (str(span["name"]), f"{span['duration_ms']} ms · {span['status']}")
                for span in trace["spans"]
            ],
        )

    async def _command_why(self, routed: RoutedInput) -> None:
        try:
            offset = parse_why_offset(routed.arguments)
            if self._conversation_id is None:
                self._renderer.text(
                    "There are no previous Talos responses to inspect in this conversation."
                )
                return
            report = await self._agent.why_report(self._conversation_id, offset)
            self._renderer.table(
                "TALOS - DECISION AUTOPSY",
                [
                    (
                        "Target response",
                        f"#{report.target.response_number} ({report.target.offset})",
                    ),
                    ("Message", report.target.message_id),
                    ("Run", report.target.run_id or "Unavailable"),
                ],
            )
            for section in report.sections:
                self._renderer.table(
                    section.title,
                    [
                        ("Evidence", section.status),
                        *(("Observed", fact) for fact in section.facts),
                        *(
                            (str(key), str(value))
                            for record in section.records
                            for key, value in record.items()
                        ),
                    ],
                )
            for limitation in report.limitations:
                self._renderer.text(limitation)
        except TalosError as exc:
            self._renderer.text(str(exc), style="yellow")

    def _command_help(self, _: RoutedInput) -> None:
        self._render_help()

    async def _command_ui(self, _: RoutedInput) -> None:
        if self._ui_opener is None:
            self._renderer.text("The local Web UI is unavailable in this runtime.")
            return
        await self._ui_opener()
        self._renderer.text("Opened the local Web UI.")

    async def _command_tools(self, routed: RoutedInput) -> None:
        if not routed.arguments:
            self._renderer.table(
                "Available tools", [(name, "enabled") for name in self._agent.list_tools()]
            )
            return
        if len(routed.arguments) < 3 or routed.arguments[0] != "call":
            self._renderer.text("Use /tools or /tools call NAME JSON_OBJECT")
            return
        try:
            arguments = json.loads(routed.raw_arguments.split(maxsplit=2)[2])
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            result = await self._agent.call_tool(routed.arguments[1], arguments)
            self._renderer.text(json.dumps(result, ensure_ascii=False))
        except (ValueError, TalosError) as exc:
            self._renderer.text(str(exc), style="red")

    async def _command_skills(self, routed: RoutedInput) -> None:
        if not routed.arguments:
            self._renderer.table(
                "Active skills", [(skill.id, skill.version) for skill in self._agent.list_skills()]
            )
            return
        valid = len(routed.arguments) == 2 and routed.arguments[0] in {"add", "enable", "disable"}
        if not valid:
            self._renderer.text("Use /skills [add PATH|enable ID|disable ID]")
            return
        if self._skill_updater is None:
            self._renderer.text("Skill management is unavailable in this runtime.")
            return
        try:
            skills = await self._skill_updater(routed.arguments[0], routed.arguments[1])
            self._renderer.text("Active skills: " + ", ".join(skill.id for skill in skills))
        except (TalosError, OSError) as exc:
            self._renderer.text(str(exc), style="red")

    async def _command_knowledge(self, routed: RoutedInput) -> None:
        if len(routed.arguments) == 2 and routed.arguments[0] == "add":
            await self._command_knowledge_add(routed.arguments[1])
            return
        if routed.arguments:
            self._renderer.text("Use /knowledge or /knowledge add PROJECT_FILE")
            return
        sources = await self._agent.list_knowledge()
        self._renderer.table(
            "Knowledge sources",
            [
                (
                    item["name"] if isinstance(item, dict) else item.name,
                    str(item["chunk_count"] if isinstance(item, dict) else item.chunk_count),
                )
                for item in sources
            ],
        )

    async def _command_knowledge_add(self, path: str) -> None:
        if self._knowledge_importer is None:
            self._renderer.text("Knowledge import requires the local service.")
            return
        try:
            job_id = await self._knowledge_importer(path)
            self._renderer.text(f"Knowledge ingestion queued: {job_id}")
        except (TalosError, OSError) as exc:
            self._renderer.text(str(exc), style="red")

    async def _command_rag(self, routed: RoutedInput) -> None:
        if not routed.arguments:
            self._renderer.text("Use /rag SEARCH_QUERY")
            return
        hits = await self._agent.search_knowledge(" ".join(routed.arguments))
        self._renderer.table(
            "Knowledge matches",
            [
                (
                    item["marker"] if isinstance(item, dict) else item.marker,
                    item["source_name"] if isinstance(item, dict) else item.chunk.source_name,
                )
                for item in hits
            ],
        )

    async def _command_repository(self, routed: RoutedInput) -> None:
        if self._repository_handler is None:
            self._renderer.text("Repository indexing requires the local service.")
            return
        try:
            self._renderer.text(await self._repository_handler(routed.arguments))
        except (TalosError, OSError) as exc:
            self._renderer.text(str(exc), style="red")

    def _render_help(self) -> None:
        descriptions = {
            "/help": "Show commands",
            "/exit": "Leave the shell; runtime state is preserved",
            "/new": "Start a conversation using current configuration",
            "/history": "Show this shell's completed exchanges",
            "/clear": "Clear the terminal",
            "/status": "Show runtime and conversation status",
            "/config": "Show safe resolved configuration",
            "/usage": "Show session token usage",
            _REPOSITORY_COMMAND: "Repository intelligence status",
            _KNOWLEDGE_COMMAND: "Knowledge status",
            "/rag": "Retrieval status",
            _TOOLS_COMMAND: "Tool status",
            _SKILLS_COMMAND: "Skill status",
            "/trace": "Trace status",
            "/why": "Inspect a previous response: /why or /why -N",
            "/ui": "Local Web UI status",
        }
        self._renderer.table("Commands", [(command, descriptions[command]) for command in COMMANDS])

    def _render_history(self) -> None:
        if not self._history:
            self._renderer.text("No completed exchanges in this shell.")
            return
        rows: Sequence[tuple[str, str]] = [
            (str(index), f"{item.prompt} → {item.response}")
            for index, item in enumerate(self._history, start=1)
        ]
        self._renderer.table("Session history", rows)
