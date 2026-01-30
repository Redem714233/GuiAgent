import pyautogui
import time

class Actor:
    def __init__(self):
        # 安全设置：鼠标移动到左上角强行终止
        pyautogui.FAILSAFE = True

    def execute(self, plan):
        """
        根据 Brain 的计划执行具体操作
        plan 示例: {"action": "click", "coordinates": [100, 200], "explanation": "点击浏览器图标"}
        """
        action_type = plan.get('action')

        try:
            if action_type == 'click':
                coords = plan.get('coordinates')
                if coords:
                    pyautogui.click(x=coords[0], y=coords[1])

            elif action_type == 'type':
                text = plan.get('text')
                if text:
                    pyautogui.write(text, interval=0.1)

                # Check for special keys
                enter = plan.get('enter', False)
                if enter:
                   pyautogui.press('enter')

            elif action_type == 'key_combo':
                # e.g. "ctrl", "c"
                keys = plan.get('keys', [])
                if keys:
                    pyautogui.hotkey(*keys)

            elif action_type == 'scroll':
                amount = plan.get('amount', 0)
                pyautogui.scroll(amount)

            elif action_type == 'wait':
                time.sleep(2)

            # TODO: 添加更多动作，如 extract_data (提取数据存数据库)

        except Exception as e:
            print(f"执行动作失败: {e}")
