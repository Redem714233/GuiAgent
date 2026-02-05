"""
动作处理器 - Enhanced with Skyvern patterns

执行各种 Web 自动化动作，支持完整的 Action 类型系统
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from backend.executor import Executor
from backend.actions.actions import (
    Action, ActionStatus, ActionType,
    ClickAction, InputTextAction, UploadFileAction, DownloadFileAction,
    SelectOptionAction, CheckboxAction, HoverAction,
    GotoUrlAction, ScrollAction, WaitAction, KeypressAction,
    MoveAction, DragAction, ReloadPageAction, ClosePageAction,
    ExtractAction, CompleteAction, TerminateAction, NullAction,
    SolveCaptchaAction, VerificationCodeAction
)

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """动作执行结果"""
    success: bool  # 是否成功
    should_finish: bool  # 是否应该结束任务
    message: str  # 结果消息
    data: list = None  # 提取的数据（可选）
    action: Action | None = None  # 执行的动作对象

    def __post_init__(self):
        if self.data is None:
            self.data = []


class ActionHandler:
    """
    动作处理器

    Enhanced with Skyvern's action type system and error handling patterns
    """

    def __init__(self, executor: Executor, action_delay: float = 2.0, max_retries: int = 3):
        self.executor = executor
        self.action_delay = action_delay
        self.max_retries = max_retries

    async def execute(
        self,
        action: Action | Dict[str, Any],
        screen_width: int = 1280,
        screen_height: int = 720
    ) -> ActionResult:
        """
        执行单个动作

        Args:
            action: Action 对象或动作字典（兼容旧格式）
            screen_width: 屏幕宽度（像素）
            screen_height: 屏幕高度（像素）

        Returns:
            ActionResult: 执行结果
        """
        # 兼容旧格式：如果是字典，先转换为 Action 对象
        if isinstance(action, dict):
            action = self._convert_legacy_action(action)

        logger.info(f"Executing action: {action}")

        # 更新状态为运行中
        action.status = ActionStatus.RUNNING

        try:
            # 根据 action_type 分发到对应的处理器
            if isinstance(action, ClickAction):
                result = await self._handle_click(action, screen_width, screen_height)
            elif isinstance(action, InputTextAction):
                result = await self._handle_input_text(action)
            elif isinstance(action, UploadFileAction):
                result = await self._handle_upload_file(action)
            elif isinstance(action, DownloadFileAction):
                result = await self._handle_download_file(action)
            elif isinstance(action, SelectOptionAction):
                result = await self._handle_select_option(action)
            elif isinstance(action, CheckboxAction):
                result = await self._handle_checkbox(action)
            elif isinstance(action, HoverAction):
                result = await self._handle_hover(action)
            elif isinstance(action, GotoUrlAction):
                result = await self._handle_goto_url(action)
            elif isinstance(action, ScrollAction):
                result = await self._handle_scroll(action)
            elif isinstance(action, WaitAction):
                result = await self._handle_wait(action)
            elif isinstance(action, KeypressAction):
                result = await self._handle_keypress(action)
            elif isinstance(action, MoveAction):
                result = await self._handle_move(action)
            elif isinstance(action, DragAction):
                result = await self._handle_drag(action)
            elif isinstance(action, ReloadPageAction):
                result = await self._handle_reload_page(action)
            elif isinstance(action, ClosePageAction):
                result = await self._handle_close_page(action)
            elif isinstance(action, ExtractAction):
                result = await self._handle_extract(action)
            elif isinstance(action, CompleteAction):
                result = await self._handle_complete(action)
            elif isinstance(action, TerminateAction):
                result = await self._handle_terminate(action)
            elif isinstance(action, NullAction):
                result = await self._handle_null(action)
            elif isinstance(action, SolveCaptchaAction):
                result = await self._handle_solve_captcha(action)
            elif isinstance(action, VerificationCodeAction):
                result = await self._handle_verification_code(action)
            else:
                result = ActionResult(
                    success=False,
                    should_finish=False,
                    message=f"Unknown action type: {type(action)}",
                    action=action
                )

            # 更新动作状态
            if result.success:
                action.status = ActionStatus.COMPLETED
            else:
                action.status = ActionStatus.FAILED
                action.error_message = result.message

            result.action = action

            # 每个动作后固定延迟（除非是结束动作）
            if result.success and not result.should_finish:
                await asyncio.sleep(self.action_delay)

            return result

        except Exception as e:
            logger.error(f"Action execution failed: {e}", exc_info=True)
            action.status = ActionStatus.FAILED
            action.error_message = str(e)
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Action failed: {e}",
                action=action
            )

    async def execute_with_retry(
        self,
        action: Action | Dict[str, Any],
        screen_width: int = 1280,
        screen_height: int = 720,
        max_retries: int = None
    ) -> ActionResult:
        """
        执行动作，失败时自动重试

        Args:
            action: Action 对象或动作字典
            screen_width: 屏幕宽度
            screen_height: 屏幕高度
            max_retries: 最大重试次数（None 则使用默认值）

        Returns:
            ActionResult: 执行结果
        """
        if max_retries is None:
            max_retries = self.max_retries

        last_error = None

        for attempt in range(max_retries):
            try:
                result = await self.execute(action, screen_width, screen_height)

                # 如果成功，直接返回
                if result.success:
                    if attempt > 0:
                        logger.info(f"Action succeeded on attempt {attempt + 1}/{max_retries}")
                    return result

                # 如果失败但不应该重试（如 DecisiveAction），直接返回
                if isinstance(action, (CompleteAction, TerminateAction)):
                    return result

                last_error = result.message

            except Exception as e:
                last_error = str(e)
                logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}")

            # 如果还有重试机会，等待后重试
            if attempt < max_retries - 1:
                backoff_time = 2 ** attempt  # 指数退避：1s, 2s, 4s...
                logger.info(f"Retrying in {backoff_time}s...")
                await asyncio.sleep(backoff_time)

        # 所有重试都失败了
        logger.error(f"All {max_retries} attempts failed. Last error: {last_error}")

        if isinstance(action, dict):
            action = self._convert_legacy_action(action)

        action.status = ActionStatus.FAILED
        action.error_message = f"Failed after {max_retries} attempts: {last_error}"

        return ActionResult(
            success=False,
            should_finish=False,
            message=f"Failed after {max_retries} attempts: {last_error}",
            action=action
        )

    def _convert_legacy_action(self, action_dict: Dict[str, Any]) -> Action:
        """
        转换旧格式的动作字典为 Action 对象

        支持旧的 do(action="Tap") 格式
        """
        metadata = action_dict.get("_metadata")

        # 处理 finish 动作
        if metadata == "finish":
            return CompleteAction(
                description=action_dict.get("message", "Task completed")
            )

        # 处理 do 动作
        action_name = action_dict.get("action")

        if action_name == "Tap":
            element = action_dict.get("element", [500, 500])
            return ClickAction(
                element_id="legacy",
                x=element[0] if len(element) > 0 else 500,
                y=element[1] if len(element) > 1 else 500
            )
        elif action_name == "Type":
            return InputTextAction(
                element_id="legacy",
                text=action_dict.get("text", "")
            )
        elif action_name == "Scroll":
            direction = action_dict.get("direction", "down")
            scroll_y = 600 if direction == "down" else -600
            return ScrollAction(scroll_y=scroll_y)
        elif action_name == "Wait":
            duration_str = action_dict.get("duration", "1 seconds")
            try:
                duration = float(duration_str.replace("seconds", "").replace("second", "").strip())
            except ValueError:
                duration = 1.0
            return WaitAction(seconds=duration)
        elif action_name == "Navigate":
            return GotoUrlAction(url=action_dict.get("url", ""))
        elif action_name == "Extract":
            return ExtractAction(
                data_extraction_goal=action_dict.get("fields", [])
            )
        else:
            return NullAction(description=f"Unknown legacy action: {action_name}")

    # ========================================================================
    # Action Handlers - 每个 Action 类型对应一个处理方法
    # ========================================================================

    async def _handle_click(
        self,
        action: ClickAction,
        screen_width: int,
        screen_height: int
    ) -> ActionResult:
        """处理点击动作"""
        # 如果提供了具体坐标，使用坐标点击
        if action.x is not None and action.y is not None:
            x, y = action.x, action.y
            # 如果坐标是相对坐标（0-1000），转换为绝对坐标
            if x <= 1000 and y <= 1000:
                x = int(x / 1000 * screen_width)
                y = int(y / 1000 * screen_height)
        else:
            # 否则使用默认中心点
            x, y = screen_width // 2, screen_height // 2

        logger.info(f"Clicking at ({x}, {y}), button={action.button}, repeat={action.repeat}")

        # 执行点击
        for _ in range(action.repeat):
            await self.executor.click_center((x, y))
            if action.repeat > 1:
                await asyncio.sleep(0.1)  # 多次点击之间短暂延迟

        return ActionResult(
            success=True,
            should_finish=False,
            message=f"Clicked at ({x}, {y}) {action.repeat} time(s)"
        )

    async def _handle_input_text(self, action: InputTextAction) -> ActionResult:
        """处理文本输入动作"""
        if not action.text:
            return ActionResult(
                success=False,
                should_finish=False,
                message="No text provided"
            )

        logger.info(f"Typing text: {action.text[:50]}...")
        await self.executor.type_text(action.text)

        return ActionResult(
            success=True,
            should_finish=False,
            message=f"Typed text: {action.text[:50]}..."
        )

    async def _handle_upload_file(self, action: UploadFileAction) -> ActionResult:
        """处理文件上传动作"""
        logger.info(f"Uploading file: {action.file_path}")

        try:
            await self.executor.upload_file(action.file_path)
            return ActionResult(
                success=True,
                should_finish=False,
                message=f"Uploaded file: {action.file_path}"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"File upload failed: {e}"
            )

    async def _handle_download_file(self, action: DownloadFileAction) -> ActionResult:
        """处理文件下载动作"""
        logger.info(f"Downloading file: {action.file_name}")

        try:
            if action.download_url:
                import os
                from backend.storage import ensure_dir

                download_dir = "data/downloads"
                ensure_dir(download_dir)
                save_path = os.path.join(download_dir, action.file_name)

                await self.executor.download_file(action.download_url, save_path)
                return ActionResult(
                    success=True,
                    should_finish=False,
                    message=f"Downloaded file to: {save_path}"
                )
            else:
                return ActionResult(
                    success=False,
                    should_finish=False,
                    message="No download URL provided"
                )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"File download failed: {e}"
            )

    async def _handle_select_option(self, action: SelectOptionAction) -> ActionResult:
        """处理下拉选择动作"""
        logger.info(f"Selecting option: {action.option}")

        try:
            selector = f"select"  # 简化版选择器

            if action.option.value is not None:
                await self.executor.select_option(selector, value=action.option.value)
            elif action.option.label is not None:
                await self.executor.select_option(selector, label=action.option.label)
            elif action.option.index is not None:
                await self.executor.select_option(selector, index=action.option.index)
            else:
                return ActionResult(
                    success=False,
                    should_finish=False,
                    message="No option value, label, or index provided"
                )

            return ActionResult(
                success=True,
                should_finish=False,
                message=f"Selected option: {action.option}"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Select option failed: {e}"
            )

    async def _handle_checkbox(self, action: CheckboxAction) -> ActionResult:
        """处理复选框动作"""
        logger.info(f"Setting checkbox to: {action.is_checked}")

        try:
            selector = f'input[type="checkbox"]'  # 简化版选择器
            await self.executor.checkbox(selector, action.is_checked)

            return ActionResult(
                success=True,
                should_finish=False,
                message=f"Set checkbox to {action.is_checked}"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Checkbox operation failed: {e}"
            )

    async def _handle_hover(self, action: HoverAction) -> ActionResult:
        """处理悬停动作"""
        logger.info(f"Hovering over element: {action.element_id}")

        try:
            # 使用默认中心点
            x, y = 640, 360
            await self.executor.hover(x, y, action.hold_seconds)

            return ActionResult(
                success=True,
                should_finish=False,
                message=f"Hovered at ({x}, {y}) for {action.hold_seconds}s"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Hover failed: {e}"
            )

    async def _handle_goto_url(self, action: GotoUrlAction) -> ActionResult:
        """处理导航动作"""
        if not action.url:
            return ActionResult(
                success=False,
                should_finish=False,
                message="No URL provided"
            )

        logger.info(f"Navigating to {action.url}")
        await self.executor.goto(action.url)

        return ActionResult(
            success=True,
            should_finish=False,
            message=f"Navigated to {action.url}"
        )

    async def _handle_scroll(self, action: ScrollAction) -> ActionResult:
        """处理滚动动作"""
        # 如果提供了目标位置，滚动到该位置
        if action.y is not None:
            logger.info(f"Scrolling to position y={action.y}")
            await self.executor.scroll_to(action.y)
            return ActionResult(
                success=True,
                should_finish=False,
                message=f"Scrolled to position y={action.y}"
            )

        # 否则按增量滚动
        logger.info(f"Scrolling by ({action.scroll_x}, {action.scroll_y})")
        await self.executor.scroll_by(action.scroll_y)

        return ActionResult(
            success=True,
            should_finish=False,
            message=f"Scrolled by ({action.scroll_x}, {action.scroll_y})"
        )

    async def _handle_wait(self, action: WaitAction) -> ActionResult:
        """处理等待动作"""
        logger.info(f"Waiting {action.seconds} seconds")
        await asyncio.sleep(action.seconds)

        return ActionResult(
            success=True,
            should_finish=False,
            message=f"Waited {action.seconds} seconds"
        )

    async def _handle_keypress(self, action: KeypressAction) -> ActionResult:
        """处理按键动作"""
        logger.info(f"Pressing keys: {action.keys}")

        try:
            await self.executor.press_keys(action.keys, action.hold)
            return ActionResult(
                success=True,
                should_finish=False,
                message=f"Pressed keys: {action.keys}"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Keypress failed: {e}"
            )

    async def _handle_move(self, action: MoveAction) -> ActionResult:
        """处理鼠标移动动作"""
        logger.info(f"Moving mouse to ({action.x}, {action.y})")

        try:
            await self.executor.move_mouse(action.x, action.y)
            return ActionResult(
                success=True,
                should_finish=False,
                message=f"Moved mouse to ({action.x}, {action.y})"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Mouse move failed: {e}"
            )

    async def _handle_drag(self, action: DragAction) -> ActionResult:
        """处理拖拽动作"""
        logger.info(f"Dragging from ({action.start_x}, {action.start_y})")

        try:
            if action.path and len(action.path) > 0:
                # 使用路径的最后一个点作为终点
                end_x, end_y = action.path[-1]
            else:
                return ActionResult(
                    success=False,
                    should_finish=False,
                    message="No drag path provided"
                )

            await self.executor.drag(action.start_x, action.start_y, end_x, end_y)
            return ActionResult(
                success=True,
                should_finish=False,
                message=f"Dragged from ({action.start_x}, {action.start_y}) to ({end_x}, {end_y})"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Drag failed: {e}"
            )

    async def _handle_reload_page(self, action: ReloadPageAction) -> ActionResult:
        """处理页面刷新动作"""
        logger.info("Reloading page")

        try:
            await self.executor.reload()
            return ActionResult(
                success=True,
                should_finish=False,
                message="Page reloaded"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Reload page failed: {e}"
            )

    async def _handle_close_page(self, action: ClosePageAction) -> ActionResult:
        """处理关闭页面动作"""
        logger.info("Closing page")

        try:
            await self.executor.close_page()
            return ActionResult(
                success=True,
                should_finish=False,
                message="Page closed"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Close page failed: {e}"
            )

    async def _handle_extract(self, action: ExtractAction) -> ActionResult:
        """处理数据提取动作"""
        logger.info(f"Extracting data: {action.data_extraction_goal}")

        # 截图
        import base64
        import os
        from backend.storage import timestamp_name

        screenshot_path = os.path.join("data/screenshots", timestamp_name("screenshot"))
        await self.executor.screenshot(screenshot_path)

        with open(screenshot_path, "rb") as f:
            screenshot_base64 = base64.b64encode(f.read()).decode("ascii")

        # 获取当前URL
        current_url = await self.executor.get_url()

        # 调用 VLM 提取数据
        from backend.vlm_service import VLMService
        vlm = VLMService()

        extracted_data, _ = vlm.extract_from_page(
            task=action.data_extraction_goal or "Extract data from page",
            mode="list",
            annotated_image_base64=screenshot_base64,
            current_url=current_url,
        )

        items = extracted_data.get("items", [])

        return ActionResult(
            success=True,
            should_finish=False,
            message=f"Extracted {len(items)} items",
            data=items
        )

    async def _handle_complete(self, action: CompleteAction) -> ActionResult:
        """处理任务完成动作"""
        logger.info("Task completed successfully")

        return ActionResult(
            success=True,
            should_finish=True,
            message=action.description or "Task completed",
            data=action.extracted_data or []
        )

    async def _handle_terminate(self, action: TerminateAction) -> ActionResult:
        """处理任务终止动作"""
        logger.info("Task terminated")

        return ActionResult(
            success=True,
            should_finish=True,
            message=action.description or "Task terminated"
        )

    async def _handle_null(self, action: NullAction) -> ActionResult:
        """处理空动作"""
        logger.info("Null action (no-op)")

        return ActionResult(
            success=True,
            should_finish=False,
            message="No action performed"
        )

    async def _handle_solve_captcha(self, action: SolveCaptchaAction) -> ActionResult:
        """处理验证码动作"""
        logger.info(f"Solving CAPTCHA: {action.captcha_type}")

        # TODO: 实现验证码求解逻辑
        # 可以集成第三方验证码服���

        return ActionResult(
            success=False,
            should_finish=False,
            message="CAPTCHA solving not yet implemented"
        )

    async def _handle_verification_code(self, action: VerificationCodeAction) -> ActionResult:
        """处理验证码输入动作"""
        logger.info("Entering verification code")

        # 输入验证码
        await self.executor.type_text(action.verification_code)

        return ActionResult(
            success=True,
            should_finish=False,
            message="Verification code entered"
        )
