"""Plugin API 2 in a real workflow run: exits pick the branch, `ctx.check`, named inputs, config.

The package is the spec's worked example, a Rules node (first rule that holds wins), plus a node
with fixed exits and one with two named inputs.
"""

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.services import plugin_loader, plugin_store
from app.services.workflow_executor import execute_workflow

_MANIFEST = {
    "pluginApi": 2,
    "id": "acme",
    "name": "Acme",
    "version": "1.0.0",
    "nodes": [
        {
            "key": "rules",
            "name": "Rules",
            "kind": "action",
            "function": "rules",
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
                },
                {"key": "otherwise", "label": "Otherwise", "default": "nobody"},
            ],
            "exitsFrom": {"field": "rules", "column": "answer"},
            "exits": [{"key": "otherwise"}],
        },
        {
            "key": "pick",
            "name": "Pick",
            "kind": "action",
            "function": "pick",
            "fields": [
                {"key": "exit", "label": "Exit"},
                {"key": "mode", "label": "Mode", "type": "select", "default": "a"},
                {"key": "onlyB", "label": "Only B", "showIf": {"mode": "b"}},
            ],
            "exits": [{"key": "first"}, {"key": "second"}],
        },
        {
            "key": "join",
            "name": "Join",
            "kind": "action",
            "function": "join",
            "inputs": [{"key": "left"}, {"key": "right"}],
        },
    ],
}

_HANDLER = """
def rules(inputs, config, ctx):
    for row in config["rules"]:
        if ctx.check(row["when"]):
            return {"exit": row["answer"], "answer": row["answer"], "rule": row["name"]}
    return {"exit": "otherwise", "answer": config["otherwise"], "rule": None}


def pick(inputs, config, ctx):
    out = {"seen": sorted(config), "ctx": dict(ctx)}
    if config.get("exit"):
        out["exit"] = config["exit"]
    return out


def join(inputs, config, ctx, inputs_by_key):
    return {"left": inputs_by_key.get("left"), "right": inputs_by_key.get("right")}
"""

_RULES = [
    {"name": "from an outsider", "when": "$msg.body.outsider == True", "answer": "nobody"},
    {"name": "mentions me", "when": "'penny' in $msg.body.mentions", "answer": "auto"},
    {"name": "a reply to me", "when": "$msg.body.replyTo == 'penny'", "answer": "reply"},
    # Never reached when an earlier rule holds; it fails whenever it is checked.
    {"name": "broken", "when": "1 / 0 == 1", "answer": "auto"},
]


def _node(node_id: str, node_type: str, label: str, **data: object) -> dict:
    return {"id": node_id, "type": node_type, "data": {"label": label, **data}}


def _set(label: str) -> dict:
    return _node(label, "set", label, mappings=[{"key": "x", "value": "1"}])


def _edge(source: str, target: str, handle: str | None = None, target_handle: str | None = None):
    edge = {"id": f"{source}-{target}-{handle}-{target_handle}", "source": source, "target": target}
    if handle:
        edge["sourceHandle"] = handle
    if target_handle:
        edge["targetHandle"] = target_handle
    return edge


