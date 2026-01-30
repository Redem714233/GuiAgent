import mss
import os
from datetime import datetime

class Observer:
    def __init__(self, output_dir="data/screenshots"):
        self.output_dir = output_dir
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

    def capture_screen(self):
        """
        捕获当前屏幕并保存
        """
        with mss.mss() as sct:
            # 获取第一个显示器 (主屏)
            monitor = sct.monitors[1]

            # 生成文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(self.output_dir, f"screenshot_{timestamp}.png")

            # 保存
            sct.shot(mon=1, output=filename)
            return filename
