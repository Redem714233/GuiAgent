from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from backend.executor import Executor
from backend.omniparser_service import OmniParserService
from backend.planner import Planner
from backend.output_store import OutputStore
from backend.schemas import ParseRequest, StepRequest
from backend.storage import ensure_dir, timestamp_name


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

                # 过滤已见过的URL（去重）
                new_items = []
                for item in items:
                    item_url = item.get("url", "")

                    # GitHub特殊处理：如果没有URL但title是owner/repo格式，自动生成URL
                    if not item_url and "github.com" in current_url.lower():
                        title = item.get("title", "")
                        # 匹配 "owner/repo" 格式
                        if "/" in title and not title.startswith("http"):
                            # 移除多余空格
                            repo_path = title.strip()
                            # 如果是纯 owner/repo 格式
                            if repo_path.count("/") == 1 and " " not in repo_path:
                                item_url = f"https://github.com/{repo_path}"
                                item["url"] = item_url

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
                        # 没有URL的项目（如评论），直接添加
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
                    if "url" not in item or not item["url"]:
                        continue

                    try:
                        # 为每个详情页提取设置30秒总超时
                        async def extract_detail():
                            # 导航到详情页
                            detail_url = item["url"]
                            if not detail_url.startswith("http"):
                                # 相对URL，补全
                                from urllib.parse import urljoin
                                detail_url = urljoin(current_url, detail_url)

                            await self.executor.goto(detail_url)
                            await self.executor.wait_for_load(timeout_ms=10000)  # 10秒超时
                            await self.executor.wait_for_stable(1000)  # 减少到1秒

                            # 截图并提取详情
                            if use_omniparser:
                                screenshot_path, parse_resp = await self._capture_and_parse()
                                detail_image_base64 = parse_resp.annotated_image_base64
                            else:
                                screenshot_path = os.path.join(self.data_dir, timestamp_name("screenshot"))
                                await self.executor.screenshot(screenshot_path)
                                import base64
                                with open(screenshot_path, "rb") as f:
                                    detail_image_base64 = base64.b64encode(f.read()).decode("ascii")

                            detail_url_actual = await self.executor.get_url()

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
                                await self.executor.goto(current_url)
                                await self.executor.wait_for_load(timeout_ms=10000)
                                await self.executor.wait_for_stable(1000)

                        # 使用asyncio.wait_for添加30秒总超时
                        await asyncio.wait_for(extract_detail(), timeout=30.0)

                    except asyncio.TimeoutError:
                        errors.append(f"Detail extraction timeout for item {idx} (url: {item.get('url', 'N/A')})")
                        continue
                    except Exception as exc:
                        errors.append(f"Detail extraction failed for item {idx} (url: {item.get('url', 'N/A')}): {type(exc).__name__}: {exc}")
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

    async def _capture_and_parse(self):
        """截图并解析元素"""
        screenshot_path = os.path.join(self.data_dir, timestamp_name("screenshot"))
        await self.executor.screenshot(screenshot_path)

        import base64
        with open(screenshot_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")

        parse_resp = self.parser_service.parse(
            ParseRequest(image_base64=image_b64, use_paddleocr=True)
        )

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
