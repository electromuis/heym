"""Plugin API 2 loop nodes: a plugin action that loops like the Loop block, one pass per call.

The runs use real workflows with the Loop block's handles; waits pause and resume from the snapshot as
the background resume does (through JSON, as a restart reads it from the database).
"""

import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from app.models.plugin_schemas import PluginManifest
from app.services import plugin_loader, plugin_loops, plugin_store, plugin_waits
from app.services.pending_review_cancel import cancel_pending_review_execution
from app.services.workflow_executor import (
    WorkflowCancelledError,
    WorkflowExecutor,
    execute_workflow,
    resume_workflow_execution,
)

_MANIFEST = {
    "pluginApi": 2,
    "id": "acme",
    "name": "Acme",
    "version": "1.0.0",
    "nodes": [
        {
            "key": "steps",
            "name": "Steps",
            "kind": "action",
            "function": "steps",
            "loop": True,
            "fields": [{"key": "n", "label": "N"}],
        },
        {"key": "waiter", "name": "Waiter", "kind": "action", "function": "waiter", "loop": True},
        {"key": "boom", "name": "Boom", "kind": "action", "function": "boom"},
        {"key": "stop", "name": "Stop", "kind": "action", "function": "stop"},
    ],
}

_HANDLER = """
ENDED = []
STOP = []


def steps(inputs, config, ctx):
    i = ctx.loop["index"]
    seen = ctx.loop["state"].get("seen", [])
    if i >= int(config["n"]):
        return {"exit": "done", "seen": seen, "passes": i}
    return {"exit": "step", "item": i, "row": [i, i + 10], "state": {"seen": seen + [i]}}


def waiter(inputs, config, ctx):
    i = ctx.loop["index"]
    total = ctx.loop["state"].get("total", 0)
    if i >= 2:
        return {"exit": "done", "total": total}
    if ctx.resume is None:
        ctx.wait("w-%d" % i)
    return {"exit": "step", "value": ctx.resume["value"], "state": {"total": total + ctx.resume["value"]}}


def boom(inputs, config, ctx):
    raise RuntimeError("boom")


def stop(inputs, config, ctx):
    for event in STOP:
        event.set()
    return {"stopped": True}


def loop_end(state, ctx):
    ENDED.append((ctx.node_label, state))
"""


def _node(node_id: str, node_type: str, label: str, **data: object) -> dict:
    return {"id": node_id, "type": node_type, "data": {"label": label, **data}}


def _plugin(node_id: str, key: str, label: str | None = None, **config: object) -> dict:
    return _node(
        node_id, "plugin", label or node_id, pluginId="acme", pluginNodeKey=key, config=config
    )


def _set(label: str, value: str) -> dict:
    return _node(label, "set", label, mappings=[{"key": "v", "value": value}])


def _edge(source: str, target: str, sh: str | None = None, th: str | None = None) -> dict:
    edge = {"id": f"{source}-{target}-{sh}-{th}", "source": source, "target": target}
    if sh:
        edge["sourceHandle"] = sh
    if th:
        edge["targetHandle"] = th
    return edge


def _values(result, label: str) -> list:
    return [
        r["output"]["v"]
        for r in result.node_results
        if r["node_label"] == label and r["status"] == "success"
    ]


def _outputs(result, label: str) -> list[dict]:
    return [
        r["output"]
        for r in result.node_results
        if r["node_label"] == label and r["status"] == "success"
    ]


