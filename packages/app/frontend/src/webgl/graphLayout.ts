import type { GraphData, GraphNode } from "../api/types";

export interface GraphPosition {
  x: number;
  y: number;
  z: number;
}

function hash(value: string): number {
  return [...value].reduce((result, character) => ((result * 31) + character.charCodeAt(0)) >>> 0, 2166136261);
}

function radiusFor(node: GraphNode): number {
  if (node.type === "file") return 2.1;
  if (node.type === "entity") return 1.35;
  return 0.7;
}

export function layoutGraph(data: GraphData): Map<string, GraphPosition> {
  return new Map(data.nodes.map((node, index) => {
    const value = hash(node.id);
    const angle = ((value % 360) * Math.PI) / 180 + index * 0.37;
    const radius = radiusFor(node) + ((value >>> 8) % 30) / 100;
    return [node.id, {
      x: Math.cos(angle) * radius,
      y: (((value >>> 16) % 150) / 100) - 0.75,
      z: Math.sin(angle) * radius,
    }];
  }));
}
