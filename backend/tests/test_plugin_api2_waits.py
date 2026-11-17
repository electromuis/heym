"""Plugin API 2 waits: `ctx.wait` pauses a run, the waits route or a timeout resumes it.

The run tests pause a real workflow and resume it from its snapshot as the background resume does;
the store, the route and the sweep are tested against mocked sessions.
"""

import copy
import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.api import plugins as plugins_api
from app.db.models import PluginWait
from app.services import plugin_loader, plugin_store, plugin_waits
from app.services.workflow_executor import execute_workflow, resume_workflow_execution

_MANIFEST = {
    "pluginApi": 2,
    "id": "acme",
    "name": "Acme",
    "version": "1.0.0",
    "nodes": [
        {
            "key": "job",
            "name": "Remote job",
            "kind": "action",
            "function": "job",
            "fields": [
                {"key": "key", "label": "Key"},
                {"key": "timeout", "label": "Timeout"},
            ],
            "exits": [{"key": "done"}, {"key": "timedOut"}],
        },
        {
            "key": "bare",
            "name": "Waits without timedOut",
            "kind": "action",
            "function": "bare",
            "exits": [{"key": "done"}],
        },
    ],
}

_HANDLER = """
def job(inputs, config, ctx):
    if ctx.resume is not None:
        return {**ctx.resume, "woken": True}
    ctx.wait_token(config["key"])
    if config.get("careless"):
        try:
            ctx.wait(config["key"], timeout=config.get("timeout"))
        except Exception:
            return {"exit": "done", "swallowed": True}
    ctx.wait(config["key"], timeout=config.get("timeout"))


def bare(inputs, config, ctx):
    ctx.wait("bare", timeout=10)
"""


def _node(node_id: str, node_type: str, label: str, **data: object) -> dict:
    return {"id": node_id, "type": node_type, "data": {"label": label, **data}}


def _set(label: str) -> dict:
    return _node(label, "set", label, mappings=[{"key": "x", "value": "1"}])


def _edge(source: str, target: str, handle: str | None = None) -> dict:
    edge = {"id": f"{source}-{target}-{handle}", "source": source, "target": target}
    if handle:
        edge["sourceHandle"] = handle
    return edge


