from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _keep_alive_env(name: str, default: str) -> Any:
    value = os.getenv(name, default).strip()
    if re.match(r"^-?\d+$", value):
        try:
            return int(value)
        except Exception:
            return value
    return value


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

    # Speed knobs
    ollama_keep_alive: Any = _keep_alive_env("OLLAMA_KEEP_ALIVE", "-1")
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
    ollama_temperature: float = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
    planner_temperature: float = float(os.getenv("OLLAMA_PLANNER_TEMPERATURE", "0.1"))
    verifier_temperature: float = float(os.getenv("OLLAMA_VERIFIER_TEMPERATURE", "0.05"))
    ollama_think: Any = os.getenv("OLLAMA_THINK", "false").strip().lower() not in {"0", "false", "no", "off"}

    # Policy knobs
    agent_policy_mode: str = os.getenv("AGENT_POLICY_MODE", "builder").strip().lower()
    agent_extra_system_prompt: str = os.getenv("AGENT_EXTRA_SYSTEM_PROMPT", "").strip()

    # Memory subsystem
    memory_enabled: bool = _bool_env("MEMORY_ENABLED", True)
    memory_db: Path = Path(os.getenv("MEMORY_DB_PATH", "storage/memory/manus_memory.db")).resolve()
    memory_timeout: int = int(os.getenv("MEMORY_TIMEOUT", "600"))
    memory_seed: int = int(os.getenv("MEMORY_SEED", "7"))
    bridge_stale_run_seconds: int = int(os.getenv("BRIDGE_STALE_RUN_SECONDS", "300"))
    planner_history_items: int = int(os.getenv("PLANNER_HISTORY_ITEMS", "3"))
    verifier_output_chars: int = int(os.getenv("VERIFIER_OUTPUT_CHARS", "1200"))


SETTINGS = Settings()
