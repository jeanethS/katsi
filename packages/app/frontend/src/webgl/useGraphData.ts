import { useEffect, useState } from "react";
import { request } from "../api/client";
import type { GraphData, GraphEdge, GraphNode } from "../api/types";

const demoGraph: GraphData = {
  nodes: [
    { id: "file:notes", label: "notes", type: "file", path: "/home/j/notes", summary: "Personal notes vault" },
    { id: "file:research", label: "research", type: "file", path: "/home/j/research", summary: "Research materials" },
    { id: "file:projects", label: "projects", type: "file", path: "/home/j/projects", summary: "Active projects" },
    { id: "file:china-trip", label: "china-trip.md", type: "file", path: "/home/j/notes/china-trip.md", summary: "Notes on payment adoption and local infrastructure" },
    { id: "file:venus-pitch", label: "venus-pitch.md", type: "file", path: "/home/j/notes/venus-pitch.md", summary: "Pitch about shared infrastructure" },
    { id: "entity:Alipay", label: "Alipay", type: "entity", kind: "organization" },
    { id: "entity:fintech", label: "fintech", type: "entity", kind: "topic" },
    { id: "entity:infrastructure", label: "infrastructure", type: "entity", kind: "concept" },
    { id: "topic:coordination", label: "coordination", type: "topic" },
    { id: "topic:payments", label: "payments", type: "topic" },
  ],
  edges: [
    { source: "file:china-trip", target: "entity:Alipay", type: "mentions", weight: 1 },
    { source: "file:china-trip", target: "entity:fintech", type: "mentions", weight: 0.8 },
    { source: "file:venus-pitch", target: "entity:infrastructure", type: "mentions", weight: 0.9 },
    { source: "file:venus-pitch", target: "topic:coordination", type: "about", weight: 1 },
    { source: "file:china-trip", target: "topic:payments", type: "about", weight: 0.9 },
    { source: "file:notes", target: "file:china-trip", type: "references", weight: 1 },
    { source: "file:notes", target: "file:venus-pitch", type: "references", weight: 1 },
    { source: "file:research", target: "file:projects", type: "duplicate", weight: 0.72 },
  ],
};

function normalizeGraph(data: GraphData): GraphData {
  const nodeIds = new Set(data.nodes.map((node) => node.id));
  const edges = data.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  return { nodes: data.nodes, edges };
}

export interface UseGraphDataResult {
  data: GraphData;
  loading: boolean;
  error: boolean;
}

export function useGraphData(): UseGraphDataResult {
  const [data, setData] = useState<GraphData>(demoGraph);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(false);

    request<GraphData>("/api/graph", { signal: controller.signal })
      .then((payload) => {
        const normalized = normalizeGraph(payload);
        setData(normalized.nodes.length > 0 ? normalized : demoGraph);
        setError(false);
      })
      .catch((cause: unknown) => {
        if ((cause as Error).name !== "AbortError") {
          setError(true);
        }
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, []);

  return { data, loading, error };
}
