"""
测试反思机制 - 翻页任务
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.planner import Planner
from backend.reflection_engine import ReflectionEngine


async def test_reflection_pagination():
    """测试反思机制的翻页功能"""

    executor = Executor()
    planner = Planner()

    reflection_engine = ReflectionEngine(
        executor=executor,
        planner=planner,
        vlm=planner.vlm,
        max_steps=10,
        max_retries_per_step=3,
    )

    try:
        await executor.start()
        print("[OK] Browser started")

        # 测试任务：翻页
        task = "访问https://books.toscrape.com/index.html，翻到第二页"

        print("\n" + "=" * 60)
        print("Testing Reflection Mechanism with Pagination")
        print("=" * 60)
        print(f"\nTask: {task}\n")

        # 执行任务
        result = await reflection_engine.run_task_with_reflection(task=task)

        print("\n" + "=" * 60)
        print("Execution Result")
        print("=" * 60)
        print(f"Status: {result['status']}")
        print(f"Final URL: {result['final_url']}")
        print(f"Reasoning: {result['reasoning']}")

        print("\nPlanned Steps:")
        for i, step in enumerate(result['plan'], 1):
            print(f"  {i}. {step}")

        print("\nExecution History:")
        for step in result['steps']:
            step_idx = step['step_index'] + 1
            retry_idx = step['retry_index']
            status = step['status']
            description = step['description']
            action = step['action']
            before_url = step['before_url']
            after_url = step['after_url']
            verification = step['verification']

            status_icon = "✓" if status == "success" else "✗"
            print(f"\n  {status_icon} Step {step_idx} (Retry {retry_idx}): {description}")
            print(f"    Action: {action}")
            print(f"    URL: {before_url} → {after_url}")
            print(f"    Verification: {verification.get('reasoning', 'N/A')}")
            print(f"    Status: {status}")

        # 检查是否成功翻页
        final_url = result['final_url']
        if 'page-2' in final_url or 'page=2' in final_url:
            print("\n[SUCCESS] Successfully navigated to page 2!")
        else:
            print(f"\n[WARNING] Not on page 2. Final URL: {final_url}")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(test_reflection_pagination())
