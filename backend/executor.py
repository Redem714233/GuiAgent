from __future__ import annotations

import asyncio
import os
import logging
from typing import Optional, Tuple

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


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
        try:
            # 设置30秒超时，并且只等待 domcontentloaded 状态
            await self._page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"Page goto timeout or error: {e}, continuing anyway")
            # 即使超时也继续执行

    async def screenshot(self, path: str, max_retries: int = 2) -> None:
        """
        截图方法，带重试和超时保护
        参考 Open-AutoGLM 的实现，使用更可靠的超时机制
        """
        for attempt in range(max_retries):
            try:
                await self._ensure_page()

                # 使用 Playwright 的超时参数（30秒）
                # 如果失败，会抛出 TimeoutError
                await self._page.screenshot(path=path, full_page=False, timeout=30000)
                logger.info(f"Screenshot saved successfully to {path}")
                return  # 成功，直接返回

            except Exception as e:
                logger.warning(f"Screenshot attempt {attempt + 1}/{max_retries} failed: {e}")

                if attempt < max_retries - 1:
                    # 还有重试机会，重启浏览器
                    logger.info("Restarting browser for retry...")
                    await self._restart()
                    await self._ensure_page()
                    await asyncio.sleep(2)  # 等待2秒让浏览器稳定
                else:
                    # 最后一次尝试也失败了，创建一个黑色占位图
                    logger.error(f"All screenshot attempts failed, creating fallback image")
                    self._create_fallback_screenshot(path)
                    return

    def _create_fallback_screenshot(self, path: str) -> None:
        """创建黑色占位图（参考 Open-AutoGLM）"""
        try:
            from PIL import Image
            # 创建一个黑色图像
            img = Image.new('RGB', (1280, 720), color='black')
            img.save(path)
            logger.info(f"Created fallback screenshot at {path}")
        except Exception as e:
            logger.error(f"Failed to create fallback screenshot: {e}")
            raise

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
        # 优先等待 domcontentloaded，这��更可靠
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass  # 如果超时，继续执行

        # 尝试等待 load 状态
        try:
            await self._page.wait_for_load_state("load", timeout=min(timeout_ms, 5000))
        except Exception:
            pass  # 如果超时，继续执行

        # 短暂等待页面稳定
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

    async def go_back(self) -> None:
        await self._ensure_page()
        try:
            await self._page.go_back()
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.go_back()

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
