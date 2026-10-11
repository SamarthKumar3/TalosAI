from __future__ import annotations

import difflib
import shlex
from dataclasses import dataclass
from enum import StrEnum

COMMANDS = (
    "/help",
    "/exit",
    "/new",
    "/history",
    "/clear",
    "/status",
    "/config",
    "/repo",
    "/knowledge",
    "/rag",
    "/tools",
    "/skills",
    "/trace",
    "/why",
    "/usage",
    "/ui",
)


class InputKind(StrEnum):
    EMPTY = "empty"
    MESSAGE = "message"
    COMMAND = "command"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class RoutedInput:
    kind: InputKind
    text: str = ""
    command: str = ""
    arguments: tuple[str, ...] = ()
    raw_arguments: str = ""
    error: str = ""


def route_input(value: str) -> RoutedInput:
    stripped = value.lstrip()
    if not stripped:
        return RoutedInput(InputKind.EMPTY)
    if stripped.startswith("//"):
        return RoutedInput(InputKind.MESSAGE, text=stripped[1:])
    if not stripped.startswith("/"):
        return RoutedInput(InputKind.MESSAGE, text=stripped)
    try:
        parts = shlex.split(stripped, posix=True)
    except ValueError as exc:
        return RoutedInput(InputKind.INVALID, error=f"Cannot parse command: {exc}")
    command = parts[0].lower()
    if command not in COMMANDS:
        close = difflib.get_close_matches(command, COMMANDS, n=1)
        suggestion = f" Did you mean {close[0]}?" if close else ""
        return RoutedInput(
            InputKind.INVALID,
            error=f"Unknown command {command}.{suggestion} Use /help to list commands.",
        )
    return RoutedInput(
        InputKind.COMMAND,
        command=command,
        arguments=tuple(parts[1:]),
        raw_arguments=stripped[len(parts[0]) :].lstrip(),
    )
