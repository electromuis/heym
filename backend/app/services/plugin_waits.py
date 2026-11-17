"""Plugin API 2 waits: a plugin node's ``ctx.wait`` pauses its run until something outside answers.

Modelled on Codex follow-ups: the node returns a pending result whose metadata kind is
``plugin_wait``; the pause is stored as a ``PluginWait`` row with the resume snapshot, the run's
history row stays ``pending``. ``POST /api/plugins/{id}/waits/{key}`` (or the timeout sweep)
claims the row and resumes the run in the background: the paused node runs again with
``ctx.resume`` set to the body. See ``docs/plugin-api-2.md``, "Pausing a run".
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import hmac
import logging
import secrets
import uuid
from concurrent.futures import CancelledError
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import ExecutionHistory, PluginWait, Workflow

logger = logging.getLogger("heym.plugins")

PENDING_KIND = "plugin_wait"
TIMED_OUT_BODY: dict[str, str] = {"exit": "timedOut"}
MAX_KEY_LENGTH = 255
SWEEP_SECONDS = 30.0


class PluginWaitRequested(BaseException):
    """Raised by ``ctx.wait``. A BaseException so a handler's ``except Exception`` can't swallow it."""

    def __init__(self, key: str, timeout: float | None, token_hash: str | None) -> None:
        super().__init__(f"waiting on {key}")
        self.key = key
        self.timeout = timeout
        self.token_hash = token_hash


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(wait: PluginWait, token: str | None) -> bool:
    if not token or not wait.token_hash:
        return False
    return hmac.compare_digest(wait.token_hash, hash_token(token))


def check_key(key: object) -> str:
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LENGTH:
        raise ValueError(f"ctx.wait: the key must be a text of 1 to {MAX_KEY_LENGTH} characters")
    return key


def pending_metadata(plugin_id: str, request: PluginWaitRequested) -> dict:
    """The node result's metadata; ``_extract_pending_metadata`` reads it from ``hitl``."""
    return {
        "hitl": {
            "kind": PENDING_KIND,
            "plugin_id": plugin_id,
            "key": request.key,
            "timeout": request.timeout,
            "token_hash": request.token_hash,
        }
    }


def pending_output(request: PluginWaitRequested) -> dict:
    expires_at = _expires_at(request.timeout)
    return {
        "status": "waiting",
        "key": request.key,
        "expiresAt": expires_at.isoformat() if expires_at else None,
    }


def _expires_at(timeout: float | None) -> datetime | None:
    if timeout is None:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=float(timeout))


def is_plugin_wait(execution_result: Any) -> bool:
    pending = getattr(execution_result, "pending_review", None) or {}
    return isinstance(pending, dict) and pending.get("kind") == PENDING_KIND


def key_is_waiting(plugin_id: str, key: str) -> bool:
    """Whether a run already waits on this package's key (checked from the node's thread)."""
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        found = db.execute(
            select(PluginWait.id).where(
                PluginWait.plugin_id == plugin_id,
                PluginWait.wait_key == key,
                PluginWait.status == "pending",
            )
        ).first()
        return found is not None


def _bucket_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    return raw.isoformat() if hasattr(raw, "isoformat") else str(raw)


