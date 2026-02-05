"""
Web Agent - 基于 AutoGLM 架构的电脑端自动操控系统

核心架构：
1. 循环执行：截图 → VLM推理 → 解析动作 → 执行动作
2. 使用相对坐标系统（0-1000）
3. 固定延迟策略，不依赖复杂的页面加载等待
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.executor import Executor
from backend.planner import Planner
from backend.storage import ensure_dir, timestamp_name

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Agent 配置"""
    max_steps: int = 20  # 最大步数
    verbose: bool = True  # 是否输出详细日志
    action_delay: float = 2.0  # 每个动作后的延迟（秒）
    screenshot_dir: str = "data/screenshots"  # 截图保存目录


@dataclass
class StepResult:
    """单步执行结果"""
    success: bool  # 是否成功
    finished: bool  # 是否完成任务
    action: Optional[Dict[str, Any]]  # 执行的动作
    thinking: str  # AI的思考过程
    message: str  # 结果消息
    screenshot_path: Optional[str] = None  # 截图路径


class WebAgent:
    """
    Web 自动化 Agent

    参考 AutoGLM 的 PhoneAgent 实现，核心流程：
    1. 截图当前页面
    2. 发送给 VLM（带任务描述和历史上下文）
    3. VLM 返回动作（do(action="Tap", element=[x,y]) 或 finish(message="...")）
    4. 解析并执行动作
    5. 固定延迟
    6. 检查是否完成，否则继续循环
    """

    def __init__(
        self,
        executor: Executor,
        planner: Planner,
        config: Optional[AgentConfig] = None,
    ):
        self.executor = executor
        self.planner = planner
        self.config = config or AgentConfig()

        # 确保截图目录存在
        ensure_dir(self.config.screenshot_dir)

        # 上下文管理
        self._context: List[Dict[str, Any]] = []
        self._step_count = 0

        # 数据提取累积
        self._extracted_data: List[Dict[str, Any]] = []
        self._target_count: Optional[int] = None  # 目标提取数量

    async def run(self, task: str, start_url: Optional[str] = None) -> str:
        """
        执行任务的主入口

        Args:
            task: 用户任务描述
            start_url: 起始URL（可选）

        Returns:
            任务执行结果消息
        """
        logger.info(f"Starting task: {task}")

        # 初始化
        self._context = []
        self._step_count = 0
        self._extracted_data = []  # 重置提取的数据

        # 如果提供了起始URL，先导航
        if start_url:
            logger.info(f"Navigating to start URL: {start_url}")
            await self.executor.goto(start_url)
            await asyncio.sleep(self.config.action_delay)

        # 第一步：获取初始截图和状态
        result = await self._execute_step(user_prompt=task, is_first=True)

        # 循环执行直到完成或达到最大步数
        while not result.finished and self._step_count < self.config.max_steps:
            result = await self._execute_step()

        # 保存提取的数据到 Excel（如果有）
        excel_path = None
        if self._extracted_data:
            logger.info(f"Saving {len(self._extracted_data)} extracted items to Excel")
            excel_path = self._save_to_excel(self._extracted_data)

        # 返回最终结果
        if result.finished:
            logger.info(f"Task completed: {result.message}")
            if excel_path:
                return f"{result.message}\n\nExtracted {len(self._extracted_data)} items and saved to: {excel_path}"
            return result.message
        else:
            logger.warning(f"Task stopped after {self._step_count} steps")
            message = f"Task stopped after {self._step_count} steps. Last message: {result.message}"
            if excel_path:
                message += f"\n\nExtracted {len(self._extracted_data)} items and saved to: {excel_path}"
            return message

    def _save_to_excel(self, data: List[Dict[str, Any]]) -> str:
        """保存数据到 Excel"""
        import pandas as pd
        from backend.storage import timestamp_name

        # 创建 DataFrame
        df = pd.DataFrame(data)

        # 生成文件名
        output_dir = "data/outputs"
        ensure_dir(output_dir)
        filename = timestamp_name("extracted_data", ext=".xlsx")
        filepath = os.path.join(output_dir, filename)

        # 保存到 Excel
        df.to_excel(filepath, index=False, engine='openpyxl')
        logger.info(f"Saved data to {filepath}")

        return filepath

    async def _execute_step(
        self,
        user_prompt: Optional[str] = None,
        is_first: bool = False
    ) -> StepResult:
        """
        执行单步操作

        Args:
            user_prompt: 用户提示（仅第一步使用）
            is_first: 是否是第一步

        Returns:
            StepResult: 执行结果
        """
        self._step_count += 1
        logger.info(f"Step {self._step_count}/{self.config.max_steps}")

        try:
            # 1. 截图
            screenshot_path = os.path.join(
                self.config.screenshot_dir,
                timestamp_name("screenshot")
            )
            await self.executor.screenshot(screenshot_path)

            # 读取截图为 base64
            import base64
            with open(screenshot_path, "rb") as f:
                screenshot_base64 = base64.b64encode(f.read()).decode("ascii")

            # 获取当前URL
            current_url = await self.executor.get_url()

            # 2. 构建消息发送给 VLM
            if is_first:
                # 第一步：发送任务描述
                prompt = self._build_first_prompt(user_prompt, current_url)
            else:
                # 后续步骤：发送上一步的结果
                prompt = self._build_followup_prompt(current_url)

            # 3. 调用 VLM 获取动作
            from backend.vlm_service import VLMService

            if not hasattr(self.planner, '_vlm') or self.planner._vlm is None:
                self.planner._vlm = VLMService()

            response_data, response_debug = self.planner._vlm.get_next_action(
                task=prompt,
                screenshot_base64=screenshot_base64,
                current_url=current_url,
                step_count=self._step_count,
                context=str(self._context[-3:]) if self._context else "",  # 最近3步的上下文
            )

            # 4. 解析动作
            from backend.actions.parser import parse_action
            action = parse_action(response_data.get("action", ""))

            # 5. 执行动作
            from backend.actions.handler import ActionHandler
            action_handler = ActionHandler(self.executor, self.config.action_delay)

            # 获取屏幕尺寸（从 executor 获取）
            screen_width = 1280  # TODO: 从 executor 获取实际尺寸
            screen_height = 720

            action_result = await action_handler.execute(action, screen_width, screen_height)

            # 6. 累积提取的数据
            if action_result.data:
                self._extracted_data.extend(action_result.data)
                logger.info(f"Accumulated {len(action_result.data)} items, total: {len(self._extracted_data)}")

            # 7. 更新上下文
            self._context.append({
                "step": self._step_count,
                "action": action,
                "result": action_result.message,
                "screenshot": screenshot_path,
            })

            # 8. 返回结果
            return StepResult(
                success=action_result.success,
                finished=action_result.should_finish,
                action=action,
                thinking=response_data.get("thinking", ""),
                message=action_result.message,
                screenshot_path=screenshot_path,
            )

        except Exception as e:
            logger.error(f"Step {self._step_count} failed: {e}", exc_info=True)
            return StepResult(
                success=False,
                finished=True,  # 错误时结束任务
                action=None,
                thinking="",
                message=f"Error: {e}",
            )

    def _build_first_prompt(self, task: str, current_url: str) -> str:
        """构建第一步的提示词"""
        return f"""You are a web automation agent. Your task is:

{task}

Current URL: {current_url}

IMPORTANT: If the task asks to "extract", "get", "collect", or "提取" data, you MUST use the Extract action to extract structured data. Do NOT just view the page.

Please analyze the screenshot and decide the next action. You can use these actions:
- do(action="Tap", element=[x, y]) - Click at relative coordinates (0-1000 range)
- do(action="Type", text="...") - Type text into focused input
- do(action="Scroll", direction="down"|"up") - Scroll the page
- do(action="Extract", fields=["field1", "field2"]) - Extract structured data from current page
- do(action="Wait", duration="N seconds") - Wait for N seconds
- finish(message="...") - Complete the task with a message

Return your action in the exact format shown above."""

    def _build_followup_prompt(self, current_url: str) -> str:
        """构建后续步骤的提示词"""
        last_action = self._context[-1]["action"] if self._context else None
        last_result = self._context[-1]["result"] if self._context else ""

        # 检查是否已经提取了数据
        extracted_count = len(self._extracted_data)

        # 检查是否达到目标数量
        target_info = ""
        if self._target_count:
            target_info = f"\nTarget: {self._target_count} items, Current: {extracted_count} items"
            if extracted_count >= self._target_count:
                target_info += "\n⚠️ TARGET REACHED! Use finish() action now."

        return f"""Continue the task.

Current URL: {current_url}
Last action: {last_action}
Last result: {last_result}
Extracted items so far: {extracted_count}{target_info}

IMPORTANT: If you haven't extracted the required data yet, use the Extract action now. Do NOT finish the task without extracting data if the task requires it.

Please analyze the screenshot and decide the next action:
- do(action="Extract", fields=["field1", "field2"]) - Extract structured data from current page
- do(action="Scroll", direction="down"|"up") - Scroll to see more content
- finish(message="...") - Complete the task (only after extracting required data)"""
