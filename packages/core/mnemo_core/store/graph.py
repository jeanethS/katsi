"""Kùzu-backed graph store for file relationships."""

from __future__ import annotations

from pathlib import Path

import kuzu

from mnemo_core.models import FileRecord, IndexStatus


def _unwrap(val):
    """Return the Python value from a kuzu Value, or val itself if already bare."""
    return val.value if hasattr(val, "value") else val


class GraphStore:
    """Kùzu-backed graph store for files, entities, topics, and relationships."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(db_path))
        self._conn = kuzu.Connection(self._db)
        self.init_schema()

    def init_schema(self) -> None:
        """Run the DDL idempotently (IF NOT EXISTS)."""
        self._conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS File("
            "id STRING, path STRING, name STRING, ext STRING, "
            "summary STRING, mtime DOUBLE, "
            "PRIMARY KEY(id))"
        )
        self._conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS Entity("
            "name STRING, kind STRING, "
            "PRIMARY KEY(name))"
        )
        self._conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS Topic("
            "name STRING, "
            "PRIMARY KEY(name))"
        )
        self._conn.execute(
            "CREATE REL TABLE IF NOT EXISTS REFERENCES("
            "FROM File TO File)"
        )
        self._conn.execute(
            "CREATE REL TABLE IF NOT EXISTS MENTIONS("
            "FROM File TO Entity, weight DOUBLE)"
        )
        self._conn.execute(
            "CREATE REL TABLE IF NOT EXISTS ABOUT("
            "FROM File TO Topic, weight DOUBLE)"
        )
        self._conn.execute(
            "CREATE REL TABLE IF NOT EXISTS DUPLICATE_OF("
            "FROM File TO File, similarity DOUBLE)"
        )

    def upsert_file(self, file: FileRecord) -> None:
        """MERGE the File node by id with all fields."""
        self._conn.execute(
            "MERGE (f:File {id: $id}) "
            "SET f.path = $path, f.name = $name, f.ext = $ext, "
            "f.summary = $summary, f.mtime = $mtime",
            {
                "id": file.id,
                "path": file.path,
                "name": file.name,
                "ext": file.ext,
                "summary": file.summary if file.summary is not None else "",
                "mtime": file.mtime,
            },
        )

    def upsert_entity(self, name: str, kind: str) -> None:
        """MERGE the Entity node by name, set kind."""
        self._conn.execute(
            "MERGE (e:Entity {name: $name}) SET e.kind = $kind",
            {"name": name, "kind": kind},
        )

    def upsert_topic(self, name: str) -> None:
        """MERGE the Topic node by name."""
        self._conn.execute(
            "MERGE (t:Topic {name: $name})",
            {"name": name},
        )

    def add_mentions(self, file_id: str, entities: list[dict], weight: float = 1.0) -> None:
        """For each entity in @entities: upsert entity, then MERGE MENTIONS edge."""
        for ent in entities:
            self.upsert_entity(ent["name"], ent["kind"])
            self._conn.execute(
                "MATCH (f:File {id: $fid}), (e:Entity {name: $ename}) "
                "MERGE (f)-[:MENTIONS {weight: $w}]->(e)",
                {"fid": file_id, "ename": ent["name"], "w": weight},
            )

    def add_about(self, file_id: str, topics: list[str], weight: float = 1.0) -> None:
        """For each topic: upsert_topic, then MERGE ABOUT edge."""
        for topic in topics:
            self.upsert_topic(topic)
            self._conn.execute(
                "MATCH (f:File {id: $fid}), (t:Topic {name: $tname}) "
                "MERGE (f)-[:ABOUT {weight: $w}]->(t)",
                {"fid": file_id, "tname": topic, "w": weight},
            )

    def add_reference(self, src_file_id: str, dst_file_id: str) -> None:
        """MATCH both File nodes, MERGE REFERENCES edge. Skip if dst missing."""
        r = self._conn.execute(
            "MATCH (f:File {id: $fid}) RETURN f",
            {"fid": dst_file_id},
        )
        if not r.has_next():
            return
        self._conn.execute(
            "MATCH (src:File {id: $src}), (dst:File {id: $dst}) "
            "MERGE (src)-[:REFERENCES]->(dst)",
            {"src": src_file_id, "dst": dst_file_id},
        )

    def add_duplicate(self, src_file_id: str, dst_file_id: str, similarity: float) -> None:
        """MATCH both File nodes, MERGE DUPLICATE_OF edge."""
        self._conn.execute(
            "MATCH (src:File {id: $src}), (dst:File {id: $dst}) "
            "MERGE (src)-[:DUPLICATE_OF {similarity: $sim}]->(dst)",
            {"src": src_file_id, "dst": dst_file_id, "sim": similarity},
        )

    def neighbors(self, file_id: str, hops: int = 1) -> list[dict]:
        """Return 1-hop neighbors across all relationship types."""
        if hops != 1:
            raise NotImplementedError("Only hops=1 is supported in v0.1")

        results: list[dict] = []

        # a) REFERENCES: (f)-[:REFERENCES]->(o:File)
        r = self._conn.execute(
            "MATCH (f:File {id: $id})-[:REFERENCES]->(o:File) RETURN o.id AS file_id",
            {"id": file_id},
        )
        while r.has_next():
            row = r.get_next()
            results.append({
                "file_id": _unwrap(row[0]),
                "via": "references",
                "name": None,
                "score": 1.0,
            })

        # b) MENTIONS shared entity: (f)-[:MENTIONS]->(e)<-[:MENTIONS]-(o:File)
        r = self._conn.execute(
            "MATCH (f:File {id: $id})-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(o:File) "
            "WHERE o.id <> $id "
            "RETURN o.id AS file_id, e.name AS name",
            {"id": file_id},
        )
        while r.has_next():
            row = r.get_next()
            results.append({
                "file_id": _unwrap(row[0]),
                "via": "mentioned-entity",
                "name": _unwrap(row[1]),
                "score": 1.0,
            })

        # c) ABOUT shared topic: (f)-[:ABOUT]->(t)<-[:ABOUT]-(o:File)
        r = self._conn.execute(
            "MATCH (f:File {id: $id})-[:ABOUT]->(t:Topic)<-[:ABOUT]-(o:File) "
            "WHERE o.id <> $id "
            "RETURN o.id AS file_id, t.name AS name",
            {"id": file_id},
        )
        while r.has_next():
            row = r.get_next()
            results.append({
                "file_id": _unwrap(row[0]),
                "via": "shared-topic",
                "name": _unwrap(row[1]),
                "score": 1.0,
            })

        # d) DUPLICATE_OF: (f)-[:DUPLICATE_OF]->(o:File)
        r = self._conn.execute(
            "MATCH (f:File {id: $id})-[d:DUPLICATE_OF]->(o:File) "
            "RETURN o.id AS file_id, d.similarity AS score",
            {"id": file_id},
        )
        while r.has_next():
            row = r.get_next()
            results.append({
                "file_id": _unwrap(row[0]),
                "via": "duplicate",
                "name": None,
                "score": _unwrap(row[1]),
            })

        return results

    def get_file(self, file_id: str) -> FileRecord | None:
        """MATCH (f:File {id:$id}) RETURN f; return a FileRecord or None."""
        r = self._conn.execute(
            "MATCH (f:File {id: $id}) RETURN f",
            {"id": file_id},
        )
        if not r.has_next():
            return None
        row = r.get_next()
        # Kùzu returns the node as a dict-like value
        node = _unwrap(row[0])
        if isinstance(node, dict):
            return FileRecord(
                id=node.get("id", file_id),
                path=node.get("path", ""),
                name=node.get("name", ""),
                ext=node.get("ext", ""),
                mime="",
                size_bytes=0,
                mtime=node.get("mtime", 0.0),
                content_hash="",
                status=IndexStatus.INDEXED,
                summary=node.get("summary", None),
            )
        # Fallback: try treating as kuzu node struct
        try:
            vals = {col: _unwrap(node[i]) for i, col in enumerate(
                ["id", "path", "name", "ext", "summary", "mtime"]
            )}
        except (TypeError, IndexError):
            return None
        return FileRecord(
            id=vals["id"],
            path=vals["path"],
            name=vals["name"],
            ext=vals["ext"],
            mime="",
            size_bytes=0,
            mtime=vals["mtime"],
            content_hash="",
            status=IndexStatus.INDEXED,
            summary=vals.get("summary"),
        )

    def delete_by_file(self, file_id: str) -> None:
        """DETACH DELETE the File node and its edges."""
        self._conn.execute(
            "MATCH (f:File {id: $id}) DETACH DELETE f",
            {"id": file_id},
        )
