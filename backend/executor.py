from __future__ import annotations

import asyncio
import os
from typing import Optional, Tuple

from playwright.async_api import async_playwright


class Executor:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._channel = os.getenv("PLAYWRIGHT_CHANNEL")
        self._executable = os.getenv("PLAYWRIGHT_EXECUTABLE")
        self._start_url = os.getenv("PLAYWRIGHT_START_URL")

    def _on_new_page(self, page) -> None:
        # If navigation opens a new tab/window, switch to it for subsequent actions/screenshot.
        self._page = page
        try:
            asyncio.create_task(page.bring_to_front())
        except RuntimeError:
            pass

    async def _start_fresh(self) -> None:
        self._playwright = await async_playwright().start()
        launch_kwargs = {"headless": False}
        if self._channel:
            launch_kwargs["channel"] = self._channel
        if self._executable:
            launch_kwargs["executable_path"] = self._executable
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context()
        self._context.on("page", self._on_new_page)
        self._page = await self._context.new_page()
        if self._start_url:
            await self._page.goto(self._start_url)

    async def _ensure_page(self) -> None:
        if self._playwright is None:
            await self._start_fresh()
            return
        if self._browser is None or not self._browser.is_connected():
            await self._restart()
            await self._start_fresh()
            return
        if self._context is None:
            self._context = await self._browser.new_context()
            self._context.on("page", self._on_new_page)
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
            if self._start_url:
                await self._page.goto(self._start_url)

    async def _restart(self) -> None:
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._playwright = None
        self._page = None

    async def start(self) -> None:
        await self._ensure_page()

    async def stop(self) -> None:
        await self._restart()

    async def goto(self, url: str) -> None:
        await self._ensure_page()
        await self._page.goto(url)

    async def screenshot(self, path: str) -> None:
        try:
            await self._ensure_page()
            await self._page.screenshot(path=path, full_page=True)
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.screenshot(path=path, full_page=True)

    async def click_center(self, center: Tuple[int, int]) -> None:
        try:
            await self._ensure_page()
            await self._page.mouse.click(center[0], center[1])
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.mouse.click(center[0], center[1])

    async def click_point(self, point: Tuple[int, int]) -> None:
        try:
            await self._ensure_page()
            await self._page.mouse.click(point[0], point[1])
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.mouse.click(point[0], point[1])

    async def type_text(self, text: str) -> None:
        try:
            await self._ensure_page()
            await self._page.keyboard.type(text)
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.keyboard.type(text)

    async def press(self, key: str) -> None:
        try:
            await self._ensure_page()
            await self._page.keyboard.press(key)
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.keyboard.press(key)

    async def get_dom_elements(self) -> list[dict]:
        await self._ensure_page()
        dom_elements: list[dict] = []
        handles = await self._page.query_selector_all("input, textarea")
        for handle in handles:
            box = await handle.bounding_box()
            if not box:
                continue
            meta = await handle.evaluate(
                """el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    aria: el.getAttribute('aria-label') || ''
                })"""
            )
            content = " ".join([meta.get("tag", ""), meta.get("type", ""), meta.get("placeholder", ""), meta.get("aria", "")]).strip()
            dom_elements.append(
                {
                    "type": "dom_input",
                    "content": content,
                    "center": (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)),
                    "bbox": (int(box["x"]), int(box["y"]), int(box["width"]), int(box["height"])),
                }
            )
        return dom_elements

    async def wait_for_load(self, timeout_ms: int = 15000) -> None:
        await self._ensure_page()
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            await self._page.wait_for_load_state("load", timeout=timeout_ms)
