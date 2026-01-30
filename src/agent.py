import time
import traceback
from src.observer import Observer
from src.brain import Brain
from src.actor import Actor
from src.tools.parser import OmniParserLocal, OmniParserMock

class GUIAgent:
    def __init__(self):
        self.observer = Observer()
        # 尝试加载本地 OmniParser，如果失败则回退到Mock (或直接报错)
        try:
            self.parser = OmniParserLocal()
        except Exception as e:
            print(f"Failed to load OmniParserLocal: {e}. Using Mock.")
            traceback.print_exc()
            self.parser = OmniParserMock()

        self.brain = Brain()
        self.actor = Actor()
        self.memory = []  #短期记忆，记录执行过的步骤

    def run(self, task_description):
        """
        Agent 主循环
        Observation -> Perception(Parser) -> Thought -> Action
        """
        step = 0
        max_steps = 20 # 防止死循环

        while step < max_steps:
            print(f"\n--- Step {step + 1} ---")

            # 1. 观察 (Observe)
            screenshot_path = self.observer.capture_screen()
            print(f"屏幕截图已保存: {screenshot_path}")

            # 2. 感知 (Perceive) - 新增步骤
            # 使用 OmniParser 进行元素检测和 OCR，生成带标记的图
            annotated_path, ui_elements = self.parser.parse(screenshot_path)

            # 3. 思考 (Think)
            # 将带标记的图、元素列表传给大脑
            # 大脑现在可以输出 "点击 ID 1" 这样的指令，而不是模糊坐标
            action_plan = self.brain.think(task_description, annotated_path, ui_elements, self.memory)

            if not action_plan:
                print("大脑未能生成计划，停止运行。")
                break

            print(f"计划执行操作: {action_plan.get('explanation')}")

            # 4. 行动 (Act)
            if action_plan.get('action') == 'finish':
                print("任务完成！")
                break

            self.actor.execute(action_plan)

            # 5. 记忆 (Memorize)
            self.memory.append({
                "step": step,
                "action": action_plan,
                "timestamp": time.time()
            })

            step += 1
            time.sleep(5) # 等待页面响应和OmniParser处理完成（增加到5秒）

