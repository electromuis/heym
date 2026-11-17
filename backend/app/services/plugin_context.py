"""What a Plugin API 2 handler gets besides its inputs: the resolved config and the `ctx` object.

API 1 handlers keep the plain dict ``{"node_id", "node_label"}`` and the shallow ``$`` resolution in
``plugin_node._resolve_config``. Everything here applies only to packages with ``"pluginApi": 2``
(see ``docs/plugin-api-2.md``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

from app.models.plugin_schemas import PluginField, PluginManifest, PluginNodeDef
from app.services import plugin_credentials
from app.services.node_execution.base import NodeExecutionContext

logger = logging.getLogger("heym.plugins")


class PluginContext(Mapping):
    """The ``ctx`` of an API 2 handler. It still reads like the API 1 dict (``ctx["node_id"]``)."""

    def __init__(
        self,
        exec_ctx: NodeExecutionContext,
        manifest: PluginManifest,
        node_def: PluginNodeDef,
        resume: dict | None = None,
        loop: dict | None = None,
    ) -> None:
        self._exec = exec_ctx
        self.plugin_id = manifest.id
        self.node_key = node_def.key
        self.node_id = exec_ctx.node_id
        self.node_label = exec_ctx.node_label
        executor = exec_ctx.executor
        self.workflow_id = _text_or_none(getattr(executor, "workflow_id", None))
        self.execution_id = _text_or_none(getattr(executor, "execution_id", None))
        self.resume = resume
        # A loop node's pass: {"index", "state"} (docs/plugin-api-2.md, "Loop nodes"); None elsewhere.
        self.loop = loop
        self._tokens: dict[str, str] = {}

    def _as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_label": self.node_label,
            "workflow_id": self.workflow_id,
            "execution_id": self.execution_id,
        }

    def __getitem__(self, key: str) -> Any:
        return self._as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._as_dict())

    def __len__(self) -> int:
        return len(self._as_dict())

    def log(self, text: str) -> None:
        """A line in Heym's log, tagged with the plugin and the node."""
        logger.info("plugin %s node %s: %s", self.plugin_id, self.node_label, text)

    def wait_token(self, key: str) -> str:
        """The token that may resume a wait on `key`, minted once per key; only its hash is stored."""
        from app.services import plugin_waits

        key = plugin_waits.check_key(key)
        if key not in self._tokens:
            self._tokens[key] = plugin_waits.new_token()
        return self._tokens[key]

    def wait(self, key: str, timeout: float | None = None) -> None:
        """Pause the run until `POST /api/plugins/{id}/waits/{key}` or the timeout; never returns."""
        from app.services import plugin_waits

        key = plugin_waits.check_key(key)
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            raise ValueError("ctx.wait: timeout must be a positive number of seconds")
        if plugin_waits.key_is_waiting(self.plugin_id, key):
            raise ValueError(f"ctx.wait: a run already waits on the key {key!r}")
        token = self._tokens.get(key)
        raise plugin_waits.PluginWaitRequested(
            key, timeout, plugin_waits.hash_token(token) if token else None
        )

    def check(self, condition: str) -> bool:
        """Evaluate a Condition-language text on the node's inputs; raises with its error."""
        executor = self._exec.executor
        return bool(
            executor.evaluate_condition_strict(condition, self._exec.inputs, self._exec.node_id)
        )

    def evaluate(self, expression: str) -> Any:
        """Resolve a `$`-expression on the node's inputs, keeping its type."""
        return self._exec.executor.resolve_expression(
            expression, self._exec.inputs, self._exec.node_id, preserve_type=True
        )


def _text_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def shown(field: PluginField, values: Mapping[str, Any]) -> bool:
    """Whether a field's `showIf` holds for the form's values."""
    for other, wanted in (field.show_if or {}).items():
        value = values.get(other)
        if isinstance(wanted, list):
            if value not in wanted:
                return False
        elif value != wanted:
            return False
    return True


def resolve_config(
    exec_ctx: NodeExecutionContext,
    fields: list[PluginField],
    raw: Mapping[str, Any],
    plugin_id: str = "",
    secrets: list[str] | None = None,
) -> dict:
    """The config an API 2 handler gets.

    A missing value takes its field's default; a field hidden by `showIf` is left out; `$` values are
    resolved, also inside list rows, keyValue values and json strings. A `condition` stays text, for
    the handler to check with `ctx.check`. A `credential` becomes its decrypted values, and their
    secrets are added to `secrets` for masking. Keys the form doesn't define pass through as saved.
    """
    secrets = secrets if secrets is not None else []
    with_defaults = dict(raw)
    for field in fields:
        if field.key not in with_defaults and field.default is not None:
            with_defaults[field.key] = field.default
    out: dict = {}
    defined = {f.key for f in fields}
    for key, value in with_defaults.items():
        if key not in defined:
            out[key] = _resolve_value(exec_ctx, value)
    for field in fields:
        if field.key not in with_defaults or not shown(field, with_defaults):
            continue
        value = with_defaults[field.key]
        out[field.key] = _resolve_field(exec_ctx, field, value, plugin_id, secrets)
    return out


