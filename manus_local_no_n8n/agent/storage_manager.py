from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import SETTINGS

STORAGE_ROOT_NAME = "storage"
MANIFEST_NAME = "manifest.jsonl"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".svg"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".md", ".rtf", ".html", ".htm"}
CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".cpp", ".c", ".h", ".hpp", ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".sql", ".sh", ".ps1", ".bat", ".cmd", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".xml"}
DATA_EXTS = {".csv", ".tsv", ".json", ".jsonl", ".parquet", ".xlsx", ".xls"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}


def storage_root() -> Path:
    root = SETTINGS.workspace / STORAGE_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def manifest_path() -> Path:
    return storage_root() / MANIFEST_NAME


def _slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        return "item"
    return text[:max_len].strip("-") or "item"


def _domain_from_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    host = host.replace("www.", "")
    return _slugify(host, 40)


def _infer_category(extension: str, kind: str = "", category: str | None = None) -> tuple[str, str]:
    if category:
        cat = _slugify(category, 40)
        return cat, "general"
    ext = extension.lower()
    kind_l = (kind or "").lower()
    if "screenshot" in kind_l:
        if "browser" in kind_l:
            return "screenshots", "browser"
        if "desktop" in kind_l:
            return "screenshots", "desktop"
        return "screenshots", "general"
    if ext in IMAGE_EXTS:
        return "images", "general"
    if ext in VIDEO_EXTS:
        return "videos", "general"
    if ext in AUDIO_EXTS:
        return "audio", "general"
    if ext in ARCHIVE_EXTS:
        return "archives", "general"
    if ext in DATA_EXTS:
        return "data", "general"
    if ext in CODE_EXTS:
        return "code", "general"
    if ext in DOC_EXTS:
        return "documents", "general"
    mime, _ = mimetypes.guess_type(f"x{ext}")
    if mime:
        if mime.startswith("image/"):
            return "images", "general"
        if mime.startswith("video/"):
            return "videos", "general"
        if mime.startswith("audio/"):
            return "audio", "general"
        if mime.startswith("text/"):
            return "documents", "general"
    return "misc", "general"


def _unique_path(folder: Path, base_name: str, extension: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / f"{base_name}{extension}"
    if not candidate.exists():
        return candidate
    for idx in range(2, 1000):
        candidate = folder / f"{base_name}-{idx}{extension}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate unique storage path.")


def _write_manifest(record: dict[str, Any]) -> None:
    path = manifest_path()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def reserve_storage_path(
    *,
    extension: str,
    kind: str = "artifact",
    purpose: str = "",
    title: str = "",
    source_url: str = "",
    suggested_name: str = "",
    category: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    if extension and not extension.startswith("."):
        extension = "." + extension
    extension = extension or ".bin"
    cat, sub = _infer_category(extension, kind=kind, category=category)
    folder = storage_root() / cat / sub
    domain = _domain_from_url(source_url)
    preferred = suggested_name or title or purpose or domain or kind or "item"
    base = _slugify(preferred, 70)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if domain and domain not in base:
        base = _slugify(f"{domain}-{base}", 70)
    final_base = _slugify(f"{base}-{stamp}", 96)
    target = _unique_path(folder, final_base, extension)
    meta = {
        "storage_path": str(target.relative_to(SETTINGS.workspace)).replace("\\", "/"),
        "category": cat,
        "subcategory": sub,
        "kind": kind,
        "purpose": purpose,
        "title": title,
        "source_url": source_url,
        "extension": extension,
        "created_unix": time.time(),
    }
    return target, meta


def record_storage_file(path: Path, meta: dict[str, Any]) -> dict[str, Any]:
    stat = path.stat()
    record = {
        **meta,
        "size_bytes": stat.st_size,
        "modified_unix": stat.st_mtime,
    }
    _write_manifest(record)
    return record


def store_text_artifact(
    text: str,
    *,
    purpose: str = "text output",
    title: str = "",
    source_url: str = "",
    suggested_name: str = "",
    extension: str = ".md",
    kind: str = "text_artifact",
    category: str | None = None,
) -> dict[str, Any]:
    target, meta = reserve_storage_path(
        extension=extension,
        kind=kind,
        purpose=purpose,
        title=title,
        source_url=source_url,
        suggested_name=suggested_name,
        category=category,
    )
    target.write_text(text, encoding="utf-8")
    return record_storage_file(target, meta)


def store_existing_file(
    path: str,
    *,
    purpose: str = "",
    title: str = "",
    source_url: str = "",
    suggested_name: str = "",
    kind: str = "file_artifact",
    category: str | None = None,
    move: bool = False,
) -> dict[str, Any]:
    src = Path(path)
    if not src.is_absolute():
        src = (SETTINGS.workspace / src).resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(str(src))
    target, meta = reserve_storage_path(
        extension=src.suffix,
        kind=kind,
        purpose=purpose,
        title=title or src.stem,
        source_url=source_url,
        suggested_name=suggested_name or src.stem,
        category=category,
    )
    if move:
        shutil.move(str(src), str(target))
    else:
        shutil.copy2(str(src), str(target))
    meta["source_path"] = str(src)
    meta["move"] = move
    return record_storage_file(target, meta)


def list_recent_storage(limit: int = 20) -> list[dict[str, Any]]:
    root = storage_root()
    items: list[dict[str, Any]] = []
    for file in root.rglob("*"):
        if file.is_file() and file.name != MANIFEST_NAME:
            stat = file.stat()
            items.append({
                "path": str(file.relative_to(SETTINGS.workspace)).replace("\\", "/"),
                "size_bytes": stat.st_size,
                "modified_unix": stat.st_mtime,
            })
    items.sort(key=lambda item: item["modified_unix"], reverse=True)
    return items[:limit]
