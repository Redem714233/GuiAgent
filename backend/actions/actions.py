"""
Action models for browser automation

Based on Skyvern's action abstraction system with proper type hierarchy
"""
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.actions.action_types import ActionType


class ActionStatus(StrEnum):
    """Status of action execution"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SelectOption(BaseModel):
    """Option for select dropdown"""
    label: str | None = None
    value: str | None = None
    index: int | None = None

    def __repr__(self) -> str:
        return f"SelectOption(label={self.label}, value={self.value}, index={self.index})"


class VerificationStatus(StrEnum):
    """Status of user goal verification"""
    COMPLETE = "complete"  # Goal achieved successfully
    TERMINATE = "terminate"  # Goal cannot be achieved, stop trying
    CONTINUE = "continue"  # Goal not yet achieved, continue with more steps


class CompleteVerifyResult(BaseModel):
    """Result of goal verification"""
    status: VerificationStatus
    thoughts: str
    page_info: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == VerificationStatus.COMPLETE

    @property
    def is_terminate(self) -> bool:
        return self.status == VerificationStatus.TERMINATE

    @property
    def is_continue(self) -> bool:
        return self.status == VerificationStatus.CONTINUE


class InputOrSelectContext(BaseModel):
    """Context information for input/select actions"""
    intention: str | None = None
    field: str | None = None
    is_required: bool | None = None
    is_search_bar: bool | None = None
    is_location_input: bool | None = None
    is_date_related: bool | None = None
    date_format: str | None = None


class ClickContext(BaseModel):
    """Context information for click actions"""
    thought: str | None = None
    single_option_click: bool | None = None


class Action(BaseModel):
    """
    Base action class

    All actions inherit from this base class and include common metadata
    """
    model_config = ConfigDict(from_attributes=True)

    action_type: ActionType
    status: ActionStatus = ActionStatus.PENDING

    # Identifiers
    action_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    step_order: int | None = None
    action_order: int | None = None

    # Element information
    element_id: str | None = None
    xpath: str | None = None

    # LLM reasoning
    confidence_float: float | None = None
    description: str | None = None
    reasoning: str | None = None
    intention: str | None = None

    # Execution metadata
    created_at: datetime | None = None
    modified_at: datetime | None = None
    error_message: str | None = None

    # Screenshot reference
    screenshot_path: str | None = None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(action_type={self.action_type}, status={self.status})"


class WebAction(Action):
    """
    Base class for actions that interact with web elements

    These actions require an element_id to identify the target element
    """
    element_id: str  # Required for web actions


class DecisiveAction(Action):
    """
    Base class for actions that end the task (Complete, Terminate)
    """
    errors: list[str] = []
    data_extraction_goal: str | None = None


# ============================================================================
# Concrete Action Classes
# ============================================================================

class ClickAction(WebAction):
    """Click on a web element"""
    action_type: ActionType = ActionType.CLICK

    # Click options
    x: int | None = None  # Specific x coordinate (optional)
    y: int | None = None  # Specific y coordinate (optional)
    button: Literal["left", "right", "middle"] = "left"
    repeat: int = 1  # 1=single, 2=double, 3=triple click

    # File download
    file_url: str | None = None
    download: bool = False

    # Context
    click_context: ClickContext | None = None

    def __repr__(self) -> str:
        return f"ClickAction(element_id={self.element_id}, button={self.button}, repeat={self.repeat})"


class InputTextAction(WebAction):
    """Input text into a form field"""
    action_type: ActionType = ActionType.INPUT_TEXT

    text: str
    input_or_select_context: InputOrSelectContext | None = None
    totp_code_required: bool = False

    def __repr__(self) -> str:
        text_preview = self.text[:50] + "..." if len(self.text) > 50 else self.text
        return f"InputTextAction(element_id={self.element_id}, text='{text_preview}')"


class UploadFileAction(WebAction):
    """Upload a file to a file input element"""
    action_type: ActionType = ActionType.UPLOAD_FILE

    file_path: str  # Local file path to upload
    is_upload_file_tag: bool = True

    def __repr__(self) -> str:
        return f"UploadFileAction(element_id={self.element_id}, file_path={self.file_path})"


class DownloadFileAction(Action):
    """Download a file from the browser"""
    action_type: ActionType = ActionType.DOWNLOAD_FILE

    file_name: str
    download_url: str | None = None
    download: bool = True

    def __repr__(self) -> str:
        return f"DownloadFileAction(file_name={self.file_name}, url={self.download_url})"


class SelectOptionAction(WebAction):
    """Select an option from a dropdown"""
    action_type: ActionType = ActionType.SELECT_OPTION

    option: SelectOption
    input_or_select_context: InputOrSelectContext | None = None
    download: bool = False

    def __repr__(self) -> str:
        return f"SelectOptionAction(element_id={self.element_id}, option={self.option})"


class CheckboxAction(WebAction):
    """Toggle a checkbox"""
    action_type: ActionType = ActionType.CHECKBOX

    is_checked: bool  # Target state (True=checked, False=unchecked)

    def __repr__(self) -> str:
        return f"CheckboxAction(element_id={self.element_id}, is_checked={self.is_checked})"


class HoverAction(WebAction):
    """Hover over a web element"""
    action_type: ActionType = ActionType.HOVER

    hold_seconds: float = 0.0  # How long to hold the hover

    def __repr__(self) -> str:
        return f"HoverAction(element_id={self.element_id}, hold_seconds={self.hold_seconds})"


class GotoUrlAction(Action):
    """Navigate to a URL"""
    action_type: ActionType = ActionType.GOTO_URL

    url: str
    is_magic_link: bool = False  # Special handling for magic links

    def __repr__(self) -> str:
        return f"GotoUrlAction(url={self.url})"


class ScrollAction(Action):
    """Scroll the page"""
    action_type: ActionType = ActionType.SCROLL

    # Target position (optional)
    x: int | None = None
    y: int | None = None

    # Scroll delta (required if x/y not provided)
    scroll_x: int = 0
    scroll_y: int = 0

    def __repr__(self) -> str:
        if self.x is not None or self.y is not None:
            return f"ScrollAction(to x={self.x}, y={self.y})"
        return f"ScrollAction(by scroll_x={self.scroll_x}, scroll_y={self.scroll_y})"


class WaitAction(Action):
    """Wait for a specified duration"""
    action_type: ActionType = ActionType.WAIT

    seconds: float = 1.0

    def __repr__(self) -> str:
        return f"WaitAction(seconds={self.seconds})"


class KeypressAction(Action):
    """Press keyboard keys"""
    action_type: ActionType = ActionType.KEYPRESS

    keys: list[str] = []  # e.g., ["Control", "c"] for Ctrl+C
    hold: bool = False  # Hold keys down
    duration: int = 0  # Duration in milliseconds

    def __repr__(self) -> str:
        return f"KeypressAction(keys={self.keys}, hold={self.hold})"


class MoveAction(Action):
    """Move mouse to a position"""
    action_type: ActionType = ActionType.MOVE

    x: int
    y: int

    def __repr__(self) -> str:
        return f"MoveAction(x={self.x}, y={self.y})"


class DragAction(Action):
    """Drag from one position to another"""
    action_type: ActionType = ActionType.DRAG

    start_x: int | None = None
    start_y: int | None = None
    path: list[tuple[int, int]] = []  # List of (x, y) coordinates

    def __repr__(self) -> str:
        return f"DragAction(start=({self.start_x}, {self.start_y}), path_length={len(self.path)})"


class ReloadPageAction(Action):
    """Reload the current page"""
    action_type: ActionType = ActionType.RELOAD_PAGE

    def __repr__(self) -> str:
        return "ReloadPageAction()"


class ClosePageAction(Action):
    """Close the current page/tab"""
    action_type: ActionType = ActionType.CLOSE_PAGE

    def __repr__(self) -> str:
        return "ClosePageAction()"


class ExtractAction(Action):
    """Extract data from the current page"""
    action_type: ActionType = ActionType.EXTRACT

    data_extraction_goal: str | None = None
    data_extraction_schema: dict[str, Any] | list | str | None = None

    def __repr__(self) -> str:
        return f"ExtractAction(goal={self.data_extraction_goal})"


class CompleteAction(DecisiveAction):
    """Mark the task as successfully completed"""
    action_type: ActionType = ActionType.COMPLETE

    verified: bool = False
    extracted_data: dict[str, Any] | list | None = None

    def __repr__(self) -> str:
        return f"CompleteAction(verified={self.verified})"


class TerminateAction(DecisiveAction):
    """Terminate the task (goal cannot be achieved)"""
    action_type: ActionType = ActionType.TERMINATE

    def __repr__(self) -> str:
        return f"TerminateAction(errors={len(self.errors)})"


class NullAction(Action):
    """No-op action (do nothing)"""
    action_type: ActionType = ActionType.NULL_ACTION

    def __repr__(self) -> str:
        return "NullAction()"


class SolveCaptchaAction(Action):
    """Solve a CAPTCHA challenge"""
    action_type: ActionType = ActionType.SOLVE_CAPTCHA

    captcha_type: str | None = None  # e.g., "recaptcha", "hcaptcha"

    def __repr__(self) -> str:
        return f"SolveCaptchaAction(type={self.captcha_type})"


class VerificationCodeAction(Action):
    """Enter a verification code (e.g., 2FA)"""
    action_type: ActionType = ActionType.VERIFICATION_CODE

    verification_code: str

    def __repr__(self) -> str:
        return f"VerificationCodeAction(code={'*' * len(self.verification_code)})"


# ============================================================================
# Action Factory
# ============================================================================

def create_action_from_dict(data: dict[str, Any]) -> Action:
    """
    Factory function to create the appropriate Action subclass from a dictionary

    Args:
        data: Dictionary containing action data with 'action_type' key

    Returns:
        Appropriate Action subclass instance

    Raises:
        ValueError: If action_type is unknown or invalid
    """
    action_type = data.get("action_type")

    if not action_type:
        raise ValueError("Missing 'action_type' in action data")

    # Map action types to classes
    action_map = {
        ActionType.CLICK: ClickAction,
        ActionType.INPUT_TEXT: InputTextAction,
        ActionType.UPLOAD_FILE: UploadFileAction,
        ActionType.DOWNLOAD_FILE: DownloadFileAction,
        ActionType.SELECT_OPTION: SelectOptionAction,
        ActionType.CHECKBOX: CheckboxAction,
        ActionType.HOVER: HoverAction,
        ActionType.GOTO_URL: GotoUrlAction,
        ActionType.SCROLL: ScrollAction,
        ActionType.WAIT: WaitAction,
        ActionType.KEYPRESS: KeypressAction,
        ActionType.MOVE: MoveAction,
        ActionType.DRAG: DragAction,
        ActionType.RELOAD_PAGE: ReloadPageAction,
        ActionType.CLOSE_PAGE: ClosePageAction,
        ActionType.EXTRACT: ExtractAction,
        ActionType.COMPLETE: CompleteAction,
        ActionType.TERMINATE: TerminateAction,
        ActionType.NULL_ACTION: NullAction,
        ActionType.SOLVE_CAPTCHA: SolveCaptchaAction,
        ActionType.VERIFICATION_CODE: VerificationCodeAction,
    }

    action_class = action_map.get(action_type)
    if not action_class:
        raise ValueError(f"Unknown action_type: {action_type}")

    return action_class.model_validate(data)
