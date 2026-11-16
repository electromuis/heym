from __future__ import annotations

from app.models.plugin_schemas import PluginManifest, PluginNodeDef
from app.services import plugin_context, plugin_credentials, plugin_loader, plugin_store
from app.services.node_execution.base import NodeExecutionContext
from app.services.node_execution.nodes.plugin_node import (
    _resolve_config,
    _safe_ctx,
    resolve_node_def,
)

#: ``triggered_by`` of a run a plugin trigger started (app/services/plugin_triggers.py).
PLUGIN_TRIGGER_SOURCE = "plugin"


def execute(ctx: NodeExecutionContext) -> object:
    """Execute a `pluginTrigger` node by calling its handler function."""
    plugin_id = ctx.node_data.get("pluginId")
    if not plugin_id:
        raise ValueError("Plugin trigger node is missing pluginId")
    manifest, node_def = resolve_node_def(ctx, "trigger")
    if manifest.plugin_api >= 2:
        return _execute_api2(ctx, manifest, node_def)
    config = _resolve_config(ctx)
    result = plugin_loader.call_handler(
        plugin_id,
        plugin_store.plugins_root(),
        node_def.function,
        {"config": config, "ctx": _safe_ctx(ctx)},
    )
    return result if isinstance(result, dict) else {"value": result}


def _event(ctx: NodeExecutionContext) -> object:
    """The event a plugin trigger started this run with, when it was this node."""
    initial = ctx.node_data.get("_initial_inputs") or {}
    if not isinstance(initial, dict) or initial.get("triggered_by") != PLUGIN_TRIGGER_SOURCE:
        return None
    if initial.get("trigger_node_id") not in (None, ctx.node_id):
        return None
    return initial.get("event")


def _execute_api2(
    ctx: NodeExecutionContext, manifest: PluginManifest, node_def: PluginNodeDef
) -> dict:
    """An API 2 trigger: the event is its output, or the trigger function shapes it; exits apply."""
    raw_config = ctx.node_data.get("config", {}) or {}
    secrets: list[str] = []
    config = plugin_context.resolve_config(ctx, node_def.fields, raw_config, manifest.id, secrets)
    event = _event(ctx)
    root = plugin_store.plugins_root()
    if node_def.source is None or node_def.function:
        result = plugin_loader.call_handler(
            manifest.id,
            root,
            node_def.function,
            {"config": config, "ctx": plugin_context.PluginContext(ctx, manifest, node_def)},
            optional_kwargs={"event": event},
        )
    else:
        result = event if event is not None else {}
    output = dict(result) if isinstance(result, dict) else {"value": result}
    if node_def.has_exits():
        plugin_context.route_exit(ctx, node_def, config, output)
    return plugin_credentials.mask_output(output, secrets)
