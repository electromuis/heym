"""Plugin API 2 triggers that hear events by themselves (docs/plugin-api-2.md, "Triggers").

A trigger node whose manifest node has ``source: "push"`` gets its own URL
(``POST /api/plugins/{pluginId}/hooks/{hookId}``, in ``app/api/plugins.py``); the package's
``receive`` turns a request into events. One with ``source: "loop"`` runs the package's
``listen(config, emit, ctx)`` in a thread on the leader instance, one per active workflow node,
as the IMAP trigger polls. Both start one run per event that passes the node's
"Only start when" condition (``data.onlyStartWhen``, over ``$event``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, cast, select

from app.db.models import Plugin, Workflow
from app.db.session import async_session_maker
from app.models.plugin_schemas import PluginManifest, PluginNodeDef
from app.services import plugin_context, plugin_loader, plugin_store
from app.services.distributed_lock import lock_service
from app.services.node_execution.base import NodeExecutionContext

logger = logging.getLogger("heym.plugins.triggers")

TRIGGER_SOURCE = "plugin"
HOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_HOOK_BODY_BYTES = 1024 * 1024
RECEIVE_TIMEOUT_SECONDS = 10.0
_RESTART_DELAYS = (5, 10, 20, 40, 80, 160, 300)


class TriggerContext(Mapping):
    """The ``ctx`` of ``receive`` and ``listen``: who is asking, plus ``stopping`` for a listener."""

    def __init__(
        self,
        plugin_id: str,
        node_def: PluginNodeDef,
        workflow_id: uuid.UUID,
        node_id: str,
        stopping: threading.Event | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.node_key = node_def.key
        self.workflow_id = str(workflow_id)
        self.node_id = node_id
        self.stopping = stopping or threading.Event()

    def _as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "node_key": self.node_key,
            "workflow_id": self.workflow_id,
            "node_id": self.node_id,
        }

    def __getitem__(self, key: str) -> Any:
        return self._as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._as_dict())

    def __len__(self) -> int:
        return len(self._as_dict())

    def log(self, text: str) -> None:
        logger.info("plugin %s trigger %s: %s", self.plugin_id, self.node_id, text)


def is_active_trigger(node: Mapping[str, Any], plugin_id: str | None = None) -> bool:
    data = node.get("data") or {}
    return (
        node.get("type") == "pluginTrigger"
        and data.get("active", True) is not False
        and (plugin_id is None or data.get("pluginId") == plugin_id)
    )


def trigger_node_def(manifest: PluginManifest, node: Mapping[str, Any]) -> PluginNodeDef | None:
    """The manifest node a canvas trigger node stands for, if it hears events by itself."""
    key = (node.get("data") or {}).get("pluginNodeKey")
    node_def = manifest.get_node(key) if key else None
    if node_def is None or node_def.kind != "trigger" or node_def.source is None:
        return None
    return node_def


def receive_function(node_def: PluginNodeDef) -> str:
    return f"receive_{node_def.key}"


def event_passes(node: Mapping[str, Any], event: Any) -> bool:
    """The node's "Only start when" condition over ``$event``; empty passes, an error doesn't."""
    condition = str((node.get("data") or {}).get("onlyStartWhen") or "").strip()
    if not condition:
        return True
    from app.services.workflow_executor import WorkflowExecutor

    try:
        return bool(WorkflowExecutor([], []).evaluate_condition_strict(condition, {"event": event}))
    except Exception as exc:
        logger.warning("Only start when of trigger %s failed: %s", node.get("id"), exc)
        return False


def trigger_config(
    manifest: PluginManifest, node_def: PluginNodeDef, node: Mapping[str, Any], owner_id: uuid.UUID
) -> tuple[dict, list[str]]:
    """The trigger node's config as its handler gets it, credentials of the workflow's owner."""
    from app.services.workflow_executor import WorkflowExecutor

    executor = WorkflowExecutor([], [], actor_user_id=owner_id)
    exec_ctx = NodeExecutionContext(
        executor=executor,
        node_id=str(node.get("id")),
        inputs={},
        allow_branch_skip=False,
        start_time=0.0,
        node=dict(node),
        node_type="pluginTrigger",
        node_data=dict(node.get("data") or {}),
        node_label=str((node.get("data") or {}).get("label") or node.get("id")),
    )
    secrets: list[str] = []
    raw = (node.get("data") or {}).get("config") or {}
    config = plugin_context.resolve_config(exec_ctx, node_def.fields, raw, manifest.id, secrets)
    return config, secrets


def as_events(result: object) -> list[Any]:
    """What ``receive`` gave back as a list of events: None is none, a list is one run each."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return [result]


