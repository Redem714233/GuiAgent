from __future__ import annotations

import asyncio
import os
import logging
from typing import Any, Dict, List, Optional

from backend.executor import Executor
from backend.omniparser_service import OmniParserService
from backend.planner import Planner
from backend.output_store import OutputStore
from backend.schemas import ParseRequest, StepRequest
from backend.storage import ensure_dir, timestamp_name
from backend.scrolling_screenshot import take_scrolling_screenshot

logger = logging.getLogger(__name__)


class ExtractionEngine:
    """
    自动化数据提取引擎

    实现端到端的数据提取流程：
    1. 解析任务规格
    2. 导航到目标网站
    3. 循环提取列表数据
    4. 可选：进入详情页提取
    5. 保存到Excel
    """

    def __init__(
        self,
        executor: Executor,
        parser_service: OmniParserService,
        planner: Planner,
        output_store: OutputStore,
        data_dir: str,
    ) -> None:
        self.executor = executor
        self.parser_service = parser_service
        self.planner = planner
        self.output_store = output_store
        self.data_dir = data_dir
        ensure_dir(self.data_dir)

        # 存储当前提取进度，供外部查询
        self.current_progress: List[Dict[str, Any]] = []
        self.is_extracting: bool = False

    async def run_extraction(
        self,
        task: str,
        max_items: int = 10,
        strategy: Optional[Dict[str, Any]] = None,
        use_omniparser: bool = False,
        use_reflection: bool = True,  # 新增：默认启用反思机制
    ) -> Dict[str, Any]:
        """
        运行完整的数据提取流程

        Args:
            task: 用户任务描述
            max_items: 最大提取条目数
            strategy: 提取策略配置
                - list_only: 仅提取列表，不进入详情页
                - open_detail: 'same_tab' | 'new_tab'
                - scroll_strategy: 'auto' | 'manual'
                - max_scrolls: 最大滚动次数

        Returns:
            {
                'status': 'success' | 'partial' | 'failed',
                'items_extracted': int,
                'file_path': str,
                'progress': list[dict],
                'errors': list[str]
            }
        """
        strategy = strategy or {}
        list_only = strategy.get("list_only", False)
        max_scrolls = strategy.get("max_scrolls", 5)

        progress: List[Dict[str, Any]] = []
        errors: List[str] = []
        extracted_items: List[Dict[str, Any]] = []
        seen_urls: set[str] = set()  # URL 去重
        seen_titles: set[str] = set()  # Title 去重（新增）

        # 初始化实例进度状态
        self.current_progress = progress
        self.is_extracting = True

        try:
            # 阶段1: 解析任务规格
            progress.append({"stage": "parse_spec", "status": "running"})
            spec, spec_debug = self.planner.extract_task_spec(task)
            progress[-1]["status"] = "completed"
            progress[-1]["spec"] = spec

            target_count = min(spec.get("count", max_items), max_items)
            fields = spec.get("fields", ["title", "url", "content"])

            # 阶段2: 导航到目标网站
            progress.append({"stage": "navigate", "status": "running"})
            target_site = spec.get("target_site")
            if target_site:
                # 如果有明确的目标网站，直接导航
                if not target_site.startswith("http"):
                    target_site = f"https://{target_site}"
                await self.executor.goto(target_site)
                await self.executor.wait_for_load()
                await self.executor.wait_for_stable(2000)
            else:
                # 否则使用步骤规划导航
                steps = self.planner.plan_steps(task, max_steps=3)
                for step in steps[:2]:  # 最多执行前2步导航
                    await self._execute_step(step)
                    await asyncio.sleep(1)

            current_url = await self.executor.get_url()
            progress[-1]["status"] = "completed"
            progress[-1]["url"] = current_url

            # 检测任务是否要求先翻页再提取
            import re
            pagination_match = re.search(r'翻到第(\d+)页|翻页到第(\d+)页|go to page (\d+)|page (\d+)', task, re.IGNORECASE)
            target_page = None
            pre_paginated = False  # 标记是否已经预翻页
            if pagination_match:
                # 提取目标页码
                target_page = int([g for g in pagination_match.groups() if g][0])
                logger.info(f"Detected pagination request: go to page {target_page}")

            # 如果需要先翻页，执行预翻页
            if target_page and target_page > 1:
                progress.append({"stage": "pre_pagination", "status": "running", "target_page": target_page})
                logger.info(f"Pre-pagination: navigating to page {target_page} before extraction")

                # 翻页 (target_page - 1) 次
                for page_num in range(2, target_page + 1):
                    logger.info(f"Pre-pagination: clicking to page {page_num}")

                    # 标记元素
                    dom_result = await self.executor.mark_page_elements()
                    dom_elements = dom_result.get('elements', [])

                    # 截图
                    import base64
                    screenshot_path = f"data/pre_pagination_page_{page_num - 1}.png"
                    await self.executor.screenshot(screenshot_path)
                    with open(screenshot_path, "rb") as f:
                        image_base64 = base64.b64encode(f.read()).decode("ascii")

                    current_url = await self.executor.get_url()

                    # 简化方法：直接从 DOM 元素中查找 next 按钮
                    next_page_element_id = None
                    for elem in dom_elements:
                        text = elem.get('text', '').lower()
                        if 'next' in text or '下一页' in text or '›' in text:
                            next_page_element_id = elem.get('id')
                            logger.info(f"Found next button: {elem.get('id')} with text '{elem.get('text')}'")
                            break

                    if not next_page_element_id:
                        logger.error(f"Pre-pagination failed: no next button found on page {page_num - 1}")
                        errors.append(f"Pre-pagination failed: no next button found on page {page_num - 1}")
                        break

                    # 使用反思机制翻页
                    if use_reflection:
                        logger.info(f"Pre-pagination: using reflection to go to page {page_num}")
                        pagination_result = await self._click_next_page_with_reflection(
                            {"next_page_element_id": next_page_element_id},
                            dom_elements,
                            current_url,
                            max_retries=3
                        )
                        if not pagination_result["success"]:
                            logger.error(f"Pre-pagination failed: {pagination_result['reasoning']}")
                            errors.append(f"Pre-pagination failed: {pagination_result['reasoning']}")
                            break
                    else:
                        # 不使用反思机制
                        success = await self.executor.click_element_by_id(next_page_element_id)
                        if not success:
                            logger.error(f"Pre-pagination failed: could not click element {next_page_element_id}")
                            errors.append(f"Pre-pagination failed: could not click element {next_page_element_id}")
                            break
                        await asyncio.sleep(2)
                        await self.executor.wait_for_stable(2000)

                    logger.info(f"Pre-pagination: successfully navigated to page {page_num}")

                progress[-1]["status"] = "completed"
                current_url = await self.executor.get_url()
                progress[-1]["final_url"] = current_url
                logger.info(f"Pre-pagination completed. Now on page {target_page}, URL: {current_url}")
                pre_paginated = True  # 标记已经预翻页

            # 阶段3: 循环提取列表数据
            progress.append({"stage": "extract_list", "status": "running", "items": 0})
            scroll_count = 0
            consecutive_empty = 0  # 连续空提取次数

            while len(extracted_items) < target_count and scroll_count < max_scrolls:
                # 标记当前视口的元素
                current_url = await self.executor.get_url()
                logger.info(f"Extracting from URL: {current_url}")
                logger.info("Marking page elements")
                dom_result = await self.executor.mark_page_elements()
                dom_elements = dom_result.get('elements', [])
                logger.info(f"Marked {len(dom_elements)} elements")

                # 截图
                screenshot_path = os.path.join(self.data_dir, timestamp_name("screenshot"))
                await self.executor.screenshot(screenshot_path)
                import base64
                with open(screenshot_path, "rb") as f:
                    image_base64 = base64.b64encode(f.read()).decode("ascii")

                # 提取当前页面的列表项（传入DOM元素列表）
                list_data, list_debug = self.planner.extract_from_page(
                    task=task,
                    mode="list",
                    annotated_image_base64=image_base64,
                    current_url=current_url,
                    elements=dom_elements,
                )

                items = list_data.get("items", [])
                next_action = list_data.get("next", "stop")

                # 调试日志：显示 VLM 的决策
                logger.info(f"VLM extracted {len(items)} items from {current_url}, next_action={next_action}")
                if items:
                    logger.info(f"First item title: {items[0].get('title', 'N/A')}")
                if next_action == "next_page":
                    next_page_id = list_data.get("next_page_element_id")
                    logger.info(f"VLM suggested next_page with element_id={next_page_id}")

                # 调试：记录VLM返回的数据类型
                if items and not isinstance(items[0], dict):
                    errors.append(f"VLM returned invalid items format. Expected list of dicts, got list of {type(items[0]).__name__}. First item: {items[0]}")
                    errors.append(f"Full list_data: {list_data}")
                    items = []  # 清空以避免后续错误

                # 过滤已见过的URL和Title（去重）
                new_items = []
                for item in items:
                    # 验证item类型
                    if not isinstance(item, dict):
                        errors.append(f"Skipping non-dict item (type: {type(item).__name__}): {item}")
                        continue

                    # 如果有element_id，从DOM元素列表中提取href作为URL
                    if "element_id" in item and item["element_id"]:
                        element_id = item["element_id"]
                        # 验证element_id是字符串
                        if isinstance(element_id, str):
                            item["_saved_element_id"] = element_id

                            # 从DOM元素列表中查找对应的href
                            for elem in dom_elements:
                                if elem.get('id') == element_id:
                                    href = elem.get('attributes', {}).get('href')
                                    if href and not item.get("url"):
                                        # 如果item没有URL，使用从DOM提取的href
                                        item["url"] = href
                                        logger.info(f"Extracted href from element {element_id}: {href}")
                                    break
                        else:
                            errors.append(f"Warning: Invalid element_id format: {element_id}")

                    # 保存标题用于重新查找元素（当页面重新加载后）
                    if "title" in item and item["title"]:
                        item["_saved_title"] = item["title"]

                    item_url = item.get("url", "")
                    item_title = item.get("title", "")

                    # 标准化URL用于去重
                    normalized_url = item_url
                    if item_url:
                        # 补全相对URL
                        if item_url.startswith("/"):
                            from urllib.parse import urljoin
                            normalized_url = urljoin(current_url, item_url)
                        # 统一URL格式（去除尾部斜杠、转小写）
                        normalized_url = normalized_url.rstrip("/").lower()

                    # 标准化Title用于去重（去除空格、转小写）
                    normalized_title = item_title.strip().lower() if item_title else ""

                    # 去重逻辑：检查 URL 或 Title
                    is_duplicate = False
                    if normalized_url and normalized_url in seen_urls:
                        is_duplicate = True
                    elif normalized_title and normalized_title in seen_titles:
                        is_duplicate = True

                    if not is_duplicate:
                        # 添加到已见集合
                        if normalized_url:
                            seen_urls.add(normalized_url)
                        if normalized_title:
                            seen_titles.add(normalized_title)

                        # 补全item中的URL为完整URL
                        if item_url and item_url.startswith("/"):
                            from urllib.parse import urljoin
                            item["url"] = urljoin(current_url, item_url)
                        new_items.append(item)
                    # 如果既没有URL也没有Title，也添加（可能是特殊项目）
                    elif not item_url and not item_title:
                        new_items.append(item)

                if new_items:
                    extracted_items.extend(new_items[:target_count - len(extracted_items)])
                    progress[-1]["items"] = len(extracted_items)
                    consecutive_empty = 0
                else:
                    consecutive_empty += 1

                # 检查连续空提取
                if consecutive_empty >= 2:
                    # 连续2次空提取，可能已经到底
                    break

                # 根据VLM建议决定下一步动作（先翻页，再检查是否够了）
                # 如果已经预翻页到目标页，不要再翻页
                if pre_paginated and next_action == "next_page":
                    logger.info("Already pre-paginated to target page, ignoring VLM's next_page suggestion")
                    next_action = "stop"

                if next_action == "scroll":
                    # 使用智能滚动翻页（参考 Skyvern）
                    scroll_success = await self._smart_scroll_pagination()
                    if scroll_success:
                        await self.executor.wait_for_stable(1500)
                        scroll_count += 1
                    else:
                        # 已到达页面底部
                        logger.info("Reached page bottom, stopping scroll")
                        break
                elif next_action == "next_page":
                    # 识别并点击"下一页"按钮
                    if use_reflection:
                        # 使用反思机制翻页（带验证和重试）
                        logger.info("Using reflection mechanism for pagination")
                        pagination_result = await self._click_next_page_with_reflection(
                            list_data, dom_elements, current_url, max_retries=3
                        )
                        if pagination_result["success"]:
                            scroll_count += 1
                            logger.info(f"Successfully paginated with reflection: {pagination_result['reasoning']}")
                        else:
                            logger.error(f"Failed to paginate after {pagination_result['retry_count']} retries: {pagination_result['reasoning']}")
                            # 翻页失败，停止提��
                            break
                    else:
                        # 原有逻辑（不验证）
                        next_page_clicked = await self._click_next_page_button(
                            list_data, dom_elements, current_url
                        )
                        if next_page_clicked:
                            # 等待新页面加载
                            await self.executor.wait_for_stable(2000)
                            scroll_count += 1
                            logger.info("Successfully clicked next page button")
                        else:
                            # 如果无法点击下一页按钮，尝试智能滚动
                            logger.warning("Failed to click next page button, falling back to smart scroll")
                            scroll_success = await self._smart_scroll_pagination()
                            if scroll_success:
                                await self.executor.wait_for_stable(1500)
                                scroll_count += 1
                            else:
                                # 已到达页面底部
                                break
                else:
                    # stop
                    break

                # 翻页后，检查是否已经提取够了
                if len(extracted_items) >= target_count:
                    logger.info(f"Reached target count ({target_count}), stopping extraction")
                    break

            progress[-1]["status"] = "completed"
            progress[-1]["items"] = len(extracted_items)

            # 阶段4: 可选 - 提取详情页数据
            if not list_only and extracted_items:
                progress.append({"stage": "extract_details", "status": "running", "processed": 0})

                # 保存列表页的 URL（在进入详情页之前）
                list_page_url = await self.executor.get_url()

                for idx, item in enumerate(extracted_items):
                    # 验证item类型
                    if not isinstance(item, dict):
                        errors.append(f"Item {idx} is not a dict (type: {type(item).__name__}): {item}")
                        continue

                    # 检查是否有URL或element_id
                    has_url = "url" in item and item["url"]
                    has_element_id = "_saved_element_id" in item and item["_saved_element_id"]

                    if not has_url and not has_element_id:
                        # 既没有URL也没有element_id，跳过
                        continue

                    try:
                        # 为每个详情页提取设置30秒总超时
                        async def extract_detail():
                            # 导航到详情页：优先使用URL，否则使用点击
                            if has_url:
                                detail_url = item["url"]
                                if not detail_url.startswith("http"):
                                    # 相对URL，补全
                                    from urllib.parse import urljoin
                                    detail_url = urljoin(list_page_url, detail_url)
                                progress[-1]["current_action"] = f"Navigating to {detail_url}"
                                await self.executor.goto(detail_url)
                                # 固定延迟
                                await asyncio.sleep(2)
                            elif has_element_id:
                                # 使用DOM element_id点击
                                # 参考Skyvern：在当前列表页重新标记并查找元素
                                saved_title = item.get("_saved_title", "")
                                saved_element_id = item["_saved_element_id"]

                                logger.info(f"Attempting to click element for item: title='{saved_title}', saved_id={saved_element_id}")
                                progress[-1]["current_action"] = f"Re-marking page to find element"

                                # 重新标记当前页面（列表页）
                                dom_elements_result = await self.executor.mark_page_elements()
                                dom_elements = dom_elements_result.get('elements', [])
                                logger.info(f"Re-marked page, found {len(dom_elements)} interactive elements")

                                # 改进的元素匹配逻辑（参考Skyvern的哈希匹配思想）
                                element_id = None
                                best_match_score = 0

                                if saved_title:
                                    # 标准化saved_title用于匹配
                                    saved_title_lower = saved_title.lower().strip()
                                    saved_title_words = set(saved_title_lower.split())

                                    for elem in dom_elements:
                                        elem_text = elem.get('text', '').strip()
                                        elem_text_lower = elem_text.lower()

                                        # 计算匹配分数
                                        score = 0

                                        # 1. 完全匹配（最高优先级）
                                        if saved_title_lower == elem_text_lower:
                                            score = 100
                                        # 2. 包含匹配
                                        elif saved_title_lower in elem_text_lower:
                                            score = 80
                                        elif elem_text_lower in saved_title_lower:
                                            score = 70
                                        # 3. 词语重叠匹配
                                        else:
                                            elem_words = set(elem_text_lower.split())
                                            common_words = saved_title_words & elem_words
                                            if common_words and len(common_words) >= min(3, len(saved_title_words)):
                                                score = 50 + len(common_words) * 5

                                        # 4. 优先选择<a>标签
                                        if score > 0 and elem.get('tag') == 'a':
                                            score += 10

                                        # 更新最佳匹配
                                        if score > best_match_score:
                                            best_match_score = score
                                            element_id = elem.get('id')
                                            logger.info(f"Found better match: id={element_id}, score={score}, text='{elem_text[:50]}'")

                                if element_id and best_match_score >= 50:
                                    logger.info(f"Using matched element {element_id} with score {best_match_score}")
                                else:
                                    # 匹配失败，跳过这个item
                                    error_msg = f"Could not find reliable element match for title '{saved_title}' (best score: {best_match_score})"
                                    logger.error(error_msg)
                                    raise Exception(error_msg)

                                progress[-1]["current_action"] = f"Clicking element {element_id}"

                                # 点击并等待导航
                                try:
                                    # 使用DOM方法点击元素
                                    success = await self.executor.click_element_by_id(element_id)
                                    if not success:
                                        raise Exception(f"Failed to click element {element_id}")
                                    # 等待页面稳定
                                    await asyncio.sleep(3)
                                except Exception as e:
                                    logger.error(f"Click navigation failed: {e}")
                                    raise  # 重新抛出异常，让外层处理

                            # 参考 AutoGLM：固定延迟而不是等待页面加载
                            progress[-1]["current_action"] = "Waiting after navigation"
                            logger.info(f"Waiting 2 seconds after navigation (item {idx})")
                            await asyncio.sleep(2)  # 固定2秒延迟
                            logger.info(f"Wait completed (item {idx})")

                            # 检查当前 URL，如果是 about:blank 说明导航失败
                            current_check_url = await self.executor.get_url()
                            if current_check_url == "about:blank" or not current_check_url:
                                raise Exception(f"Navigation failed: page is blank (url: {current_check_url})")

                            # 截图并提取详情
                            progress[-1]["current_action"] = "Taking screenshot"
                            logger.info(f"Starting screenshot (item {idx})")

                            if use_omniparser:
                                screenshot_path, parse_resp = await self._capture_and_parse()
                                detail_image_base64 = parse_resp.annotated_image_base64
                            else:
                                screenshot_path = os.path.join(self.data_dir, timestamp_name("screenshot"))
                                await self.executor.screenshot(screenshot_path)
                                import base64
                                with open(screenshot_path, "rb") as f:
                                    detail_image_base64 = base64.b64encode(f.read()).decode("ascii")

                            logger.info(f"Screenshot completed (item {idx})")

                            detail_url_actual = await self.executor.get_url()

                            progress[-1]["current_action"] = "Extracting detail data with VLM"
                            detail_data, detail_debug = self.planner.extract_from_page(
                                task=task,
                                mode="detail",
                                annotated_image_base64=detail_image_base64,
                                current_url=detail_url_actual,
                            )

                            # 合并详情数据到列表项
                            if "data" in detail_data:
                                item.update(detail_data["data"])

                            progress[-1]["processed"] = idx + 1

                            # 返回列表页（如果需要继续提取）
                            if idx < len(extracted_items) - 1:
                                if has_url:
                                    # 使用URL导航，返回到保存的列表页 URL
                                    progress[-1]["current_action"] = "Returning to list page"
                                    await self.executor.goto(list_page_url)
                                else:
                                    # 使用点击进入，需要后退
                                    progress[-1]["current_action"] = "Going back to list page"
                                    await self.executor.go_back()
                                # 固定延迟
                                progress[-1]["current_action"] = "Waiting after return"
                                await asyncio.sleep(2)

                        # 使用asyncio.wait_for添加30秒总超时
                        await asyncio.wait_for(extract_detail(), timeout=30.0)

                    except asyncio.TimeoutError:
                        progress[-1]["current_action"] = f"Timeout at item {idx}"
                        errors.append(f"Detail extraction timeout for item {idx} (url: {item.get('url', 'N/A')}, element_id: {item.get('_saved_element_id', 'N/A')})")
                        # 超时后尝试返回列表页
                        try:
                            await self.executor.goto(list_page_url)
                            await asyncio.sleep(2)
                        except:
                            pass
                        continue
                    except Exception as exc:
                        progress[-1]["current_action"] = f"Error at item {idx}: {type(exc).__name__}"
                        errors.append(f"Detail extraction failed for item {idx} (url: {item.get('url', 'N/A')}, element_id: {item.get('_saved_element_id', 'N/A')}): {type(exc).__name__}: {exc}")
                        # 出错后尝试返回列表页
                        try:
                            await self.executor.goto(list_page_url)
                            await asyncio.sleep(2)
                        except:
                            pass
                        continue

                progress[-1]["status"] = "completed"

            # 阶段5: 保存到Excel
            progress.append({"stage": "save", "status": "running"})

            # 重置输出存储
            self.output_store.reset()

            # 清理并添加所有行
            for item in extracted_items:
                # 创建清理后的副本，移除内部字段
                cleaned_item = {k: v for k, v in item.items()
                               if not k.startswith('_') and k not in ['click_point', 'element_id']}
                self.output_store.append_row(cleaned_item)

            # 保存Excel
            if extracted_items:
                file_path = self.output_store.save_excel()
                progress[-1]["status"] = "completed"
                progress[-1]["file_path"] = file_path
            else:
                progress[-1]["status"] = "skipped"
                progress[-1]["reason"] = "no_items"
                file_path = None

            # 返回结果（也清理返回的数据）
            cleaned_items = [{k: v for k, v in item.items()
                            if not k.startswith('_') and k not in ['click_point', 'element_id']}
                           for item in extracted_items]

            status = "success" if len(extracted_items) >= target_count else "partial"
            if not extracted_items:
                status = "failed"

            return {
                "status": status,
                "items_extracted": len(extracted_items),
                "target_count": target_count,
                "file_path": os.path.basename(file_path) if file_path else None,
                "progress": progress,
                "errors": errors,
                "items": cleaned_items,  # 返回清理后的数据供前端预览
            }

        except Exception as exc:
            errors.append(f"Extraction failed: {exc}")
            return {
                "status": "failed",
                "items_extracted": len(extracted_items),
                "target_count": max_items,
                "file_path": None,
                "progress": progress,
                "errors": errors,
                "items": extracted_items,
            }
        finally:
            # 清理提取状态
            self.is_extracting = False

    async def _capture_and_parse(self):
        """截图并解析元素（简化版，移除额外的超时包装）"""
        screenshot_path = os.path.join(self.data_dir, timestamp_name("screenshot"))

        # screenshot 方法本身已经有超时保护和重试机制
        logger.info(f"Starting screenshot to {screenshot_path}")
        await self.executor.screenshot(screenshot_path)
        logger.info(f"Screenshot completed")

        logger.info("Reading screenshot file")
        import base64
        with open(screenshot_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")

        logger.info("Parsing screenshot with OmniParser")
        parse_resp = self.parser_service.parse(
            ParseRequest(image_base64=image_b64, use_paddleocr=True)
        )
        logger.info("Screenshot parsed successfully")

        return screenshot_path, parse_resp

    async def _execute_step(self, step_task: str) -> None:
        """执行单个步骤（用于导航）"""
        screenshot_path, parse_resp = await self._capture_and_parse()
        current_url = await self.executor.get_url()

        # 使用planner决策
        from backend.schemas import PlanRequest
        plan_resp = self.planner.plan(
            PlanRequest(
                task=step_task,
                elements=parse_resp.elements,
                image_size=parse_resp.image_size,
                annotated_image_base64=parse_resp.annotated_image_base64,
            )
        )

        # 执行动作
        if plan_resp.action_tool == "goto" and plan_resp.action_url:
            await self.executor.goto(plan_resp.action_url)
            await self.executor.wait_for_load()
        elif plan_resp.action_tool == "click":
            if plan_resp.target_point:
                await self.executor.click_point(plan_resp.target_point)
            elif plan_resp.target_id is not None:
                elem = next((e for e in parse_resp.elements if e.id == plan_resp.target_id), None)
                if elem:
                    await self.executor.click_center(elem.center)
            await self.executor.wait_for_load()
        elif plan_resp.action_tool == "type":
            if plan_resp.target_point:
                await self.executor.click_point(plan_resp.target_point)
            elif plan_resp.target_id is not None:
                elem = next((e for e in parse_resp.elements if e.id == plan_resp.target_id), None)
                if elem:
                    await self.executor.click_center(elem.center)

            if plan_resp.action_text:
                await self.executor.type_text(plan_resp.action_text)
            if plan_resp.action_key:
                await self.executor.press(plan_resp.action_key)
            await self.executor.wait_for_load()

    async def _click_next_page_with_reflection(
        self,
        list_data: Dict[str, Any],
        dom_elements: List[Dict[str, Any]],
        current_url: str,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        使用反思机制点击"下一页"按钮（带验证和重试）

        Args:
            list_data: VLM返回的列表数据，可能包含next_page_element_id
            dom_elements: 当前页面的DOM元素列表
            current_url: 当前页面URL
            max_retries: 最大重试次数

        Returns:
            {
                "success": bool,
                "retry_count": int,
                "reasoning": str,
                "before_url": str,
                "after_url": str
            }
        """
        import base64

        retry_count = 0

        while retry_count < max_retries:
            logger.info(f"Pagination attempt {retry_count + 1}/{max_retries}")

            # 1. 捕获执行前状态
            before_url = await self.executor.get_url()

            # 截图（前）
            before_screenshot_path = f"data/pagination_before_{retry_count}.png"
            await self.executor.screenshot(before_screenshot_path)
            with open(before_screenshot_path, "rb") as f:
                before_image_base64 = base64.b64encode(f.read()).decode("ascii")

            # 获取元素列表（前）
            dom_result_before = await self.executor.mark_page_elements()
            elements_before = dom_result_before.get('elements', [])

            # 2. 执行翻页动作
            try:
                click_success = await self._click_next_page_button(
                    list_data, dom_elements, current_url
                )
                if not click_success:
                    logger.warning(f"Failed to click next page button on attempt {retry_count + 1}")
                    retry_count += 1
                    continue
            except Exception as e:
                logger.error(f"Error clicking next page button: {e}")
                retry_count += 1
                continue

            # 3. 等待页面稳定
            await asyncio.sleep(2)
            await self.executor.wait_for_stable(2000)

            # 4. 捕获执行后状态
            after_url = await self.executor.get_url()

            # 截图（后）
            after_screenshot_path = f"data/pagination_after_{retry_count}.png"
            await self.executor.screenshot(after_screenshot_path)
            with open(after_screenshot_path, "rb") as f:
                after_image_base64 = base64.b64encode(f.read()).decode("ascii")

            # 获取元素列表（后）
            dom_result_after = await self.executor.mark_page_elements()
            elements_after = dom_result_after.get('elements', [])

            # 5. 验证翻页是否成功
            # 创建 VLMService 实例
            if not hasattr(self, '_vlm_service'):
                from backend.vlm_service import VLMService
                self._vlm_service = VLMService()

            verification, verification_raw = self._vlm_service.verify_step_success(
                task="翻页到下一页",
                step_description="click next page button",
                action_taken="click pagination button",
                before_url=before_url,
                after_url=after_url,
                before_image_base64=before_image_base64,
                after_image_base64=after_image_base64,
                elements_before=elements_before,
                elements_after=elements_after,
            )

            logger.info(f"Pagination verification: {verification}")

            # 6. 决策
            if verification.get("success", False):
                # 成功
                return {
                    "success": True,
                    "retry_count": retry_count,
                    "reasoning": verification.get("reasoning", "Pagination successful"),
                    "before_url": before_url,
                    "after_url": after_url,
                    "verification": verification
                }
            elif verification.get("should_retry", True) and retry_count < max_retries - 1:
                # 失败但可重试
                logger.warning(f"Pagination failed but retrying: {verification.get('reasoning', '')}")
                retry_count += 1
                continue
            else:
                # 失败且不可重试
                logger.error(f"Pagination failed permanently: {verification.get('reasoning', '')}")
                return {
                    "success": False,
                    "retry_count": retry_count,
                    "reasoning": verification.get("reasoning", "Pagination failed"),
                    "before_url": before_url,
                    "after_url": after_url,
                    "verification": verification
                }

        # 达到最大重试次数
        return {
            "success": False,
            "retry_count": retry_count,
            "reasoning": f"Max retries ({max_retries}) reached",
            "before_url": before_url,
            "after_url": after_url,
            "verification": {"reasoning": "Max retries reached"}
        }

    async def _click_next_page_button(
        self,
        list_data: Dict[str, Any],
        dom_elements: List[Dict[str, Any]],
        current_url: str,
    ) -> bool:
        """
        识别并点击"下一页"按钮

        Args:
            list_data: VLM返回的列表数据，可能包含next_page_element_id
            dom_elements: 当前页面的DOM元素列表
            current_url: 当前页面URL

        Returns:
            是否成功点击下一页按钮
        """
        try:
            # 方法1: 检查VLM是否返回了next_page_element_id
            next_page_element_id = list_data.get("next_page_element_id")

            if next_page_element_id:
                logger.info(f"VLM provided next_page_element_id: {next_page_element_id}")
                # 使用element_id点击
                success = await self.executor.click_element_by_id(next_page_element_id)
                if success:
                    return True
                logger.warning(f"Failed to click element {next_page_element_id}")

            # 方法2: 滚动到底部，重新标记元素，然后在DOM中搜索翻页按钮
            logger.info("Scrolling to bottom to find pagination button")
            await self.executor.scroll_to_bottom()
            await self.executor.wait_for_stable(1000)

            # 重新标记底部视口的元素
            dom_result = await self.executor.mark_page_elements()
            bottom_elements = dom_result.get('elements', [])
            logger.info(f"Marked {len(bottom_elements)} elements at bottom")

            # 合并顶部和底部的元素
            all_elements = dom_elements + bottom_elements

            next_page_keywords = [
                "下一页", "next page", "next", "›", "»", "→",
                "下页", "nextpage", "page-next", "pagination-next",
                "翻页", "more", "load more"
            ]

            # 收集所有可能的下一页按钮
            potential_buttons = []
            for elem in all_elements:
                elem_text = (elem.get("text", "") or "").lower().strip()
                elem_id = elem.get("id", "")
                elem_class = (elem.get("attributes", {}).get("class", "") or "").lower()
                elem_aria_label = (elem.get("attributes", {}).get("aria-label", "") or "").lower()
                elem_href = (elem.get("attributes", {}).get("href", "") or "").lower()

                # 检查文本、class、aria-label是否包含下一页关键词
                for keyword in next_page_keywords:
                    keyword_lower = keyword.lower()
                    if (keyword_lower in elem_text or
                        keyword_lower in elem_class or
                        keyword_lower in elem_aria_label):

                        # 计算优先级分数
                        score = 0
                        # 优先选择 href 包含 "page-" 的链接（真正的翻页）
                        if "page-" in elem_href:
                            score += 100
                        # 优先选择 class 包含 "pag" 的元素（pagination）
                        if "pag" in elem_class:
                            score += 50
                        # 优先选择文本正好是 "next" 或 "下一页" 的元素
                        if elem_text in ["next", "下一页", "›", "»", "→"]:
                            score += 30
                        # 避免选择包含其他文字的链接（可能是分类链接）
                        if len(elem_text) > 10:
                            score -= 20

                        element_id = elem.get("id")
                        if element_id:
                            potential_buttons.append({
                                "id": element_id,
                                "text": elem_text,
                                "href": elem_href,
                                "score": score
                            })
                        break

            # 按分数排序，优先点击分数最高的
            potential_buttons.sort(key=lambda x: x["score"], reverse=True)

            if potential_buttons:
                logger.info(f"Found {len(potential_buttons)} potential next page buttons")
                for button in potential_buttons:
                    logger.info(f"Trying next page button: {button['id']} (text: {button['text']}, href: {button['href']}, score: {button['score']})")
                    success = await self.executor.click_element_by_id(button["id"])
                    if success:
                        return True
            else:
                logger.warning("No potential next page buttons found in DOM")

            # 方法3: 使用VLM重新分析页面，专门寻找下一页按钮
            logger.info("Attempting to find next page button using VLM")
            next_button_id = await self._find_next_page_button_with_vlm(all_elements, current_url)
            if next_button_id:
                logger.info(f"VLM found next page button: {next_button_id}")
                success = await self.executor.click_element_by_id(next_button_id)
                if success:
                    return True

            logger.warning("Could not find or click next page button")
            return False

        except Exception as e:
            logger.error(f"Error clicking next page button: {e}")
            return False

    async def _find_next_page_button_with_vlm(
        self,
        dom_elements: List[Dict[str, Any]],
        current_url: str,
    ) -> Optional[str]:
        """
        使用VLM专门识别"下一页"按钮

        Args:
            dom_elements: 当前页面的DOM元素列表
            current_url: 当前页面URL

        Returns:
            下一页按钮的element_id，如果未找到则返回None
        """
        try:
            # 重新截图
            screenshot_path = os.path.join(self.data_dir, timestamp_name("screenshot"))
            await self.executor.screenshot(screenshot_path)
            import base64
            with open(screenshot_path, "rb") as f:
                image_base64 = base64.b64encode(f.read()).decode("ascii")

            # 使用专门的 find_next_page_button 方法
            result, _ = self.planner.vlm_service.find_next_page_button(
                annotated_image_base64=image_base64,
                elements=dom_elements,
            )

            # 检查返回结果
            if isinstance(result, dict):
                next_page_element_id = result.get("next_page_element_id")
                confidence = result.get("confidence", 0.0)

                if next_page_element_id and confidence > 0.5:
                    logger.info(f"VLM found next page button: {next_page_element_id} (confidence: {confidence})")
                    return next_page_element_id

            return None

        except Exception as e:
            logger.error(f"Error finding next page button with VLM: {e}")
            return None

    async def _smart_scroll_pagination(self) -> bool:
        """
        智能滚动翻页（参考 Skyvern）

        使用滚动位置检测来判断是否到达页面底部，
        并使用带重叠的滚动来确保内容连续性。

        Returns:
            是否成功滚动（False 表示已到达底部）
        """
        try:
            # 检查页面是否可滚动
            is_scrollable = await self.executor.is_page_scrollable()
            if not is_scrollable:
                logger.info("Page is not scrollable")
                return False

            # 获取滚动前的位置
            scroll_info_before = await self.executor.get_scroll_position()
            scroll_y_before = scroll_info_before.get('scrollY', 0)

            # 检查是否已经到达底部
            is_at_bottom = await self.executor.is_at_page_bottom(threshold=25)
            if is_at_bottom:
                logger.info("Already at page bottom")
                return False

            # 滚动到下一页（带 200px 重叠）
            scroll_y_after = await self.executor.scroll_to_next_page(need_overlap=True)

            # 检查滚动是否有效（滚动距离 > 25px）
            scroll_distance = abs(scroll_y_after - scroll_y_before)
            if scroll_distance <= 25:
                logger.info(f"Scroll distance too small ({scroll_distance}px), reached bottom")
                return False

            logger.info(f"Scrolled from {scroll_y_before}px to {scroll_y_after}px (distance: {scroll_distance}px)")
            return True

        except Exception as e:
            logger.error(f"Error in smart scroll pagination: {e}")
            # 降级到简单滚动
            try:
                await self.executor.scroll_by(600)
                return True
            except Exception:
                return False
