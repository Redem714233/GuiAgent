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
        use_omniparser: bool = True,
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
        seen_urls: set[str] = set()  # 去重

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

                # 提取当前页面的列表项
                list_data, list_debug = self.planner.extract_from_page(
                    task=task,
                    mode="list",
                    annotated_image_base64=image_base64,
                    current_url=current_url,
                )

                items = list_data.get("items", [])
                next_action = list_data.get("next", "stop")

                # 调试：记录VLM返回的数据类型
                if items and not isinstance(items[0], dict):
                    errors.append(f"VLM returned invalid items format. Expected list of dicts, got list of {type(items[0]).__name__}. First item: {items[0]}")
                    errors.append(f"Full list_data: {list_data}")
                    items = []  # 清空以避免后续错误

                # 过滤已见过的URL（去重）
                new_items = []
                for item in items:
                    # 验证item类型
                    if not isinstance(item, dict):
                        errors.append(f"Skipping non-dict item (type: {type(item).__name__}): {item}")
                        continue

                    # 如果有click_point，验证并保存（用于后续点击）
                    if "click_point" in item and item["click_point"]:
                        click_point = item["click_point"]
                        # 验证click_point格式
                        if isinstance(click_point, (list, tuple)) and len(click_point) == 2:
                            try:
                                x, y = int(click_point[0]), int(click_point[1])
                                # 保存为元组
                                item["_saved_click_point"] = (x, y)
                            except (ValueError, TypeError) as e:
                                errors.append(f"Warning: Invalid click_point format: {click_point}, error: {e}")
                        else:
                            errors.append(f"Warning: click_point must be [x, y], got: {click_point}")

                    item_url = item.get("url", "")

                    # 标准化URL用于去重
                    normalized_url = item_url
                    if item_url:
                        # 补全相对URL
                        if item_url.startswith("/"):
                            from urllib.parse import urljoin
                            normalized_url = urljoin(current_url, item_url)
                        # 统一URL格式（去除尾部斜杠、转小写）
                        normalized_url = normalized_url.rstrip("/").lower()

                    if normalized_url and normalized_url not in seen_urls:
                        seen_urls.add(normalized_url)
                        # 补全item中的URL为完整URL
                        if item_url and item_url.startswith("/"):
                            from urllib.parse import urljoin
                            item["url"] = urljoin(current_url, item_url)
                        new_items.append(item)
                    elif not item_url:
                        # 没有URL的项目（可能有element_id用于点击，或者是评论等），直接添加
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

                for idx, item in enumerate(extracted_items):
                    # 验证item类型
                    if not isinstance(item, dict):
                        errors.append(f"Item {idx} is not a dict (type: {type(item).__name__}): {item}")
                        continue

                    # 检查是否有URL或click_point
                    has_url = "url" in item and item["url"]
                    has_click_point = "_saved_click_point" in item and item["_saved_click_point"]

                    if not has_url and not has_click_point:
                        # 既没有URL也没有click_point，跳过
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
                                    detail_url = urljoin(current_url, detail_url)
                                progress[-1]["current_action"] = f"Navigating to {detail_url}"
                                await self.executor.goto(detail_url)
                                # 固定延迟
                                await asyncio.sleep(2)
                            elif has_click_point:
                                # 使用VLM给出的坐标点击
                                click_point = item["_saved_click_point"]
                                x, y = click_point
                                progress[-1]["current_action"] = f"Clicking at ({x}, {y})"
                                await self.executor.click_center((x, y))

                            # 参考 AutoGLM：固定延迟而不是等待页面加载
                            progress[-1]["current_action"] = "Waiting after navigation"
                            logger.info(f"Waiting 2 seconds after navigation (item {idx})")
                            await asyncio.sleep(2)  # 固定2秒延迟
                            logger.info(f"Wait completed (item {idx})")

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
                                    # 使用URL导航，直接返回列表页
                                    progress[-1]["current_action"] = "Returning to list page"
                                    await self.executor.goto(current_url)
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
                        errors.append(f"Detail extraction timeout for item {idx} (url: {item.get('url', 'N/A')}, click_point: {item.get('_saved_click_point', 'N/A')})")
                        continue
                    except Exception as exc:
                        progress[-1]["current_action"] = f"Error at item {idx}: {type(exc).__name__}"
                        errors.append(f"Detail extraction failed for item {idx} (url: {item.get('url', 'N/A')}, click_point: {item.get('_saved_click_point', 'N/A')}): {type(exc).__name__}: {exc}")
                        continue

                progress[-1]["status"] = "completed"

            # 阶段5: 保存到Excel
            progress.append({"stage": "save", "status": "running"})

            # 重置输出存储
            self.output_store.reset()

            # 添加所有行
            for item in extracted_items:
                self.output_store.append_row(item)

            # 保存Excel
            if extracted_items:
                file_path = self.output_store.save_excel()
                progress[-1]["status"] = "completed"
                progress[-1]["file_path"] = file_path
            else:
                progress[-1]["status"] = "skipped"
                progress[-1]["reason"] = "no_items"
                file_path = None

            # 返回结果
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
                "items": extracted_items,  # 返回提取的数据供前端预览
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
