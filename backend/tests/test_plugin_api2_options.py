"""Plugin API 2 loaded options: POST /api/plugins/{id}/options calls `options_<field>`."""

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.api import plugins as plugins_api
from app.models.plugin_schemas import PluginManifest
from app.services import plugin_loader, plugin_store

_MANIFEST = {
    "pluginApi": 2,
    "id": "acme",
    "name": "Acme",
    "version": "1.0.0",
    "credentials": [
        {
            "key": "hub",
            "name": "Hub",
            "fields": [{"key": "token", "label": "Token", "secret": True}],
        }
    ],
    "nodes": [
        {
            "key": "run",
            "name": "Run",
            "kind": "action",
            "fields": [
                {"key": "hub", "label": "Hub", "type": "credential", "credentialType": "hub"},
                {"key": "agent", "label": "Agent", "type": "select", "loadOptions": True},
                {"key": "rooms", "label": "Rooms", "type": "multiselect", "loadOptions": "rooms"},
                {"key": "bad", "label": "Bad", "type": "select", "loadOptions": True},
                {"key": "plain", "label": "Plain", "type": "select"},
            ],
        }
    ],
}

_HANDLER = """
def options_agent(config, ctx):
    hub = config.get("hub") or {}
    return [{"label": "Penny", "value": "penny"}, {"value": hub.get("token", "none")}]


def rooms(config, ctx):
    return ["general", "ops"]


def options_bad(config, ctx):
    raise ValueError("hub refused " + str((config.get("hub") or {}).get("token")))
"""

_TOKEN = "secret-hub-token-123"


class PluginOptionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "acme").mkdir()
        (root / "acme" / "plugin.json").write_text(json.dumps(_MANIFEST))
        (root / "acme" / "handler.py").write_text(_HANDLER)
        plugin_loader.clear_cache()
        self.addCleanup(plugin_loader.clear_cache)
        for patcher in (
            patch.object(plugin_store, "plugins_root", return_value=root),
            patch.object(plugins_api, "require_plugins_enabled", return_value=None),
            patch.object(
                plugins_api,
                "_enabled_manifest",
                AsyncMock(return_value=PluginManifest.model_validate(_MANIFEST)),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.user = SimpleNamespace(id=uuid.uuid4())

    async def _options(self, field: str, config: dict | None = None, credential=None):
        body = plugins_api.PluginOptionsRequest(node="run", field=field, config=config or {})
        with (
            patch(
                "app.api.credentials._get_accessible_credential", AsyncMock(return_value=credential)
            ),
            patch.object(plugins_api, "decrypt_config", lambda _c: credential.config),
        ):
            options = await plugins_api.load_plugin_options("acme", body, self.user, MagicMock())
        return [(o.label, o.value) for o in options]

    def _credential(self) -> SimpleNamespace:
        from app.db.models import CredentialType

        return SimpleNamespace(
            type=CredentialType.plugin,
            encrypted_config="x",
            config={"plugin_id": "acme", "credential_type": "hub", "values": {"token": _TOKEN}},
        )

    async def test_options_from_the_handler(self) -> None:
        self.assertEqual(await self._options("rooms"), [("general", "general"), ("ops", "ops")])

    async def test_credential_field_is_resolved_for_the_user(self) -> None:
        options = await self._options("agent", {"hub": str(uuid.uuid4())}, self._credential())
        self.assertEqual(options, [("Penny", "penny"), (_TOKEN, _TOKEN)])

    async def test_without_credential(self) -> None:
        self.assertEqual((await self._options("agent"))[1], ("none", "none"))

    async def test_failure_is_422_and_masked(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await self._options("bad", {"hub": str(uuid.uuid4())}, self._credential())
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertNotIn(_TOKEN, ctx.exception.detail)
        self.assertIn("hub refused secret-**", ctx.exception.detail)

    async def test_field_without_loaded_options_is_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await self._options("plain")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_slow_function_times_out(self) -> None:
        with (
            patch.object(plugins_api, "OPTIONS_TIMEOUT_SECONDS", 0.05),
            patch.object(
                plugin_loader, "call_handler", lambda *_a, **_k: __import__("time").sleep(1)
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await self._options("rooms")
        self.assertEqual(ctx.exception.status_code, 504)


if __name__ == "__main__":
    unittest.main()
