"""Plugin API 2 triggers: the event as output, the filter, the push hook's answers, the listener."""

import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.api import plugins as plugins_api
from app.models.plugin_schemas import PluginManifest
from app.services import plugin_loader, plugin_store, plugin_triggers
from app.services.workflow_executor import execute_workflow

_MANIFEST = {
    "pluginApi": 2,
    "id": "acme",
    "name": "Acme",
    "version": "1.0.0",
    "nodes": [
        {
            "key": "onMessage",
            "name": "On a message",
            "kind": "trigger",
            "source": "push",
            "fields": [{"key": "key", "label": "Key"}],
            "exits": [{"key": "dm"}, {"key": "mention"}],
        },
        {
            "key": "onShaped",
            "name": "Shaped",
            "kind": "trigger",
            "source": "loop",
            "function": "shape",
        },
        {"key": "old", "name": "Old style", "kind": "trigger"},
    ],
}

_HANDLER = """
def receive(request, config, ctx):
    if request["headers"].get("x-key") != config["key"]:
        raise PermissionError("bad key")
    body = request["json"] or {}
    if body.get("ignore"):
        return None
    return body.get("events", body)


def shape(config, ctx, event):
    return {"shaped": event}


def trigger(config, ctx):
    return {"old": True}


CALLS = []


def listen(config, emit, ctx):
    CALLS.append(1)
    emit({"n": len(CALLS)})
    if len(CALLS) == 1:
        raise RuntimeError("lost the connection")
"""


