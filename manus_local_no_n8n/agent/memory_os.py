
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None


class MemoryError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\t+", " ", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def lexical_score(query: str, text: str) -> float:
    q = set(re.findall(r"[a-zA-Z0-9_]{2,}", query.lower()))
    t = set(re.findall(r"[a-zA-Z0-9_]{2,}", text.lower()))
    if not q or not t:
        return 0.0
    return len(q & t) / max(1, len(q))


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def parse_embedding_json(s: Optional[str]) -> List[float]:
    if not s:
        return []
    try:
        value = json.loads(s)
        if isinstance(value, list):
            return [float(x) for x in value]
    except Exception:
        return []
    return []


def _post_json(url: str, payload: Dict[str, Any], timeout: int = 300) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise MemoryError(f"Ollama HTTP {e.code}: {body[:1000]}") from e
    except urllib.error.URLError as e:
        raise MemoryError(f"Ollama connection failed: {e}") from e
    try:
        return json.loads(data)
    except Exception as e:
        raise MemoryError(f"Invalid JSON from Ollama: {data[:500]}") from e


def ollama_embed(model: str, text: str, host: str, timeout: int = 300) -> List[float]:
    data = _post_json(f"{host.rstrip('/')}/api/embed", {"model": model, "input": text}, timeout=timeout)
    emb = data.get("embeddings")
    if isinstance(emb, list) and emb and isinstance(emb[0], list):
        return [float(x) for x in emb[0]]
    if isinstance(emb, list):
        return [float(x) for x in emb]
    raise MemoryError(f"Unexpected embedding response: {data}")


def ollama_chat_json(
    model: str,
    system: str,
    user: str,
    host: str,
    timeout: int = 600,
    schema: Optional[Dict[str, Any]] = None,
    temperature: float = 0.0,
    seed: int = 7,
    keep_alive: Any = "-1",
    think: Any = False,
    num_ctx: int = 4096,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": schema if schema is not None else "json",
        "keep_alive": keep_alive,
        "think": think,
        "options": {
            "temperature": temperature,
            "seed": seed,
            "num_ctx": num_ctx,
            "top_p": 1,
            "top_k": 1,
            "repeat_penalty": 1.0,
        },
    }
    data = _post_json(f"{host.rstrip('/')}/api/chat", payload, timeout=timeout)
    msg = data.get("message", {}).get("content", "")
    if not msg:
        raise MemoryError(f"Empty response from Ollama: {data}")
    try:
        return json.loads(msg)
    except json.JSONDecodeError as e:
        raise MemoryError(f"Model did not return valid JSON: {msg[:1500]}") from e


@dataclasses.dataclass
class Node:
    node_id: str
    title: str
    summary: str
    start_index: Optional[int]
    end_index: Optional[int]
    text: Optional[str]
    children: List["Node"]
    level: int
    raw: Dict[str, Any]


@dataclasses.dataclass
class EvidenceItem:
    evid_id: str
    evid_type: str
    label: str
    text: str
    score: float
    source_ref: str


@dataclasses.dataclass
class AnswerBundle:
    decision: str
    reason: str
    answer: str
    citations: List[Dict[str, Any]]
    used_pages: List[int]
    used_memory_ids: List[str]
    used_node_ids: List[str]
    memory_writes: List[Dict[str, Any]]


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  title TEXT,
  sha1 TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
  page_pk INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id TEXT NOT NULL,
  page_no INTEGER NOT NULL,
  text TEXT NOT NULL,
  text_sha1 TEXT NOT NULL,
  embedding_json TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(doc_id, page_no)
);

CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT,
  start_index INTEGER,
  end_index INTEGER,
  level INTEGER NOT NULL,
  text TEXT,
  embedding_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
  memory_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  subject TEXT,
  text TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_ref TEXT,
  created_at TEXT NOT NULL,
  last_accessed_at TEXT NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0,
  strength REAL NOT NULL,
  novelty REAL NOT NULL,
  confidence REAL NOT NULL,
  embedding_json TEXT
);

