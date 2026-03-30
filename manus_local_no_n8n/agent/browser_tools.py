from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import SETTINGS
from .storage_manager import reserve_storage_path, record_storage_file


BrowserToolFunc = Callable[..., dict[str, Any]]


class BrowserRuntime:
    def __init__(self) -> None:
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.headless = SETTINGS.browser_headless

    def ensure_started(self, headless: bool | None = None) -> None:
        if headless is not None:
            self.headless = headless
        if self.page and not self.page.is_closed():
            return
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        downloads_path = SETTINGS.workspace / SETTINGS.storage_root / "downloads" / "browser"
        downloads_path.mkdir(parents=True, exist_ok=True)
        self.context = self.browser.new_context(
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(SETTINGS.browser_timeout_ms)

    def current_page(self):
        self.ensure_started()
        return self.page

    def close(self) -> None:
        for obj_name in ("page", "context", "browser"):
            obj = getattr(self, obj_name)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, obj_name, None)
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None


RUNTIME = BrowserRuntime()
VISIBLE_ELEMENT_SELECTOR = 'a, button, input, textarea, select, [role="button"], [contenteditable="true"]'


def _workspace_path(path_str: str) -> Path:
    path = Path(path_str)
    candidate = (SETTINGS.workspace / path).resolve() if not path.is_absolute() else path.resolve()
    if candidate != SETTINGS.workspace and SETTINGS.workspace not in candidate.parents:
        raise ValueError(f"Path escapes workspace: {path_str}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _compact(text: str, max_chars: int = 160) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return collapsed[:max_chars]


def _escape_selector_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _suggest_selector(meta: dict[str, Any]) -> str:
    if meta.get("id"):
        return f'#{_escape_selector_text(str(meta["id"]))}'
    if meta.get("name"):
        return f'[name="{_escape_selector_text(str(meta["name"]))}"]'
    if meta.get("aria_label"):
        return f'[aria-label="{_escape_selector_text(str(meta["aria_label"]))}"]'
    if meta.get("placeholder"):
        return f'[placeholder="{_escape_selector_text(str(meta["placeholder"]))}"]'
    if meta.get("text"):
        return f'text="{_escape_selector_text(str(meta["text"]))}"'
    return meta.get("tag", "body")


def _best_effort_networkidle(page, timeout_ms: int) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


def browser_launch(headless: bool | None = None) -> dict[str, Any]:
    RUNTIME.ensure_started(headless=headless)
    page = RUNTIME.current_page()
    return {
        "status": "ready",
        "headless": RUNTIME.headless,
        "url": page.url,
        "title": page.title() if page.url and page.url != "about:blank" else "",
    }


def browser_open(url: str, wait_until: str = "load") -> dict[str, Any]:
    page = RUNTIME.current_page()
    response = page.goto(url, wait_until=wait_until)
    return {
        "url": page.url,
        "title": page.title(),
        "status": response.status if response else None,
    }


def browser_snapshot(max_items: int = 40, text_chars: int = 3000) -> dict[str, Any]:
    page = RUNTIME.current_page()
    body_text = _compact(page.locator("body").inner_text(timeout=SETTINGS.browser_timeout_ms), max_chars=text_chars)
    handles = page.locator(VISIBLE_ELEMENT_SELECTOR)
    count = min(handles.count(), max_items)
    items: list[dict[str, Any]] = []
    for i in range(count):
        loc = handles.nth(i)
        try:
            if not loc.is_visible():
                continue
            tag = (loc.evaluate("(el) => el.tagName.toLowerCase()") or "").strip()
            is_form_control = tag in {"input", "textarea", "select"}
            value = loc.input_value(timeout=500) if is_form_control else ""
            meta = {
                "tag": tag,
                "text": _compact(loc.inner_text(timeout=1000), max_chars=80),
                "aria_label": _compact(loc.get_attribute("aria-label") or "", max_chars=80),
                "placeholder": _compact(loc.get_attribute("placeholder") or "", max_chars=80),
                "name": _compact(loc.get_attribute("name") or "", max_chars=80),
                "id": _compact(loc.get_attribute("id") or "", max_chars=80),
                "type": _compact(loc.get_attribute("type") or "", max_chars=40),
                "value": _compact(value, max_chars=80),
            }
            meta["selector_hint"] = _suggest_selector(meta)
            items.append(meta)
        except Exception:
            continue
    return {
        "url": page.url,
        "title": page.title(),
        "text_excerpt": body_text,
        "elements": items,
    }


def browser_click(selector: str, timeout_ms: int | None = None) -> dict[str, Any]:
    page = RUNTIME.current_page()
    timeout_ms = timeout_ms or SETTINGS.browser_timeout_ms
    locator = page.locator(selector).first
    locator.click(timeout=timeout_ms)
    _best_effort_networkidle(page, timeout_ms)
    return {"clicked": selector, "url": page.url, "title": page.title()}


def browser_click_text(text: str, exact: bool = False, timeout_ms: int | None = None) -> dict[str, Any]:
    page = RUNTIME.current_page()
    timeout_ms = timeout_ms or SETTINGS.browser_timeout_ms
    locator = page.get_by_text(text, exact=exact).first
    locator.click(timeout=timeout_ms)
    _best_effort_networkidle(page, timeout_ms)
    return {"clicked_text": text, "exact": exact, "url": page.url, "title": page.title()}


def browser_type(
    selector: str,
    text: str,
    press_enter: bool = False,
    clear_first: bool = True,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    page = RUNTIME.current_page()
    timeout_ms = timeout_ms or SETTINGS.browser_timeout_ms
    locator = page.locator(selector).first
    if clear_first:
        locator.fill("", timeout=timeout_ms)
    locator.fill(text, timeout=timeout_ms)
    if press_enter:
        locator.press("Enter", timeout=timeout_ms)
    return {"typed_into": selector, "text_length": len(text), "pressed_enter": press_enter}


def browser_type_by_label(label: str, text: str, press_enter: bool = False, timeout_ms: int | None = None) -> dict[str, Any]:
    page = RUNTIME.current_page()
    timeout_ms = timeout_ms or SETTINGS.browser_timeout_ms
    locator = page.get_by_label(label).first
    locator.fill(text, timeout=timeout_ms)
    if press_enter:
        locator.press("Enter", timeout=timeout_ms)
    return {"label": label, "text_length": len(text), "pressed_enter": press_enter}


def browser_press(key: str, selector: str | None = None, timeout_ms: int | None = None) -> dict[str, Any]:
    page = RUNTIME.current_page()
    timeout_ms = timeout_ms or SETTINGS.browser_timeout_ms
    if selector:
        page.locator(selector).first.press(key, timeout=timeout_ms)
    else:
        page.keyboard.press(key)
    return {"pressed": key, "selector": selector}


def browser_read(selector: str = "body", max_chars: int = 5000) -> dict[str, Any]:
    page = RUNTIME.current_page()
    text = page.locator(selector).first.inner_text(timeout=SETTINGS.browser_timeout_ms)
    return {
        "selector": selector,
        "url": page.url,
        "title": page.title(),
        "text": _compact(text, max_chars=max_chars),
    }


def browser_wait_for_text(text: str, timeout_ms: int | None = None) -> dict[str, Any]:
    page = RUNTIME.current_page()
    page.get_by_text(text, exact=False).first.wait_for(timeout=timeout_ms or SETTINGS.browser_timeout_ms)
    return {"found_text": text, "url": page.url, "title": page.title()}


def browser_screenshot(path: str | None = None, full_page: bool = False, purpose: str = "browser screenshot") -> dict[str, Any]:
    page = RUNTIME.current_page()
    if path:
        target = _workspace_path(path)
        meta: dict[str, Any] | None = None
    else:
        target, meta = reserve_storage_path(
            extension=".png",
            kind="browser_screenshot",
            purpose=purpose,
            title=_page_title_slug(page),
            source_url=page.url,
            suggested_name=_page_title_slug(page),
        )
    page.screenshot(path=str(target), full_page=full_page)
    result = {"path": str(target.relative_to(SETTINGS.workspace)).replace("\\", "/"), "full_page": full_page, "url": page.url, "title": page.title()}
    if meta is not None:
        result["storage"] = record_storage_file(target, meta)
    return result


def browser_save_page_text(selector: str = "body", max_chars: int = 12000, purpose: str = "page text capture") -> dict[str, Any]:
    page = RUNTIME.current_page()
    text_value = page.locator(selector).first.inner_text(timeout=SETTINGS.browser_timeout_ms)
    target, meta = reserve_storage_path(
        extension=".md",
        kind="browser_text_capture",
        purpose=purpose,
        title=_page_title_slug(page),
        source_url=page.url,
        suggested_name=_page_title_slug(page),
    )
    content = f"# {page.title()}\n\nURL: {page.url}\n\n{text_value[:max_chars]}\n"
    target.write_text(content, encoding="utf-8")
    return {
        "path": str(target.relative_to(SETTINGS.workspace)).replace("\\", "/"),
        "url": page.url,
        "title": page.title(),
        "storage": record_storage_file(target, meta),
    }


def browser_close() -> dict[str, Any]:
    RUNTIME.close()
    return {"status": "closed"}


BROWSER_TOOL_REGISTRY: dict[str, BrowserToolFunc] = {
    "browser_launch": browser_launch,
    "browser_open": browser_open,
    "browser_snapshot": browser_snapshot,
    "browser_click": browser_click,
    "browser_click_text": browser_click_text,
    "browser_type": browser_type,
    "browser_type_by_label": browser_type_by_label,
    "browser_press": browser_press,
    "browser_read": browser_read,
    "browser_wait_for_text": browser_wait_for_text,
    "browser_screenshot": browser_screenshot,
    "browser_save_page_text": browser_save_page_text,
    "browser_close": browser_close,
}

BROWSER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "browser_launch",
            "description": "Start a persistent Chromium browser session for web automation. Use headed mode when the user wants to watch actions happen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "headless": {"type": "boolean", "description": "Run browser without a visible window. Defaults to env setting."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "Open a URL in the current browser tab.",
            "parameters": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "Absolute URL to open."},
                    "wait_until": {"type": "string", "description": "Playwright wait mode such as load, domcontentloaded, or networkidle."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": "Capture the current page state, including a text excerpt and candidate interactive elements with selector hints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_items": {"type": "integer", "description": "Maximum interactive elements to return."},
                    "text_chars": {"type": "integer", "description": "Maximum page text characters to include."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element using a Playwright locator selector such as #id, [name=\"q\"], text=\"Sign in\", or button:has-text(\"Submit\").",
            "parameters": {
                "type": "object",
                "required": ["selector"],
                "properties": {
                    "selector": {"type": "string", "description": "Playwright locator selector."},
                    "timeout_ms": {"type": "integer", "description": "Optional click timeout in milliseconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click_text",
            "description": "Click the first visible element that matches the given text.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Visible text to click."},
                    "exact": {"type": "boolean", "description": "Require an exact text match."},
                    "timeout_ms": {"type": "integer", "description": "Optional click timeout in milliseconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Fill an input or textarea identified by a Playwright selector.",
            "parameters": {
                "type": "object",
                "required": ["selector", "text"],
                "properties": {
                    "selector": {"type": "string", "description": "Playwright locator selector."},
                    "text": {"type": "string", "description": "Text to fill."},
                    "press_enter": {"type": "boolean", "description": "Press Enter after filling."},
                    "clear_first": {"type": "boolean", "description": "Clear the field before filling."},
                    "timeout_ms": {"type": "integer", "description": "Optional fill timeout in milliseconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type_by_label",
            "description": "Fill an input by its label text for forms that expose good accessibility labels.",
            "parameters": {
                "type": "object",
                "required": ["label", "text"],
                "properties": {
                    "label": {"type": "string", "description": "Accessible label text."},
                    "text": {"type": "string", "description": "Text to fill."},
                    "press_enter": {"type": "boolean", "description": "Press Enter after filling."},
                    "timeout_ms": {"type": "integer", "description": "Optional fill timeout in milliseconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press",
            "description": "Send a keyboard key such as Enter, Tab, Escape, Control+L, or ArrowDown. When selector is provided, send the key to that element.",
            "parameters": {
                "type": "object",
                "required": ["key"],
                "properties": {
                    "key": {"type": "string", "description": "Keyboard key or chord."},
                    "selector": {"type": "string", "description": "Optional Playwright locator selector."},
                    "timeout_ms": {"type": "integer", "description": "Optional timeout in milliseconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_read",
            "description": "Read visible text from a selector, usually body or a main content container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Playwright selector to read from. Defaults to body."},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for_text",
            "description": "Wait until specific text appears on the page.",
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Visible text to wait for."},
                    "timeout_ms": {"type": "integer", "description": "Optional timeout in milliseconds."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Save a screenshot of the current page into the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative output path inside the workspace."},
                    "full_page": {"type": "boolean", "description": "Capture the full page."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_save_page_text",
            "description": "Save visible page text into the smart storage folder with an automatic name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Selector to read text from."},
                    "max_chars": {"type": "integer", "description": "Maximum characters to save."},
                    "purpose": {"type": "string", "description": "Purpose for naming the saved file."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close",
            "description": "Close the browser session and free resources.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def browser_execute_tool(name: str, arguments: dict[str, Any]) -> str:
    if name not in BROWSER_TOOL_REGISTRY:
        return json.dumps({"error": f"Unknown browser tool: {name}"})
    try:
        result = BROWSER_TOOL_REGISTRY[name](**arguments)
        return json.dumps(result, ensure_ascii=False)
    except PlaywrightTimeoutError as exc:
        return json.dumps({"error": f"Browser timeout: {exc}"}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
