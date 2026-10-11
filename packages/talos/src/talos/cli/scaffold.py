from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from talos.errors import ConfigurationError


def initialize_project(destination: Path) -> Path:
    """Create a portable SDK configuration without overwriting existing files."""
    destination = destination.resolve()
    templates = files("talos").joinpath("templates")
    if not templates.is_dir():
        # Editable checkouts use the same reviewed files that the wheel bundles.
        templates = Path(__file__).resolve().parents[5]
    planned: list[tuple[Path, bytes]] = []

    def collect(source: object, relative: Path) -> None:
        # Traversable and Path share these operations; recurse through the resource API.
        from importlib.resources.abc import Traversable

        if not isinstance(source, Traversable):
            raise ConfigurationError("SDK templates are unavailable. Reinstall Talos.")
        if source.is_dir():
            for child in source.iterdir():
                collect(child, relative / child.name)
        else:
            planned.append((destination / relative, source.read_bytes()))

    for directory in ("config", "skills"):
        collect(templates.joinpath(directory), Path(directory))
    for source, target in ((".env.example", ".env.example"), ("talos.example.yaml", "talos.yaml")):
        planned.append((destination / target, templates.joinpath(source).read_bytes()))
    if any(not path.resolve().is_relative_to(destination) for path, _ in planned):
        raise ConfigurationError("Project paths must remain inside the destination directory")
    conflicts = [str(path.relative_to(destination)) for path, _ in planned if path.exists()]
    if conflicts:
        raise ConfigurationError("Existing files would be overwritten: " + ", ".join(conflicts))
    for path, content in planned:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(content)
    return destination.resolve()