async def persist_pending_plugin_wait(
    *,
    db: AsyncSession,
    workflow: Workflow,
    enriched_inputs: dict,
    execution_result: Any,
    trigger_source: str | None,
    credentials_owner_id: uuid.UUID,
    trace_user_id: uuid.UUID | None,
    public_base_url: str,
    history_entry: ExecutionHistory | None = None,
    history_entry_id: uuid.UUID | None = None,
) -> tuple[ExecutionHistory, PluginWait]:
    """Store the pause: the history row (``pending``) and the wait with its resume snapshot."""
    if execution_result.status != "pending" or not execution_result.resume_snapshot:
        raise ValueError("persist_pending_plugin_wait requires a pending result with a snapshot")
    pending = execution_result.pending_review or {}
    snapshot = copy.deepcopy(execution_result.resume_snapshot)
    snapshot["actor_user_id"] = snapshot.get("actor_user_id") or str(credentials_owner_id)
    snapshot["credentials_owner_id"] = str(credentials_owner_id)
    snapshot["trace_user_id"] = str(trace_user_id) if trace_user_id else None
    snapshot["trigger_source"] = trigger_source
    snapshot["public_base_url"] = public_base_url
    # The node runs again with the body as hitl_resume_context[node_id], read as ctx.resume.
    snapshot["hitl_resume_mode"] = "rerun_agent"

    known_bucket = snapshot.get("analytics_bucket_time") or snapshot.get("analytics_bucket_start")
    if history_entry is None:
        started = datetime.now(timezone.utc)
        history_entry = ExecutionHistory(
            **({"id": history_entry_id} if history_entry_id is not None else {}),
            workflow_id=workflow.id,
            inputs=enriched_inputs,
            outputs={},
            node_results=[],
            status="pending",
            execution_time_ms=execution_result.execution_time_ms,
            trigger_source=trigger_source,
            started_at=started,
        )
        db.add(history_entry)
        snapshot["analytics_bucket_time"] = _bucket_text(known_bucket or started.isoformat())
    elif known_bucket:
        snapshot["analytics_bucket_time"] = _bucket_text(known_bucket)
    else:
        snapshot.pop("analytics_bucket_time", None)
    snapshot.pop("analytics_bucket_start", None)
    if history_entry.started_at is None:
        history_entry.started_at = datetime.now(timezone.utc)
    execution_result.resume_snapshot = copy.deepcopy(snapshot)

    history_entry.status = "pending"
    history_entry.inputs = enriched_inputs
    history_entry.outputs = copy.deepcopy(execution_result.outputs)
    history_entry.node_results = copy.deepcopy(execution_result.node_results)
    history_entry.execution_time_ms = execution_result.execution_time_ms
    history_entry.trigger_source = trigger_source
    await db.flush()

    wait = PluginWait(
        plugin_id=str(pending.get("plugin_id") or ""),
        wait_key=str(pending.get("key") or ""),
        workflow_id=workflow.id,
        execution_history_id=history_entry.id,
        node_id=snapshot["paused_node_id"],
        node_label=snapshot["paused_node_label"],
        token_hash=pending.get("token_hash") or None,
        execution_snapshot=snapshot,
        status="pending",
        resume_body={},
        expires_at=_expires_at(pending.get("timeout")),
    )
    db.add(wait)
    await db.flush()
    return history_entry, wait


