export type SynthesisMode = "auto" | "local" | "cloud";

export type GraphNodeType = "file" | "entity" | "topic";
export type GraphEdgeType = "mentions" | "about" | "references" | "duplicate";

export interface GraphNode {
  id: string;
  label: string;
  type: GraphNodeType;
  path?: string;
  summary?: string;
  kind?: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: GraphEdgeType;
  weight: number;
}

/** "empty" is a real but unindexed library; "unavailable" means the graph could not be read. */
export type GraphStatus = "ready" | "empty" | "unavailable";

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  status?: GraphStatus;
}

export interface IndexResult {
  indexed: number;
  skipped: number;
  error: number;
  total: number;
}

export interface Folder {
  id: string;
  path: string;
  status: "pending" | "indexing" | "indexed" | "error";
  files_total: number;
  files_indexed: number;
  files_error: number;
  last_indexed_at: string | null;
}

export interface Status {
  ollama: { reachable: boolean; models: string[] };
  counts: { files: number; chunks: number; entities: number; topics: number };
  db_bytes: { vectors: number; graph: number };
  synth: { backend: SynthesisMode | "return_only"; cloud_configured: boolean };
}

export interface Source {
  file_id: string;
  path: string;
  summary: string;
  why: string;
}

export interface Receipt {
  mode: SynthesisMode;
  escalated: boolean;
  files: number;
  chunks: number;
  tokens_local: number;
  tokens_cloud: number;
  seconds: number;
}
