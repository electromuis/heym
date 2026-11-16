"""Plugin API 2 credential types: checking, merging, what the browser sees, the test, a run's values."""

import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api.credentials import (
    get_masked_value,
    get_public_credential_fields,
    merge_credential_config_for_update,
    validate_credential_config,
)
from app.db.models import CredentialType
from app.services import plugin_context, plugin_credentials, plugin_loader, plugin_store
from app.services.credential_context import credential_context_value
from app.services.workflow_executor import execute_workflow

_TOKEN = "hub-token-0123456789"

_MANIFEST = {
    "pluginApi": 2,
    "id": "acme",
    "name": "Acme",
    "version": "1.0.0",
    "credentials": [
        {
            "key": "hub",
            "name": "Acme hub",
            "fields": [
                {"key": "url", "label": "Hub address", "required": True},
                {"key": "token", "label": "API key", "secret": True, "required": True},
                {"key": "port", "label": "Port", "type": "number"},
            ],
            "test": "test_hub",
        },
        {"key": "other", "name": "Other", "fields": [{"key": "x", "label": "X"}]},
    ],
    "nodes": [
        {
            "key": "call",
            "name": "Call",
            "kind": "action",
            "function": "call",
            "fields": [
                {"key": "hub", "label": "Hub", "type": "credential", "credentialType": "hub"}
            ],
        }
    ],
}

_HANDLER = """
def test_hub(values, ctx):
    if values["url"] != "http://hub":
        raise ValueError("cannot reach hub with key " + values["token"])


def call(inputs, config, ctx):
    hub = config["hub"]
    if hub["url"] == "http://fail":
        raise ValueError("refused key " + hub["token"])
    return {"url": hub["url"], "echo": hub["token"]}
"""


def _config(**values: object) -> dict:
    base = {"url": "http://hub", "token": _TOKEN}
    base.update(values)
    return {"plugin_id": "acme", "credential_type": "hub", "values": base}


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
        patcher = patch.object(plugin_store, "plugins_root", return_value=root)
        patcher.start()
        self.addCleanup(patcher.stop)


class PluginCredentialConfigTests(_PackageTestCase):
    def _refused(self, config: dict) -> str:
        with self.assertRaises(HTTPException) as ctx:
            validate_credential_config(CredentialType.plugin, config)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertNotIn(_TOKEN, ctx.exception.detail)
        return ctx.exception.detail

    def test_valid_config(self) -> None:
        validate_credential_config(CredentialType.plugin, _config(port=8793))

    def test_refusals_name_the_field(self) -> None:
        self.assertIn("API key is required", self._refused(_config(token="")))
        self.assertIn("unknown field extra", self._refused(_config(extra=_TOKEN)))
        self.assertIn("Port must be a number", self._refused(_config(port="8793")))
        self.assertIn("Unknown key", self._refused({**_config(), "more": 1}))

    def test_unknown_package_or_type(self) -> None:
        self.assertIn("not installed", self._refused({**_config(), "plugin_id": "nope"}))
        self.assertIn("no credential type", self._refused({**_config(), "credential_type": "x"}))

    def test_update_keeps_a_blank_secret(self) -> None:
        merged = merge_credential_config_for_update(
            CredentialType.plugin, _config(), {"values": {"url": "http://new", "token": ""}}
        )
        self.assertEqual(merged["values"], {"url": "http://new", "token": _TOKEN})

    def test_update_cannot_change_the_type(self) -> None:
        with self.assertRaises(HTTPException):
            merge_credential_config_for_update(
                CredentialType.plugin, _config(), {"credential_type": "other"}
            )

    def test_browser_sees_no_secret(self) -> None:
        fields = get_public_credential_fields(CredentialType.plugin, _config(port=1))
        self.assertEqual(
            fields,
            {
                "plugin_id": "acme",
                "credential_type": "hub",
                "url": "http://hub",
                "token_set": "true",
                "port": "1",
            },
        )
        self.assertEqual(get_masked_value(CredentialType.plugin, _config()), "hub-tok**")

    def test_no_credentials_value(self) -> None:
        credential = SimpleNamespace(type=CredentialType.plugin)
        self.assertIsNone(asyncio.run(credential_context_value(credential, _config())))

    def test_connection_test_masks_the_secret(self) -> None:
        self.assertEqual(plugin_credentials.run_test(_config()), (True, "Connection works"))
        ok, message = plugin_credentials.run_test(_config(url="http://elsewhere"))
        self.assertFalse(ok)
        self.assertIn("cannot reach hub with key hub-tok**", message)
        self.assertNotIn(_TOKEN, message)


class PluginCredentialRunTests(_PackageTestCase):
    def _run(self, stored: dict | None) -> dict:
        nodes = [
            {"id": "in", "type": "textInput", "data": {"label": "msg"}},
            {
                "id": "c",
                "type": "plugin",
                "data": {
                    "label": "call",
                    "pluginId": "acme",
                    "pluginNodeKey": "call",
                    "config": {"hub": str(uuid.uuid4())},
                },
            },
        ]
        edges = [{"id": "e", "source": "in", "target": "c"}]
        with patch.object(plugin_context, "load_credential_config", return_value=stored):
            result = execute_workflow(uuid.uuid4(), nodes, edges, {"body": {}})
        return next(r for r in result.node_results if r["node_id"] == "c")

    def test_handler_gets_values_and_output_is_masked(self) -> None:
        result = self._run(_config())
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual(result["output"], {"url": "http://hub", "echo": "hub-tok**"})

    def test_error_is_masked(self) -> None:
        result = self._run(_config(url="http://fail"))
        self.assertEqual(result["status"], "error")
        self.assertIn("refused key hub-tok**", result["error"])
        self.assertNotIn(_TOKEN, result["error"])

    def test_credential_of_another_type_is_refused(self) -> None:
        result = self._run({"plugin_id": "acme", "credential_type": "other", "values": {"x": "1"}})
        self.assertEqual(result["status"], "error")
        self.assertIn("not of the type this field needs", result["error"])

    def test_missing_credential(self) -> None:
        result = self._run(None)
        self.assertIn("the chosen credential was not found", result["error"])


if __name__ == "__main__":
    unittest.main()
