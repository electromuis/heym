"""Loop nodes of Plugin API 2: a plugin action that loops like the Loop block (docs/plugin-api-2.md, "Loop nodes").

The engine's loop-back rules ask ``is_loop_node`` instead of checking for the Loop block, so an edge into a loop
plugin node's ``loop`` handle is a loop-back edge too. A loop plugin node has no item list: its handler is called
once per pass and says with ``exit`` whether there is a next pass (``step``) or not (``done``). What it carries from
pass to pass is the ``state`` it returns, kept in ``executor.loop_states`` and so in a paused run's snapshot.

The Loop block's own state (``items``, ``current_index``, ``total``) is untouched; a plugin loop's state is
``{"plugin": True, "index", "state", "done"}``.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

logger = logging.getLogger("heym.plugins")

PLUGIN_LOOP_FLAG = "_pluginLoop"
STEP_HANDLE = "exit-step"
DONE_HANDLE = "exit-done"


def is_loop_node(node: dict | None) -> bool:
    """The Loop block, or a plugin node whose manifest says ``"loop": true`` (marked by ``mark_plugin_loops``)."""
    if not node:
        return False
    if node.get("type") == "loop":
        return True
    return node.get("type") == "plugin" and (node.get("data") or {}).get(PLUGIN_LOOP_FLAG) is True


def body_handle(node: dict | None) -> str:
    """The source handle a loop's body hangs from."""
    return STEP_HANDLE if (node or {}).get("type") == "plugin" else "loop"


def done_handle(node: dict | None) -> str:
    """The source handle a loop leaves by."""
    return DONE_HANDLE if (node or {}).get("type") == "plugin" else "done"


def output_is_done(node: dict | None, output: Any) -> bool:
    """Whether a loop node's output leaves the loop."""
    if not isinstance(output, dict):
        return False
    if (node or {}).get("type") == "plugin":
        return output.get("exit") == "done"
    return output.get("branch") == "done"


def state_is_open(state: dict) -> bool:
    """Whether a loop has another pass to give: the Loop block has items left, a plugin loop hasn't said done."""
    if state.get("plugin"):
        return not state.get("done")
    return state["current_index"] < state["total"]


def mark_plugin_loops(nodes: dict[str, dict]) -> None:
    """Mark the plugin nodes whose manifest action is a loop, so ``is_loop_node`` needs no manifest."""
    manifests: dict[str, Any] = {}
    for node in nodes.values():
        if node.get("type") != "plugin":
            continue
        data = node.get("data")
        if not isinstance(data, dict):
            continue
        node_def = _node_def(data, manifests)
        if node_def is not None and node_def.loop:
            data[PLUGIN_LOOP_FLAG] = True
        else:
            data.pop(PLUGIN_LOOP_FLAG, None)


def _node_def(data: dict, manifests: dict[str, Any]) -> Any:
    from app.services import plugin_loader, plugin_store

    plugin_id = data.get("pluginId")
    if not plugin_id:
        return None
    if plugin_id not in manifests:
        try:
            manifests[plugin_id] = plugin_loader.load_manifest(
                plugin_id, plugin_store.plugins_root()
            )
        except Exception:  # noqa: BLE001 - a missing plugin fails when its node runs, not here
            manifests[plugin_id] = None
    manifest = manifests[plugin_id]
    if manifest is None or manifest.plugin_api < 2:
        return None
    key = data.get("pluginNodeKey")
    node_def = manifest.get_node(key) if key else None
    if node_def is None:
        node_def = next((n for n in manifest.resolved_nodes() if n.kind == "action"), None)
    return node_def


def _came_back(exec_ctx: Any) -> bool:
    """Whether this call arrived over a loop-back edge (as ``loop_node`` decides it for the Loop block)."""
    executor = exec_ctx.executor
    for edge in executor.edges:
        if (
            edge.get("source") != exec_ctx.node_id
            and edge.get("target") == exec_ctx.node_id
            and edge.get("targetHandle") == "loop"
        ):
            source_id = edge.get("source", "")
            if (
                executor.get_node_label(source_id) in exec_ctx.inputs
                and source_id in executor.node_outputs
            ):
                return True
    return False


def begin_pass(exec_ctx: Any) -> dict:
    """``ctx.loop`` for this call: the pass number and the state the previous pass returned.

    Nothing is written here: a pass that pauses on ``ctx.wait`` runs again from the start when it resumes, and
    must see the same pass number.
    """
    state = exec_ctx.executor.loop_states.get(exec_ctx.node_id)
    if not isinstance(state, dict) or not state.get("plugin") or not _came_back(exec_ctx):
        return {"index": 0, "state": {}}
    return {"index": int(state["index"]) + 1, "state": copy.deepcopy(state.get("state") or {})}


