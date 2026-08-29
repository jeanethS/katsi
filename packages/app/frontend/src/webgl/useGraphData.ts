import { useEffect, useState } from "react";
import { request } from "../api/client";
import type { GraphData, GraphStatus } from "../api/types";

const emptyGraph: GraphData = { nodes: [], edges: [] };

function normalizeGraph(data: GraphData): GraphData {
  const nodeIds = new Set(data.nodes.map((node) => node.id));
  const edges = data.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  return { nodes: data.nodes, edges };
}

export interface UseGraphDataResult {
  data: GraphData;
  loading: boolean;
  error: boolean;
  /** "ready" | "empty" | "unavailable" — never sample data standing in for the user's library. */
  status: GraphStatus;
}

export function useGraphData(): UseGraphDataResult {
  const [data, setData] = useState<GraphData>(emptyGraph);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [status, setStatus] = useState<GraphStatus>("empty");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(false);

    request<GraphData>("/api/graph", { signal: controller.signal })
      .then((payload) => {
        const normalized = normalizeGraph(payload);
        setData(normalized);
        setStatus(payload.status ?? (normalized.nodes.length > 0 ? "ready" : "empty"));
        setError(false);
      })
      .catch((cause: unknown) => {
        if ((cause as Error).name !== "AbortError") {
          setData(emptyGraph);
          setStatus("unavailable");
          setError(true);
        }
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, []);

  return { data, loading, error, status };
}
