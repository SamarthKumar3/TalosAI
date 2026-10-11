from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from rich.console import Console
from rich.table import Table


class ShellRenderer(Protocol):
    def welcome(self, agent_name: str, model: str) -> None: ...

    def text(self, value: str, *, style: str | None = None) -> None: ...

    def delta(self, value: str) -> None: ...

    def end_response(self) -> None: ...

    def clear(self) -> None: ...

    def table(self, title: str, rows: Sequence[tuple[str, str]]) -> None: ...


class RichShellRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def welcome(self, agent_name: str, model: str) -> None:
        self.console.print(f"{agent_name} is ready · {model}", style="bold cyan", markup=False)
        self.console.print("Type /help for commands or /exit to leave the shell.")

    def text(self, value: str, *, style: str | None = None) -> None:
        self.console.print(value, style=style, markup=False)

    def delta(self, value: str) -> None:
        self.console.print(value, end="", markup=False, soft_wrap=True)

    def end_response(self) -> None:
        self.console.print()

    def clear(self) -> None:
        self.console.clear()

    def table(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        table = Table(title=title, show_header=False)
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Value")
        for name, value in rows:
            table.add_row(name, value)
        self.console.print(table)


def safe_configuration_rows(raw: Mapping[str, Any]) -> list[tuple[str, str]]:
    provider = raw["provider"]
    model = raw["model"]
    pipeline = raw["pipeline"]
    if not isinstance(provider, Mapping):
        raise TypeError("Resolved provider configuration is invalid")
    if not isinstance(model, Mapping):
        raise TypeError("Resolved model configuration is invalid")
    if not isinstance(pipeline, Mapping):
        raise TypeError("Resolved pipeline configuration is invalid")
    return [
        ("Agent", str(raw["agent_name"])),
        ("Provider", str(provider["alias"])),
        ("Model alias", str(model["alias"])),
        ("Model", str(model["model_id"])),
        ("Pipeline", str(pipeline["id"])),
        ("Configuration", str(raw["version"])),
    ]