def finish_pass(exec_ctx: Any, node_key: str, loop: dict, output: dict) -> None:
    """Keep the pass's state, take it out of the output and pick ``step`` or ``done`` as the Loop block does."""
    chosen = output.get("exit", "step")
    if chosen not in ("step", "done"):
        raise ValueError(
            f"Plugin loop node {node_key} returned an unknown exit; declared: step, done"
        )
    state = output.pop("state", {})
    if state is None:
        state = {}
    if not isinstance(state, dict):
        raise ValueError(f"Plugin loop node {node_key} returned a state that is not a JSON object")
    try:
        state = json.loads(json.dumps(state))
    except (TypeError, ValueError):
        raise ValueError(f"Plugin loop node {node_key} returned a state that is not JSON") from None
    output["exit"] = chosen
    executor = exec_ctx.executor
    node_id = exec_ctx.node_id
    with executor.lock:
        executor.loop_states[node_id] = {
            "plugin": True,
            "index": loop["index"],
            "state": state,
            "done": chosen == "done",
        }
    output["_skip_loop_source_handles"] = [DONE_HANDLE if chosen == "step" else STEP_HANDLE]
    if not exec_ctx.allow_branch_skip:
        return
    with executor.lock:
        if chosen == "done":
            executor.skip_branch_targets_preserving_shared_downstream(
                node_id,
                active_targets=executor.get_downstream_nodes(node_id, DONE_HANDLE),
                inactive_targets=executor.get_downstream_nodes(node_id, STEP_HANDLE),
                inactive_stop_node_ids={node_id},
            )
        else:
            executor.skip_branch_targets_preserving_shared_downstream(
                node_id,
                active_targets=executor.get_downstream_nodes(node_id, STEP_HANDLE),
                inactive_targets=executor.get_downstream_nodes(node_id, DONE_HANDLE),
                active_exclude_node_ids={node_id},
                inactive_stop_node_ids={node_id},
            )


class LoopEndContext:
    """The ``ctx`` of ``loop_end``: who the loop was. The run is over, so there is nothing to check or wait on."""

    def __init__(
        self,
        plugin_id: str,
        node_key: str,
        node_id: str,
        node_label: str,
        workflow_id: Any,
        execution_id: Any,
    ) -> None:
        self.plugin_id = plugin_id
        self.node_key = node_key
        self.node_id = node_id
        self.node_label = node_label
        self.workflow_id = None if workflow_id in (None, "") else str(workflow_id)
        self.execution_id = None if execution_id in (None, "") else str(execution_id)

    def log(self, text: str) -> None:
        logger.info("plugin %s node %s: %s", self.plugin_id, self.node_label, text)


def end_open_loops(executor: Any) -> None:
    """Call ``loop_end(state, ctx)`` for each plugin loop of a run that stops before its loop said done."""
    with executor.lock:
        ended = _close_open(executor.loop_states)
    _call_loop_ends(
        executor.nodes, ended, executor.workflow_id, getattr(executor, "execution_id", None)
    )


def end_loops_in_snapshot(snapshot: dict, execution_id: Any = None) -> None:
    """The same for a paused run that is stopped: its loops' states are in the snapshot it was paused with."""
    nodes = {str(n.get("id")): n for n in snapshot.get("nodes") or [] if isinstance(n, dict)}
    ended = _close_open(snapshot.get("loop_states") or {})
    _call_loop_ends(nodes, ended, snapshot.get("workflow_id"), execution_id)


def _close_open(loop_states: dict) -> list[tuple[str, dict]]:
    ended: list[tuple[str, dict]] = []
    for node_id, state in loop_states.items():
        if isinstance(state, dict) and state.get("plugin") and not state.get("done"):
            state["done"] = True
            ended.append((node_id, copy.deepcopy(state.get("state") or {})))
    return ended


def _call_loop_ends(
    nodes: dict, ended: list[tuple[str, dict]], workflow_id: Any, execution_id: Any
) -> None:
    """A failing ``loop_end`` is logged: the run has stopped either way."""
    from app.services import plugin_loader, plugin_store

    for node_id, state in ended:
        data = (nodes.get(node_id) or {}).get("data") or {}
        plugin_id = data.get("pluginId")
        if not plugin_id:
            continue
        node_key = str(data.get("pluginNodeKey") or "")
        root = plugin_store.plugins_root()
        try:
            names = [f"loop_end_{node_key}", "loop_end"] if node_key else ["loop_end"]
            function = next(
                (n for n in names if plugin_loader.has_function(plugin_id, root, n)), None
            )
            if function is None:
                continue
            ctx = LoopEndContext(
                plugin_id,
                node_key,
                node_id,
                str(data.get("label") or node_id),
                workflow_id,
                execution_id,
            )
            plugin_loader.call_handler(plugin_id, root, function, {"state": state, "ctx": ctx})
        except Exception:  # noqa: BLE001
            logger.exception("plugin %s: loop_end of node %s failed", plugin_id, node_id)
