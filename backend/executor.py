from __future__ import annotations

import asyncio
import contextlib
import os
import logging
import shlex
from pathlib import Path
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
        self._persistent_context = str(os.getenv("PLAYWRIGHT_PERSISTENT_CONTEXT", "0")).strip().lower() in {
            "1", "true", "yes", "on"
        }
        self._user_data_dir = str(
            os.getenv(
                "PLAYWRIGHT_USER_DATA_DIR",
                Path("data").joinpath("playwright_user_data").as_posix(),
            )
        ).strip()

        # DOM 服务（延迟初始化）
        self._dom_service = None

    @property
    def dom_service(self):
        """延迟初始化 DOM 服务"""
        if self._dom_service is None:
            from .dom_service import DOMService
            self._dom_service = DOMService()
        return self._dom_service

    def _on_new_page(self, page) -> None:
        # Only switch to new page when it has a meaningful non-blank URL.
        # Download popups often open as about:blank briefly; switching immediately can break flow.
        async def _maybe_switch() -> None:
            try:
                for _ in range(8):
                    if page.is_closed():
                        return
                    try:
                        page_url = (page.url or "").strip().lower()
                    except Exception:
                        page_url = ""

                    if page_url and not page_url.startswith("about:blank"):
                        self._page = page
                        with contextlib.suppress(Exception):
                            await page.bring_to_front()
                        return
                    await asyncio.sleep(0.1)
            except Exception:
                return

        try:
            asyncio.create_task(_maybe_switch())
        except RuntimeError:
            pass

    async def _start_fresh(self) -> None:
        self._playwright = await async_playwright().start()
        base_args = [
            "--disable-features=DownloadBubble,DownloadBubbleV2",
            "--safebrowsing-disable-download-protection",
        ]

        if str(self._channel or "").lower() == "msedge":
            base_args.extend(
                [
                    "--hide-edge-download-bubble",
                    "--disable-features=msEdgeDownloadBubble,msEdgeDownloadBubbleV2",
                ]
            )

        extra_args_raw = str(os.getenv("PLAYWRIGHT_BROWSER_ARGS", "") or "").strip()
        extra_args: list[str] = []
        if extra_args_raw:
            with contextlib.suppress(Exception):
                extra_args = [arg for arg in shlex.split(extra_args_raw, posix=False) if str(arg or "").strip()]

        merged_args: list[str] = []
        seen_args: set[str] = set()
        for arg in [*base_args, *extra_args]:
            token = str(arg or "").strip()
            if not token or token in seen_args:
                continue
            seen_args.add(token)
            merged_args.append(token)

        launch_kwargs = {
            "headless": False,
            "args": merged_args,
        }

        # 根据 channel 选择浏览器类型
        if self._channel in ["msedge", "chrome"]:
            browser_type = self._playwright.chromium
            launch_kwargs["channel"] = self._channel
            # 注意：使用 channel 时不要设置 executable_path
        elif self._channel == "firefox":
            browser_type = self._playwright.firefox
            if self._channel:
                launch_kwargs["channel"] = self._channel
        elif self._channel == "webkit":
            browser_type = self._playwright.webkit
        else:
            # 没有指定 channel，使用默认 chromium
            browser_type = self._playwright.chromium
            # 只有在没有 channel 时才使用 executable_path
            if self._executable:
                launch_kwargs["executable_path"] = self._executable

        self._browser = None

        # 创建更真实的浏览器上下文（反反爬虫）
        context_options = {
            "viewport": {"width": self._viewport_width, "height": self._viewport_height},
            "device_scale_factor": self._device_scale_factor,
            # 添加真实的 User-Agent
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            # 添加常见的浏览器 headers
            "extra_http_headers": {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            },
            # 允许地理位置、通知等权限
            "permissions": ["geolocation", "notifications"],
            # 设置时区
            "timezone_id": "Asia/Shanghai",
            # 设置语言
            "locale": "zh-CN",
        }

        if self._persistent_context:
            user_data_dir = str(Path(self._user_data_dir).expanduser().resolve())
            os.makedirs(user_data_dir, exist_ok=True)
            logger.info(f"Launching persistent context with user data dir: {user_data_dir}")
            persistent_launch_kwargs = {**launch_kwargs, **context_options}
            self._context = await browser_type.launch_persistent_context(
                user_data_dir=user_data_dir,
                **persistent_launch_kwargs,
            )
            self._browser = self._context.browser
        else:
            self._browser = await browser_type.launch(**launch_kwargs)
            self._context = await self._browser.new_context(**context_options)

        self._context.on("page", self._on_new_page)
        existing_pages = [page for page in list(self._context.pages) if page and not page.is_closed()]
        self._page = existing_pages[0] if existing_pages else await self._context.new_page()

        # 注入反检测脚本（隐藏 webdriver 特征）
        stealth_init_script = """
            // 覆盖 navigator.webdriver
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });

            // 覆盖 chrome 对象
            window.chrome = {
                runtime: {}
            };

            // 覆盖 permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """
        with contextlib.suppress(Exception):
            await self._context.add_init_script(stealth_init_script)
        with contextlib.suppress(Exception):
            await self._page.add_init_script(stealth_init_script)

        if self._start_url:
            await self._page.goto(self._start_url)

    async def _ensure_page(self) -> None:
        if self._playwright is None:
            await self._start_fresh()
            return
        if self._browser is not None and not self._browser.is_connected():
            await self._restart()
            await self._start_fresh()
            return
        if self._context is None:
            if self._persistent_context:
                await self._restart()
                await self._start_fresh()
                return
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
        context = self._context
        browser = self._browser
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser and browser.is_connected():
                await browser.close()
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

    async def scroll_to_bottom(self) -> None:
        """滚动到页面底部"""
        await self._ensure_page()
        try:
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            await self._restart()
            await self._ensure_page()
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

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

    # ========================================================================
    # 新增方法：支持更多动作类型
    # ========================================================================

    async def upload_file(self, file_path: str, selector: str = 'input[type="file"]') -> None:
        """
        上传文件到文件输入框

        Args:
            file_path: 本地文件路径
            selector: 文件输入框的选择器
        """
        await self._ensure_page()
        try:
            # 查找文件输入框
            file_input = await self._page.query_selector(selector)
            if not file_input:
                raise ValueError(f"File input not found with selector: {selector}")

            # 设置文件
            await file_input.set_input_files(file_path)
            logger.info(f"Uploaded file: {file_path}")
        except Exception as e:
            logger.error(f"File upload failed: {e}")
            await self._restart()
            await self._ensure_page()
            file_input = await self._page.query_selector(selector)
            if file_input:
                await file_input.set_input_files(file_path)

    async def download_file(self, download_url: str, save_path: str) -> None:
        """
        下载文件

        Args:
            download_url: 下载链接
            save_path: 保存路径
        """
        await self._ensure_page()
        try:
            # 等待下载事件
            async with self._page.expect_download() as download_info:
                # 触发下载（导航到下载链接）
                await self._page.goto(download_url)

            download = await download_info.value
            # 保存文件
            await download.save_as(save_path)
            logger.info(f"Downloaded file to: {save_path}")
        except Exception as e:
            logger.error(f"File download failed: {e}")
            raise

    async def select_option(self, selector: str, value: str = None, label: str = None, index: int = None) -> None:
        """
        选择下拉框选项

        Args:
            selector: 下拉框选择器
            value: 选项的 value 属性
            label: 选项的文本
            index: 选项的索引
        """
        await self._ensure_page()
        try:
            if value is not None:
                await self._page.select_option(selector, value=value)
            elif label is not None:
                await self._page.select_option(selector, label=label)
            elif index is not None:
                await self._page.select_option(selector, index=index)
            else:
                raise ValueError("Must provide value, label, or index")

            logger.info(f"Selected option in {selector}")
        except Exception as e:
            logger.error(f"Select option failed: {e}")
            await self._restart()
            await self._ensure_page()
            if value is not None:
                await self._page.select_option(selector, value=value)
            elif label is not None:
                await self._page.select_option(selector, label=label)
            elif index is not None:
                await self._page.select_option(selector, index=index)

    async def checkbox(self, selector: str, checked: bool) -> None:
        """
        设置复选框状态

        Args:
            selector: 复选框选择器
            checked: 目标状态（True=选中，False=未选中）
        """
        await self._ensure_page()
        try:
            checkbox_element = await self._page.query_selector(selector)
            if not checkbox_element:
                raise ValueError(f"Checkbox not found with selector: {selector}")

            # 检查当前状态
            is_checked = await checkbox_element.is_checked()

            # 如果状态不匹配，点击切换
            if is_checked != checked:
                await checkbox_element.click()

            logger.info(f"Set checkbox {selector} to {checked}")
        except Exception as e:
            logger.error(f"Checkbox operation failed: {e}")
            await self._restart()
            await self._ensure_page()
            checkbox_element = await self._page.query_selector(selector)
            if checkbox_element:
                is_checked = await checkbox_element.is_checked()
                if is_checked != checked:
                    await checkbox_element.click()

    async def hover(self, x: int, y: int, hold_seconds: float = 0.0) -> None:
        """
        悬停在指定位置

        Args:
            x: X 坐标
            y: Y 坐标
            hold_seconds: 悬停持续时间（秒）
        """
        await self._ensure_page()
        try:
            await self._page.mouse.move(x, y)
            if hold_seconds > 0:
                await asyncio.sleep(hold_seconds)
            logger.info(f"Hovered at ({x}, {y}) for {hold_seconds}s")
        except Exception as e:
            logger.error(f"Hover failed: {e}")
            await self._restart()
            await self._ensure_page()
            await self._page.mouse.move(x, y)
            if hold_seconds > 0:
                await asyncio.sleep(hold_seconds)

    async def press_keys(self, keys: list[str], hold: bool = False) -> None:
        """
        按下键盘按键（支持组合键）

        Args:
            keys: 按键列表，例如 ["Control", "c"] 表示 Ctrl+C
            hold: 是否按住不放
        """
        await self._ensure_page()
        try:
            if len(keys) == 1:
                # 单个按键
                if hold:
                    await self._page.keyboard.down(keys[0])
                else:
                    await self._page.keyboard.press(keys[0])
            else:
                # 组合键：按住前面的键，按下最后一个键
                for key in keys[:-1]:
                    await self._page.keyboard.down(key)

                await self._page.keyboard.press(keys[-1])

                # 释放前面的键
                for key in reversed(keys[:-1]):
                    await self._page.keyboard.up(key)

            logger.info(f"Pressed keys: {keys}")
        except Exception as e:
            logger.error(f"Press keys failed: {e}")
            await self._restart()
            await self._ensure_page()
            # 重试逻辑
            if len(keys) == 1:
                await self._page.keyboard.press(keys[0])
            else:
                for key in keys[:-1]:
                    await self._page.keyboard.down(key)
                await self._page.keyboard.press(keys[-1])
                for key in reversed(keys[:-1]):
                    await self._page.keyboard.up(key)

    async def move_mouse(self, x: int, y: int) -> None:
        """
        移动鼠标到指定位置

        Args:
            x: X 坐标
            y: Y 坐标
        """
        await self._ensure_page()
        try:
            await self._page.mouse.move(x, y)
            logger.info(f"Moved mouse to ({x}, {y})")
        except Exception as e:
            logger.error(f"Move mouse failed: {e}")
            await self._restart()
            await self._ensure_page()
            await self._page.mouse.move(x, y)

    async def drag(self, start_x: int, start_y: int, end_x: int, end_y: int) -> None:
        """
        拖拽操作

        Args:
            start_x: 起始 X 坐标
            start_y: 起始 Y 坐标
            end_x: 结束 X 坐标
            end_y: 结束 Y 坐标
        """
        await self._ensure_page()
        try:
            # 移动到起始位置
            await self._page.mouse.move(start_x, start_y)
            # 按下鼠标
            await self._page.mouse.down()
            # 移动到结束位置
            await self._page.mouse.move(end_x, end_y)
            # 释放鼠标
            await self._page.mouse.up()
            logger.info(f"Dragged from ({start_x}, {start_y}) to ({end_x}, {end_y})")
        except Exception as e:
            logger.error(f"Drag failed: {e}")
            await self._restart()
            await self._ensure_page()
            await self._page.mouse.move(start_x, start_y)
            await self._page.mouse.down()
            await self._page.mouse.move(end_x, end_y)
            await self._page.mouse.up()

    async def reload(self) -> None:
        """刷新当前页面"""
        await self._ensure_page()
        try:
            await self._page.reload(timeout=30000, wait_until="domcontentloaded")
            logger.info("Page reloaded")
        except Exception as e:
            logger.warning(f"Page reload timeout or error: {e}, continuing anyway")

    async def close_page(self) -> None:
        """关闭当前页面"""
        await self._ensure_page()
        try:
            await self._page.close()
            self._page = None
            logger.info("Page closed")
        except Exception as e:
            logger.error(f"Close page failed: {e}")

    def get_viewport_size(self) -> Tuple[int, int]:
        """
        获取视口大小

        Returns:
            (width, height) 元组
        """
        return (self._viewport_width, self._viewport_height)

    # ========================================================================
    # DOM 元素定位方法（基于 Skyvern）
    # ========================================================================

    async def mark_page_elements(self) -> dict:
        """
        标记页面上的所有可交互元素

        Returns:
            {
                'elements': [...],  # 元素列表
                'count': int,       # 元素数量
                'viewport': {...}   # 视口信息
            }
        """
        await self._ensure_page()
        return await self.dom_service.mark_page_elements(self._page)

    async def click_element_by_id(self, element_id: str) -> bool:
        """
        通过 unique_id 点击元素

        Args:
            element_id: 元素的 unique_id

        Returns:
            是否成功点击
        """
        await self._ensure_page()
        return await self.dom_service.click_element_by_id(self._page, element_id)

    async def get_element_center(self, element_id: str) -> Optional[dict]:
        """
        获取元素的中心坐标

        Args:
            element_id: 元素的 unique_id

        Returns:
            {'x': int, 'y': int} 或 None
        """
        await self._ensure_page()
        return await self.dom_service.get_element_center(self._page, element_id)

    # ========================================================================
    # 翻页和滚动方法（参考 Skyvern）
    # ========================================================================

    async def is_page_scrollable(self) -> bool:
        """
        检查页面是否可滚动

        Returns:
            是否可滚动
        """
        await self._ensure_page()
        try:
            # 先注入 JavaScript
            await self._page.evaluate(self.dom_service.dom_marker_js)
            result = await self._page.evaluate("() => isWindowScrollable()")
            return bool(result)
        except Exception as e:
            logger.warning(f"Failed to check if page is scrollable: {e}")
            return False

    async def get_scroll_position(self) -> dict:
        """
        获取当前滚动位置和页面尺寸

        Returns:
            {
                'scrollWidth': int,
                'scrollHeight': int,
                'scrollX': int,
                'scrollY': int
            }
        """
        await self._ensure_page()
        try:
            # 先注入 JavaScript
            await self._page.evaluate(self.dom_service.dom_marker_js)
            result = await self._page.evaluate("() => getScrollWidthAndHeight()")
            return result
        except Exception as e:
            logger.warning(f"Failed to get scroll position: {e}")
            return {'scrollWidth': 0, 'scrollHeight': 0, 'scrollX': 0, 'scrollY': 0}

    async def scroll_to_top(self) -> float:
        """
        滚动到页面顶部

        Returns:
            当前滚动位置（应该是 0）
        """
        await self._ensure_page()
        try:
            # 先注入 JavaScript
            await self._page.evaluate(self.dom_service.dom_marker_js)
            await self._page.evaluate("() => safeScrollToTop()")
            logger.info(f"Scrolled to top")
            return 0.0
        except Exception as e:
            logger.error(f"Failed to scroll to top: {e}")
            return 0.0

    async def scroll_to_next_page(self, need_overlap: bool = True) -> float:
        """
        滚动到下一页（带重叠）

        Args:
            need_overlap: 是否需要 200px 重叠（默认 True）

        Returns:
            当前滚动位置
        """
        await self._ensure_page()

        last_error: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                # 先注入 JavaScript
                await self._page.evaluate(self.dom_service.dom_marker_js)
                scroll_y = await self._page.evaluate(
                    "(needOverlap) => scrollToNextPage(needOverlap)",
                    need_overlap
                )
                logger.info(f"Scrolled to next page, current position: {scroll_y}")
                return float(scroll_y)
            except Exception as e:
                last_error = e
                err_text = str(e)
                logger.error(f"Failed to scroll to next page (attempt {attempt}/2): {e}")

                # 页面正在导航/上下文销毁时，等待加载稳定后重试一次
                if attempt == 1 and (
                    "Execution context was destroyed" in err_text
                    or "Target page, context or browser has been closed" in err_text
                ):
                    try:
                        await self.wait_for_load(timeout_ms=10000)
                        await self.wait_for_stable(600)
                        await self._ensure_page()
                        continue
                    except Exception as recover_error:
                        logger.warning(f"Failed to recover page state before scroll retry: {recover_error}")
                break

        logger.error(f"Failed to scroll to next page after retries: {last_error}")
        return 0.0

    async def is_at_page_bottom(self, threshold: int = 25) -> bool:
        """
        检测是否到达页面底部

        Args:
            threshold: 阈值（默认 25px）

        Returns:
            是否到达底部
        """
        await self._ensure_page()
        try:
            result = await self._page.evaluate(
                "(threshold) => isAtPageBottom(threshold)",
                threshold
            )
            return bool(result)
        except Exception as e:
            logger.warning(f"Failed to check if at page bottom: {e}")
            return False

    # ========================================================================
    # Cookie 管理方法（反反爬虫）
    # ========================================================================

    async def save_cookies(self, file_path: str) -> None:
        """
        保存当前浏览器的 Cookies 到文件

        Args:
            file_path: Cookie 文件路径（JSON 格式）
        """
        await self._ensure_page()
        import json
        cookies = await self._context.cookies()
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        logger.info(f"Cookies saved to {file_path}")

    async def load_cookies(self, file_path: str) -> None:
        """
        从文件加载 Cookies 到浏览器

        Args:
            file_path: Cookie 文件路径（JSON 格式）
        """
        await self._ensure_page()
        import json
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            await self._context.add_cookies(cookies)
            logger.info(f"Cookies loaded from {file_path}")
        except FileNotFoundError:
            logger.warning(f"Cookie file not found: {file_path}")
        except Exception as e:
            logger.error(f"Failed to load cookies: {e}")

    async def wait_for_manual_login(self, timeout_seconds: int = 300) -> bool:
        """
        等待用户手动登录（用于需要登录的网站）

        Args:
            timeout_seconds: 等待超时时间（秒）

        Returns:
            是否成功登录（通过 URL 变化判断）
        """
        await self._ensure_page()
        import asyncio

        logger.info(f"Waiting for manual login (timeout: {timeout_seconds}s)...")
        logger.info("Please login in the browser window, then the script will continue automatically.")

        start_url = self._page.url
        start_time = asyncio.get_event_loop().time()

        while True:
            await asyncio.sleep(2)
            current_url = self._page.url

            # 检查 URL 是否变化（通常登录后会跳转）
            if current_url != start_url and "login" not in current_url.lower():
                logger.info(f"Login detected! URL changed to: {current_url}")
                return True

            # 检查超时
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout_seconds:
                logger.warning(f"Manual login timeout after {timeout_seconds}s")
                return False