def run_inputs(node_id: str, event: Any) -> dict[str, Any]:
    return {
        "triggered_by": TRIGGER_SOURCE,
        "trigger_node_id": node_id,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }


async def start_run(workflow_id: uuid.UUID, node_id: str, event: Any) -> None:
    """One run of the workflow with this event, its history written as any trigger's."""
    from app.services.trigger_run_dispatch import run_trigger_workflow

    try:
        await run_trigger_workflow(
            workflow_id=workflow_id,
            inputs=run_inputs(node_id, event),
            trigger_source=TRIGGER_SOURCE,
            trigger_label="Plugin trigger",
            log=logger,
        )
    except Exception:
        logger.exception("Plugin trigger run of workflow %s failed to start", workflow_id)


_background_runs: set[asyncio.Task] = set()


def start_runs_in_background(workflow_id: uuid.UUID, node: Mapping[str, Any], events: list) -> int:
    """Start a run per event that passes the filter; the caller doesn't wait for them."""
    started = 0
    for event in events:
        if not event_passes(node, event):
            continue
        task = asyncio.create_task(start_run(workflow_id, str(node.get("id")), event))
        _background_runs.add(task)
        task.add_done_callback(_background_runs.discard)
        started += 1
    return started


async def find_hook(
    db: Any, plugin_id: str, hook_id: str
) -> tuple[Workflow, dict, PluginManifest, PluginNodeDef] | None:
    """The workflow and push trigger node a hook id names, if it is active."""
    if not HOOK_ID_RE.match(hook_id):
        return None
    plugin = (
        await db.execute(select(Plugin).where(Plugin.plugin_id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None or not plugin.enabled:
        return None
    manifest = PluginManifest.model_validate(plugin.manifest)
    if manifest.plugin_api < 2:
        return None
    rows = await db.execute(select(Workflow).where(cast(Workflow.nodes, String).contains(hook_id)))
    for workflow in rows.scalars().all():
        for node in workflow.nodes or []:
            if not is_active_trigger(node, plugin_id):
                continue
            if (node.get("data") or {}).get("hookId") != hook_id:
                continue
            node_def = trigger_node_def(manifest, node)
            if node_def is not None and node_def.source == "push":
                return workflow, node, manifest, node_def
    return None


def request_view(method: str, headers: Mapping[str, str], query: Mapping, body: bytes) -> dict:
    """The request as ``receive`` sees it."""
    try:
        parsed = json.loads(body) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return {
        "method": method,
        "headers": {k.lower(): v for k, v in headers.items()},
        "query": dict(query),
        "body": body,
        "json": parsed,
    }


def call_receive(
    manifest: PluginManifest,
    node_def: PluginNodeDef,
    request: dict,
    config: dict,
    ctx: TriggerContext,
) -> object:
    """Run the package's ``receive_<node key>`` (or ``receive``) for a hook request."""
    root = plugin_store.plugins_root()
    function = receive_function(node_def)
    if not plugin_loader.has_function(manifest.id, root, function):
        function = "receive"
    return plugin_loader.call_handler(
        manifest.id, root, function, {"request": request, "config": config, "ctx": ctx}
    )


# --- loop triggers -------------------------------------------------------------------------


@dataclass
class _Listener:
    key: str
    stopping: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class PluginTriggerListenerManager:
    """Runs ``listen`` for each active loop trigger node, on the leader only."""

    def __init__(self, scan_seconds: float = 15.0) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._listeners: dict[str, _Listener] = {}
        self._scan_seconds = scan_seconds
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for key in list(self._listeners):
            self._stop_listener(key)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                if lock_service.is_leader:
                    await self.scan()
                else:
                    for key in list(self._listeners):
                        self._stop_listener(key)
            except Exception:
                logger.exception("Error in the plugin trigger listener loop")
            await asyncio.sleep(self._scan_seconds)

    async def scan(self) -> None:
        """Start listeners for new or changed loop trigger nodes, stop those that went away."""
        from app.config import settings

        wanted: dict[str, tuple[Workflow, dict, PluginManifest, PluginNodeDef]] = {}
        if not settings.plugins_enabled:
            for key in list(self._listeners):
                self._stop_listener(key)
            return
        async with async_session_maker() as db:
            plugins = (await db.execute(select(Plugin).where(Plugin.enabled.is_(True)))).scalars()
            manifests = {
                p.plugin_id: PluginManifest.model_validate(p.manifest) for p in plugins.all()
            }
            manifests = {k: m for k, m in manifests.items() if m.plugin_api >= 2}
            if manifests:
                rows = await db.execute(
                    select(Workflow).where(cast(Workflow.nodes, String).contains("pluginTrigger"))
                )
                for workflow in rows.scalars().all():
                    for node in workflow.nodes or []:
                        plugin_id = (node.get("data") or {}).get("pluginId")
                        manifest = manifests.get(plugin_id)
                        if manifest is None or not is_active_trigger(node, plugin_id):
                            continue
                        node_def = trigger_node_def(manifest, node)
                        if node_def is None or node_def.source != "loop":
                            continue
                        wanted[listener_key(workflow, node, manifest)] = (
                            workflow,
                            node,
                            manifest,
                            node_def,
                        )
        for key in list(self._listeners):
            if key not in wanted:
                self._stop_listener(key)
        for key, found in wanted.items():
            if key not in self._listeners:
                self._start_listener(key, *found)

    def _stop_listener(self, key: str) -> None:
        listener = self._listeners.pop(key, None)
        if listener is not None:
            listener.stopping.set()

    def _start_listener(
        self,
        key: str,
        workflow: Workflow,
        node: dict,
        manifest: PluginManifest,
        node_def: PluginNodeDef,
    ) -> None:
        listener = _Listener(key=key)
        ctx = TriggerContext(
            manifest.id, node_def, workflow.id, str(node.get("id")), listener.stopping
        )
        workflow_id, owner_id = workflow.id, workflow.owner_id
        loop = self._loop

        def emit(event: Any) -> None:
            if listener.stopping.is_set() or loop is None or not event_passes(node, event):
                return
            asyncio.run_coroutine_threadsafe(start_run(workflow_id, ctx.node_id, event), loop)

        def body() -> None:
            run_listener(manifest, node_def, node, owner_id, emit, ctx)

        listener.thread = threading.Thread(
            target=body, name=f"plugin-listen-{key[:40]}", daemon=True
        )
        self._listeners[key] = listener
        listener.thread.start()


def listener_key(workflow: Workflow, node: Mapping[str, Any], manifest: PluginManifest) -> str:
    """Changes when the node, its workflow's save or the package version changes: a restart."""
    data = json.dumps(node.get("data") or {}, sort_keys=True, default=str)
    return f"{workflow.id}:{node.get('id')}:{manifest.version}:{hash(data)}"


def run_listener(
    manifest: PluginManifest,
    node_def: PluginNodeDef,
    node: Mapping[str, Any],
    owner_id: uuid.UUID,
    emit: Callable[[Any], None],
    ctx: TriggerContext,
    sleep: Callable[[float], bool] | None = None,
) -> None:
    """Keep ``listen`` running until stopped: after it returns or raises, again after a pause."""
    wait = sleep or ctx.stopping.wait
    attempt = 0
    while not ctx.stopping.is_set():
        try:
            config, _secrets = trigger_config(manifest, node_def, node, owner_id)
            plugin_loader.call_handler(
                manifest.id,
                plugin_store.plugins_root(),
                "listen",
                {"config": config, "emit": emit, "ctx": ctx},
            )
            attempt = 0
        except Exception as exc:
            logger.warning(
                "Plugin %s listener for node %s failed: %s", manifest.id, ctx.node_id, exc
            )
        if ctx.stopping.is_set():
            return
        delay = _RESTART_DELAYS[min(attempt, len(_RESTART_DELAYS) - 1)]
        attempt += 1
        wait(delay)


plugin_trigger_listener_manager = PluginTriggerListenerManager()
