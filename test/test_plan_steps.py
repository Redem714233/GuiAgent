"""
Test if plan_steps correctly decomposes pagination tasks
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.vlm_service import VLMService


def test_plan_steps():
    """Test if VLM can correctly plan pagination tasks"""

    vlm = VLMService()

    # Test task with pagination
    task = "访问https://books.toscrape.com/index.html，翻到第二页，提取2本书的名称和价格"

    print("=" * 60)
    print("Testing plan_steps with pagination task")
    print("=" * 60)
    print(f"\nTask: {task}\n")

    steps, debug = vlm.plan_steps(task=task, max_steps=6)

    print("Planned steps:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")

    print(f"\nTotal steps: {len(steps)}")

    # Check if pagination step is included
    has_pagination = any(
        'next' in step.lower() or
        '下一页' in step.lower() or
        '翻页' in step.lower() or
        'page 2' in step.lower() or
        '第二页' in step.lower()
        for step in steps
    )

    if has_pagination:
        print("\n[SUCCESS] Pagination step found in plan!")
    else:
        print("\n[WARNING] No pagination step found in plan")
        print("This may cause the agent to extract data from page 1 instead of page 2")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    test_plan_steps()