class _PackageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        package = root / "acme"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps(_MANIFEST))
        (package / "handler.py").write_text(_HANDLER)
        plugin_loader.clear_cache()
        self.addCleanup(plugin_loader.clear_cache)
        for patcher in (
            patch.object(plugin_store, "plugins_root", return_value=root),
            patch.object(plugin_waits, "key_is_waiting", return_value=False),
            patch.object(plugin_waits, "new_token", return_value="tok-1"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class WaitRunTests(_PackageTestCase):
    def _graph(self, node_key: str = "job", config: dict | None = None) -> tuple[list, list]:
        nodes = [
            _node("in", "textInput", "msg"),
            _node("j", "plugin", "remote", pluginId="acme", pluginNodeKey=node_key, config=config),
            _set("donePath"),
            _set("timedOutPath"),
            _set("after"),
        ]
        edges = [
            _edge("in", "j"),
            _edge("j", "donePath", "exit-done"),
            _edge("j", "timedOutPath", "exit-timedOut"),
            _edge("donePath", "after"),
            _edge("timedOutPath", "after"),
        ]
        return nodes, edges

    def _pause(self, config: dict | None = None):
        nodes, edges = self._graph(config=config or {"key": "run-42", "timeout": 60})
        return execute_workflow(uuid.uuid4(), nodes, edges, {"text": "hi"})

    def _resume(self, paused, body: dict):
        snapshot = copy.deepcopy(paused.resume_snapshot)
        snapshot["hitl_resume_mode"] = "rerun_agent"  # what persist_pending_plugin_wait stores
        return resume_workflow_execution(snapshot=snapshot, resolved_output=body)

    @staticmethod
    def _ran(result) -> list[str]:
        return sorted(
            r["node_label"]
            for r in result.node_results
            if r["node_type"] == "set" and r["status"] == "success"
        )

    def test_wait_pauses_the_run(self) -> None:
        result = self._pause()
        self.assertEqual(result.status, "pending")
        self.assertEqual(
            result.pending_review,
            {
                "kind": "plugin_wait",
                "plugin_id": "acme",
                "key": "run-42",
                "timeout": 60,
                "token_hash": hashlib.sha256(b"tok-1").hexdigest(),
            },
        )
        self.assertEqual(result.outputs["remote"]["status"], "waiting")
        self.assertIsNotNone(result.outputs["remote"]["expiresAt"])
        self.assertTrue(plugin_waits.is_plugin_wait(result))
        self.assertEqual(self._ran(result), [])
        # The token itself is nowhere in what gets stored.
        self.assertNotIn("tok-1", json.dumps(result.resume_snapshot, default=str))

    def test_handler_catching_exception_does_not_swallow_the_wait(self) -> None:
        self.assertEqual(self._pause({"key": "k", "careless": True}).status, "pending")

    def test_resume_body_is_ctx_resume_and_picks_the_exit(self) -> None:
        resumed = self._resume(self._pause(), {"exit": "done", "answer": 42})
        self.assertEqual(resumed.status, "success")
        node = [r for r in resumed.node_results if r["node_id"] == "j"][-1]
        self.assertEqual(node["output"]["answer"], 42)
        self.assertTrue(node["output"]["woken"])
        self.assertEqual(self._ran(resumed), ["after", "donePath"])

    def test_timeout_takes_timed_out(self) -> None:
        resumed = self._resume(self._pause(), dict(plugin_waits.TIMED_OUT_BODY))
        self.assertEqual(self._ran(resumed), ["after", "timedOutPath"])

    def test_wait_without_timeout_needs_no_timed_out_exit(self) -> None:
        result = self._pause({"key": "run-7"})
        self.assertEqual(result.status, "pending")
        self.assertIsNone(result.outputs["remote"]["expiresAt"])

    def test_timeout_without_timed_out_exit_fails_the_node(self) -> None:
        nodes, edges = self._graph("bare")
        result = execute_workflow(uuid.uuid4(), nodes, edges, {"text": "hi"})
        node = next(r for r in result.node_results if r["node_id"] == "j")
        self.assertEqual(node["status"], "error")
        self.assertIn("timedOut", node["error"])

    def test_a_key_that_already_waits_fails_the_node(self) -> None:
        with patch.object(plugin_waits, "key_is_waiting", return_value=True):
            result = self._pause()
        node = next(r for r in result.node_results if r["node_id"] == "j")
        self.assertEqual(node["status"], "error")
        self.assertIn("already waits", node["error"])


class PersistTests(unittest.IsolatedAsyncioTestCase):
    def _paused(self) -> SimpleNamespace:
        return SimpleNamespace(
            status="pending",
            pending_review={
                "kind": "plugin_wait",
                "plugin_id": "acme",
                "key": "run-42",
                "timeout": 60,
                "token_hash": "h" * 64,
            },
            resume_snapshot={"paused_node_id": "j", "paused_node_label": "remote", "nodes": []},
            outputs={"remote": {"status": "waiting"}},
            node_results=[{"node_id": "j", "status": "pending"}],
            execution_time_ms=5.0,
        )

    async def test_stores_the_wait_and_a_pending_history_row(self) -> None:
        db = MagicMock()
        db.flush = AsyncMock()
        owner = uuid.uuid4()
        workflow = SimpleNamespace(id=uuid.uuid4())
        history, wait = await plugin_waits.persist_pending_plugin_wait(
            db=db,
            workflow=workflow,
            enriched_inputs={"text": "hi"},
            execution_result=self._paused(),
            trigger_source="API",
            credentials_owner_id=owner,
            trace_user_id=None,
            public_base_url="http://heym",
        )
        self.assertEqual(history.status, "pending")
        self.assertIsInstance(wait, PluginWait)
        self.assertEqual((wait.plugin_id, wait.wait_key, wait.node_id), ("acme", "run-42", "j"))
        self.assertEqual(wait.token_hash, "h" * 64)
        self.assertEqual(wait.execution_snapshot["hitl_resume_mode"], "rerun_agent")
        self.assertEqual(wait.execution_snapshot["credentials_owner_id"], str(owner))
        remaining = wait.expires_at - datetime.now(timezone.utc)
        self.assertTrue(timedelta(seconds=50) < remaining <= timedelta(seconds=60))

    async def test_the_hitl_persister_routes_a_plugin_wait(self) -> None:
        from app.services import hitl_service

        stored = (MagicMock(), MagicMock())
        with patch.object(
            plugin_waits, "persist_pending_plugin_wait", AsyncMock(return_value=stored)
        ) as persist:
            got = await hitl_service.persist_pending_hitl_execution(
                db=MagicMock(),
                workflow=MagicMock(),
                enriched_inputs={},
                execution_result=self._paused(),
                trigger_source="API",
                credentials_owner_id=uuid.uuid4(),
                trace_user_id=None,
                public_base_url="http://heym",
            )
        self.assertIs(got, stored)
        persist.assert_awaited_once()


def _wait(status: str = "pending", token: str | None = "tok-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        status=status,
        token_hash=plugin_waits.hash_token(token) if token else None,
    )


class WaitRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _post(
        self,
        wait: SimpleNamespace | None,
        body: bytes = b'{"exit": "done", "answer": 42}',
        token: str | None = "tok-1",
        user: object | None = None,
        has_access: bool = False,
        claimed: bool = True,
    ):
        request = MagicMock()
        request.headers = {"X-Heym-Wait-Token": token} if token else {}
        request.body = AsyncMock(return_value=body)
        db = MagicMock()
        db.commit = AsyncMock()
        db.get = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), owner_id=uuid.uuid4()))
        claim = AsyncMock(return_value=claimed)
        resume = MagicMock()
        with (
            patch.object(plugins_api, "require_plugins_enabled", return_value=None),
            patch.object(plugin_waits, "find_wait", AsyncMock(return_value=wait)),
            patch.object(plugin_waits, "claim_wait", claim),
            patch.object(plugin_waits, "resume_in_background", resume),
            patch(
                "app.services.workflow_access.user_has_workflow_access",
                AsyncMock(return_value=has_access),
            ),
        ):
            response = await plugins_api.resume_plugin_wait("acme", "run-42", request, db, user)
        return response, claim, resume

    async def test_the_token_resumes_with_the_body(self) -> None:
        wait = _wait()
        response, claim, resume = await self._post(wait)
        self.assertEqual(response.status_code, 202)
        claim.assert_awaited_once()
        self.assertEqual(claim.await_args.kwargs["body"], {"exit": "done", "answer": 42})
        self.assertEqual(claim.await_args.kwargs["status"], "resumed")
        resume.assert_called_once_with(wait.id)

    async def test_a_signed_in_user_with_access_may_resume(self) -> None:
        response, _, _ = await self._post(
            _wait(token=None), token=None, user=SimpleNamespace(id=uuid.uuid4()), has_access=True
        )
        self.assertEqual(response.status_code, 202)

    async def _status_of(self, **kwargs: object) -> int:
        with self.assertRaises(HTTPException) as ctx:
            await self._post(**kwargs)
        return ctx.exception.status_code

    async def test_errors(self) -> None:
        user = SimpleNamespace(id=uuid.uuid4())
        self.assertEqual(await self._status_of(wait=None), 404)
        self.assertEqual(await self._status_of(wait=_wait(), token="wrong"), 401)
        self.assertEqual(await self._status_of(wait=_wait(), token=None, user=user), 401)
        self.assertEqual(await self._status_of(wait=_wait(status="resumed")), 409)
        self.assertEqual(await self._status_of(wait=_wait(), claimed=False), 409)
        self.assertEqual(await self._status_of(wait=_wait(), body=b"[1, 2]"), 422)
        self.assertEqual(await self._status_of(wait=_wait(), body=b"{nope"), 422)

    async def test_an_empty_body_is_an_empty_object(self) -> None:
        _, claim, _ = await self._post(_wait(), body=b"")
        self.assertEqual(claim.await_args.kwargs["body"], {})


class SweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_passed_timeouts_resume_with_timed_out(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        rows = MagicMock()
        rows.scalars.return_value.all.return_value = ids
        db = MagicMock()
        db.execute = AsyncMock(return_value=rows)
        db.commit = AsyncMock()
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=db)
        session.__aexit__ = AsyncMock(return_value=False)
        # The second wait was resumed by someone else in between.
        claim = AsyncMock(side_effect=[True, False])
        resume = MagicMock()
        with (
            patch("app.db.session.async_session_maker", MagicMock(return_value=session)),
            patch.object(plugin_waits, "claim_wait", claim),
            patch.object(plugin_waits, "resume_in_background", resume),
        ):
            count = await plugin_waits.sweep_timeouts()
        self.assertEqual(count, 1)
        self.assertEqual(claim.await_args_list[0].kwargs["body"], {"exit": "timedOut"})
        self.assertEqual(claim.await_args_list[0].kwargs["status"], "timed_out")
        resume.assert_called_once_with(ids[0])


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_claimed_wait_is_resumed(self) -> None:
        db = MagicMock()
        db.get = AsyncMock(return_value=SimpleNamespace(status="pending"))
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=db)
        session.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("app.db.session.async_session_maker", MagicMock(return_value=session)),
            patch.object(plugin_waits, "_resume", AsyncMock()) as resume,
        ):
            await plugin_waits.resume_plugin_wait(uuid.uuid4())
        resume.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
