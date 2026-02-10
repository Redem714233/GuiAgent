"""
反思引擎 - 实现 Skyvern 风格的执行验证和重试机制
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any, Dict, List, Optional

from backend.executor import Executor
from backend.vlm_service import VLMService
from backend.planner import Planner

logger = logging.getLogger(__name__)


class ReflectionEngine:
    """
    反思引擎：规划 → 执行 → 验证 → 决策（重试/继续/完成）

    类似 Skyvern 的执行流程：
    1. 规划步骤 (plan_steps)
    2. 执行单个步骤 (execute_step)
    3. 验证执行结果 (verify_step)
    4. 决策下一步 (decide_next_action)
       - 成功 → 继续下一步
       - 失败但可重试 → 重试当前步骤
       - 失败且不可重试 → 终止
    """

    def __init__(
        self,
        executor: Executor,
        planner: Planner,
        vlm: VLMService,
        max_steps: int = 10,
        max_retries_per_step: int = 3,
    ):
        self.executor = executor
        self.planner = planner
        self.vlm = vlm
        self.max_steps = max_steps
        self.max_retries_per_step = max_retries_per_step

    async def run_task_with_reflection(
        self,
        task: str,
    ) -> Dict[str, Any]:
        """
        使用反思机制执行任务

        返回:
        {
            "status": "success" | "failed" | "terminated",
            "steps": [
                {
                    "step_index": int,
                    "retry_index": int,
                    "description": str,
                    "action": str,
                    "verification": {...},
                    "status": "success" | "failed"
                }
            ],
            "final_url": str,
            "reasoning": str
        }
        """
        logger.info(f"Starting task with reflection: {task}")

        # 阶段1: 规划步骤
        steps_list, plan_debug = self.vlm.plan_steps(task=task, max_steps=self.max_steps)
        logger.info(f"Planned {len(steps_list)} steps: {steps_list}")

        execution_history: List[Dict[str, Any]] = []
        current_step_index = 0

        # 阶段2: 逐步执行
        while current_step_index < len(steps_list):
            step_description = steps_list[current_step_index]
            logger.info(f"\n{'='*60}")
            logger.info(f"Step {current_step_index + 1}/{len(steps_list)}: {step_description}")
            logger.info(f"{'='*60}")

            # 执行步骤（带重试）
            step_result = await self._execute_step_with_retry(
                task=task,
                step_description=step_description,
                step_index=current_step_index,
            )

            execution_history.append(step_result)

            # 根据验证结果决策下一步
            if step_result["status"] == "success":
                logger.info(f"✓ Step {current_step_index + 1} succeeded")
                current_step_index += 1
            elif step_result["status"] == "failed":
                logger.error(f"✗ Step {current_step_index + 1} failed after {step_result['retry_index']} retries")

                # 询问 VLM 是否需要调整计划
                should_replan = await self._should_replan_after_failure(
                    task=task,
                    failed_step=step_description,
                    step_index=current_step_index,
                    failure_reason=step_result.get("verification", {}).get("reasoning", ""),
                    remaining_steps=steps_list[current_step_index + 1:]
                )

                if should_replan:
                    logger.warning("VLM suggests replanning, skipping remaining steps")
                    break
                else:
                    # 继续下一步
                    current_step_index += 1
            elif step_result["status"] == "terminated":
                logger.error(f"✗ Step {current_step_index + 1} terminated (unrecoverable error)")
                break

            # 检查是否达到最大步数
            if current_step_index >= self.max_steps:
                logger.warning(f"Reached max steps ({self.max_steps})")
                break

        # 阶段3: 总结结果
        final_url = await self.executor.get_url()

        success_count = sum(1 for s in execution_history if s["status"] == "success")
        failed_count = sum(1 for s in execution_history if s["status"] == "failed")

        overall_status = "success" if success_count == len(steps_list) else "partial" if success_count > 0 else "failed"

        return {
            "status": overall_status,
            "steps": execution_history,
            "final_url": final_url,
            "reasoning": f"Completed {success_count}/{len(steps_list)} steps successfully, {failed_count} failed",
            "plan": steps_list,
            "plan_debug": plan_debug,
        }

    async def _execute_step_with_retry(
        self,
        task: str,
        step_description: str,
        step_index: int,
    ) -> Dict[str, Any]:
        """
        执行单个步骤，带重试机制

        返回:
        {
            "step_index": int,
            "retry_index": int,
            "description": str,
            "action": str,
            "verification": {...},
            "status": "success" | "failed" | "terminated"
        }
        """
        retry_index = 0

        while retry_index < self.max_retries_per_step:
            logger.info(f"Attempt {retry_index + 1}/{self.max_retries_per_step}")

            # 1. 捕获执行前状态
            before_url = await self.executor.get_url()
            before_screenshot = await self._capture_screenshot()
            before_elements = await self._get_page_elements()

            # 2. 执行动作
            try:
                action_result = await self._execute_single_action(
                    task=task,
                    step_description=step_description,
                )
            except Exception as e:
                logger.error(f"Action execution failed: {e}")
                action_result = {
                    "success": False,
                    "action": "error",
                    "error": str(e)
                }

            # 等待页面稳定
            await asyncio.sleep(2)
            await self.executor.wait_for_stable(2000)

            # 3. 捕获执行后状态
            after_url = await self.executor.get_url()
            after_screenshot = await self._capture_screenshot()
            after_elements = await self._get_page_elements()

            # 4. 验证执行结果
            verification, verification_raw = self.vlm.verify_step_success(
                task=task,
                step_description=step_description,
                action_taken=action_result.get("action", "unknown"),
                before_url=before_url,
                after_url=after_url,
                before_image_base64=before_screenshot,
                after_image_base64=after_screenshot,
                elements_before=before_elements,
                elements_after=after_elements,
            )

            logger.info(f"Verification result: {verification}")

            # 5. 处理特殊情况
            special_case = verification.get("special_case")
            if special_case:
                logger.warning(f"Special case detected: {special_case}")

                if special_case == "login_required":
                    # 需要登录 → 终止任务
                    return {
                        "step_index": step_index,
                        "retry_index": retry_index,
                        "description": step_description,
                        "action": action_result.get("action", "unknown"),
                        "before_url": before_url,
                        "after_url": after_url,
                        "verification": verification,
                        "verification_raw": verification_raw,
                        "status": "terminated",
                        "termination_reason": "login_required",
                        "user_message": "该网站需要登录，任务已终止"
                    }

                elif special_case == "ad_popup":
                    # 广告弹窗 → 尝试关闭
                    logger.info("Attempting to close ad popup...")
                    popup_element_id = verification.get("popup_element_id")

                    if popup_element_id:
                        try:
                            # 点击关闭按钮
                            await self.executor.click_element_by_id(popup_element_id)
                            await asyncio.sleep(1)
                            await self.executor.wait_for_stable(1000)
                            logger.info("Ad popup closed successfully")

                            # 重试当前步骤
                            retry_index += 1
                            continue
                        except Exception as e:
                            logger.error(f"Failed to close ad popup: {e}")

                    # 如果无法关闭，尝试按 ESC 键
                    try:
                        await self.executor.press("Escape")
                        await asyncio.sleep(1)
                        logger.info("Pressed ESC to close popup")
                        retry_index += 1
                        continue
                    except Exception as e:
                        logger.error(f"Failed to press ESC: {e}")

                elif special_case == "captcha":
                    # 验证码 → 终止任务
                    return {
                        "step_index": step_index,
                        "retry_index": retry_index,
                        "description": step_description,
                        "action": action_result.get("action", "unknown"),
                        "before_url": before_url,
                        "after_url": after_url,
                        "verification": verification,
                        "verification_raw": verification_raw,
                        "status": "terminated",
                        "termination_reason": "captcha",
                        "user_message": "遇到验证码，任务已终止"
                    }

                elif special_case == "error_page":
                    # 错误页面 → 终止任务
                    return {
                        "step_index": step_index,
                        "retry_index": retry_index,
                        "description": step_description,
                        "action": action_result.get("action", "unknown"),
                        "before_url": before_url,
                        "after_url": after_url,
                        "verification": verification,
                        "verification_raw": verification_raw,
                        "status": "terminated",
                        "termination_reason": "error_page",
                        "user_message": f"页面错误: {verification.get('reasoning', '未知错误')}"
                    }

            # 6. 决策下一步
            if verification.get("success", False):
                # 成功 → 返回
                return {
                    "step_index": step_index,
                    "retry_index": retry_index,
                    "description": step_description,
                    "action": action_result.get("action", "unknown"),
                    "before_url": before_url,
                    "after_url": after_url,
                    "verification": verification,
                    "verification_raw": verification_raw,
                    "status": "success"
                }
            elif verification.get("should_retry", True) and retry_index < self.max_retries_per_step - 1:
                # 失败但可重试 → 重试
                logger.warning(f"Step failed but retrying: {verification.get('reasoning', '')}")
                retry_index += 1
                continue
            else:
                # 失败且不可重试 → 返回失败
                logger.error(f"Step failed permanently: {verification.get('reasoning', '')}")
                return {
                    "step_index": step_index,
                    "retry_index": retry_index,
                    "description": step_description,
                    "action": action_result.get("action", "unknown"),
                    "before_url": before_url,
                    "after_url": after_url,
                    "verification": verification,
                    "verification_raw": verification_raw,
                    "status": "failed" if verification.get("should_retry", True) else "terminated"
                }

        # 达到最大重试次数
        return {
            "step_index": step_index,
            "retry_index": retry_index,
            "description": step_description,
            "action": "max_retries_reached",
            "before_url": before_url,
            "after_url": after_url,
            "verification": {"reasoning": f"Max retries ({self.max_retries_per_step}) reached"},
            "status": "failed"
        }

    async def _execute_single_action(
        self,
        task: str,
        step_description: str,
    ) -> Dict[str, Any]:
        """
        执行单个动作

        返回:
        {
            "success": bool,
            "action": str,
            "details": {...}
        }
        """
        # 检查是否是提取步骤（排除 goto 等导航步骤）
        extract_keywords = ["extract", "提取", "采集", "收集", "抓取", "scrape", "copy", "复制"]
        is_extract = any(kw in step_description.lower() for kw in extract_keywords)
        is_navigation = any(kw in step_description.lower() for kw in ["goto", "click", "navigate", "访问", "打开", "点击"])

        if is_extract and not is_navigation:
            # 提取步骤：直接返回成功，不执行任何动作
            logger.info(f"Detected extraction step, skipping action execution")
            return {
                "success": True,
                "action": "extract (no browser action needed)",
                "details": {"tool": "extract", "description": step_description}
            }

        # 标记页面元素
        dom_result = await self.executor.mark_page_elements()
        elements = dom_result.get('elements', [])

        # 截图
        screenshot_path = "data/temp_screenshot.png"
        await self.executor.screenshot(screenshot_path)

        with open(screenshot_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("ascii")

        # 获取当前URL
        current_url = await self.executor.get_url()

        # 使用VLM决策动作
        action, action_raw = self.vlm.decide(
            task=step_description,
            elements=elements,
            annotated_image_base64=image_base64,
            image_size=(1280, 720),
            plan_context=task,
        )

        logger.info(f"VLM decided action: {action}")

        # 执行动作
        tool = action.get("tool", "")

        if tool == "click":
            element_id = action.get("id")
            if element_id:
                success = await self.executor.click_element_by_id(f"skyvern-{element_id}")
                return {"success": success, "action": f"click element {element_id}", "details": action}
            else:
                point = action.get("point")
                if point:
                    await self.executor.click_at_point(point[0], point[1])
                    return {"success": True, "action": f"click at {point}", "details": action}

        elif tool == "type":
            text = action.get("text", "")
            await self.executor.type_text(text)
            return {"success": True, "action": f"type '{text}'", "details": action}

        elif tool == "press":
            key = action.get("key", "")
            await self.executor.press(key)
            return {"success": True, "action": f"press {key}", "details": action}

        elif tool == "goto":
            url = action.get("url", "")
            await self.executor.goto(url)
            return {"success": True, "action": f"goto {url}", "details": action}

        elif tool == "scroll":
            delta = action.get("scroll", 0)
            await self.executor.scroll_by(delta)
            return {"success": True, "action": f"scroll {delta}px", "details": action}

        elif tool == "wait":
            ms = action.get("ms", 1000)
            await asyncio.sleep(ms / 1000)
            return {"success": True, "action": f"wait {ms}ms", "details": action}

        return {"success": False, "action": "unknown", "details": action}

    async def _capture_screenshot(self) -> str:
        """捕获截图并返回 base64"""
        screenshot_path = "data/temp_screenshot.png"
        await self.executor.screenshot(screenshot_path)

        with open(screenshot_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")

    async def _get_page_elements(self) -> List[Dict[str, Any]]:
        """获取页面元素列表"""
        dom_result = await self.executor.mark_page_elements()
        return dom_result.get('elements', [])

    async def _should_replan_after_failure(
        self,
        task: str,
        failed_step: str,
        step_index: int,
        failure_reason: str,
        remaining_steps: List[str]
    ) -> bool:
        """
        询问 VLM 是否需要在失败后重新规划

        返回:
            True - 需要终止并重新规划
            False - 可以继续执行剩余步骤
        """
        prompt = f"""任务: {task}

步骤 {step_index + 1} 失败: {failed_step}
失败原因: {failure_reason}

剩余步骤:
{chr(10).join(f"{i+1}. {s}" for i, s in enumerate(remaining_steps))}

问题: 是否应该终止当前计划？

回答 "yes" 如果:
- 失败的步骤是关键步骤，后续步骤依赖它
- 当前状态与预期差距太大，继续执行没有意义
- 需要完全不同的策略

回答 "no" 如果:
- 失败的步骤不影响后续步骤
- 可以跳过这一步继续执行
- 剩余步骤仍然有价值

只回答 "yes" 或 "no"，不要解释。"""

        try:
            # 简单的文本判断（避免调用 VLM）
            # 如果剩余步骤为空，直接终止
            if not remaining_steps:
                return True

            # 如果失败原因包含关键词，建议重新规划
            critical_keywords = ["无法找到", "页面错误", "导航失败", "元素不存在"]
            if any(kw in failure_reason for kw in critical_keywords):
                logger.info("Detected critical failure, suggesting replan")
                return True

            # 默认继续执行
            return False

        except Exception as e:
            logger.error(f"Failed to check replan: {e}")
            return False  # 出错时继续执行
