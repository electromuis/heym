import { describe, expect, it } from "vitest";

import type { PluginSummary } from "@/types/workflow";

import { findPluginNode, isPluginLoopNode } from "@/composables/usePluginCatalog";

const rows: PluginSummary[] = [
  {
    id: "acme",
    name: "Acme",
    version: "1",
    kind: "action",
    description: "",
    enabled: true,
    nodes: [
      { key: "plain", name: "Plain", kind: "action", description: "", fields: [] },
      {
        key: "steps",
        name: "Steps",
        kind: "action",
        description: "",
        fields: [],
        loop: true,
        exits: [{ key: "step", label: "step" }, { key: "done", label: "done" }],
      },
    ],
  },
];

describe("usePluginCatalog", () => {
  it("knows a loop node by its manifest", () => {
    expect(isPluginLoopNode(rows, "acme", "steps")).toBe(true);
    expect(isPluginLoopNode(rows, "acme", "plain")).toBe(false);
    expect(isPluginLoopNode(rows, "other", "steps")).toBe(false);
    expect(isPluginLoopNode(rows, undefined, undefined)).toBe(false);
  });

  it("falls back to the package's first action without a node key", () => {
    expect(findPluginNode(rows, "acme", undefined)?.key).toBe("plain");
  });
});
