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

            # 阶段3: 循环提取列表数据
            progress.append({"stage": "extract_list", "status": "running", "items": 0})
            scroll_count = 0
            consecutive_empty = 0  # 连续空提取次数

            while len(extracted_items) < target_count and scroll_count < max_scrolls:
                # 标记DOM元素
                dom_elements_result = await self.executor.mark_page_elements()
                dom_elements = dom_elements_result.get('elements', [])

                # 截图并解析
                if use_omniparser:
                    screenshot_path, parse_resp = await self._capture_and_parse()
                    image_base64 = parse_resp.annotated_image_base64
                else:
                    # 使用原始截图，不经过OmniParser
                    screenshot_path = os.path.join(self.data_dir, timestamp_name("screenshot"))
                    await self.executor.screenshot(screenshot_path)
                    import base64
                    with open(screenshot_path, "rb") as f:
                        image_base64 = base64.b64encode(f.read()).decode("ascii")

                current_url = await self.executor.get_url()

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

                # 检查是否需要继续
                if len(extracted_items) >= target_count:
                    break

                if consecutive_empty >= 2:
                    # 连续2次空提取，可能已经到底
                    break

                # 根据VLM建议决定下一步动作
                if next_action == "scroll":
                    await self.executor.scroll_by(600)
                    await self.executor.wait_for_stable(1500)
                    scroll_count += 1
                elif next_action == "next_page":
                    # TODO: 识别并点击"下一页"按钮
                    # 暂时使用滚动代替
                    await self.executor.scroll_by(600)
                    await self.executor.wait_for_stable(1500)
                    scroll_count += 1
                else:
                    # stop
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

        await self.executor.wait_for_stable(1000)
