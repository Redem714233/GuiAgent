"""
Action types for browser automation

Based on Skyvern's action type system
"""
from enum import StrEnum


class ActionType(StrEnum):
    """Enumeration of all supported action types"""

    # Basic interactions
    CLICK = "click"
    INPUT_TEXT = "input_text"
    UPLOAD_FILE = "upload_file"
    DOWNLOAD_FILE = "download_file"

    # Form interactions
    SELECT_OPTION = "select_option"
    CHECKBOX = "checkbox"

    # Navigation and control
    GOTO_URL = "goto_url"
    SCROLL = "scroll"
    WAIT = "wait"
    RELOAD_PAGE = "reload_page"
    CLOSE_PAGE = "close_page"

    # Advanced interactions
    HOVER = "hover"
    KEYPRESS = "keypress"
    MOVE = "move"
    DRAG = "drag"

    # Data extraction
    EXTRACT = "extract"

    # Task control
    COMPLETE = "complete"
    TERMINATE = "terminate"
    NULL_ACTION = "null_action"

    # Special actions
    SOLVE_CAPTCHA = "solve_captcha"
    VERIFICATION_CODE = "verification_code"

    def is_web_action(self) -> bool:
        """Check if this action requires a web element"""
        return self in [
            ActionType.CLICK,
            ActionType.INPUT_TEXT,
            ActionType.UPLOAD_FILE,
            ActionType.SELECT_OPTION,
            ActionType.CHECKBOX,
            ActionType.HOVER,
        ]

    def is_decisive_action(self) -> bool:
        """Check if this action ends the task"""
        return self in [
            ActionType.COMPLETE,
            ActionType.TERMINATE,
        ]


# Actions that require post-execution screenshot
POST_ACTION_SCREENSHOT_TYPES = [
    ActionType.CLICK,
    ActionType.INPUT_TEXT,
    ActionType.UPLOAD_FILE,
    ActionType.SELECT_OPTION,
    ActionType.CHECKBOX,
    ActionType.GOTO_URL,
    ActionType.SCROLL,
    ActionType.WAIT,
]
