"""
强制翻页测试 - 明确告诉 VLM 需要翻页
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.planner import Planner


async def main():
    executor = Executor()
    planner = Planner()

    try:
        await executor.start()
        print("✅ 浏览器已启动\n")

        # 访问测试网站
        url = "https://books.toscrape.com/index.html"
        print(f"📖 访问: {url}")
        await executor.goto(url)
        await executor.wait_for_stable(2000)

        # 第一次提取
        print("\n" + "=" * 60)
        print("第一页提取")
        print("=" * 60)

        dom_result = await executor.mark_page_elements()
        dom_elements = dom_result.get('elements', [])

        screenshot_path = "test_page1.png"
        await executor.screenshot(screenshot_path)

        import base64
        with open(screenshot_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("ascii")

        # 明确告诉 VLM 需要翻页
        task = "提取前2本书的名称和价格。注意：页面底部有翻页按钮，需要识别'next'按钮以便翻到下一页。"

        list_data, _ = planner.extract_from_page(
            task=task,
            mode="list",
            annotated_image_base64=image_base64,
            current_url=url,
            elements=dom_elements,
        )

        items = list_data.get("items", [])
        next_action = list_data.get("next", "stop")
        next_page_element_id = list_data.get("next_page_element_id")

        print(f"\n✅ 第一页提取了 {len(items)} 本书")
        for i, item in enumerate(items[:2], 1):
            print(f"  {i}. {item.get('title', 'N/A')} - {item.get('price', 'N/A')}")

        print(f"\n📄 VLM 决策:")
        print(f"  - next: {next_action}")
        print(f"  - next_page_element_id: {next_page_element_id}")

        # 如果 VLM 识别了翻页按钮，尝试点击
        if next_action == "next_page" and next_page_element_id:
            print(f"\n🖱️  点击翻页按钮: {next_page_element_id}")
            success = await executor.click_element_by_id(next_page_element_id)

            if success:
                print("✅ 点击成功！等待页面加载...")
                await executor.wait_for_stable(2000)

                new_url = await executor.get_url()
                print(f"📍 新URL: {new_url}")

                # 第二页提取
                print("\n" + "=" * 60)
                print("第二页提取")
                print("=" * 60)

                dom_result2 = await executor.mark_page_elements()
                dom_elements2 = dom_result2.get('elements', [])

                screenshot_path2 = "test_page2.png"
                await executor.screenshot(screenshot_path2)

                with open(screenshot_path2, "rb") as f:
                    image_base64_2 = base64.b64encode(f.read()).decode("ascii")

                list_data2, _ = planner.extract_from_page(
                    task="提取前2本书的名称和价格",
                    mode="list",
                    annotated_image_base64=image_base64_2,
                    current_url=new_url,
                    elements=dom_elements2,
                )

                items2 = list_data2.get("items", [])
                print(f"\n✅ 第二页提取了 {len(items2)} 本书")
                for i, item in enumerate(items2[:2], 1):
                    print(f"  {i}. {item.get('title', 'N/A')} - {item.get('price', 'N/A')}")

                print("\n" + "=" * 60)
                print("✅ 翻页测试成功！")
                print("=" * 60)
            else:
                print("❌ 点击失败")
        else:
            print("\n⚠️  VLM 没有识别翻页按钮")
            print("可能原因：")
            print("  1. VLM 没有看到翻页按钮")
            print("  2. 任务描述不够明确")
            print("  3. 翻页按钮不���视野内")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
