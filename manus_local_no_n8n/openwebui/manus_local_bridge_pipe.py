from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        BRIDGE_URL: str = Field(
            default="http://host.docker.internal:8787",
            description=(
                "URL of the local Manus bridge server. Because Open WebUI is running in Docker, "
                "host.docker.internal usually points back to the Windows host machine."
            ),
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=900,
            description="How long Open WebUI should wait for the bridge to finish a task.",
        )
        DEFAULT_AGENT_MODEL: str = Field(
            default="qwen3:8b",
            description="Optional Ollama model override for the inner agent. Leave empty to use the bridge default.",
        )
        MAX_STEPS: int = Field(
            default=20,
            description="Maximum number of autonomous tool loop steps per request.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [
            {
                "id": "manus-local-bridge",
                "name": "Manus Local Bridge",
            }
        ]

    def _extract_task(self, body: dict[str, Any]) -> str:
        messages = body.get("messages") or []
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    parts: list[str] = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            parts.append(str(item.get("text", "")))
                    return "\n".join(part for part in parts if part).strip()
        return ""

    def _call_bridge(self, task: str) -> str:
        payload: dict[str, Any] = {
            "task": task,
            "max_steps": self.valves.MAX_STEPS,
        }
        requested_model = self.valves.DEFAULT_AGENT_MODEL.strip()
        if requested_model and requested_model.lower() not in {"manus local bridge", "manus-local-bridge"}:
            payload["model"] = requested_model

        data = json.dumps(payload).encode("utf-8")
        base_url = self.valves.BRIDGE_URL.rstrip("/")
        request = urllib.request.Request(
            f"{base_url}/run-local-desktop-agent",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.valves.REQUEST_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return (
                f"Bridge request failed with HTTP {exc.code}.\n"
                f"URL: {base_url}/run-local-desktop-agent\n\n"
                f"Response:\n{body}"
            )
        except Exception as exc:  # noqa: BLE001
            return (
                f"Failed to reach the local Manus bridge at {base_url}.\n"
                f"Error: {exc}\n\n"
                f"Make sure start_manus_openwebui_bridge.bat is running on the Windows host."
            )

        lines = [
            f"Task completed in {result.get('duration_seconds', '?')}s.",
            "",
            result.get("final_output") or "The task finished but did not return a final summary.",
        ]

        artifacts = result.get("recent_artifacts") or []
        if artifacts:
            lines.append("")
            lines.append("Artifacts created or updated:")
            for item in artifacts:
                lines.append(f"- {item.get('path')} ({item.get('size_bytes', 0)} bytes)")

        log_path = result.get("log_path")
        if log_path:
            lines.append("")
            lines.append(f"Bridge log: {log_path}")

        return "\n".join(lines)

    async def pipe(self, body: dict[str, Any]):
        task = self._extract_task(body)
        if not task:
            return "No user task was found in the chat payload."
        return await asyncio.to_thread(self._call_bridge, task)