CREATE TABLE IF NOT EXISTS memory_links (
  src_memory_id TEXT NOT NULL,
  dst_memory_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (src_memory_id, dst_memory_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_pages_doc_page ON pages(doc_id, page_no);
CREATE INDEX IF NOT EXISTS idx_nodes_doc ON nodes(doc_id);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
"""


class MemoryStore:
    def __init__(self, path: Path):
        ensure_parent(path)
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def init(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def upsert_document(self, doc_id: str, source_path: str, source_kind: str, title: str, sha1: str) -> None:
        self.conn.execute(
            """
            INSERT INTO documents(doc_id, source_path, source_kind, title, sha1, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
              source_path=excluded.source_path,
              source_kind=excluded.source_kind,
              title=excluded.title,
              sha1=excluded.sha1
            """,
            (doc_id, source_path, source_kind, title, sha1, utc_now()),
        )

    def replace_pages(self, doc_id: str, pages: Dict[int, str], embeddings: Dict[int, List[float]]) -> None:
        self.conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
        now = utc_now()
        for page_no, text in sorted(pages.items()):
            emb = embeddings.get(page_no)
            self.conn.execute(
                """
                INSERT INTO pages(doc_id, page_no, text, text_sha1, embedding_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (doc_id, page_no, text, sha1_text(text), json.dumps(emb) if emb else None, now),
            )

    def replace_nodes(self, doc_id: str, nodes: Sequence[Node], embeddings: Dict[str, List[float]]) -> None:
        self.conn.execute("DELETE FROM nodes WHERE doc_id = ?", (doc_id,))
        now = utc_now()
        for n in nodes:
            emb = embeddings.get(n.node_id)
            self.conn.execute(
                """
                INSERT INTO nodes(node_id, doc_id, title, summary, start_index, end_index, level, text, embedding_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    n.node_id,
                    doc_id,
                    n.title,
                    n.summary,
                    n.start_index,
                    n.end_index,
                    n.level,
                    n.text,
                    json.dumps(emb) if emb else None,
                    now,
                ),
            )

    def all_pages(self, doc_id: Optional[str] = None):
        if doc_id:
            return list(self.conn.execute("SELECT * FROM pages WHERE doc_id = ? ORDER BY page_no", (doc_id,)))
        return list(self.conn.execute("SELECT * FROM pages ORDER BY doc_id, page_no"))

    def pages_in_ranges(self, doc_id: str, ranges: Sequence[Tuple[int, int]]):
        if not ranges:
            return []
        clauses = []
        params: List[Any] = [doc_id]
        for start, end in ranges:
            clauses.append("(page_no BETWEEN ? AND ?)")
            params.extend([start, end])
        sql = "SELECT * FROM pages WHERE doc_id = ? AND (" + " OR ".join(clauses) + ") ORDER BY page_no"
        return list(self.conn.execute(sql, params))

    def all_nodes(self, doc_id: Optional[str] = None):
        if doc_id:
            return list(self.conn.execute("SELECT * FROM nodes WHERE doc_id = ? ORDER BY level, start_index", (doc_id,)))
        return list(self.conn.execute("SELECT * FROM nodes ORDER BY doc_id, level, start_index"))

    def all_memories(self):
        return list(self.conn.execute("SELECT * FROM memories ORDER BY created_at DESC"))

    def upsert_memory(
        self,
        memory_id: str,
        kind: str,
        subject: str,
        text: str,
        source_type: str,
        source_ref: str,
        strength: float,
        novelty: float,
        confidence: float,
        embedding: Optional[List[float]],
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO memories(memory_id, kind, subject, text, source_type, source_ref, created_at,
                                 last_accessed_at, access_count, strength, novelty, confidence, embedding_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
              kind=excluded.kind,
              subject=excluded.subject,
              text=excluded.text,
              source_type=excluded.source_type,
              source_ref=excluded.source_ref,
              last_accessed_at=excluded.last_accessed_at,
              strength=excluded.strength,
              novelty=excluded.novelty,
              confidence=excluded.confidence,
              embedding_json=excluded.embedding_json
            """,
            (
                memory_id,
                kind,
                subject,
                text,
                source_type,
                source_ref,
                now,
                now,
                float(strength),
                float(novelty),
                float(confidence),
                json.dumps(embedding) if embedding else None,
            ),
        )

    def touch_memories(self, memory_ids: Sequence[str], bump: float = 0.05) -> None:
        now = utc_now()
        for mid in memory_ids:
            self.conn.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed_at = ?,
                    strength = MIN(1.0, strength + ?)
                WHERE memory_id = ?
                """,
                (now, bump, mid),
            )

    def decay_memories(self, factor: float = 0.995) -> None:
        self.conn.execute("UPDATE memories SET strength = MAX(0.05, strength * ?)", (factor,))

    def add_link(self, src_memory_id: str, dst_memory_id: str, relation: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO memory_links(src_memory_id, dst_memory_id, relation, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (src_memory_id, dst_memory_id, relation, utc_now()),
        )

    def get_stats(self) -> Dict[str, int]:
        c = self.conn.cursor()
        out: Dict[str, int] = {}
        for table in ("documents", "pages", "nodes", "memories", "memory_links"):
            out[table] = int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out


def _safe_str(x: Any) -> str:
    return "" if x is None else str(x).strip()


def _coerce_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    m = re.search(r"-?\d+", str(v))
    return int(m.group(0)) if m else None


def _hash_id(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _node_from_obj(obj: Dict[str, Any], level: int) -> Node:
    children = [_node_from_obj(c, level + 1) for c in obj.get("nodes", []) if isinstance(c, dict)]
    node_id = _safe_str(obj.get("node_id")) or _hash_id(
        f"{_safe_str(obj.get('title'))}|{obj.get('start_index')}|{obj.get('end_index')}|{level}"
    )
    return Node(
        node_id=node_id,
        title=_safe_str(obj.get("title")) or "(untitled)",
        summary=_safe_str(obj.get("summary")),
        start_index=_coerce_int(obj.get("start_index")),
        end_index=_coerce_int(obj.get("end_index")),
        text=obj.get("text") or obj.get("node_text"),
        children=children,
        level=level,
        raw=obj,
    )


def tree_from_json(obj: Dict[str, Any]) -> Node:
    if "nodes" in obj and "title" in obj:
        return _node_from_obj(obj, 0)
    for key in ("tree", "root", "document", "doc"):
        v = obj.get(key)
        if isinstance(v, dict) and "nodes" in v:
            return _node_from_obj(v, 0)
    root = {
        "node_id": obj.get("node_id", "root"),
        "title": obj.get("title", "Document"),
        "summary": obj.get("summary", obj.get("doc_description", "")),
        "start_index": obj.get("start_index", 1),
        "end_index": obj.get("end_index"),
        "nodes": obj.get("nodes", []),
        "text": obj.get("text") or obj.get("node_text"),
    }
    return _node_from_obj(root, 0)


def flatten_tree(root: Node) -> List[Node]:
    out: List[Node] = []
    def rec(n: Node) -> None:
        out.append(n)
        for c in n.children:
            rec(c)
    rec(root)
    return out


class SourceStore:
    def pages(self) -> Dict[int, str]:
        raise NotImplementedError


class PDFStore(SourceStore):
    def __init__(self, path: Path):
        if fitz is None:
            raise MemoryError("PyMuPDF is required for PDF support. Install pymupdf.")
        self.doc = fitz.open(path)

    def pages(self) -> Dict[int, str]:
        out: Dict[int, str] = {}
        for i in range(len(self.doc)):
            out[i + 1] = normalize_text(self.doc[i].get_text("text"))
        return out


class TextStore(SourceStore):
    def __init__(self, path: Path):
        self.text = path.read_text(encoding="utf-8-sig")

    def pages(self) -> Dict[int, str]:
        lines = [line for line in self.text.splitlines() if line.strip()]
        return {i + 1: line for i, line in enumerate(lines)}


class JsonStore(SourceStore):
    def __init__(self, path: Path):
        self.obj = json.loads(path.read_text(encoding="utf-8-sig"))

    def pages(self) -> Dict[int, str]:
        text = json.dumps(self.obj, ensure_ascii=False, indent=2)
        lines = [line for line in text.splitlines() if line.strip()]
        return {i + 1: line for i, line in enumerate(lines)}


class HtmlStore(SourceStore):
    def __init__(self, path: Path):
        self.text = path.read_text(encoding="utf-8-sig")

    def pages(self) -> Dict[int, str]:
        stripped = re.sub(r"<[^>]+>", " ", self.text)
        stripped = normalize_text(stripped)
        lines = [line for line in stripped.splitlines() if line.strip()]
        return {i + 1: line for i, line in enumerate(lines)}


def build_source_store(path: Path) -> SourceStore:
    suf = path.suffix.lower()
    if suf == ".pdf":
        return PDFStore(path)
    if suf in {".md", ".markdown", ".rst", ".txt", ".csv"}:
        return TextStore(path)
    if suf == ".json":
        return JsonStore(path)
    if suf in {".htm", ".html"}:
        return HtmlStore(path)
    raise MemoryError(f"Unsupported source type: {path.suffix}")


def synthesize_structure_for_pages(title: str, max_page: int) -> Node:
    return Node("root", title, "Synthetic whole-document node", 1, max_page, None, [], 0, {})


def infer_title(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip() or "Document"


def ingest_document(
    db_path: Path,
    source_path: Path,
    structure_path: Optional[Path],
    embed_model: str,
    host: str,
    timeout: int,
    doc_id: Optional[str] = None,
) -> Dict[str, Any]:
    store = MemoryStore(db_path)
    try:
        source = build_source_store(source_path)
        pages = source.pages()
        if not pages:
            raise MemoryError("No text extracted from source document.")
        title = infer_title(source_path)
        did = doc_id or _hash_id(str(source_path.resolve()))
        if structure_path is not None:
            root = tree_from_json(json.loads(structure_path.read_text(encoding="utf-8-sig")))
        else:
            root = synthesize_structure_for_pages(title, max(pages))
        nodes = flatten_tree(root)

        page_embeddings: Dict[int, List[float]] = {}
        for page_no, text in pages.items():
            page_embeddings[page_no] = ollama_embed(embed_model, text, host=host, timeout=timeout)

        node_embeddings: Dict[str, List[float]] = {}
        for n in nodes:
            node_text = f"Title: {n.title}\nSummary: {n.summary}\nRange: {n.start_index}-{n.end_index}"
            node_embeddings[n.node_id] = ollama_embed(embed_model, node_text, host=host, timeout=timeout)

        store.upsert_document(
            doc_id=did,
            source_path=str(source_path),
            source_kind=source_path.suffix.lower().lstrip("."),
            title=title,
            sha1=sha1_text(json.dumps(pages, sort_keys=True)),
        )
        store.replace_pages(did, pages, page_embeddings)
        store.replace_nodes(did, nodes, node_embeddings)
        store.commit()
        return {"doc_id": did, "title": title, "pages": len(pages), "nodes": len(nodes), "source": str(source_path)}
    finally:
        store.close()


def age_days(ts: str) -> float:
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = dt.datetime.now(dt.timezone.utc)
        return max(0.0, (now - d).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def effective_strength(row: sqlite3.Row) -> float:
    base = float(row["strength"])
    days = age_days(row["last_accessed_at"])
    decayed = base * math.exp(-0.03 * days)
    access_bonus = min(0.25, 0.02 * int(row["access_count"]))
    return clamp(decayed + access_bonus, 0.0, 1.0)


def retrieve_nodes(rows, query: str, q_emb: Sequence[float], top_k: int = 6):
    scored = []
    for r in rows:
        text = f"{r['title']}\n{r['summary'] or ''}"
        emb = parse_embedding_json(r["embedding_json"])
        score = 0.62 * cosine_similarity(q_emb, emb) + 0.23 * lexical_score(query, text) + 0.15 * (1.0 / (1.0 + float(r["level"])))
        scored.append((r, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def retrieve_pages(rows, query: str, q_emb: Sequence[float], top_k: int = 10):
    scored = []
    for r in rows:
        emb = parse_embedding_json(r["embedding_json"])
        score = 0.7 * cosine_similarity(q_emb, emb) + 0.3 * lexical_score(query, r["text"])
        scored.append((r, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def retrieve_memories(rows, query: str, q_emb: Sequence[float], top_k: int = 8):
    scored = []
    for r in rows:
        text = f"{r['subject'] or ''}\n{r['text']}"
        emb = parse_embedding_json(r["embedding_json"])
        strength = effective_strength(r)
        score = 0.55 * cosine_similarity(q_emb, emb) + 0.15 * lexical_score(query, text) + 0.30 * strength
        scored.append((r, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def build_candidate_evidence(node_hits, page_hits, memory_hits) -> List[EvidenceItem]:
    out: List[EvidenceItem] = []
    for row, score in node_hits:
        out.append(EvidenceItem(f"node:{row['node_id']}", "node", f"node {row['node_id']} ({row['title']})", f"Title: {row['title']}\nSummary: {row['summary'] or ''}\nRange: {row['start_index']}-{row['end_index']}", score, str(row["node_id"])))
    for row, score in page_hits:
        out.append(EvidenceItem(f"page:{row['doc_id']}:{row['page_no']}", "page", f"page {row['page_no']}", row["text"], score, f"{row['doc_id']}:{row['page_no']}"))
    for row, score in memory_hits:
        out.append(EvidenceItem(f"memory:{row['memory_id']}", "memory", f"memory {row['memory_id']} ({row['kind']})", row["text"], score, str(row["memory_id"])))
    out.sort(key=lambda x: x.score, reverse=True)
    return out


SELECT_SCHEMA = {
    "type": "object",
    "properties": {"selected_ids": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    "required": ["selected_ids", "reason"],
    "additionalProperties": False,
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["answer", "abstain"]},
        "reason": {"type": "string"},
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "object", "properties": {"evidence_id": {"type": "string"}, "quote": {"type": "string"}}, "required": ["evidence_id", "quote"], "additionalProperties": False}},
        "claims": {"type": "array", "items": {"type": "string"}},
        "memory_writes": {"type": "array", "items": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["episodic", "semantic", "summary"]}, "subject": {"type": "string"}, "text": {"type": "string"}}, "required": ["kind", "subject", "text"], "additionalProperties": False}},
    },
    "required": ["decision", "reason", "answer", "citations", "claims", "memory_writes"],
    "additionalProperties": False,
}
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"supported": {"type": "boolean"}, "unsupported_claims": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    "required": ["supported", "unsupported_claims", "reason"],
    "additionalProperties": False,
}
CONSOLIDATE_SCHEMA = {
    "type": "object",
    "properties": {"summary_subject": {"type": "string"}, "summary_text": {"type": "string"}},
    "required": ["summary_subject", "summary_text"],
    "additionalProperties": False,
}


def select_evidence(model: str, query: str, candidates: Sequence[EvidenceItem], host: str, timeout: int, seed: int, keep_alive: Any, think: Any, num_ctx: int) -> List[EvidenceItem]:
    if not candidates:
        return []
    system = "You are an evidence selector for a high-precision QA system. Select only evidence directly useful for answering the question. Return JSON only."
    candidate_block = "\n\n".join(f"ID: {c.evid_id}\nType: {c.evid_type}\nLabel: {c.label}\nScore: {c.score:.4f}\nText:\n{c.text[:1800]}" for c in candidates[:20])
    user = f"Question:\n{query}\n\nCandidate evidence:\n{candidate_block}\n\nSelect the smallest sufficient set."
    data = ollama_chat_json(model, system, user, host, timeout=timeout, seed=seed, schema=SELECT_SCHEMA, keep_alive=keep_alive, think=think, num_ctx=num_ctx)
    selected = set(data.get("selected_ids") or [])
    out = [c for c in candidates if c.evid_id in selected]
    return out or list(candidates[:min(6, len(candidates))])


def answer_with_evidence(model: str, query: str, selected: Sequence[EvidenceItem], host: str, timeout: int, seed: int, keep_alive: Any, think: Any, num_ctx: int) -> Dict[str, Any]:
    system = textwrap.dedent("""
    You are a conservative evidence-grounded answerer.
    Rules:
    - Answer only from the provided evidence.
    - If the evidence is insufficient, ambiguous, or does not directly answer the question, abstain.
    - Every citation quote must be copied verbatim from one evidence item.
    - Do not invent facts.
    - memory_writes must only contain information directly supported by the evidence.
    Return JSON only.
    """).strip()
    evidence_block = "\n\n".join(f"Evidence ID: {e.evid_id}\nType: {e.evid_type}\nLabel: {e.label}\nText:\n{e.text[:3000]}" for e in selected)
    user = f"Question:\n{query}\n\nEvidence:\n{evidence_block}"
    return ollama_chat_json(model, system, user, host, timeout=timeout, seed=seed, schema=ANSWER_SCHEMA, keep_alive=keep_alive, think=think, num_ctx=num_ctx)


def verify_claims(model: str, claims: Sequence[str], selected: Sequence[EvidenceItem], host: str, timeout: int, seed: int, keep_alive: Any, think: Any, num_ctx: int) -> Dict[str, Any]:
    if not claims:
        return {"supported": True, "unsupported_claims": [], "reason": "No claims to verify."}
    system = "You verify whether every claim is directly supported by the provided evidence. If any claim is unsupported, return supported=false. Return JSON only."
    evidence_block = "\n\n".join(f"Evidence ID: {e.evid_id}\nText:\n{e.text[:2000]}" for e in selected)
    user = f"Claims:\n{json.dumps(list(claims), ensure_ascii=False, indent=2)}\n\nEvidence:\n{evidence_block}"
    return ollama_chat_json(model, system, user, host, timeout=timeout, seed=seed, schema=VERIFY_SCHEMA, keep_alive=keep_alive, think=think, num_ctx=num_ctx)


def quotes_are_verbatim(answer_data: Dict[str, Any], selected: Sequence[EvidenceItem]) -> Tuple[bool, List[str]]:
    by_id = {e.evid_id: normalize_text(e.text) for e in selected}
    bad: List[str] = []
    for c in answer_data.get("citations", []):
        evid_id = c.get("evidence_id", "")
        quote = normalize_text(c.get("quote", ""))
        hay = by_id.get(evid_id, "")
        if not quote or not hay or quote not in hay:
            bad.append(evid_id or "(missing)")
    return (len(bad) == 0, bad)


def memory_surprise_proxy(query_text: str, memory_text: str, evidence_strength: float, novelty: float) -> float:
    qlex = lexical_score(query_text, memory_text)
    return clamp(0.45 * novelty + 0.35 * evidence_strength + 0.20 * qlex, 0.0, 1.0)


def find_nearest_memory(memories, emb: Sequence[float]):
    best_row = None
    best_sim = -1.0
    for row in memories:
        row_emb = parse_embedding_json(row["embedding_json"])
        sim = cosine_similarity(emb, row_emb)
        if sim > best_sim:
            best_row = row
            best_sim = sim
    return best_row, best_sim


def write_supported_memories(store: MemoryStore, writes: Sequence[Dict[str, Any]], query: str, source_ref: str, embed_model: str, host: str, timeout: int) -> List[str]:
    existing = store.all_memories()
    written: List[str] = []
    for w in writes:
        kind = (w.get("kind") or "episodic").strip().lower()
        subject = normalize_text(w.get("subject", ""))[:240]
        text = normalize_text(w.get("text", ""))
        if not text:
            continue
        emb = ollama_embed(embed_model, f"{subject}\n{text}", host=host, timeout=timeout)
        nearest, sim = find_nearest_memory(existing, emb)
        novelty = clamp(1.0 - max(0.0, sim), 0.0, 1.0)
        surprise = memory_surprise_proxy(query, text, 0.95, novelty)
        if nearest is not None and sim >= 0.93:
            old_strength = effective_strength(nearest)
            new_strength = clamp(0.7 * old_strength + 0.3 * surprise, 0.05, 1.0)
            memory_id = str(nearest["memory_id"])
            store.upsert_memory(memory_id, kind, subject or str(nearest["subject"] or ""), text, "qa", source_ref, new_strength, min(float(nearest["novelty"]), novelty), 0.95, emb)
            written.append(memory_id)
        else:
            if surprise < 0.35:
                continue
            memory_id = _hash_id(f"{kind}|{subject}|{text}")
            store.upsert_memory(memory_id, kind, subject, text, "qa", source_ref, clamp(0.55 + 0.35 * surprise, 0.05, 1.0), novelty, 0.95, emb)
            if nearest is not None and sim >= 0.75:
                store.add_link(memory_id, str(nearest["memory_id"]), "related")
            written.append(memory_id)
    store.commit()
    return written


def greedy_semantic_groups(rows, threshold: float = 0.82):
    groups: List[List[Any]] = []
    centroids: List[List[float]] = []
    for r in rows:
        emb = parse_embedding_json(r["embedding_json"])
        if not emb:
            groups.append([r]); centroids.append([]); continue
        best_idx = -1; best_sim = -1.0
        for i, c in enumerate(centroids):
            sim = cosine_similarity(emb, c) if c else -1.0
            if sim > best_sim:
                best_sim = sim; best_idx = i
        if best_idx >= 0 and best_sim >= threshold:
            groups[best_idx].append(r)
            c = centroids[best_idx]
            if c:
                centroids[best_idx] = [(x + y) / 2.0 for x, y in zip(c, emb)]
            else:
                centroids[best_idx] = list(emb)
        else:
            groups.append([r]); centroids.append(list(emb))
    return groups


def consolidate_memories(db_path: Path, model: str, embed_model: str, host: str, timeout: int, seed: int, keep_alive: Any, think: Any, num_ctx: int) -> Dict[str, Any]:
    store = MemoryStore(db_path)
    try:
        rows = store.all_memories()
        groups = [g for g in greedy_semantic_groups(rows) if len(g) >= 3]
        created = 0
        for g in groups:
            block = "\n\n".join(f"- {r['subject'] or ''}: {r['text']}" for r in g[:12])
            data = ollama_chat_json(
                model=model,
                system="Summarize related memories into one durable summary memory. Return JSON only.",
                user=f"Memories:\n{block}",
                host=host,
                timeout=timeout,
                seed=seed,
                schema=CONSOLIDATE_SCHEMA,
                keep_alive=keep_alive,
                think=think,
                num_ctx=num_ctx,
            )
            subject = normalize_text(data.get("summary_subject", ""))[:240]
            text = normalize_text(data.get("summary_text", ""))
            if not text:
                continue
            emb = ollama_embed(embed_model, f"{subject}\n{text}", host=host, timeout=timeout)
            memory_id = _hash_id(f"summary|{subject}|{text}")
            store.upsert_memory(memory_id, "summary", subject, text, "consolidation", ",".join(str(r["memory_id"]) for r in g[:12]), 0.90, 0.20, 0.90, emb)
            for r in g[:12]:
                store.add_link(memory_id, str(r["memory_id"]), "summarizes")
            created += 1
        store.commit()
        return {"groups": len(groups), "summaries_created": created}
    finally:
        store.close()


def load_doc_id(store: MemoryStore, preferred_doc_id: Optional[str]) -> Optional[str]:
    if preferred_doc_id:
        row = store.conn.execute("SELECT doc_id FROM documents WHERE doc_id = ?", (preferred_doc_id,)).fetchone()
        return str(row[0]) if row else None
    row = store.conn.execute("SELECT doc_id FROM documents ORDER BY created_at DESC LIMIT 1").fetchone()
    return str(row[0]) if row else None


def has_memory_data(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    store = MemoryStore(db_path)
    try:
        stats = store.get_stats()
        return stats.get("documents", 0) > 0 or stats.get("memories", 0) > 0
    finally:
        store.close()


def run_query(
    db_path: Path,
    query: str,
    model: str,
    embed_model: str,
    host: str,
    timeout: int,
    seed: int = 7,
    doc_id: Optional[str] = None,
    keep_alive: Any = "-1",
    think: Any = False,
    num_ctx: int = 4096,
) -> AnswerBundle:
    store = MemoryStore(db_path)
    try:
        store.decay_memories()
        use_doc_id = load_doc_id(store, doc_id)
        if not use_doc_id:
            raise MemoryError("No documents found in memory DB. Ingest a document first.")
        q_emb = ollama_embed(embed_model, query, host=host, timeout=timeout)
        all_nodes = store.all_nodes(use_doc_id)
        node_hits = retrieve_nodes(all_nodes, query, q_emb, top_k=8)
        ranges: List[Tuple[int, int]] = []
        for row, _ in node_hits[:5]:
            s = row["start_index"]; e = row["end_index"]
            if s is not None and e is not None:
                ranges.append((int(s), int(e)))
        page_pool = store.pages_in_ranges(use_doc_id, ranges) if ranges else store.all_pages(use_doc_id)
        if not page_pool:
            page_pool = store.all_pages(use_doc_id)
        page_hits = retrieve_pages(page_pool, query, q_emb, top_k=10)
        memory_hits = retrieve_memories(store.all_memories(), query, q_emb, top_k=8)
        candidates = build_candidate_evidence(node_hits, page_hits, memory_hits)
        selected = select_evidence(model, query, candidates, host, timeout, seed, keep_alive, think, num_ctx)
        answer_data = answer_with_evidence(model, query, selected, host, timeout, seed, keep_alive, think, num_ctx)
        ok_quotes, bad_ids = quotes_are_verbatim(answer_data, selected)
        if not ok_quotes:
            return AnswerBundle("abstain", f"Citation verification failed for: {', '.join(bad_ids)}", "Insufficient verified evidence.", [], [], [], [], [])
        verify = verify_claims(model, answer_data.get("claims", []), selected, host, timeout, seed, keep_alive, think, num_ctx)
        if not bool(verify.get("supported", False)):
            return AnswerBundle("abstain", f"Claim verification failed: {verify.get('reason', '')}", "Insufficient verified evidence.", [], [], [], [], [])
        citations = answer_data.get("citations", [])
        used_pages: List[int] = []
        used_memory_ids: List[str] = []
        used_node_ids: List[str] = []
        for c in citations:
            evid_id = str(c.get("evidence_id", ""))
            if evid_id.startswith("page:"):
                try:
                    used_pages.append(int(evid_id.split(":")[-1]))
                except Exception:
                    pass
            elif evid_id.startswith("memory:"):
                used_memory_ids.append(evid_id.split(":", 1)[1])
            elif evid_id.startswith("node:"):
                used_node_ids.append(evid_id.split(":", 1)[1])
        store.touch_memories(used_memory_ids)
        written_ids = []
        if str(answer_data.get("decision")) == "answer":
            source_ref = f"query:{sha1_text(query)[:8]}"
            written_ids = write_supported_memories(store, answer_data.get("memory_writes", []), query, source_ref, embed_model, host, timeout)
        return AnswerBundle(
            decision=str(answer_data.get("decision", "abstain")),
            reason=str(answer_data.get("reason", "")),
            answer=str(answer_data.get("answer", "")),
            citations=citations,
            used_pages=sorted(set(used_pages)),
            used_memory_ids=sorted(set(used_memory_ids + written_ids)),
            used_node_ids=sorted(set(used_node_ids)),
            memory_writes=answer_data.get("memory_writes", []),
        )
    finally:
        store.commit()
        store.close()


def format_answer_bundle(bundle: AnswerBundle) -> str:
    lines = [bundle.answer.strip() or "No answer."]
    if bundle.citations:
        lines.append("\nCitations:")
        for c in bundle.citations[:12]:
            lines.append(f"- {c.get('evidence_id')}: {c.get('quote')}")
    if bundle.used_pages:
        lines.append(f"\nUsed pages: {', '.join(str(x) for x in bundle.used_pages)}")
    if bundle.used_node_ids:
        lines.append(f"Used nodes: {', '.join(bundle.used_node_ids[:12])}")
    if bundle.used_memory_ids:
        lines.append(f"Used memories: {', '.join(bundle.used_memory_ids[:12])}")
    return "\n".join(lines).strip()


def init_db(db_path: Path) -> Dict[str, Any]:
    store = MemoryStore(db_path)
    try:
        store.init()
        return {"db": str(db_path), "stats": store.get_stats()}
    finally:
        store.close()
