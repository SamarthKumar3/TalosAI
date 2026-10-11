from __future__ import annotations

import asyncio
import builtins
import hashlib
import importlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import TypeAdapter, ValidationError

from talos.errors import ConfigurationError, TalosError

Approval = Callable[[str, str], Awaitable[bool]]


class ToolError(TalosError):
    code = "tool_error"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    entrypoint: str
    timeout_seconds: float
    max_response_bytes: int
    side_effect: bool


def tool[F: Callable[..., Any]](function: F) -> F:
    """Mark an explicitly registered, typed SDK callable as a Talos tool."""
    if not function.__doc__ or not function.__doc__.strip():
        raise ConfigurationError("A tool callable requires a description")
    _validate_signature(function)
    function.__dict__["__talos_tool__"] = True
    return function


def _validate_signature(function: Callable[..., Any]) -> None:
    hints = get_type_hints(function)
    signature = inspect.signature(function)
    for parameter in signature.parameters.values():
        if parameter.kind not in {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY}:
            raise ConfigurationError("Tool parameters must be named arguments")
        annotation = hints.get(parameter.name)
        if annotation is None:
            raise ConfigurationError(f"Tool parameter {parameter.name} needs a type annotation")
        _validate_json_type(annotation)
        TypeAdapter(annotation).json_schema()
        if parameter.default is not inspect.Parameter.empty:
            try:
                TypeAdapter(annotation).validate_python(parameter.default, strict=True)
                json.dumps(parameter.default, allow_nan=False)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ConfigurationError("Tool default is not JSON-compatible") from exc
    if "return" not in hints:
        raise ConfigurationError("A tool return type is required")
    return_annotation = hints["return"]
    _validate_json_type(return_annotation)
    TypeAdapter(return_annotation).json_schema()


def _validate_json_type(annotation: Any) -> None:
    if annotation in {str, int, float, bool, type(None)}:
        return
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {list, tuple} and arguments:
        if origin is tuple and len(arguments) != 2:
            raise ConfigurationError("Tool tuple types must be homogeneous")
        _validate_json_type(arguments[0])
        return
    if origin is dict and len(arguments) == 2 and arguments[0] is str:
        _validate_json_type(arguments[1])
        return
    if origin in {Union, UnionType} and arguments:
        for item in arguments:
            _validate_json_type(item)
        return
    raise ConfigurationError("Tool type is not explicitly JSON-compatible")


