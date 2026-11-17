from __future__ import annotations

import time
from importlib import import_module

from app.models.plugin_schemas import PluginManifest, PluginNodeDef
from app.services import (
    plugin_context,
    plugin_credentials,
    plugin_loader,
    plugin_loops,
    plugin_store,
    plugin_waits,
)
from app.services.node_execution.base import NodeExecutionContext


def _resolve_config(ctx: NodeExecutionContext) -> dict:
    raw_config = ctx.node_data.get("config", {}) or {}
    resolved: dict = {}
    for key, value in raw_config.items():
        if isinstance(value, str) and value.startswith("$"):
            resolved[key] = ctx.executor.resolve_expression(
                value, ctx.inputs, ctx.node_id, preserve_type=True
            )
        else:
            resolved[key] = value
    return resolved


def _safe_ctx(ctx: NodeExecutionContext) -> dict:
    return {"node_id": ctx.node_id, "node_label": ctx.node_label}


def resolve_node_def(
    ctx: NodeExecutionContext, expected_kind: str
) -> tuple[PluginManifest, PluginNodeDef]:
    """Find the package manifest and the plugin node referenced by this node.

    Uses `pluginNodeKey` when present; otherwise falls back to the first node of
    the expected kind in the package manifest.
    """
    plugin_id = ctx.node_data.get("pluginId")
    if not plugin_id:
        raise ValueError("Plugin node is missing pluginId")
    manifest = plugin_loader.load_manifest(plugin_id, plugin_store.plugins_root())
    node_key = ctx.node_data.get("pluginNodeKey")
    node_def = manifest.get_node(node_key) if node_key else None
    if node_def is None:
        node_def = next((n for n in manifest.resolved_nodes() if n.kind == expected_kind), None)
    if node_def is None:
        raise ValueError(f"Plugin '{plugin_id}' has no '{expected_kind}' node")
    return manifest, node_def


def resolve_node_function(ctx: NodeExecutionContext, expected_kind: str) -> str:
    """The handler function of the plugin node referenced by this node."""
    return resolve_node_def(ctx, expected_kind)[1].function


def execute(ctx: NodeExecutionContext) -> object:
    """Execute a `plugin` (action) node by calling its handler function."""
    plugin_id = ctx.node_data.get("pluginId")
    if not plugin_id:
        raise ValueError("Plugin node is missing pluginId")
    manifest, node_def = resolve_node_def(ctx, "action")
    if manifest.plugin_api >= 2:
        return _execute_api2(ctx, manifest, node_def)
    function_name = node_def.function
    config = _resolve_config(ctx)
    result = plugin_loader.call_handler(
        plugin_id,
        plugin_store.plugins_root(),
        function_name,
        {"inputs": ctx.inputs, "config": config, "ctx": _safe_ctx(ctx)},
    )
    return result if isinstance(result, dict) else {"value": result}


def _take_resume(ctx: NodeExecutionContext) -> dict | None:
    """The body that resumed this node after a wait, taken once so a later pass sees None."""
    executor = ctx.executor
    resume_context = getattr(executor, "hitl_resume_context", None)
    if not isinstance(resume_context, dict):
        return None
    with executor.lock:
        resume = resume_context.pop(ctx.node_id, None)
    return resume if isinstance(resume, dict) else None


def _waiting_result(
    ctx: NodeExecutionContext,
    manifest: PluginManifest,
    node_def: PluginNodeDef,
    config: dict,
    request: plugin_waits.PluginWaitRequested,
) -> object:
    # A loop's pass is woken by a timeout like any wait, and still answers with step or done.
    if (
        request.timeout is not None
        and not node_def.loop
        and "timedOut" not in node_def.exit_keys(config)
    ):
        raise ValueError(
            f"Plugin node {node_def.key} waits with a timeout but declares no timedOut exit"
        )
    node_result_cls = import_module("app.services.workflow_executor").NodeResult
    return node_result_cls(
        node_id=ctx.node_id,
        node_label=ctx.node_label,
        node_type=ctx.node_type,
        status="pending",
        output=plugin_waits.pending_output(request),
        execution_time_ms=(time.time() - ctx.start_time) * 1000,
        metadata=plugin_waits.pending_metadata(manifest.id, request),
    )


def _execute_api2(
    ctx: NodeExecutionContext, manifest: PluginManifest, node_def: PluginNodeDef
) -> object:
    """An API 2 action: full config resolution, the `ctx` object, named inputs, exits and waits."""
    raw_config = ctx.node_data.get("config", {}) or {}
    secrets: list[str] = []
    config = plugin_context.resolve_config(ctx, node_def.fields, raw_config, manifest.id, secrets)
    loop = plugin_loops.begin_pass(ctx) if node_def.loop else None
    plugin_ctx = plugin_context.PluginContext(
        ctx, manifest, node_def, resume=_take_resume(ctx), loop=loop
    )
    try:
        result = plugin_loader.call_handler(
            manifest.id,
            plugin_store.plugins_root(),
            node_def.function,
            {"inputs": ctx.inputs, "config": config, "ctx": plugin_ctx},
            optional_kwargs={"inputs_by_key": plugin_context.inputs_by_key(ctx, node_def)},
        )
    except plugin_waits.PluginWaitRequested as request:
        return _waiting_result(ctx, manifest, node_def, config, request)
    except plugin_loader.PluginRuntimeError as exc:
        if not secrets:
            raise
        raise plugin_loader.PluginRuntimeError(
            plugin_credentials.mask_text(str(exc), secrets)
        ) from None
    output = result if isinstance(result, dict) else {"value": result}
    if loop is not None:
        plugin_loops.finish_pass(ctx, node_def.key, loop, output)
    elif node_def.has_exits():
        plugin_context.route_exit(ctx, node_def, config, output)
    return plugin_credentials.mask_output(output, secrets)
