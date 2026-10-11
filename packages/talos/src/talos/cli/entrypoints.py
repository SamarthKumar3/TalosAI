"""SDK scaffolding and command help without interactive CLI dependencies."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from talos.cli.arguments import _parser, _talos_parser
from talos.cli.scaffold import initialize_project
from talos.errors import TalosError


def _require_cli() -> None:
    if any(
        importlib.util.find_spec(name) is None for name in ("rich", "keyring", "prompt_toolkit")
    ):
        sys.stderr.write(
            "This command needs the terminal tools. Run: "
            "python -m pip install 'talos-runtime[cli]'\n"
        )
        raise SystemExit(2)


def talos_main() -> None:
    options = _talos_parser().parse_args(sys.argv[1:])
    if options.command == "init":
        try:
            destination = initialize_project(Path(options.directory))
        except (TalosError, OSError) as exc:
            sys.stderr.write(f"Talos command failed: {exc}\n")
            raise SystemExit(2) from exc
        print(f"SDK configuration created in {destination}")
        return
    _require_cli()
    from talos.cli.main import talos_main as run

    run()


def hi_main() -> None:
    _parser("hi").parse_args(sys.argv[1:])
    _require_cli()
    from talos.cli.main import hi_main as run

    run()
