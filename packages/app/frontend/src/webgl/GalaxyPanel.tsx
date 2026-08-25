import type { GraphData, GraphNode } from "../api/types";

interface GalaxyPanelProps {
  data: GraphData;
  emptyLabel: string;
  node: GraphNode | null;
  relationshipLabel: string;
}

export function GalaxyPanel({ data, emptyLabel, node, relationshipLabel }: GalaxyPanelProps) {
  if (!node) return <aside className="galaxy-panel galaxy-panel-empty"><p>{emptyLabel}</p></aside>;
  const relationships = data.edges.filter((edge) => edge.source === node.id || edge.target === node.id).length;
  return <aside className="galaxy-panel">
    <p className="galaxy-node-type">{node.type}</p>
    <h2>{node.label}</h2>
    {node.summary && <p>{node.summary}</p>}
    {node.path && <p className="path">{node.path}</p>}
    {node.kind && <p className="galaxy-kind">{node.kind}</p>}
    <p className="galaxy-relationships">{relationships} {relationshipLabel}</p>
  </aside>;
}
