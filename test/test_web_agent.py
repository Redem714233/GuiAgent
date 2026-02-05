"""
测试 WebAgent 的简单脚本
"""

import asyncio
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.web_agent import WebAgent, AgentConfig
from backend.executor import Executor
from backend.planner import Planner
from dotenv import load_dotenv
load_dotenv()


async def test_web_agent():
    """测试 WebAgent"""
    print("=" * 60)
    print("WebAgent 测试")
    print("=" * 60)

    # 创建实例
    executor = Executor()
    planner = Planner()

    # 配置
    config = AgentConfig(
        max_steps=10,
        verbose=True,
        action_delay=2.0,
        screenshot_dir="data/screenshots"
    )

    # 创建 Agent
    agent = WebAgent(executor, planner, config)

    # 测试任务
    task = "打开豆瓣电影Top250，提取前5部电影的标题和评分"
    start_url = "https://movie.douban.com/top250"

    print(f"\n任务: {task}")
    print(f"起始URL: {start_url}\n")

    try:
        # 执行任务
        result = await agent.run(task, start_url)

        print("\n" + "=" * 60)
        print("任务完成!")
        print("=" * 60)
        print(f"结果: {result}")

    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # 清理
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(test_web_agent())
