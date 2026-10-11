"""Bounded, non-executable tool manifests shared by CLI, API and SDK."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import string
from collections.abc import Awaitable, Callable
from typing import Any

import yaml

from talos.errors import ConfigurationError
from talos.tools.builtins import calculator
from talos.tools.runtime import tool

TYPES = {"string": str, "number": float, "integer": int, "boolean": bool}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def parse_manifest(content: str) -> dict[str, Any]:
    try:
        size = len(content.encode())
    except UnicodeError as exc:
        raise ConfigurationError("Tool manifest must contain valid Unicode") from exc
    if size > 32768:
        raise ConfigurationError("Tool manifest exceeds 32 KiB")
    try:
        value = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConfigurationError("Tool manifest is not valid YAML") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Tool manifest must be a mapping")
    allowed = {
        "schema_version",
        "name",
        "description",
        "kind",
        "parameters",
        "template",
        "python_file",
        "function",
        "sha256",
    }
    if set(value) - allowed or value.get("schema_version") != 1:
        raise ConfigurationError("Unknown tool fields or schema version")
    if not isinstance(value.get("name"), str) or not IDENTIFIER.fullmatch(value["name"]):
        raise ConfigurationError("Tool name must be a simple identifier")
    description = value.get("description")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1000:
        raise ConfigurationError("A description of up to 1,000 characters is required")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in {"template", "calculator", "python"}:
        raise ConfigurationError("Tool kind must be template, calculator or python")
    parameters = value.get("parameters")
    if not isinstance(parameters, dict) or len(parameters) > 16:
        raise ConfigurationError("Provide at most 16 named parameters")
    for name, parameter in parameters.items():
        if (
            not isinstance(name, str)
            or not IDENTIFIER.fullmatch(name)
            or not isinstance(parameter, dict)
            or set(parameter) != {"type"}
            or not isinstance(parameter["type"], str)
            or parameter["type"] not in TYPES
        ):
            raise ConfigurationError("Parameters require a simple name and a supported type")
        if kind == "calculator" and parameter["type"] not in {"number", "integer"}:
            raise ConfigurationError("Calculator parameters must be numeric")
    if kind == "python":
        if "template" in value or not all(
            isinstance(value.get(key), str) for key in ("python_file", "function", "sha256")
        ):
            raise ConfigurationError("Python tools require file, function and reviewed SHA-256")
        if not IDENTIFIER.fullmatch(value["function"]) or not re.fullmatch(
            r"[a-f0-9]{64}", value["sha256"]
        ):
            raise ConfigurationError("Invalid Python function or SHA-256")
    else:
        template = value.get("template")
        if (
            set(value) & {"python_file", "function", "sha256"}
            or not isinstance(template, str)
            or len(template) > 4000
        ):
            raise ConfigurationError("Provide a template of up to 4,000 characters")
        try:
            for _, field, spec, conversion in string.Formatter().parse(template):
                if field is not None and (field not in parameters or spec or conversion):
                    raise ConfigurationError("Templates accept plain parameter names only")
        except ValueError as exc:
            raise ConfigurationError("Invalid template placeholders") from exc
        if kind == "calculator":
            expression = template.format_map(dict.fromkeys(parameters, 1))
            try:
                tree = ast.parse(expression, mode="eval")
                nodes = list(ast.walk(tree))
                allowed_nodes = (
                    ast.Expression,
                    ast.BinOp,
                    ast.UnaryOp,
                    ast.Constant,
                    ast.Add,
                    ast.Sub,
                    ast.Mult,
                    ast.Div,
                    ast.UAdd,
                    ast.USub,
                )
                if (
                    len(expression) > 200
                    or len(nodes) > 100
                    or any(not isinstance(node, allowed_nodes) for node in nodes)
                ):
                    raise ValueError("Unsupported operation")
                if any(
                    isinstance(node, ast.Constant)
                    and (not isinstance(node.value, (int, float)) or isinstance(node.value, bool))
                    for node in nodes
                ):
                    raise ValueError("Numeric constants required")
            except (SyntaxError, ValueError) as exc:
                raise ConfigurationError(
                    "Calculator templates accept bounded arithmetic only"
                ) from exc
    return value


def manifest_callable(
    manifest: dict[str, Any],
    python_runner: Callable[[dict[str, Any], dict[str, Any]], Awaitable[str]] | None = None,
) -> Callable[..., Awaitable[str]]:
    async def invoke(**arguments: Any) -> str:
        if len(json.dumps(arguments, allow_nan=False).encode()) > 16384:
            raise ValueError("Tool arguments exceed 16 KiB")
        if manifest["kind"] == "python":
            if python_runner is None:
                raise ConfigurationError(
                    "Python sandbox is unavailable; host execution is disabled"
                )
            return await python_runner(manifest, arguments)
        rendered = str(manifest["template"]).format_map(arguments)
        if len(rendered.encode()) > 32768:
            raise ValueError("Tool output exceeds 32 KiB")
        return str(calculator(rendered)) if manifest["kind"] == "calculator" else rendered

    annotations = {name: TYPES[value["type"]] for name, value in manifest["parameters"].items()}
    invoke.__annotations__ = {**annotations, "return": str}
    signature = inspect.Signature(
        [
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)
            for name, annotation in annotations.items()
        ],
        return_annotation=str,
    )
    invoke.__dict__["__signature__"] = signature
    invoke.__name__ = manifest["name"]
    invoke.__doc__ = manifest["description"]
    invoke.__dict__["__talos_manifest_hash__"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()
    return tool(invoke)
