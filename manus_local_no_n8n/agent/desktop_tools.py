from __future__ import annotations

import json
import os
import re
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

from .config import SETTINGS
from .storage_manager import reserve_storage_path, record_storage_file


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Desktop tools currently support Windows only.")


def _lazy_imports():
    _require_windows()
    import mss
    import psutil
    import pyautogui
    import pygetwindow as gw
    import pyperclip
    return mss, psutil, pyautogui, gw, pyperclip


def _workspace_path(path_str: str) -> Path:
    path = Path(path_str)
    candidate = (SETTINGS.workspace / path).resolve() if not path.is_absolute() else path.resolve()
    if candidate != SETTINGS.workspace and SETTINGS.workspace not in candidate.parents:
        raise ValueError(f"Path escapes workspace: {path_str}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _workspace_or_abs(path_str: str | None) -> str | None:
    if not path_str:
        return None
    path = Path(path_str)
    if path.is_absolute():
        return str(path)
    return str((SETTINGS.workspace / path).resolve())


def _configure_pyautogui(pyautogui) -> None:
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = SETTINGS.desktop_action_pause


def _normalize_keys(keys: str | list[str]) -> list[str]:
    if isinstance(keys, list):
        return [str(k).strip() for k in keys if str(k).strip()]
    parts = [p.strip() for p in re.split(r"[+,]", keys) if p.strip()]
    return parts


def desktop_get_screen_size() -> dict[str, Any]:
    _, _, pyautogui, _, _ = _lazy_imports()
    _configure_pyautogui(pyautogui)
    width, height = pyautogui.size()
    return {"width": width, "height": height}


def desktop_get_mouse_position() -> dict[str, Any]:
    _, _, pyautogui, _, _ = _lazy_imports()
    _configure_pyautogui(pyautogui)
    x, y = pyautogui.position()
    return {"x": x, "y": y}


def desktop_move_mouse(x: int, y: int, duration: float = 0.15) -> dict[str, Any]:
    _, _, pyautogui, _, _ = _lazy_imports()
    _configure_pyautogui(pyautogui)
    pyautogui.moveTo(x, y, duration=max(0, duration))
    return {"x": x, "y": y, "duration": duration}


def desktop_click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.1,
) -> dict[str, Any]:
    _, _, pyautogui, _, _ = _lazy_imports()
    _configure_pyautogui(pyautogui)
    if x is not None and y is not None:
        pyautogui.click(x=x, y=y, button=button, clicks=clicks, interval=interval)
        return {"x": x, "y": y, "button": button, "clicks": clicks}
    pyautogui.click(button=button, clicks=clicks, interval=interval)
    cur_x, cur_y = pyautogui.position()
    return {"x": cur_x, "y": cur_y, "button": button, "clicks": clicks}


def desktop_scroll(clicks: int) -> dict[str, Any]:
    _, _, pyautogui, _, _ = _lazy_imports()
    _configure_pyautogui(pyautogui)
    pyautogui.scroll(clicks)
    return {"scroll_clicks": clicks}


def desktop_press_keys(keys: str | list[str]) -> dict[str, Any]:
    _, _, pyautogui, _, _ = _lazy_imports()
    _configure_pyautogui(pyautogui)
    normalized = _normalize_keys(keys)
    if not normalized:
        raise ValueError("No keys provided.")
    if len(normalized) == 1:
        pyautogui.press(normalized[0])
    else:
        pyautogui.hotkey(*normalized)
    return {"keys": normalized}


def desktop_type_text(
    text: str,
    interval: float | None = None,
    press_enter: bool = False,
    use_clipboard: bool = False,
) -> dict[str, Any]:
    _, _, pyautogui, _, pyperclip = _lazy_imports()
    _configure_pyautogui(pyautogui)
    interval = SETTINGS.desktop_typing_interval if interval is None else interval
    if use_clipboard:
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
    else:
        pyautogui.write(text, interval=max(0, interval))
    if press_enter:
        pyautogui.press("enter")
    return {
        "text_length": len(text),
        "press_enter": press_enter,
        "use_clipboard": use_clipboard,
    }


