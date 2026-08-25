import { useEffect, useRef } from "react";
import { gsap } from "gsap";
import * as THREE from "three";
import type { GraphData, GraphNode } from "../api/types";
import { layoutGraph } from "./graphLayout";

interface KnowledgeGalaxyProps {
  data: GraphData;
  loading: boolean;
  onSelect: (node: GraphNode) => void;
  selectedId?: string;
}

const colors: Record<GraphNode["type"], number> = { file: 0x6fbf9a, entity: 0xd9a441, topic: 0x79a8d8 };

export function KnowledgeGalaxy({ data, loading, onSelect, selectedId }: KnowledgeGalaxyProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof WebGLRenderingContext === "undefined") return;

    const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, canvas });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
    camera.position.set(0, 0, 7);
    const constellation = new THREE.Group();
    scene.add(constellation);
    const positions = layoutGraph(data);
    const meshes: THREE.Mesh[] = [];
    const nodeById = new Map(data.nodes.map((node) => [node.id, node]));

    for (const edge of data.edges) {
      const source = positions.get(edge.source);
      const target = positions.get(edge.target);
      if (!source || !target) continue;
      const geometry = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(source.x, source.y, source.z), new THREE.Vector3(target.x, target.y, target.z),
      ]);
      constellation.add(new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: 0x6f7485, transparent: true, opacity: 0.32 * edge.weight })));
    }

    for (const node of data.nodes) {
      const position = positions.get(node.id);
      if (!position) continue;
      const size = node.type === "file" ? 0.13 : node.type === "entity" ? 0.1 : 0.08;
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(size, 20, 20),
        new THREE.MeshBasicMaterial({ color: colors[node.type], transparent: true, opacity: 0.96 }),
      );
      mesh.position.set(position.x, position.y, position.z);
      mesh.userData.id = node.id;
      mesh.scale.setScalar(0.01);
      constellation.add(mesh);
      meshes.push(mesh);
    }

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const context = gsap.context(() => {
      gsap.to(meshes.map((mesh) => mesh.scale), { x: 1, y: 1, z: 1, duration: 0.55, ease: "back.out(1.6)", stagger: 0.04 });
      if (!reducedMotion) gsap.to(constellation.rotation, { y: Math.PI * 2, duration: 90, ease: "none", repeat: -1 });
    });
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const resize = () => {
      const { width, height } = canvas.getBoundingClientRect();
      renderer.setSize(width, height, false);
      camera.aspect = width / Math.max(height, 1);
      camera.updateProjectionMatrix();
    };
    const onClick = (event: PointerEvent) => {
      const rect = canvas.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObjects(meshes)[0];
      const node = hit && nodeById.get(hit.object.userData.id as string);
      if (node) selectRef.current(node);
    };
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(canvas);
    canvas.addEventListener("pointerup", onClick);
    resize();
    let frame = 0;
    const render = () => { renderer.render(scene, camera); frame = requestAnimationFrame(render); };
    render();
    return () => {
      cancelAnimationFrame(frame);
      canvas.removeEventListener("pointerup", onClick);
      resizeObserver.disconnect();
      context.revert();
      scene.traverse((object) => {
        const disposable = object as THREE.Mesh | THREE.Line;
        disposable.geometry?.dispose();
        const material = disposable.material;
        if (Array.isArray(material)) material.forEach((entry) => entry.dispose());
        else material?.dispose();
      });
      renderer.dispose();
    };
  }, [data]);

  return <div className={`knowledge-galaxy ${loading ? "is-loading" : ""}`}>
    <canvas aria-label="Knowledge graph" ref={canvasRef} />
    <div aria-hidden="true" className="galaxy-halo" />
    {selectedId && <span className="galaxy-selection" />}
  </div>;
}
