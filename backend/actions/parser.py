"""
动作解析器 - 参考 AutoGLM 的实现

安全地解析 VLM 返回的动作字符串
"""

import ast
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def parse_action(response: str) -> Dict[str, Any]:
    """
    解析 AI 模型输出的动作字符串

    支持格式:
    1. do(action="Type", text="xxx")
    2. do(action="Tap", element=[x,y])
    3. do(action="Scroll", direction="down")
    4. do(action="Wait", duration="1 seconds")
    5. finish(message="xxx")

    Args:
        response: VLM 返回的动作字符串

    Returns:
        解析后的动作字典

    Examples:
        >>> parse_action('do(action="Tap", element=[500, 300])')
        {'_metadata': 'do', 'action': 'Tap', 'element': [500, 300]}

        >>> parse_action('finish(message="Task completed")')
        {'_metadata': 'finish', 'message': 'Task completed'}
    """
    response = response.strip()

    try:
        # 特殊处理 Type 动作（避免 eval，因为文本可能包含特殊字符）
        if response.startswith('do(action="Type"'):
            # 提取 text 参数
            text_start = response.find('text="') + 6
            text_end = response.rfind('")')
            if text_start > 5 and text_end > text_start:
                text = response[text_start:text_end]
                return {
                    "_metadata": "do",
                    "action": "Type",
                    "text": text
                }

        # 处理 do(...) 格式
        if response.startswith("do("):
            # 转义特殊字符
            response = response.replace('\n', '\\n').replace('\r', '\\r')

            # 使用 AST 安全解析
            tree = ast.parse(response, mode="eval")
            call = tree.body

            if not isinstance(call, ast.Call):
                raise ValueError("Invalid action format")

            action = {"_metadata": "do"}

            # 提取关键字参数
            for keyword in call.keywords:
                key = keyword.arg
                value = ast.literal_eval(keyword.value)  # 安全解析
                action[key] = value

            return action

        # 处理 finish(...) 格式
        elif response.startswith("finish("):
            # 提取 message
            message_start = response.find('message="') + 9
            message_end = response.rfind('")')
            if message_start > 8 and message_end > message_start:
                message = response[message_start:message_end]
                return {
                    "_metadata": "finish",
                    "message": message
                }
            else:
                # 回退：尝试 AST 解析
                tree = ast.parse(response, mode="eval")
                call = tree.body
                for keyword in call.keywords:
                    if keyword.arg == "message":
                        return {
                            "_metadata": "finish",
                            "message": ast.literal_eval(keyword.value)
                        }

        # 无法解析，返回错误动作
        logger.warning(f"Failed to parse action: {response}")
        return {
            "_metadata": "finish",
            "message": f"Failed to parse action: {response}"
        }

    except Exception as e:
        logger.error(f"Error parsing action: {e}", exc_info=True)
        return {
            "_metadata": "finish",
            "message": f"Error parsing action: {e}"
        }


def finish(message: str) -> Dict[str, Any]:
    """创建一个 finish 动作"""
    return {
        "_metadata": "finish",
        "message": message
    }
