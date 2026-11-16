"""Plugin API 2 manifests: the version gate, strict keys, palette, exits, fields, credential types."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.api import plugins as plugins_api
from app.models.plugin_schemas import PluginManifest
from app.services import plugin_store
from app.services.plugin_store import describe_manifest_errors


def _manifest(**extra: object) -> dict:
    base: dict = {
        "pluginApi": 2,
        "id": "acme",
        "name": "Acme",
        "version": "1.0.0",
        "nodes": [{"key": "run", "name": "Run", "kind": "action"}],
    }
    base.update(extra)
    return base


def _error(raw: dict) -> str:
    try:
        PluginManifest.model_validate(raw)
    except ValidationError as exc:
        return describe_manifest_errors(exc)
    raise AssertionError("the manifest was accepted")


class PluginApiVersionTests(unittest.TestCase):
    def test_newer_api_is_refused(self) -> None:
        text = _error(_manifest(pluginApi=3))
        self.assertIn("pluginApi 3 is newer than this Heym supports (2)", text)

    def test_api_must_be_a_whole_number(self) -> None:
        self.assertIn("whole number", _error(_manifest(pluginApi="2")))

    def test_unknown_key_names_place_and_key_not_value(self) -> None:
        raw = _manifest()
        raw["nodes"][0]["fields"] = [{"key": "a", "label": "A", "lable": "TOP-SECRET-VALUE"}]
        text = _error(raw)
        self.assertIn('plugin.json: nodes[0]: fields[0]: unknown key "lable"', text)
        self.assertNotIn("TOP-SECRET-VALUE", text)

    def test_unknown_package_key(self) -> None:
        self.assertIn('unknown key "colour"', _error(_manifest(colour="#ffffff")))

    def test_new_field_type_needs_api_2(self) -> None:
        raw = _manifest()
        del raw["pluginApi"]
        raw["nodes"][0]["fields"] = [{"key": "a", "label": "A", "type": "text"}]
        self.assertIn("field type text needs pluginApi 2", _error(raw))

    def test_errors_never_quote_values(self) -> None:
        raw = _manifest()
        raw["nodes"][0]["color"] = "SECRET-COLOUR"
        text = _error(raw)
        self.assertIn("colour must be #rrggbb", text)
        self.assertNotIn("SECRET-COLOUR", text)


class PluginApi2PaletteTests(unittest.TestCase):
    def test_palette_groups_and_colours(self) -> None:
        raw = _manifest(
            palette={
                "section": "Acme",
                "position": "top",
                "color": "#111111",
                "groups": [
                    {"key": "people", "label": "People", "color": "#222222"},
                    {"key": "runs", "label": "Runs"},
                ],
            }
        )
        raw["nodes"] = [
            {"key": "a", "name": "A", "kind": "action", "group": "people"},
            {"key": "b", "name": "B", "kind": "action", "group": "runs"},
            {"key": "c", "name": "C", "kind": "action", "color": "#333333", "group": "people"},
        ]
        manifest = PluginManifest.model_validate(raw)
        colours = [manifest.node_color(n) for n in manifest.nodes]
        self.assertEqual(colours, ["#222222", "#111111", "#333333"])
        self.assertEqual(manifest.palette.position, "top")

    def test_unknown_group_is_refused(self) -> None:
        raw = _manifest()
        raw["nodes"][0]["group"] = "nowhere"
        self.assertIn("unknown group", _error(raw))


def rules_manifest() -> dict:
    """The spec's worked example: a Rules node whose exits come from its rows."""
    raw = _manifest()
    raw["nodes"] = [
        {
            "key": "rules",
            "name": "Rules",
            "kind": "action",
            "fields": [
                {
                    "key": "rules",
                    "label": "Rules",
                    "type": "list",
                    "fields": [
                        {"key": "name", "label": "Name"},
                        {"key": "when", "label": "When", "type": "condition"},
                        {"key": "answer", "label": "Answer"},
                    ],
                }
            ],
            "exitsFrom": {"field": "rules", "column": "answer"},
            "exits": [{"key": "otherwise"}],
        }
    ]
    return raw


class PluginApi2ExitTests(unittest.TestCase):
    def test_exits_from_a_column_in_first_seen_order(self) -> None:
        node = PluginManifest.model_validate(rules_manifest()).nodes[0]
        config = {
            "rules": [
                {"answer": "nobody"},
                {"answer": "auto"},
                {"answer": "nobody"},
                {"answer": ""},
                {"answer": "two\nlines"},
                {"answer": "reply"},
            ]
        }
        self.assertEqual(node.exit_keys(config), ["otherwise", "nobody", "auto", "reply"])
        self.assertTrue(node.has_exits())

    def test_exits_from_must_name_a_list_column(self) -> None:
        raw = rules_manifest()
        raw["nodes"][0]["exitsFrom"]["column"] = "nope"
        self.assertIn("`exitsFrom` names an unknown column", _error(raw))

    def test_duplicate_exit_keys(self) -> None:
        raw = _manifest()
        raw["nodes"][0]["exits"] = [{"key": "ok"}, {"key": "ok"}]
        self.assertIn("exit keys must be unique", _error(raw))

    def test_bad_exit_key(self) -> None:
        raw = _manifest()
        raw["nodes"][0]["exits"] = [{"key": "1st"}]
        self.assertIn("exit key must match", _error(raw))

    def test_source_only_on_triggers(self) -> None:
        raw = _manifest()
        raw["nodes"][0]["source"] = "push"
        self.assertIn("only a trigger has a `source`", _error(raw))


