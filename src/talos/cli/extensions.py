from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import yaml
from rich.console import Console

from talos.cli import phase3
from talos.errors import ConfigurationError
from talos.extensions import ExtensionManager
from talos.tools.declarative import manifest_callable
from talos.tools.runtime import ToolRegistry
from talos.tools.sandbox import run_python


def python_tree(source: bytes) -> ast.Module:
    try:
        return ast.parse(source)
    except (SyntaxError, UnicodeError) as exc:
        raise ConfigurationError("Tool file is not valid Python") from exc


def inspect_python(value: str) -> int:
    path = Path(value).expanduser().resolve()
    if not path.is_file() or path.suffix != ".py" or path.stat().st_size > 262144:
        raise ConfigurationError("Choose a Python file of at most 256 KiB")
    source = path.read_bytes()
    tree = python_tree(source)
    Console().print(
        yaml.safe_dump(
            {
                "sha256": hashlib.sha256(source).hexdigest(),
                "functions": [
                    item.name
                    for item in tree.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                ],
            }
        ),
        markup=False,
    )
    return 0


def run(name: str, arguments: str, *, approve: bool = False) -> int:
    from functools import partial

    context = phase3.load_context(require_provider=False, interactive=False)
    manager = ExtensionManager(context.root, context.environment)
    # Verify registry containment, manifest identities and reviewed Python bytes before invocation.
    manifest = manager.registered_tool(name)
    if manifest["kind"] == "python" and not approve:
        raise ConfigurationError("Review the tool and its exact arguments, then use --approve")
    if len(arguments.encode()) > 16384:
        raise ConfigurationError("Tool arguments exceed 16 KiB")
    try:
        values = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("Tool arguments must be a JSON object") from exc
    if not isinstance(values, dict):
        raise ConfigurationError("Tool arguments must be a JSON object")
    callback = manifest_callable(manifest, partial(run_python, context.root))
    validator = ToolRegistry({}, timeout_seconds=10, max_response_bytes=32768)
    validator.register(callback)
    checked = validator.validate_arguments(name, values)

    async def invoke() -> str:
        return await callback(**checked)

    Console().print(asyncio.run(invoke()), markup=False)
    return 0


def add(
    value: str,
    *,
    reviewed_python: bool = False,
    function: str | None = None,
    sha256: str | None = None,
) -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError("Tool file was not found")
    if path.suffix == ".py":
        if not reviewed_python or not function or not sha256:
            raise ConfigurationError(
                "Python registration needs --reviewed-python, --function and --sha256"
            )
        if (
            not path.is_relative_to(context.root.resolve() / "tools")
            or path.stat().st_size > 262144
        ):
            raise ConfigurationError(
                "Reviewed Python files must be under this project's tools folder"
            )
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != sha256:
            raise ConfigurationError("Python source does not match the reviewed SHA-256")
        tree = python_tree(source)
        selected = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == function
            ),
            None,
        )
        if (
            selected is None
            or selected.args.posonlyargs
            or selected.args.vararg
            or selected.args.kwarg
            or selected.args.defaults
            or selected.args.kwonlyargs
        ):
            raise ConfigurationError(
                "Use a typed function with named required parameters and no defaults"
            )
        types = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
        parameters = {}
        for argument in selected.args.args:
            if not isinstance(argument.annotation, ast.Name) or argument.annotation.id not in types:
                raise ConfigurationError(
                    "Python sandbox inputs require str, int, float or bool annotations"
                )
            parameters[argument.arg] = {"type": types[argument.annotation.id]}
        if not isinstance(selected.returns, ast.Name) or selected.returns.id != "str":
            raise ConfigurationError("Python sandbox tools must return str")
        content = yaml.safe_dump(
            {
                "schema_version": 1,
                "name": function,
                "description": ast.get_docstring(selected)
                or f"Run {function} in the Python sandbox",
                "kind": "python",
                "parameters": parameters,
                "python_file": path.relative_to(context.root).as_posix(),
                "function": function,
                "sha256": sha256,
            }
        )
    elif (
        not path.is_file()
        or path.suffix.lower() not in {".yaml", ".yml"}
        or path.stat().st_size > 32768
    ):
        raise ConfigurationError("Choose a YAML tool manifest of at most 32 KiB")
    else:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ConfigurationError("Tool manifest must be UTF-8") from exc
    # Register locally without instantiating a model provider or importing Python source.
    manager = ExtensionManager(context.root, context.environment)
    result = manager.add_tool(content=content, allow_python=reviewed_python)
    Console().print(f"Tool saved to {result['path']}. Original YAML retained.", markup=False)
    try:
        token = phase3.local_api_token(context, create=False)
        response = httpx.post(
            f"{phase3._local_api_base_url(context.environment)}/api/v1/extensions/tools/reload",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
    except (httpx.HTTPError, ConfigurationError):
        Console(stderr=True).print("Start or restart the API to load the saved tool.", markup=False)
    Console().print(
        "New conversations use the tool. Python tools require the separate Docker sandbox.",
        markup=False,
    )
    return 0
