from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable

TEXT_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".scss", ".sql",
    ".sh", ".ps1", ".bat", ".java", ".go", ".rs", ".c", ".cpp", ".h",
}


class VectorStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = {"items": []}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        self.data = {"items": []}
        self.save()

    def add(self, source: str, chunk: str, embedding: list[float]) -> None:
        self.data["items"].append({
            "source": source,
            "chunk": chunk,
            "embedding": embedding,
        })

    def search(self, embedding: list[float], top_k: int = 5) -> list[dict]:
        ranked = []
        for item in self.data["items"]:
            score = cosine_similarity(embedding, item["embedding"])
            ranked.append({**item, "score": score})
        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked[:top_k]


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    a = list(a)
    b = list(b)
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def iter_text_files(root: Path, limit_files: int = 250) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if len(files) >= limit_files:
            break
        if not path.is_file():
            continue
        if any(part.startswith(".") and part not in {".github"} for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_EXTENSIONS and path.stat().st_size <= 250_000:
            files.append(path)
    return files


def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 150) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = max(0, end - overlap)
    return chunks