class ToolRegistry:
    def __init__(
        self,
        definitions: Mapping[str, ToolDefinition],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> None:
        self._definitions = dict(definitions)
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._functions: dict[str, Callable[..., Any]] = {}
        self._model_enabled = set(self._definitions)
        self._custom_side_effects: dict[str, bool] = {}
        for name, definition in self._definitions.items():
            module_name, separator, symbol = definition.entrypoint.partition(":")
            if not separator or module_name != "talos.tools.builtins":
                raise ConfigurationError("Shipped tool entrypoint is not allowlisted")
            try:
                function = getattr(importlib.import_module(module_name), symbol)
            except (ImportError, AttributeError) as exc:
                raise ConfigurationError(f"Tool adapter for {name} is unavailable") from exc
            _validate_signature(function)
            self._functions[name] = function

    def fork(self) -> ToolRegistry:
        copied = ToolRegistry(
            self._definitions,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
        )
        copied._functions = dict(self._functions)
        copied._custom_side_effects = dict(self._custom_side_effects)
        copied._model_enabled = set(self._model_enabled)
        return copied

    def register(
        self,
        function: Callable[..., Any],
        *,
        name: str | None = None,
        side_effect: bool = True,
        model_enabled: bool = False,
    ) -> None:
        if not getattr(function, "__talos_tool__", False):
            raise ConfigurationError("SDK tools must use @tool")
        selected = name or function.__name__
        if selected in self._functions:
            raise ConfigurationError(f"Duplicate tool name: {selected}")
        if model_enabled and side_effect:
            raise ConfigurationError("Side-effectful SDK tools cannot be model enabled")
        _validate_signature(function)
        self._functions[selected] = function
        self._custom_side_effects[selected] = side_effect
        if model_enabled:
            self._model_enabled.add(selected)

    def list(self) -> tuple[str, ...]:
        return tuple(sorted(self._functions))

    def requires_approval(self, name: str) -> bool:
        if name not in self._functions:
            raise ToolError(f"Unknown tool: {name}")
        definition = self._definitions.get(name)
        return bool((definition and definition.side_effect) or self._custom_side_effects.get(name))

    def version(self, name: str) -> str:
        if name not in self._functions:
            raise ToolError(f"Unknown tool: {name}")
        definition = self._definitions.get(name)
        if definition is not None:
            return definition.version
        function = self._functions[name]
        manifest_hash = getattr(function, "__talos_manifest_hash__", None)
        if isinstance(manifest_hash, str):
            return manifest_hash
        try:
            implementation = inspect.getsource(function)
        except (OSError, TypeError):
            code = getattr(function, "__code__", None)
            implementation = code.co_code.hex() if code is not None else repr(type(function))
        return hashlib.sha256(
            (
                f"{function.__module__}:{function.__qualname__}:"
                f"{inspect.signature(function)}:{inspect.getdoc(function)}:{implementation}"
            ).encode()
        ).hexdigest()

    def validate_arguments(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        function = self._functions.get(name)
        if function is None:
            raise ToolError(f"Unknown tool: {name}")
        try:
            encoded = json.dumps(arguments, sort_keys=True, allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            raise ToolError("Tool arguments must be JSON-compatible") from exc
        if len(encoded) > self._max_response_bytes:
            raise ToolError("Tool arguments exceed their configured limit")
        try:
            bound = inspect.signature(function).bind(**arguments)
            hints = get_type_hints(function)
            return {
                key: TypeAdapter(hints[key]).validate_python(value, strict=True)
                for key, value in bound.arguments.items()
            }
        except (TypeError, ValidationError) as exc:
            raise ToolError("Tool arguments failed validation") from exc

    def model_specs(self) -> builtins.list[dict[str, Any]]:
        specs: builtins.list[dict[str, Any]] = []
        for name, function in sorted(self._functions.items()):
            if name not in self._model_enabled or self.requires_approval(name):
                continue
            hints = get_type_hints(function)
            signature = inspect.signature(function)
            properties = {
                parameter.name: TypeAdapter(hints[parameter.name]).json_schema()
                for parameter in signature.parameters.values()
            }
            required = [
                parameter.name
                for parameter in signature.parameters.values()
                if parameter.default is inspect.Parameter.empty
            ]
            definition = self._definitions.get(name)
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": (
                            definition.description
                            if definition
                            else inspect.getdoc(function) or name
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": required,
                            "additionalProperties": False,
                        },
                    },
                }
            )
        return specs

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        approval: Approval | None = None,
    ) -> Any:
        function = self._functions.get(name)
        if function is None:
            raise ToolError(f"Unknown tool: {name}")
        validated = self.validate_arguments(name, arguments)
        encoded_arguments = _encoded_arguments(arguments)
        if len(encoded_arguments) > self._max_response_bytes:
            raise ToolError("Tool arguments exceed their configured limit")
        definition = self._definitions.get(name)
        side_effect = bool(definition and definition.side_effect) or bool(
            self._custom_side_effects.get(name)
        )
        await _require_approval(name, encoded_arguments, side_effect, approval)
        hints = get_type_hints(function)
        timeout = definition.timeout_seconds if definition else self._timeout_seconds
        maximum = definition.max_response_bytes if definition else self._max_response_bytes
        result, serialized = await _execute_tool(function, validated, hints["return"], timeout)
        if len(serialized.encode()) > maximum:
            raise ToolError("Tool output exceeds its configured limit")
        return result


def _encoded_arguments(arguments: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ToolError("Tool arguments must be JSON-compatible") from exc


async def _require_approval(
    name: str, encoded_arguments: bytes, side_effect: bool, approval: Approval | None
) -> None:
    if not side_effect:
        return
    digest = hashlib.sha256(encoded_arguments).hexdigest()
    if approval is None or not await approval(name, digest):
        raise ToolError("Tool approval was not granted")


async def _execute_tool(
    function: Callable[..., Any],
    arguments: Mapping[str, Any],
    return_type: Any,
    timeout_seconds: float,
) -> tuple[Any, str]:
    try:
        async with asyncio.timeout(timeout_seconds):
            if inspect.iscoroutinefunction(function):
                result = await function(**arguments)
            else:
                result = await asyncio.to_thread(function, **arguments)
        result = TypeAdapter(return_type).validate_python(result, strict=True)
        return result, json.dumps(result, allow_nan=False)
    except TimeoutError as exc:
        raise ToolError("Tool execution timed out") from exc
    except (ValidationError, TypeError, ValueError) as exc:
        raise ToolError("Tool output failed validation") from exc
    except Exception as exc:
        raise ToolError("Tool execution failed") from exc