class _PackageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "acme").mkdir()
        (root / "acme" / "plugin.json").write_text(json.dumps(_MANIFEST))
        (root / "acme" / "handler.py").write_text(_HANDLER)
        plugin_loader.clear_cache()
        self.addCleanup(plugin_loader.clear_cache)
        patcher = patch.object(plugin_store, "plugins_root", return_value=root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manifest = PluginManifest.model_validate(_MANIFEST)


def _trigger_node(key: str, **data: object) -> dict:
    return {
        "id": "t",
        "type": "pluginTrigger",
        "data": {"label": "onMsg", "pluginId": "acme", "pluginNodeKey": key, **data},
    }


class TriggerFilterTests(unittest.TestCase):
    def test_only_start_when(self) -> None:
        node = {"id": "t", "data": {"onlyStartWhen": "$event.channel == 'general'"}}
        self.assertTrue(plugin_triggers.event_passes(node, {"channel": "general"}))
        self.assertFalse(plugin_triggers.event_passes(node, {"channel": "ops"}))
        self.assertTrue(plugin_triggers.event_passes({"data": {}}, {"x": 1}))

    def test_an_error_starts_no_run(self) -> None:
        node = {"id": "t", "data": {"onlyStartWhen": "1 / 0 == 1"}}
        self.assertFalse(plugin_triggers.event_passes(node, {}))

    def test_events(self) -> None:
        self.assertEqual(plugin_triggers.as_events(None), [])
        self.assertEqual(plugin_triggers.as_events({"a": 1}), [{"a": 1}])
        self.assertEqual(plugin_triggers.as_events([1, 2]), [1, 2])


class TriggerNodeRunTests(_PackageTestCase):
    def _run(self, node: dict, inputs: dict) -> tuple[dict, list[str]]:
        nodes = [
            node,
            {"id": "dmPath", "type": "set", "data": {"label": "dmPath", "mappings": []}},
            {"id": "mentionPath", "type": "set", "data": {"label": "mentionPath", "mappings": []}},
        ]
        edges = [
            {"id": "a", "source": "t", "target": "dmPath", "sourceHandle": "exit-dm"},
            {"id": "b", "source": "t", "target": "mentionPath", "sourceHandle": "exit-mention"},
        ]
        result = execute_workflow(uuid.uuid4(), nodes, edges, inputs)
        trigger = next(r for r in result.node_results if r["node_id"] == "t")
        ran = sorted(
            r["node_label"]
            for r in result.node_results
            if r["node_type"] == "set" and r["status"] == "success"
        )
        return trigger, ran

    def test_event_is_the_output_and_picks_the_exit(self) -> None:
        inputs = plugin_triggers.run_inputs("t", {"exit": "mention", "text": "hi"})
        trigger, ran = self._run(_trigger_node("onMessage"), inputs)
        self.assertEqual(trigger["status"], "success", trigger.get("error"))
        self.assertEqual(trigger["output"], {"exit": "mention", "text": "hi"})
        self.assertEqual(ran, ["mentionPath"])

    def test_trigger_function_shapes_the_event(self) -> None:
        inputs = plugin_triggers.run_inputs("t", {"n": 1})
        trigger, _ran = self._run(_trigger_node("onShaped"), inputs)
        self.assertEqual(trigger["output"], {"shaped": {"n": 1}})

    def test_a_run_started_elsewhere_has_no_event(self) -> None:
        trigger, _ran = self._run(_trigger_node("onShaped"), {"body": {}})
        self.assertEqual(trigger["output"], {"shaped": None})
        inputs = plugin_triggers.run_inputs("another-node", {"n": 1})
        trigger, _ran = self._run(_trigger_node("onShaped"), inputs)
        self.assertEqual(trigger["output"], {"shaped": None})

    def test_a_trigger_without_source_works_as_in_api_1(self) -> None:
        trigger, _ran = self._run(_trigger_node("old"), {"body": {}})
        self.assertEqual(trigger["output"], {"old": True})


class PushHookTests(_PackageTestCase, unittest.IsolatedAsyncioTestCase):
    async def _post(self, body: object, headers: dict | None = None, node_data: dict | None = None):
        node = _trigger_node("onMessage", config={"key": "k-123"}, **(node_data or {}))
        workflow = SimpleNamespace(id=uuid.uuid4(), owner_id=uuid.uuid4())
        found = (workflow, node, self.manifest, self.manifest.get_node("onMessage"))
        request = MagicMock()
        request.method = "POST"
        request.headers = headers or {"X-Key": "k-123"}
        request.query_params = {}
        request.body = AsyncMock(return_value=json.dumps(body).encode())
        started = []

        def start(workflow_id, trigger_node, events):
            passing = [e for e in events if plugin_triggers.event_passes(trigger_node, e)]
            started.extend(passing)
            return len(passing)

        with (
            patch.object(plugins_api, "require_plugins_enabled", return_value=None),
            patch.object(plugin_triggers, "find_hook", AsyncMock(return_value=found)),
            patch.object(plugin_triggers, "start_runs_in_background", start),
        ):
            response = await plugins_api.receive_plugin_hook("acme", "h" * 20, request, MagicMock())
        return response, started

    async def test_one_run_per_event(self) -> None:
        response, started = await self._post({"events": [{"a": 1}, {"a": 2}]})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(json.loads(response.body), {"started": 2})
        self.assertEqual(started, [{"a": 1}, {"a": 2}])

    async def test_ignored_is_204(self) -> None:
        response, started = await self._post({"ignore": True})
        self.assertEqual((response.status_code, started), (204, []))

    async def test_filtered_out_is_204(self) -> None:
        data = {"onlyStartWhen": "$event.a == 2"}
        response, started = await self._post({"events": [{"a": 1}]}, node_data=data)
        self.assertEqual((response.status_code, started), (204, []))

    async def test_refused_sender_is_401(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await self._post({"a": 1}, headers={"X-Key": "wrong"})
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_unknown_hook_is_404(self) -> None:
        with (
            patch.object(plugins_api, "require_plugins_enabled", return_value=None),
            patch.object(plugin_triggers, "find_hook", AsyncMock(return_value=None)),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await plugins_api.receive_plugin_hook("acme", "x" * 20, MagicMock(), MagicMock())
        self.assertEqual(ctx.exception.status_code, 404)


class ListenerTests(_PackageTestCase):
    def test_listener_is_started_again_after_an_error_until_stopped(self) -> None:
        node_def = self.manifest.get_node("onShaped")
        stopping = threading.Event()
        ctx = plugin_triggers.TriggerContext("acme", node_def, uuid.uuid4(), "t", stopping)
        emitted: list = []
        pauses: list[float] = []

        def sleep(seconds: float) -> bool:
            pauses.append(seconds)
            if len(pauses) == 2:
                stopping.set()
            return stopping.is_set()

        plugin_triggers.run_listener(
            self.manifest,
            node_def,
            _trigger_node("onShaped"),
            uuid.uuid4(),
            emitted.append,
            ctx,
            sleep,
        )
        self.assertEqual(emitted, [{"n": 1}, {"n": 2}])
        self.assertEqual(pauses, [5, 5])

    def test_ctx_reads_like_a_dict(self) -> None:
        ctx = plugin_triggers.TriggerContext(
            "acme", self.manifest.get_node("onShaped"), uuid.uuid4(), "t"
        )
        self.assertEqual(
            (ctx["plugin_id"], ctx["node_key"], ctx["node_id"]), ("acme", "onShaped", "t")
        )
        self.assertFalse(ctx.stopping.is_set())


if __name__ == "__main__":
    unittest.main()