async def find_wait(db: AsyncSession, plugin_id: str, key: str) -> PluginWait | None:
    """The pending wait on this key, else the latest ended one (for a 409), else None."""
    rows = await db.execute(
        select(PluginWait)
        .where(PluginWait.plugin_id == plugin_id, PluginWait.wait_key == key)
        .order_by((PluginWait.status == "pending").desc(), PluginWait.created_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def claim_wait(db: AsyncSession, wait_id: uuid.UUID, *, body: dict, status: str) -> bool:
    """End a pending wait with this body; exactly one of concurrent callers wins."""
    result = await db.execute(
        update(PluginWait)
        .where(PluginWait.id == wait_id, PluginWait.status == "pending")
        .values(status=status, resume_body=body, resolved_at=datetime.now(timezone.utc))
    )
    return result.rowcount == 1


_background: set[asyncio.Task] = set()


def resume_in_background(wait_id: uuid.UUID) -> None:
    task = asyncio.create_task(resume_plugin_wait(wait_id))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def resume_plugin_wait(wait_id: uuid.UUID) -> None:
    """Run the paused workflow on from the waiting node, then write its history like a resumed review."""
    from app.db.session import async_session_maker

    async with async_session_maker() as db:
        wait = await db.get(PluginWait, wait_id)
        if wait is None or wait.status not in ("resumed", "timed_out"):
            return
        workflow = await db.get(Workflow, wait.workflow_id)
        history_entry = await db.get(ExecutionHistory, wait.execution_history_id)
        if workflow is None or history_entry is None:
            return
        try:
            await _resume(db, wait, workflow, history_entry)
        except Exception as exc:
            logger.exception("Resuming plugin wait %s failed", wait.id)
            wait.resume_error = str(exc)
            history_entry.status = "error"
            history_entry.outputs = {"error": str(exc)}
            flag_modified(history_entry, "outputs")
        await db.commit()
    from app.services.board_run_service import resume_card_chain

    await resume_card_chain(wait.execution_history_id)


async def _resume(
    db: AsyncSession, wait: PluginWait, workflow: Workflow, history_entry: ExecutionHistory
) -> None:
    from app.api.analytics import (
        resolve_execution_analytics_bucket,
        upsert_workflow_analytics_snapshot,
    )
    from app.api.workflows import _persist_global_variables_from_execution, get_credentials_context
    from app.services.global_variables_service import get_global_variables_context
    from app.services.hitl_service import build_default_public_base_url
    from app.services.pending_execution import persist_pending_execution
    from app.services.workflow_executor import _to_json_compatible, resume_workflow_execution

    snapshot = copy.deepcopy(wait.execution_snapshot or {})
    owner_value = snapshot.get("credentials_owner_id")
    if not owner_value:
        raise ValueError("Missing credentials_owner_id in the wait's snapshot")
    owner_id = uuid.UUID(str(owner_value))
    trace_value = snapshot.get("trace_user_id")
    trace_user_id = uuid.UUID(str(trace_value)) if trace_value else None
    trigger_source = snapshot.get("trigger_source") or history_entry.trigger_source
    is_already_counted = trigger_source not in ("board", "portal")
    bucket = await resolve_execution_analytics_bucket(
        db,
        workflow_id=workflow.id,
        owner_id=workflow.owner_id,
        candidate_time=wait.created_at,
        snapshot=snapshot,
        history_started_at=history_entry.started_at,
        is_already_counted=is_already_counted,
    )
    credentials_context = await get_credentials_context(db, owner_id)
    global_variables_context = await get_global_variables_context(db, owner_id)
    result = await asyncio.to_thread(
        resume_workflow_execution,
        snapshot=snapshot,
        resolved_output=copy.deepcopy(wait.resume_body or {}),
        credentials_context=credentials_context,
        global_variables_context=global_variables_context,
        trace_user_id=trace_user_id,
    )

    if result.status == "pending":
        if isinstance(result.resume_snapshot, dict) and snapshot.get("analytics_bucket_time"):
            result.resume_snapshot.setdefault(
                "analytics_bucket_time", snapshot["analytics_bucket_time"]
            )
        await persist_pending_execution(
            db=db,
            workflow=workflow,
            enriched_inputs=history_entry.inputs,
            execution_result=result,
            trigger_source=snapshot.get("trigger_source"),
            credentials_owner_id=owner_id,
            trace_user_id=trace_user_id,
            public_base_url=build_default_public_base_url(),
            history_entry=history_entry,
        )
        return

    if getattr(result, "allow_downstream_pending", False):
        try:
            await asyncio.to_thread(result.join_allow_downstream)
        except (CancelledError, asyncio.CancelledError):
            result.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - the run's own error becomes its status
            result.status = "error"
            if isinstance(result.outputs, dict):
                result.outputs.setdefault("error", str(exc))

    history_entry.status = result.status
    history_entry.outputs = _to_json_compatible(result.outputs)
    history_entry.node_results = _to_json_compatible(result.node_results)
    history_entry.execution_time_ms = result.execution_time_ms
    flag_modified(history_entry, "outputs")
    flag_modified(history_entry, "node_results")
    for sub in result.sub_workflow_executions:
        db.add(
            ExecutionHistory(
                workflow_id=uuid.UUID(str(sub.workflow_id)),
                inputs=_to_json_compatible(sub.inputs),
                outputs=_to_json_compatible(sub.outputs),
                node_results=_to_json_compatible(sub.node_results),
                status=sub.status,
                execution_time_ms=sub.execution_time_ms,
                trigger_source=sub.trigger_source,
            )
        )
    await _persist_global_variables_from_execution(
        db,
        owner_id,
        snapshot.get("nodes") or [],
        snapshot.get("workflow_cache") or {},
        result.node_results,
        result.sub_workflow_executions,
    )
    if bucket is not None:
        await upsert_workflow_analytics_snapshot(
            db,
            workflow_id=workflow.id,
            owner_id=workflow.owner_id,
            workflow_name_snapshot=workflow.name,
            status=result.status,
            execution_time_ms=result.execution_time_ms,
            started_at=bucket,
            count_execution=not is_already_counted,
        )


async def sweep_timeouts(now: datetime | None = None) -> int:
    """End the waits whose timeout passed with ``{"exit": "timedOut"}`` and resume their runs."""
    from app.db.session import async_session_maker

    now = now or datetime.now(timezone.utc)
    claimed: list[uuid.UUID] = []
    async with async_session_maker() as db:
        rows = await db.execute(
            select(PluginWait.id).where(
                PluginWait.status == "pending",
                PluginWait.expires_at.is_not(None),
                PluginWait.expires_at <= now,
            )
        )
        for wait_id in rows.scalars().all():
            if await claim_wait(db, wait_id, body=dict(TIMED_OUT_BODY), status="timed_out"):
                claimed.append(wait_id)
        await db.commit()
    for wait_id in claimed:
        resume_in_background(wait_id)
    return len(claimed)


class PluginWaitSweeper:
    """Checks for passed timeouts every ``SWEEP_SECONDS``; the claim makes it safe on every instance."""

    def __init__(self, interval: float = SWEEP_SECONDS) -> None:
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await sweep_timeouts()
            except Exception:  # noqa: BLE001 - a bad sweep must not stop the next one
                logger.exception("Plugin wait timeout sweep failed")
            await asyncio.sleep(self.interval)


plugin_wait_sweeper = PluginWaitSweeper()