def desktop_set_clipboard(text: str) -> dict[str, Any]:
    _, _, _, _, pyperclip = _lazy_imports()
    pyperclip.copy(text)
    return {"text_length": len(text)}


def desktop_get_clipboard(max_chars: int = 2000) -> dict[str, Any]:
    _, _, _, _, pyperclip = _lazy_imports()
    text = pyperclip.paste()
    return {"text": str(text)[:max_chars]}


def desktop_screenshot(path: str | None = None, monitor: int = 1, purpose: str = "desktop screenshot") -> dict[str, Any]:
    mss, _, _, _, _ = _lazy_imports()
    if path:
        target = _workspace_path(path)
        meta: dict[str, Any] | None = None
    else:
        target, meta = reserve_storage_path(
            extension=".png",
            kind="desktop_screenshot",
            purpose=purpose,
            suggested_name=f"desktop-monitor-{monitor}",
        )
    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor < 0 or monitor >= len(monitors):
            raise ValueError(f"Monitor {monitor} is unavailable. Available monitors: 1..{len(monitors)-1}")
        shot = sct.grab(monitors[monitor])
        mss.tools.to_png(shot.rgb, shot.size, output=str(target))
    result = {"path": str(target.relative_to(SETTINGS.workspace)).replace("\\", "/"), "monitor": monitor}
    if meta is not None:
        result["storage"] = record_storage_file(target, meta)
    return result


def desktop_locate_image(template_path: str, grayscale: bool = False) -> dict[str, Any]:
    _, _, pyautogui, _, _ = _lazy_imports()
    _configure_pyautogui(pyautogui)
    target = _workspace_path(template_path)
    region = pyautogui.locateOnScreen(str(target), grayscale=grayscale)
    if region is None:
        return {"found": False, "template": str(target.relative_to(SETTINGS.workspace))}
    center = pyautogui.center(region)
    return {
        "found": True,
        "template": str(target.relative_to(SETTINGS.workspace)),
        "left": region.left,
        "top": region.top,
        "width": region.width,
        "height": region.height,
        "center_x": center.x,
        "center_y": center.y,
    }


def desktop_click_image(
    template_path: str,
    button: str = "left",
    clicks: int = 1,
    grayscale: bool = False,
) -> dict[str, Any]:
    _, _, pyautogui, _, _ = _lazy_imports()
    _configure_pyautogui(pyautogui)
    target = _workspace_path(template_path)
    region = pyautogui.locateOnScreen(str(target), grayscale=grayscale)
    if region is None:
        return {"found": False, "template": str(target.relative_to(SETTINGS.workspace))}
    center = pyautogui.center(region)
    pyautogui.click(center.x, center.y, button=button, clicks=clicks)
    return {
        "found": True,
        "template": str(target.relative_to(SETTINGS.workspace)),
        "x": center.x,
        "y": center.y,
        "button": button,
        "clicks": clicks,
    }


def desktop_list_windows(max_windows: int = 50, visible_only: bool = True) -> dict[str, Any]:
    _, _, _, gw, _ = _lazy_imports()
    windows = []
    active_title = ""
    try:
        active = gw.getActiveWindow()
        active_title = active.title if active else ""
    except Exception:
        active_title = ""

    for win in gw.getAllWindows():
        title = (win.title or "").strip()
        if not title:
            continue
        if visible_only and (win.width <= 0 or win.height <= 0):
            continue
        windows.append(
            {
                "title": title,
                "left": win.left,
                "top": win.top,
                "width": win.width,
                "height": win.height,
                "is_active": title == active_title,
            }
        )
        if len(windows) >= max_windows:
            break
    return {"active_title": active_title, "windows": windows}


def desktop_get_active_window() -> dict[str, Any]:
    _, _, _, gw, _ = _lazy_imports()
    win = gw.getActiveWindow()
    if win is None:
        return {"active": None}
    return {
        "active": {
            "title": win.title,
            "left": win.left,
            "top": win.top,
            "width": win.width,
            "height": win.height,
        }
    }