class PluginApi2ExitRunTests(unittest.TestCase):
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

    def _run_rules(self, body: dict) -> tuple[dict, list[str]]:
        nodes = [
            _node("in", "textInput", "msg"),
            _node(
                "r",
                "plugin",
                "forMe",
                pluginId="acme",
                pluginNodeKey="rules",
                config={"rules": _RULES},
            ),
            _set("nobodyPath"),
            _set("autoPath"),
            _set("replyPath"),
            _set("otherwisePath"),
            _set("woken"),
        ]
        edges = [
            _edge("in", "r"),
            _edge("r", "nobodyPath", "exit-nobody"),
            _edge("r", "autoPath", "exit-auto"),
            _edge("r", "replyPath", "exit-reply"),
            _edge("r", "otherwisePath", "exit-otherwise"),
            # Reached from two exits: runs when either is taken.
            _edge("autoPath", "woken"),
            _edge("replyPath", "woken"),
        ]
        result = execute_workflow(uuid.uuid4(), nodes, edges, {"body": body})
        rules = next(r for r in result.node_results if r["node_id"] == "r")
        ran = sorted(
            r["node_label"]
            for r in result.node_results
            if r["node_type"] == "set" and r["status"] == "success"
        )
        return rules, ran

    def test_first_rule_that_holds_picks_the_exit(self) -> None:
        rules, ran = self._run_rules({"outsider": False, "mentions": ["penny"], "replyTo": ""})
        self.assertEqual(rules["status"], "success")
        self.assertEqual(rules["output"]["exit"], "auto")
        self.assertEqual(rules["output"]["rule"], "mentions me")
        self.assertEqual(ran, ["autoPath", "woken"])

    def test_earlier_rule_wins_and_later_broken_rule_is_not_checked(self) -> None:
        rules, ran = self._run_rules({"outsider": True, "mentions": ["penny"], "replyTo": "penny"})
        self.assertEqual(
            (rules["output"]["answer"], rules["output"]["rule"]), ("nobody", "from an outsider")
        )
        self.assertEqual(ran, ["nobodyPath"])

    def test_shared_downstream_block_runs_from_the_other_exit(self) -> None:
        _rules, ran = self._run_rules({"outsider": False, "mentions": [], "replyTo": "penny"})
        self.assertEqual(ran, ["replyPath", "woken"])

    def test_otherwise_takes_the_default_answer(self) -> None:
        rules = self._run_rules_until_broken_row_is_left_out()
        self.assertEqual(rules["output"], {"exit": "otherwise", "answer": "nobody", "rule": None})

    def _run_rules_until_broken_row_is_left_out(self) -> dict:
        global _RULES
        saved = _RULES
        _RULES = saved[:3]
        try:
            rules, ran = self._run_rules({"outsider": False, "mentions": [], "replyTo": ""})
        finally:
            _RULES = saved
        self.assertEqual(ran, ["otherwisePath"])
        return rules

    def test_a_reached_broken_rule_fails_the_node(self) -> None:
        rules, ran = self._run_rules({"outsider": False, "mentions": [], "replyTo": ""})
        self.assertEqual(rules["status"], "error")
        self.assertEqual(ran, [])

    def _run_pick(self, config: dict) -> dict:
        nodes = [
            _node("in", "textInput", "msg"),
            _node("p", "plugin", "pick", pluginId="acme", pluginNodeKey="pick", config=config),
            _set("firstPath"),
            _set("secondPath"),
        ]
        edges = [
            _edge("in", "p"),
            _edge("p", "firstPath", "exit-first"),
            _edge("p", "secondPath", "exit-second"),
        ]
        result = execute_workflow(uuid.uuid4(), nodes, edges, {"body": {}})
        return next(r for r in result.node_results if r["node_id"] == "p")

    def test_no_exit_takes_the_first(self) -> None:
        self.assertEqual(self._run_pick({})["output"]["exit"], "first")

    def test_unknown_exit_fails_the_node(self) -> None:
        result = self._run_pick({"exit": "third"})
        self.assertEqual(result["status"], "error")
        self.assertIn("returned an unknown exit; declared: first, second", result["error"])

    def test_defaults_and_show_if(self) -> None:
        self.assertEqual(self._run_pick({"onlyB": "x"})["output"]["seen"], ["mode"])
        seen = self._run_pick({"mode": "b", "onlyB": "x"})["output"]["seen"]
        self.assertEqual(seen, ["mode", "onlyB"])

    def test_ctx_still_reads_like_the_api1_dict(self) -> None:
        ctx = self._run_pick({})["output"]["ctx"]
        self.assertEqual((ctx["node_id"], ctx["node_label"]), ("p", "pick"))
        self.assertIn("execution_id", ctx)

    def test_named_inputs(self) -> None:
        nodes = [
            _node("in", "textInput", "msg"),
            _node("a", "set", "a", mappings=[{"key": "side", "value": "left"}]),
            _node("b", "set", "b", mappings=[{"key": "side", "value": "right"}]),
            _node("j", "plugin", "join", pluginId="acme", pluginNodeKey="join", config={}),
        ]
        edges = [
            _edge("in", "a"),
            _edge("in", "b"),
            _edge("a", "j", target_handle="input-left"),
            _edge("b", "j", target_handle="input-right"),
        ]
        result = execute_workflow(uuid.uuid4(), nodes, edges, {"body": {}})
        join = next(r for r in result.node_results if r["node_id"] == "j")
        self.assertEqual(join["status"], "success", join.get("error"))
        self.assertEqual(join["output"], {"left": {"side": "left"}, "right": {"side": "right"}})


if __name__ == "__main__":
    unittest.main()