class _PackageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        package = root / "acme"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps(_MANIFEST))
        (package / "handler.py").write_text(_HANDLER)
        self.root = root
        plugin_loader.clear_cache()
        self.addCleanup(plugin_loader.clear_cache)
        for patcher in (
            patch.object(plugin_store, "plugins_root", return_value=root),
            patch.object(plugin_waits, "key_is_waiting", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def module(self):
        return plugin_loader._load_module("acme", self.root)

    def run_graph(self, nodes: list, edges: list, **kwargs):
        return execute_workflow(uuid.uuid4(), nodes, edges, {"text": "hi"}, **kwargs)


class PassTests(_PackageTestCase):
    def _graph(self, n: object = 3) -> tuple[list, list]:
        nodes = [
            _node("in", "textInput", "msg"),
            _plugin("steps", "steps", n=n),
            _set("body", "$steps.item"),
            _set("after", "$steps.passes"),
        ]
        edges = [
            _edge("in", "steps"),
            _edge("steps", "body", "exit-step"),
            _edge("body", "steps", None, "loop"),
            _edge("steps", "after", "exit-done"),
        ]
        return nodes, edges

    def test_one_pass_per_step_then_done(self) -> None:
        result = self.run_graph(*self._graph())
        self.assertEqual(result.status, "success")
        self.assertEqual(_values(result, "body"), [0, 1, 2])
        self.assertEqual(_values(result, "after"), [3])

    def test_state_is_carried_and_left_out_of_the_output(self) -> None:
        result = self.run_graph(*self._graph())
        passes = _outputs(result, "steps")
        self.assertEqual([p["exit"] for p in passes], ["step", "step", "step", "done"])
        self.assertEqual(passes[-1]["seen"], [0, 1, 2])
        self.assertTrue(all("state" not in p for p in passes))

    def test_done_on_the_first_pass_skips_the_body(self) -> None:
        result = self.run_graph(*self._graph(n=0))
        self.assertEqual(result.status, "success")
        self.assertEqual(_values(result, "body"), [])
        self.assertEqual(_values(result, "after"), [0])

    def test_an_unknown_exit_fails_the_node(self) -> None:
        nodes, edges = self._graph()
        handler = self.root / "acme" / "handler.py"
        handler.write_text(
            _HANDLER + "\n\ndef steps(inputs, config, ctx):\n    return {'exit': 'more'}\n"
        )
        result = self.run_graph(nodes, edges)
        self.assertEqual(result.status, "error")
        self.assertIn("unknown exit; declared: step, done", json.dumps(result.outputs))

    def test_the_loop_node_is_a_loop_to_the_engine(self) -> None:
        nodes, edges = self._graph()
        executor = WorkflowExecutor(nodes=copy.deepcopy(nodes), edges=edges)
        self.assertTrue(plugin_loops.is_loop_node(executor.nodes["steps"]))
        self.assertFalse(plugin_loops.is_loop_node(executor.nodes["body"]))
        self.assertEqual(executor.get_loop_body_node_ids("steps", edges), {"body"})
        self.assertEqual(executor.get_incoming_edge_count_for_execution("steps", edges), 1)


class NestingTests(_PackageTestCase):
    def test_inside_a_loop_block(self) -> None:
        nodes = [
            _node("in", "textInput", "msg"),
            _node("outer", "loop", "outer", arrayExpression="$msg.body.rows"),
            _plugin("steps", "steps", n="$outer.item"),
            _set("body", "$steps.item"),
            _set("rowDone", "$outer.index"),
        ]
        edges = [
            _edge("in", "outer"),
            _edge("outer", "steps", "loop"),
            _edge("steps", "body", "exit-step"),
            _edge("body", "steps", None, "loop"),
            _edge("steps", "rowDone", "exit-done"),
            _edge("rowDone", "outer", None, "loop"),
        ]
        result = execute_workflow(uuid.uuid4(), nodes, edges, {"body": {"rows": [2, 1, 3]}})
        self.assertEqual(result.status, "success")
        self.assertEqual(_values(result, "body"), [0, 1, 0, 0, 1, 2])
        self.assertEqual(_values(result, "rowDone"), [0, 1, 2])

    def test_with_a_loop_block_inside(self) -> None:
        nodes = [
            _node("in", "textInput", "msg"),
            _plugin("steps", "steps", n=2),
            _node("inner", "loop", "inner", arrayExpression="$steps.row"),
            _set("body", "$inner.item"),
            _set("rowDone", "$steps.item"),
            _set("after", "$steps.passes"),
        ]
        edges = [
            _edge("in", "steps"),
            _edge("steps", "inner", "exit-step"),
            _edge("inner", "body", "loop"),
            _edge("body", "inner", None, "loop"),
            # Not straight from done into `loop`: stock Heym runs one inner item only then, for two Loop blocks too.
            _edge("inner", "rowDone", "done"),
            _edge("rowDone", "steps", None, "loop"),
            _edge("steps", "after", "exit-done"),
        ]
        result = self.run_graph(nodes, edges)
        self.assertEqual(result.status, "success")
        self.assertEqual(_values(result, "body"), [0, 10, 1, 11])
        self.assertEqual(_values(result, "rowDone"), [0, 1])
        self.assertEqual(_values(result, "after"), [2])


class WaitTests(_PackageTestCase):
    def _graph(self) -> tuple[list, list]:
        nodes = [
            _node("in", "textInput", "msg"),
            _plugin("waiter", "waiter"),
            _set("body", "$waiter.value"),
            _set("after", "$waiter.total"),
        ]
        edges = [
            _edge("in", "waiter"),
            _edge("waiter", "body", "exit-step"),
            _edge("body", "waiter", None, "loop"),
            _edge("waiter", "after", "exit-done"),
        ]
        return nodes, edges

    @staticmethod
    def _restart_and_resume(paused, body: dict):
        # What a restart does: the snapshot comes back from the database as JSON, the plugin is imported anew.
        snapshot = json.loads(json.dumps(paused.resume_snapshot))
        snapshot["hitl_resume_mode"] = "rerun_agent"
        plugin_loader.clear_cache()
        return resume_workflow_execution(snapshot=snapshot, resolved_output=body)

    def test_a_pass_that_waits_resumes_and_the_loop_goes_on(self) -> None:
        first = self.run_graph(*self._graph())
        self.assertEqual(first.status, "pending")
        self.assertEqual(first.pending_review["key"], "w-0")

        second = self._restart_and_resume(first, {"value": 5})
        self.assertEqual(second.status, "pending")
        self.assertEqual(second.pending_review["key"], "w-1")
        self.assertEqual(_values(second, "body"), [5])

        third = self._restart_and_resume(second, {"value": 7})
        self.assertEqual(third.status, "success")
        self.assertEqual(
            _values(third, "body"), [5, 7]
        )  # a resumed run keeps the results before it
        self.assertEqual(_values(third, "after"), [12])

    def test_a_loop_pass_may_wait_with_a_timeout(self) -> None:
        handler = self.root / "acme" / "handler.py"
        handler.write_text(
            _HANDLER.replace('ctx.wait("w-%d" % i)', 'ctx.wait("w-%d" % i, timeout=5)')
        )
        first = self.run_graph(*self._graph())
        self.assertEqual(first.status, "pending")

    def test_stopping_a_paused_run_calls_loop_end_with_the_last_state(self) -> None:
        first = self.run_graph(*self._graph())
        second = self._restart_and_resume(first, {"value": 5})
        self.assertEqual(second.status, "pending")
        self.module().ENDED.clear()

        plugin_loops.end_loops_in_snapshot(json.loads(json.dumps(second.resume_snapshot)))
        self.assertEqual(self.module().ENDED, [("waiter", {"total": 5})])

    def test_cancelling_the_paused_run_ends_its_loops(self) -> None:
        first = self.run_graph(*self._graph())
        second = self._restart_and_resume(first, {"value": 5})
        self.module().ENDED.clear()

        def result(rowcount: int, scalars: list | None = None) -> MagicMock:
            row = MagicMock()
            row.rowcount = rowcount
            row.scalars.return_value.all.return_value = scalars or []
            return row

        history = MagicMock(status="pending", node_results=[])
        db = AsyncMock()
        found = MagicMock()
        found.scalar_one_or_none.return_value = history
        db.execute = AsyncMock(
            side_effect=[result(0), result(0), result(1, [second.resume_snapshot]), found]
        )
        with patch("app.services.board_run_service.resume_card_chain", AsyncMock()):
            import asyncio

            cancelled = asyncio.run(
                cancel_pending_review_execution(
                    db, workflow_id=uuid.uuid4(), execution_id=uuid.uuid4()
                )
            )
        self.assertTrue(cancelled)
        self.assertEqual(self.module().ENDED, [("waiter", {"total": 5})])


class StopTests(_PackageTestCase):
    def _graph(self, body_key: str) -> tuple[list, list]:
        nodes = [
            _node("in", "textInput", "msg"),
            _plugin("steps", "steps", n=3),
            _plugin("body", body_key),
            _set("after", "$steps.passes"),
        ]
        edges = [
            _edge("in", "steps"),
            _edge("steps", "body", "exit-step"),
            _edge("body", "steps", None, "loop"),
            _edge("steps", "after", "exit-done"),
        ]
        return nodes, edges

    def test_a_stopped_run_calls_loop_end(self) -> None:
        event = Event()
        module = self.module()
        module.STOP[:] = [event]
        module.ENDED.clear()
        try:
            self.run_graph(*self._graph("stop"), cancel_event=event)
        except WorkflowCancelledError:
            pass
        self.assertEqual(module.ENDED, [("steps", {"seen": [0]})])

    def test_a_failed_run_calls_loop_end(self) -> None:
        module = self.module()
        module.ENDED.clear()
        result = self.run_graph(*self._graph("boom"))
        self.assertEqual(result.status, "error")
        self.assertEqual(module.ENDED, [("steps", {"seen": [0]})])

    def test_a_finished_loop_gets_no_loop_end(self) -> None:
        module = self.module()
        module.ENDED.clear()
        nodes, edges = PassTests._graph(self)  # type: ignore[arg-type]
        self.assertEqual(self.run_graph(nodes, edges).status, "success")
        self.assertEqual(module.ENDED, [])


class ManifestTests(unittest.TestCase):
    @staticmethod
    def _manifest(node: dict, api: int = 2) -> PluginManifest:
        raw = {"pluginApi": api, "id": "acme", "name": "Acme", "version": "1", "nodes": [node]}
        return PluginManifest.model_validate(raw)

    def test_a_loop_has_its_two_exits(self) -> None:
        node = self._manifest({"key": "l", "name": "L", "kind": "action", "loop": True}).nodes[0]
        self.assertTrue(node.loop)
        self.assertEqual(node.exit_keys(), ["step", "done"])
        self.assertEqual(node.exits, [])

    def test_declaring_exits_or_inputs_on_a_loop_is_an_error(self) -> None:
        for extra in (
            {"exits": [{"key": "step"}]},
            {"inputs": [{"key": "a"}]},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self._manifest({"key": "l", "name": "L", "kind": "action", "loop": True, **extra})

    def test_only_an_action_loops(self) -> None:
        with self.assertRaises(ValidationError):
            self._manifest({"key": "l", "name": "L", "kind": "trigger", "loop": True})

    def test_a_dumped_manifest_reads_back(self) -> None:
        manifest = self._manifest({"key": "l", "name": "L", "kind": "action", "loop": True})
        again = PluginManifest.model_validate(manifest.model_dump(by_alias=True))
        self.assertTrue(again.nodes[0].loop)

    def test_api1_ignores_loop(self) -> None:
        node = self._manifest(
            {"key": "l", "name": "L", "kind": "action", "loop": True}, api=1
        ).nodes[0]
        self.assertFalse(node.loop)


class Api1Tests(_PackageTestCase):
    def test_an_api1_manifest_with_loop_keeps_todays_behaviour(self) -> None:
        manifest = {**_MANIFEST, "pluginApi": 1}
        (self.root / "acme" / "plugin.json").write_text(json.dumps(manifest))
        (self.root / "acme" / "handler.py").write_text(
            "def steps(inputs, config, ctx):\n    return {'once': True, 'ctx': sorted(ctx)}\n"
        )
        plugin_loader.clear_cache()
        nodes = [_node("in", "textInput", "msg"), _plugin("steps", "steps", n=3), _set("next", "1")]
        edges = [_edge("in", "steps"), _edge("steps", "next")]
        executor = WorkflowExecutor(nodes=copy.deepcopy(nodes), edges=edges)
        self.assertFalse(plugin_loops.is_loop_node(executor.nodes["steps"]))
        result = self.run_graph(nodes, edges)
        self.assertEqual(result.status, "success")
        self.assertEqual(
            _outputs(result, "steps"), [{"once": True, "ctx": ["node_id", "node_label"]}]
        )
        self.assertEqual(_values(result, "next"), ["1"])


if __name__ == "__main__":
    unittest.main()
