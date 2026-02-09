"""
调试脚本：检查 VLM 在翻页任务中返回什么
"""

import asyncio
import sys
import os
import base64
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.vlm_service import VLMService


async def debug_vlm_pagination():
    """调试 VLM 在翻页任务中的返回"""

    executor = Executor()
    vlm = VLMService()  # 直接使用 VLMService

    try:
        await executor.start()
        print("[OK] Browser started")

        # 访问第一页
        await executor.goto("https://books.toscrape.com/index.html")
        await executor.wait_for_stable(2000)
        print("[OK] Navigated to books.toscrape.com")

        # 标记元素
        dom_result = await executor.mark_page_elements()
        dom_elements = dom_result.get('elements', [])
        print(f"[OK] Marked {len(dom_elements)} elements")

        # 截图
        screenshot_path = "data/debug_screenshot.png"
        await executor.screenshot(screenshot_path)
        with open(screenshot_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("ascii")

        current_url = await executor.get_url()

        # 测试1：提取数据，看 VLM 返回什么
        print("\n" + "=" * 60)
        print("Test 1: Extract from page (asking for 2 items)")
        print("=" * 60)

        task = "提取2本书的名称和价格"
        list_data, list_raw = vlm.extract_from_page(
            task=task,
            mode="list",
            annotated_image_base64=image_base64,
            current_url=current_url,
            elements=dom_elements,
        )

        print(f"\nVLM Response:")
        print(f"  Items extracted: {len(list_data.get('items', []))}")
        print(f"  Next action: {list_data.get('next', 'N/A')}")
        print(f"  Next page element_id: {list_data.get('next_page_element_id', 'N/A')}")

        print(f"\nFull VLM response:")
        print(list_raw)

        # 测试2：明确要求翻页
        print("\n" + "=" * 60)
        print("Test 2: Extract with explicit pagination request")
        print("=" * 60)

        task2 = "提取2本书的名称和价格，然后翻到下一页"
        list_data2, list_raw2 = vlm.extract_from_page(
            task=task2,
            mode="list",
            annotated_image_base64=image_base64,
            current_url=current_url,
            elements=dom_elements,
        )

        print(f"\nVLM Response:")
        print(f"  Items extracted: {len(list_data2.get('items', []))}")
        print(f"  Next action: {list_data2.get('next', 'N/A')}")
        print(f"  Next page element_id: {list_data2.get('next_page_element_id', 'N/A')}")

        print(f"\nFull VLM response:")
        print(list_raw2)

        # 测试3：检查翻页按钮是否被标注
        print("\n" + "=" * 60)
        print("Test 3: Check if 'next' button is in elements")
        print("=" * 60)

        next_buttons = []
        for elem in dom_elements:
            text = elem.get('text', '').lower()
            elem_id = elem.get('id', '')
            if 'next' in text or '下一页' in text or '›' in text:
                next_buttons.append({
                    'id': elem_id,
                    'text': elem.get('text', ''),
                    'tag': elem.get('tagName', ''),
                    'priority': elem.get('priority', 0)
                })

        if next_buttons:
            print(f"\nFound {len(next_buttons)} potential next buttons:")
            for btn in next_buttons:
                print(f"  - ID: {btn['id']}, Text: '{btn['text']}', Tag: {btn['tag']}, Priority: {btn['priority']}")
        else:
            print("\n[WARNING] No 'next' button found in elements!")

        # 测试4：使用 plan_steps 看看会生成什么步骤
        print("\n" + "=" * 60)
        print("Test 4: Plan steps for pagination task")
        print("=" * 60)

        task3 = "访问https://books.toscrape.com/index.html，翻到第二页，提取2本书"
        steps, plan_debug = vlm.plan_steps(task=task3, max_steps=10)

        print(f"\nPlanned steps:")
        for i, step in enumerate(steps, 1):
            print(f"  {i}. {step}")

    except Exception as e:
        print(f"\n[ERROR] Debug failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(debug_vlm_pagination())
