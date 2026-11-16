"""Load installed plugin manifests and dynamically import their handlers."""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
from types import ModuleType

from app.models.plugin_schemas import PluginManifest


class PluginRuntimeError(Exception):
    """Raised when a plugin cannot be loaded or executed."""


_MODULE_CACHE: dict[str, ModuleType] = {}


def clear_cache() -> None:
    _MODULE_CACHE.clear()


def load_manifest(plugin_id: str, plugins_dir: Path) -> PluginManifest:
    manifest_path = plugins_dir / plugin_id / "plugin.json"
    if not manifest_path.exists():
        raise PluginRuntimeError(f"Plugin '{plugin_id}' is not installed")
    try:
        return PluginManifest.model_validate(json.loads(manifest_path.read_text()))
    except Exception as exc:
        raise PluginRuntimeError(f"Invalid manifest for '{plugin_id}': {exc}") from exc


def _load_module(plugin_id: str, plugins_dir: Path) -> ModuleType:
    manifest = load_manifest(plugin_id, plugins_dir)
    handler_path = plugins_dir / plugin_id / manifest.entry
    cache_key = f"{plugin_id}:{manifest.version}"
    cached = _MODULE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if not handler_path.exists():
        raise PluginRuntimeError(f"Handler '{manifest.entry}' missing for '{plugin_id}'")
    spec = importlib.util.spec_from_file_location(f"heym_plugin_{plugin_id}", handler_path)
    if spec is None or spec.loader is None:
        raise PluginRuntimeError(f"Cannot load handler for '{plugin_id}'")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PluginRuntimeError(f"Error importing '{plugin_id}': {exc}") from exc
    _MODULE_CACHE[cache_key] = module
    return module


def has_function(plugin_id: str, plugins_dir: Path, function_name: str) -> bool:
    """Whether the package's handler module defines this function."""
    return callable(getattr(_load_module(plugin_id, plugins_dir), function_name, None))


def accepted_kwargs(fn: object, optional: dict) -> dict:
    """The optional keyword arguments a function takes: by name, or all of them with **kwargs.

    Plugin API 2 adds arguments (``inputs_by_key``, ``event``) that an older handler doesn't
    declare; passing them anyway would break it with a TypeError.
    """
    try:
        parameters = inspect.signature(fn).parameters.values()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
        return dict(optional)
    names = {p.name for p in parameters}
    return {key: value for key, value in optional.items() if key in names}


def call_handler(
    plugin_id: str,
    plugins_dir: Path,
    function_name: str,
    kwargs: dict,
    optional_kwargs: dict | None = None,
) -> object:
    module = _load_module(plugin_id, plugins_dir)
    fn = getattr(module, function_name, None)
    if not callable(fn):
        raise PluginRuntimeError(f"Plugin '{plugin_id}' has no '{function_name}' function")
    if optional_kwargs:
        kwargs = {**kwargs, **accepted_kwargs(fn, optional_kwargs)}
    try:
        return fn(**kwargs)
    except Exception as exc:
        raise PluginRuntimeError(f"Plugin '{plugin_id}' {function_name} failed: {exc}") from exc
