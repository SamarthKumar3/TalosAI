from __future__ import annotations

import getpass
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent

from talos.cli.routing import COMMANDS


def resolve_prompt_name(
    environment: Mapping[str, str],
    *,
    project_root: Path,
) -> str:
    configured = environment.get("TALOS_PROMPT_NAME", "").strip()
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.name"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            shell=False,
        )
        git_name = result.stdout.strip()
        if result.returncode == 0 and git_name:
            return git_name
    except (OSError, subprocess.SubprocessError):
        pass
    return getpass.getuser().strip() or os.environ.get("USERNAME", "").strip() or "user"


class PromptToolkitReader:
    def __init__(self, *, history_path: Path, history_enabled: bool) -> None:
        if history_enabled:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history: History = FileHistory(str(history_path))
        else:
            history = InMemoryHistory()
        bindings = KeyBindings()

        @bindings.add("enter")
        def accept_input(event: KeyPressEvent) -> None:
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def insert_newline(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        self._session: PromptSession[str] = PromptSession(
            history=history,
            completer=WordCompleter(COMMANDS, sentence=True),
            complete_while_typing=False,
            enable_open_in_editor=False,
            key_bindings=bindings,
            multiline=True,
            prompt_continuation="... ",
        )

    async def read(self, prompt: str) -> str:
        return await self._session.prompt_async(prompt)
