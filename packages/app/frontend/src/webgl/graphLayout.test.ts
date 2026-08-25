import { describe, expect, it } from "vitest";
import type { GraphData } from "../api/types";
import { layoutGraph } from "./graphLayout";

const graph: GraphData = {
  nodes: [
    { id: "file:notes", label: "notes", type: "file" },
    { id: "entity:Alipay", label: "Alipay", type: "entity" },
    { id: "topic:payments", label: "payments", type: "topic" },
  ],
  edges: [],
};

describe("layoutGraph", () => {
  it("is deterministic and keeps nodes apart", () => {
    const first = layoutGraph(graph);
    const second = layoutGraph(graph);

    expect(first).toEqual(second);
    expect(first.get("file:notes")).not.toEqual(first.get("entity:Alipay"));
  });
});