def desktop_focus_window(title: str, exact: bool = False) -> dict[str, Any]:
    _, _, _, gw, _ = _lazy_imports()
    candidates = []
    title_cmp = title.lower()
    for win in gw.getAllWindows():
        current = (win.title or "").strip()
        if not current:
            continue
        matches = current.lower() == title_cmp if exact else title_cmp in current.lower()
        if matches:
            candidates.append(win)
    if not candidates:
        return {"focused": False, "title": title}
    win = candidates[0]
    try:
        if getattr(win, "isMinimized", False):
            win.restore()
    except Exception:
        pass
    try:
        win.activate()
    except Exception:
        try:
            win.minimize()
            win.restore()
            win.activate()
        except Exception as exc:
            return {"focused": False, "title": title, "error": str(exc)}
    return {
        "focused": True,
        "matched_title": win.title,
        "left": win.left,
        "top": win.top,
        "width": win.width,
        "height": win.height,
    }


def desktop_open_app(target: str, args: str = "", cwd: str | None = None) -> dict[str, Any]:
    _require_windows()
    actual_cwd = _workspace_or_abs(cwd) if cwd else str(SETTINGS.workspace)
    command = target if not args else f'"{target}" {args}'
    proc = subprocess.Popen(command, cwd=actual_cwd, shell=True)
    return {"started": True, "pid": proc.pid, "target": target, "args": args, "cwd": actual_cwd}


def desktop_open_url(url: str) -> dict[str, Any]:
    opened = webbrowser.open(url)
    return {"opened": bool(opened), "url": url}


def desktop_wait(seconds: float) -> dict[str, Any]:
    time.sleep(max(0, seconds))
    return {"slept_seconds": seconds}


def desktop_list_processes(name_filter: str = "", max_items: int = 50) -> dict[str, Any]:
    _, psutil, _, _, _ = _lazy_imports()
    name_filter = name_filter.lower().strip()
    items = []
    for proc in psutil.process_iter(["pid", "name", "status"]):
        try:
            name = proc.info.get("name") or ""
            if name_filter and name_filter not in name.lower():
                continue
            items.append(
                {
                    "pid": proc.info.get("pid"),
                    "name": name,
                    "status": proc.info.get("status"),
                }
            )
            if len(items) >= max_items:
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"processes": items}


DesktopToolFunc = Callable[..., dict[str, Any]]

DESKTOP_TOOL_REGISTRY: dict[str, DesktopToolFunc] = {
    "desktop_get_screen_size": desktop_get_screen_size,
    "desktop_get_mouse_position": desktop_get_mouse_position,
    "desktop_move_mouse": desktop_move_mouse,
    "desktop_click": desktop_click,
    "desktop_scroll": desktop_scroll,
    "desktop_press_keys": desktop_press_keys,
    "desktop_type_text": desktop_type_text,
    "desktop_set_clipboard": desktop_set_clipboard,
    "desktop_get_clipboard": desktop_get_clipboard,
    "desktop_screenshot": desktop_screenshot,
    "desktop_locate_image": desktop_locate_image,
    "desktop_click_image": desktop_click_image,
    "desktop_list_windows": desktop_list_windows,
    "desktop_get_active_window": desktop_get_active_window,
    "desktop_focus_window": desktop_focus_window,
    "desktop_open_app": desktop_open_app,
    "desktop_open_url": desktop_open_url,
    "desktop_wait": desktop_wait,
    "desktop_list_processes": desktop_list_processes,
}

