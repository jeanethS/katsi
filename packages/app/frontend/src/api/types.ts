export type SynthesisMode = "auto" | "local" | "cloud";

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
  synth: { backend: SynthesisMode; cloud_configured: boolean };
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