class PluginApi2FieldTests(unittest.TestCase):
    def _field(self, **field: object) -> dict:
        raw = _manifest()
        raw["nodes"][0]["fields"] = [{"key": "f", "label": "F", **field}]
        return raw

    def test_list_needs_columns(self) -> None:
        self.assertIn("a list needs `fields`", _error(self._field(type="list")))

    def test_list_columns_are_simple(self) -> None:
        raw = self._field(type="list", fields=[{"key": "c", "label": "C", "type": "json"}])
        self.assertIn("a list column can't be of type json", _error(raw))

    def test_credential_field_needs_a_declared_type(self) -> None:
        self.assertIn("needs `credentialType`", _error(self._field(type="credential")))
        raw = self._field(type="credential", credentialType="hub")
        self.assertIn("unknown credential type", _error(raw))

    def test_credential_field_with_declared_type(self) -> None:
        raw = self._field(type="credential", credentialType="hub")
        raw["credentials"] = [
            {
                "key": "hub",
                "name": "Hub",
                "fields": [{"key": "token", "label": "Token", "secret": True}],
            }
        ]
        manifest = PluginManifest.model_validate(raw)
        self.assertEqual(manifest.get_credential_type("hub").secret_keys(), {"token"})

    def test_show_if_names_a_field(self) -> None:
        text = _error(self._field(showIf={"other": 1}))
        self.assertIn("`showIf` names an unknown field", text)

    def test_load_options_only_on_pick_lists(self) -> None:
        self.assertIn("`loadOptions` is for select", _error(self._field(loadOptions=True)))
        manifest = PluginManifest.model_validate(self._field(type="select", loadOptions=True))
        self.assertEqual(manifest.nodes[0].fields[0].load_options_function(), "options_f")
        raw = self._field(type="multiselect", loadOptions="agents")
        manifest = PluginManifest.model_validate(raw)
        self.assertEqual(manifest.nodes[0].fields[0].load_options_function(), "agents")

    def test_all_new_keys_accepted(self) -> None:
        raw = self._field(
            type="text", help="h", placeholder="p", advanced=True, section="S", rows=3
        )
        field = PluginManifest.model_validate(raw).nodes[0].fields[0]
        self.assertEqual((field.type, field.rows, field.section), ("text", 3, "S"))

    def test_stored_dump_reads_back(self) -> None:
        raw = self._field(type="code", language="python")
        stored = PluginManifest.model_validate(raw).model_dump(by_alias=True)
        self.assertEqual(PluginManifest.model_validate(stored).model_dump(by_alias=True), stored)


class PluginApi2ListingTests(unittest.TestCase):
    def _summary(self, raw: dict) -> dict:
        manifest = PluginManifest.model_validate(raw)
        row = SimpleNamespace(
            plugin_id=manifest.id,
            name=manifest.name,
            version=manifest.version,
            kind=manifest.package_kind(),
            description="",
            enabled=True,
            manifest=manifest.model_dump(by_alias=True),
        )
        with tempfile.TemporaryDirectory() as root:
            with patch.object(plugin_store, "plugins_root", return_value=Path(root)):
                return plugins_api._to_summary(row).model_dump(by_alias=True)

    def test_api1_listing_has_no_api2_values(self) -> None:
        raw = _manifest()
        del raw["pluginApi"]
        summary = self._summary(raw)
        self.assertEqual(summary["pluginApi"], 1)
        self.assertIsNone(summary["palette"])
        self.assertIsNone(summary["credentials"])
        node = summary["nodes"][0]
        for key in ("group", "color", "subtitle", "exits", "exitsFrom", "inputs", "source"):
            self.assertIsNone(node[key], key)

    def test_api2_listing(self) -> None:
        raw = rules_manifest()
        raw["palette"] = {"section": "Acme", "color": "#123456"}
        raw["credentials"] = [
            {
                "key": "hub",
                "name": "Hub",
                "fields": [
                    {"key": "url", "label": "URL", "default": "http://hub"},
                    {"key": "token", "label": "Token", "secret": True, "default": "s3cret"},
                ],
            }
        ]
        summary = self._summary(raw)
        node = summary["nodes"][0]
        self.assertEqual(node["color"], "#123456")
        self.assertEqual(node["exitsFrom"], {"field": "rules", "column": "answer"})
        self.assertEqual([e["key"] for e in node["exits"]], ["otherwise"])
        defaults = [f["default"] for f in summary["credentials"][0]["fields"]]
        self.assertEqual(defaults, ["http://hub", None])


if __name__ == "__main__":
    unittest.main()