DESKTOP_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "desktop_get_screen_size",
            "description": "Return the primary screen width and height in pixels.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_get_mouse_position",
            "description": "Return the current mouse cursor coordinates.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_move_mouse",
            "description": "Move the mouse to a screen coordinate.",
            "parameters": {
                "type": "object",
                "required": ["x", "y"],
                "properties": {
                    "x": {"type": "integer", "description": "Screen x coordinate in pixels."},
                    "y": {"type": "integer", "description": "Screen y coordinate in pixels."},
                    "duration": {"type": "number", "description": "Movement duration in seconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": "Click the mouse at a screen coordinate, or click at the current cursor position when no coordinates are provided.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Optional screen x coordinate."},
                    "y": {"type": "integer", "description": "Optional screen y coordinate."},
                    "button": {"type": "string", "description": "Mouse button: left, right, or middle."},
                    "clicks": {"type": "integer", "description": "How many clicks to perform."},
                    "interval": {"type": "number", "description": "Delay between clicks in seconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_scroll",
            "description": "Scroll the mouse wheel up or down. Positive values scroll up, negative scroll down.",
            "parameters": {
                "type": "object",
                "required": ["clicks"],
                "properties": {
                    "clicks": {"type": "integer", "description": "Scroll delta in wheel clicks."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_press_keys",
            "description": "Press one key or a hotkey chord such as enter, ctrl+l, alt+tab, or ctrl+shift+n.",
            "parameters": {
                "type": "object",
                "required": ["keys"],
                "properties": {
                    "keys": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Single key or multiple keys for a hotkey chord.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_type_text",
            "description": "Type text into the focused app. Clipboard paste mode is faster and more reliable for long text.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Text to type."},
                    "interval": {"type": "number", "description": "Delay between keystrokes in seconds."},
                    "press_enter": {"type": "boolean", "description": "Press Enter after typing."},
                    "use_clipboard": {"type": "boolean", "description": "Paste via clipboard instead of key-by-key typing."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_set_clipboard",
            "description": "Copy text into the Windows clipboard.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Clipboard text."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_get_clipboard",
            "description": "Read text currently stored in the clipboard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_chars": {"type": "integer", "description": "Maximum characters to return."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": "Save a screenshot of a monitor into the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative output path inside the workspace."},
                    "monitor": {"type": "integer", "description": "Monitor index. 1 is usually the primary monitor."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_locate_image",
            "description": "Find a template image from the workspace on the current screen and return its position.",
            "parameters": {
                "type": "object",
                "required": ["template_path"],
                "properties": {
                    "template_path": {"type": "string", "description": "Relative path to the template image inside the workspace."},
                    "grayscale": {"type": "boolean", "description": "Use grayscale matching for looser comparison."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click_image",
            "description": "Find a template image from the workspace on the current screen and click its center.",
            "parameters": {
                "type": "object",
                "required": ["template_path"],
                "properties": {
                    "template_path": {"type": "string", "description": "Relative path to the template image inside the workspace."},
                    "button": {"type": "string", "description": "Mouse button: left, right, or middle."},
                    "clicks": {"type": "integer", "description": "How many clicks to perform."},
                    "grayscale": {"type": "boolean", "description": "Use grayscale matching for looser comparison."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_list_windows",
            "description": "List top-level desktop windows and the currently active one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_windows": {"type": "integer", "description": "Maximum windows to return."},
                    "visible_only": {"type": "boolean", "description": "Only include windows with positive size."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_get_active_window",
            "description": "Return the active foreground window title and bounds.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_focus_window",
            "description": "Focus a window by title match before typing or clicking.",
            "parameters": {
                "type": "object",
                "required": ["title"],
                "properties": {
                    "title": {"type": "string", "description": "Full or partial window title."},
                    "exact": {"type": "boolean", "description": "Require an exact title match."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_open_app",
            "description": "Open a Windows application or file using shell execution.",
            "parameters": {
                "type": "object",
                "required": ["target"],
                "properties": {
                    "target": {"type": "string", "description": "App name, executable path, document path, or shell target."},
                    "args": {"type": "string", "description": "Optional command-line arguments."},
                    "cwd": {"type": "string", "description": "Optional working directory. Relative paths are resolved inside the workspace."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_open_url",
            "description": "Open a URL using the system default browser.",
            "parameters": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "Absolute URL to open."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_wait",
            "description": "Sleep for a short period while waiting for a window or UI to update.",
            "parameters": {
                "type": "object",
                "required": ["seconds"],
                "properties": {
                    "seconds": {"type": "number", "description": "Seconds to wait."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_list_processes",
            "description": "List running processes, optionally filtered by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_filter": {"type": "string", "description": "Optional substring filter for process names."},
                    "max_items": {"type": "integer", "description": "Maximum processes to return."},
                },
            },
        },
    },
]


def desktop_execute_tool(name: str, arguments: dict[str, Any]) -> str:
    if name not in DESKTOP_TOOL_REGISTRY:
        return json.dumps({"error": f"Unknown desktop tool: {name}"})
    try:
        result = DESKTOP_TOOL_REGISTRY[name](**arguments)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
