"""An API 1 plugin manifest reads exactly as stock Heym v0.0.160 read it (docs/plugin-api-2.md, rule 2).

The reference is stock Heym's own manifest module, kept in fixtures/plugin_api1/. A real API 1
package (the rtteam dummy plugin 0.1.3) and one that carries API 2 keys without asking for
API 2 must give the same nodes, fields and listing under both.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType

from app.models.plugin_schemas import PluginManifest

_FIXTURES = Path(__file__).parent / "fixtures" / "plugin_api1"


def _stock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "stock_plugin_schemas", _FIXTURES / "stock_plugin_schemas.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Pydantic resolves the module's forward references through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _restricted(dump: object, reference: object) -> object:
    """`dump` cut down to the keys `reference` has, recursively."""
    if isinstance(reference, dict) and isinstance(dump, dict):
        return {key: _restricted(dump.get(key), value) for key, value in reference.items()}
    if isinstance(reference, list) and isinstance(dump, list):
        return [_restricted(d, r) for d, r in zip(dump, reference)] + dump[len(reference) :]
    return dump


class PluginApi1CompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stock = _stock_module()

    def _assert_same(self, raw: dict) -> PluginManifest:
        stock = self.stock.PluginManifest.model_validate(raw).model_dump(by_alias=True)
        ours = PluginManifest.model_validate(raw)
        self.assertEqual(_restricted(ours.model_dump(by_alias=True), stock), stock)
        self.assertEqual(ours.plugin_api, 1)
        return ours

    def test_rtteam_0_1_3_reads_as_on_stock(self) -> None:
        raw = json.loads((_FIXTURES / "rtteam-0.1.3.json").read_text())
        ours = self._assert_same(raw)
        self.assertEqual(len(ours.nodes), len(raw["nodes"]))

    def test_stored_dump_reads_back_the_same(self) -> None:
        raw = json.loads((_FIXTURES / "rtteam-0.1.3.json").read_text())
        stored = PluginManifest.model_validate(raw).model_dump(by_alias=True)
        again = PluginManifest.model_validate(stored)
        self.assertEqual(again.model_dump(by_alias=True), stored)

    def test_api2_keys_without_plugin_api_are_ignored(self) -> None:
        raw = {
            "id": "acme",
            "name": "Acme",
            "version": "1.0.0",
            "palette": {"section": "Acme", "groups": "not even a list"},
            "credentials": [{"bogus": True}],
            "somethingElse": 1,
            "nodes": [
                {
                    "key": "a",
                    "name": "A",
                    "kind": "action",
                    "exits": [{"key": "ok"}],
                    "group": "nowhere",
                    "color": "red",
                    "fields": [{"key": "x", "label": "X", "showIf": {"y": 1}, "unknown": 2}],
                }
            ],
        }
        ours = self._assert_same(raw)
        node = ours.nodes[0]
        self.assertEqual((node.exits, node.group, node.color), ([], None, None))
        self.assertIsNone(ours.palette)
        self.assertEqual(ours.credentials, [])
        self.assertIsNone(node.fields[0].show_if)

    def test_legacy_single_node_reads_as_on_stock(self) -> None:
        self._assert_same(
            {
                "id": "acme-crm",
                "name": "Acme CRM",
                "version": "1.0.0",
                "kind": "trigger",
                "fields": [{"key": "k", "label": "K", "type": "select", "options": []}],
            }
        )


if __name__ == "__main__":
    unittest.main()
