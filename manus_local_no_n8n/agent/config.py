from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    chat_model: str = os.getenv("OLLAMA_CHAT_MODEL", "qwen3:8b")
    embed_model: str = os.getenv("OLLAMA_EMBED_MODEL", "embeddinggemma")
    max_steps: int = int(os.getenv("AGENT_MAX_STEPS", "20"))
    command_timeout: int = int(os.getenv("AGENT_COMMAND_TIMEOUT", "45"))
    workspace: Path = Path(os.getenv("WORKSPACE", ".")).resolve()
    index_file: str = ".agent_index.json"
    browser_headless: bool = _bool_env("BROWSER_HEADLESS", False)
    browser_timeout_ms: int = int(os.getenv("BROWSER_TIMEOUT_MS", "12000"))
    desktop_action_pause: float = float(os.getenv("DESKTOP_ACTION_PAUSE", "0.12"))
    desktop_typing_interval: float = float(os.getenv("DESKTOP_TYPING_INTERVAL", "0.01"))
    storage_root: str = os.getenv("AGENT_STORAGE_ROOT", "storage")
    planner_model: str = os.getenv("OLLAMA_PLANNER_MODEL", os.getenv("OLLAMA_CHAT_MODEL", "qwen3:8b"))
    verifier_model: str = os.getenv("OLLAMA_VERIFIER_MODEL", os.getenv("OLLAMA_CHAT_MODEL", "qwen3:8b"))


SETTINGS = Settings()
