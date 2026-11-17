import { ref } from "vue";

import type { PluginNodeSummary, PluginSummary } from "@/types/workflow";

import { listPlugins } from "@/services/plugins";

// The plugin listing, loaded once and shared by every node on the canvas: a node's face (its handles) depends on
// its manifest, e.g. a Plugin API 2 loop node has Loop's handles (docs/plugin-api-2.md, "Loop nodes").
const plugins = ref<PluginSummary[]>([]);
let pendingLoad: Promise<void> | null = null;
let loaded = false;

function loadPluginCatalog(force = false): Promise<void> {
  if (loaded && !force) return Promise.resolve();
  if (!pendingLoad) {
    pendingLoad = listPlugins()
      .then((rows) => {
        plugins.value = rows;
        loaded = true;
      })
      .catch(() => {
        plugins.value = [];
      })
      .finally(() => {
        pendingLoad = null;
      });
  }
  return pendingLoad;
}

export function findPluginNode(
  rows: PluginSummary[],
  pluginId: string | undefined,
  nodeKey: string | undefined,
): PluginNodeSummary | undefined {
  if (!pluginId) return undefined;
  const plugin = rows.find((row) => row.id === pluginId);
  if (!plugin) return undefined;
  return plugin.nodes.find((node) => node.key === nodeKey)
    ?? plugin.nodes.find((node) => node.kind === "action");
}

export function isPluginLoopNode(
  rows: PluginSummary[],
  pluginId: string | undefined,
  nodeKey: string | undefined,
): boolean {
  return findPluginNode(rows, pluginId, nodeKey)?.loop === true;
}

export function usePluginCatalog() {
  return { plugins, loadPluginCatalog };
}
