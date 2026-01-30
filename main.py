import os
import time
from dotenv import load_dotenv

# 设置环境变量，强制 Transformers 和 HuggingFace Hub 离线模式
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
# 禁用 PaddlePaddle 的模型源检查，避免连接超时
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

# 加载环境变量 (需要配置 OPENAI_API_KEY)
load_dotenv()

from src.agent import GUIAgent

def main():
    print("GUI Agent 启动中...")

    # 初始化 Agent
    # 这里我们假设使用 OpenAI 的 GPT-4o 模型
    agent = GUIAgent()

    # 用户指令
    user_task = "打开edge浏览器，打开网站https://news.sina.com.cn/，复制一条新闻，然后打印到浏览器中，就算完成任务"
    print(f"收到任务: {user_task}")

    # 开始执行任务
    agent.run(user_task)

if __name__ == "__main__":
    main()
