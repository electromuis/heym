"""Credentials of a type a plugin package declares (Plugin API 2, docs/plugin-api-2.md).

They are stored as Heym credential type ``plugin`` with the config
``{"plugin_id": ..., "credential_type": ..., "values": {...}}``, encrypted like any credential.
This module checks such a config against the installed package, merges an update, says what the
browser may see, and hands a node's handler the decrypted values. Error texts name a field, never
a value.
"""

from __future__ import annotations

import json
from typing import Any

from app.models.plugin_schemas import PluginCredentialTypeDef, PluginManifest
from app.services import plugin_loader, plugin_store
from app.services.encryption import mask_api_key

PLUGIN_ID_KEY = "plugin_id"
TYPE_KEY = "credential_type"
VALUES_KEY = "values"
_CONFIG_KEYS = {PLUGIN_ID_KEY, TYPE_KEY, VALUES_KEY}


class PluginCredentialError(ValueError):
    """A plugin credential config that doesn't fit its declared type."""


def definition_for(config: dict) -> tuple[PluginManifest, PluginCredentialTypeDef]:
    """The installed package and credential type a config belongs to."""
    plugin_id = str(config.get(PLUGIN_ID_KEY) or "")
    type_key = str(config.get(TYPE_KEY) or "")
    if not plugin_id or not type_key:
        raise PluginCredentialError("A plugin credential needs plugin_id and credential_type")
    try:
        manifest = plugin_loader.load_manifest(plugin_id, plugin_store.plugins_root())
    except plugin_loader.PluginRuntimeError as exc:
        raise PluginCredentialError(f"Plugin '{plugin_id}' is not installed") from exc
    definition = manifest.get_credential_type(type_key)
    if definition is None:
        raise PluginCredentialError(f"Plugin '{plugin_id}' has no credential type '{type_key}'")
    return manifest, definition


def validate(config: dict) -> None:
    """Refuse a config with unknown keys, missing required values or values of the wrong kind."""
    unknown = set(config) - _CONFIG_KEYS
    if unknown:
        raise PluginCredentialError(f"Unknown key in plugin credential: {sorted(unknown)[0]}")
    _manifest, definition = definition_for(config)
    values = config.get(VALUES_KEY)
    if not isinstance(values, dict):
        raise PluginCredentialError("A plugin credential needs `values`")
    fields = {f.key: f for f in definition.fields}
    for key in values:
        if key not in fields:
            raise PluginCredentialError(f"{definition.name}: unknown field {key}")
    for field in definition.fields:
        value = values.get(field.key)
        empty = value is None or (isinstance(value, str) and not value.strip())
        if empty:
            if field.required:
                raise PluginCredentialError(f"{definition.name}: {field.label} is required")
            continue
        if field.type == "number" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise PluginCredentialError(f"{definition.name}: {field.label} must be a number")
        if field.type == "boolean" and not isinstance(value, bool):
            raise PluginCredentialError(f"{definition.name}: {field.label} must be yes or no")
        if field.type in ("string", "text", "select") and not isinstance(value, str):
            raise PluginCredentialError(f"{definition.name}: {field.label} must be text")
        if field.type == "select" and field.options:
            if value not in {o.value for o in field.options}:
                raise PluginCredentialError(f"{definition.name}: {field.label} is not an option")


def merge_for_update(stored: dict, incoming: dict) -> dict:
    """An edit of a stored config: a blank secret keeps the stored one; the package and type stay."""
    for key in (PLUGIN_ID_KEY, TYPE_KEY):
        if key in incoming and incoming[key] != stored.get(key):
            raise PluginCredentialError("A plugin credential can't change its package or type")
    _manifest, definition = definition_for(stored)
    merged_values = dict(stored.get(VALUES_KEY) or {})
    secrets = definition.secret_keys()
    for key, value in (incoming.get(VALUES_KEY) or {}).items():
        if key in secrets and (value is None or str(value).strip() == ""):
            continue
        merged_values[key] = value
    return {
        PLUGIN_ID_KEY: stored.get(PLUGIN_ID_KEY),
        TYPE_KEY: stored.get(TYPE_KEY),
        VALUES_KEY: merged_values,
    }


def _secret_keys_or_all(config: dict) -> set[str] | None:
    """The secret fields of the config's type; None when the package is gone (treat all as secret)."""
    try:
        return definition_for(config)[1].secret_keys()
    except PluginCredentialError:
        return None


def secret_values(config: dict) -> list[str]:
    """The secret values of a config as text, for masking run output."""
    values = config.get(VALUES_KEY) or {}
    secrets = _secret_keys_or_all(config)
    return [
        str(value)
        for key, value in values.items()
        if (secrets is None or key in secrets) and value not in (None, "")
    ]


def masked_value(config: dict) -> str | None:
    """What the credential list shows: the first secret, masked."""
    secrets = secret_values(config)
    return mask_api_key(secrets[0]) if secrets else None


def public_fields(config: dict) -> dict[str, str | None]:
    """What the browser may see: the package, the type and the values of non-secret fields."""
    out: dict[str, str | None] = {
        PLUGIN_ID_KEY: str(config.get(PLUGIN_ID_KEY) or "") or None,
        TYPE_KEY: str(config.get(TYPE_KEY) or "") or None,
    }
    secrets = _secret_keys_or_all(config)
    if secrets is None:
        return out
    for key, value in (config.get(VALUES_KEY) or {}).items():
        if key in secrets:
            out[f"{key}_set"] = "true" if value not in (None, "") else None
        else:
            out[key] = None if value is None else _as_text(value)
    return out


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def values_for_run(
    config: dict, plugin_id: str, credential_type: str, field_label: str
) -> dict[str, Any]:
    """The decrypted values a handler gets for a `credential` field, after checking they fit it."""
    if config.get(PLUGIN_ID_KEY) != plugin_id or config.get(TYPE_KEY) != credential_type:
        raise ValueError(
            f"{field_label}: the chosen credential is not of the type this field needs"
        )
    return dict(config.get(VALUES_KEY) or {})


def run_test(config: dict) -> tuple[bool, str]:
    """Call the type's `test` function; it returns nothing or raises with a message."""
    manifest, definition = definition_for(config)
    if not definition.test:
        return False, f"{definition.name} has no connection test"
    try:
        plugin_loader.call_handler(
            manifest.id,
            plugin_store.plugins_root(),
            definition.test,
            {"values": dict(config.get(VALUES_KEY) or {}), "ctx": {"plugin_id": manifest.id}},
        )
    except plugin_loader.PluginRuntimeError as exc:
        cause = exc.__cause__
        message = str(cause) if cause is not None else str(exc)
        return False, mask_text(message, secret_values(config))
    return True, "Connection works"


def mask_text(text: str, secrets: list[str]) -> str:
    """Replace secret values in a text the way Heym masks credentials in output."""
    for secret in secrets:
        # As mask_sensitive_text: shorter values would mask ordinary words.
        if len(secret) > 7:
            text = text.replace(secret, secret[:7] + "**")
    return text


def mask_output(output: dict, secrets: list[str]) -> dict:
    """A node's output with the secrets of its credentials masked, as run history shows them."""
    if not secrets:
        return output
    return json.loads(mask_text(json.dumps(output, ensure_ascii=False, default=str), secrets))