def _resolve_field(
    exec_ctx: NodeExecutionContext,
    field: PluginField,
    value: Any,
    plugin_id: str,
    secrets: list[str],
) -> Any:
    if field.type == "condition":
        return value
    if field.type == "credential":
        return _credential_values(exec_ctx, plugin_id, field, value, secrets)
    if field.type == "list":
        rows = value if isinstance(value, list) else []
        return [
            resolve_config(exec_ctx, field.fields, row, plugin_id, secrets)
            if isinstance(row, Mapping)
            else row
            for row in rows
        ]
    return _resolve_value(exec_ctx, value)


def _credential_values(
    exec_ctx: NodeExecutionContext,
    plugin_id: str,
    field: PluginField,
    credential_id: Any,
    secrets: list[str],
) -> dict | None:
    if credential_id in (None, ""):
        return None
    config = load_credential_config(exec_ctx, credential_id)
    if config is None:
        raise ValueError(f"{field.label}: the chosen credential was not found")
    values = plugin_credentials.values_for_run(
        config, plugin_id, field.credential_type or "", field.label
    )
    secrets.extend(plugin_credentials.secret_values(config))
    return values


def load_credential_config(exec_ctx: NodeExecutionContext, credential_id: Any) -> dict | None:
    """The decrypted config of a `plugin` credential the run's user may use, else None."""
    from app.db.models import CredentialType
    from app.db.session import SessionLocal
    from app.services.encryption import decrypt_config

    try:
        credential_uuid = uuid.UUID(str(credential_id))
    except ValueError:
        return None
    with SessionLocal() as db:
        credential = exec_ctx.executor._get_accessible_credential(db, credential_uuid)
        if credential is None or credential.type != CredentialType.plugin:
            return None
        return decrypt_config(credential.encrypted_config)


def _resolve_value(exec_ctx: NodeExecutionContext, value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("$"):
            return exec_ctx.executor.resolve_expression(
                value, exec_ctx.inputs, exec_ctx.node_id, preserve_type=True
            )
        return value
    if isinstance(value, list):
        return [_resolve_value(exec_ctx, item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(exec_ctx, item) for key, item in value.items()}
    return value


def inputs_by_key(exec_ctx: NodeExecutionContext, node_def: PluginNodeDef) -> dict:
    """The output that arrived at each named input (`input-<key>` handles) of the node."""
    if not node_def.inputs:
        return {}
    executor = exec_ctx.executor
    nodes = getattr(executor, "nodes", {}) or {}
    out: dict = {}
    for edge in getattr(executor, "edges", []) or []:
        handle = edge.get("targetHandle") or ""
        if edge.get("target") != exec_ctx.node_id or not handle.startswith("input-"):
            continue
        source = nodes.get(edge.get("source"), {})
        label = (source.get("data") or {}).get("label") or edge.get("source")
        if label in exec_ctx.inputs:
            out[handle.removeprefix("input-")] = exec_ctx.inputs[label]
    return out


def route_exit(
    exec_ctx: NodeExecutionContext, node_def: PluginNodeDef, config: Mapping, output: dict
) -> None:
    """Pick the branch of the node's `exit` as Switch picks its case; refuse an undeclared exit."""
    keys = node_def.exit_keys(dict(config))
    if not keys:
        raise ValueError(
            f"Plugin node {node_def.key} has no exits for this config; add a row or a fixed exit"
        )
    chosen = output.get("exit", keys[0])
    if chosen not in keys:
        raise ValueError(
            f"Plugin node {node_def.key} returned an unknown exit; declared: {', '.join(keys)}"
        )
    output["exit"] = chosen
    selected = f"exit-{chosen}"
    handles = [f"exit-{key}" for key in keys]
    output["_skip_loop_source_handles"] = [h for h in handles if h != selected]
    if not exec_ctx.allow_branch_skip:
        return
    executor = exec_ctx.executor
    node_id = exec_ctx.node_id
    active_targets = executor.get_downstream_nodes(node_id, selected)
    inactive_targets: list[str] = []
    for handle in handles:
        if handle != selected:
            inactive_targets.extend(executor.get_downstream_nodes(node_id, handle))
    loop_back_targets = executor._loop_back_targets_for_source_handle(node_id, selected)
    with executor.lock:
        executor.skip_branch_targets_preserving_shared_downstream(
            node_id,
            active_targets=active_targets,
            inactive_targets=inactive_targets,
            active_exclude_node_ids=loop_back_targets,
            inactive_stop_node_ids=loop_back_targets,
        )
