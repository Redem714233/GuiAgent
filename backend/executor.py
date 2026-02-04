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
        self._viewport_width = int(os.getenv("PLAYWRIGHT_VIEWPORT_WIDTH", "1280"))
        self._viewport_height = int(os.getenv("PLAYWRIGHT_VIEWPORT_HEIGHT", "720"))
        self._device_scale_factor = float(os.getenv("PLAYWRIGHT_DEVICE_SCALE_FACTOR", "1"))

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
        self._context = await self._browser.new_context(
            viewport={"width": self._viewport_width, "height": self._viewport_height},
            device_scale_factor=self._device_scale_factor,
        )
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
            self._context = await self._browser.new_context(
                viewport={"width": self._viewport_width, "height": self._viewport_height},
                device_scale_factor=self._device_scale_factor,
            )
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
            await self._page.screenshot(path=path, full_page=False)
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.screenshot(path=path, full_page=False)

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

    async def get_url(self) -> str:
        await self._ensure_page()
        return self._page.url

    async def wait_for_url_change(self, old_url: str, timeout_ms: int = 15000) -> None:
        await self._ensure_page()
        if self._page.url != old_url:
            return
        try:
            await self._page.wait_for_function(
                "oldUrl => location.href !== oldUrl",
                old_url,
                timeout=timeout_ms,
            )
        except Exception:
            pass

    async def wait_for_stable(self, delay_ms: int = 3000) -> None:
        await self._ensure_page()
        try:
            await self._page.wait_for_timeout(delay_ms)
        except Exception:
            pass

    async def wait_for_load(self, timeout_ms: int = 15000) -> None:
        await self._ensure_page()
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            await self._page.wait_for_load_state("load", timeout=timeout_ms)
        try:
            await self._page.wait_for_timeout(500)
        except Exception:
            pass

    async def scroll_by(self, delta_y: int) -> None:
        await self._ensure_page()
        try:
            await self._page.mouse.wheel(0, delta_y)
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.mouse.wheel(0, delta_y)

    async def scroll_to(self, y: int) -> None:
        await self._ensure_page()
        try:
            await self._page.evaluate("y => window.scrollTo(0, y)", y)
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.evaluate("y => window.scrollTo(0, y)", y)

    async def extract_text_by_selector(self, selector: str) -> str:
        await self._ensure_page()
        handle = await self._page.query_selector(selector)
        if not handle:
            return ""
        try:
            return (await handle.inner_text()) or ""
        except Exception:
            return ""

    async def extract_links(self, selector: str) -> list[dict]:
        await self._ensure_page()
        handles = await self._page.query_selector_all(selector)
        results: list[dict] = []
        for handle in handles:
            try:
                href = await handle.get_attribute("href")
                text = (await handle.inner_text()) or ""
                if href:
                    results.append({"text": text.strip(), "href": href.strip()})
            except Exception:
                continue
        return results
