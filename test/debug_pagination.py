"""
调试翻页功能 - books.toscrape.com

测试 VLM 是否正确识别翻页需求
"""

import asyncio
import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()

# 添加项目根目录到路径
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

        # 标记DOM元素
        print("\n🔍 标记页面元素...")
        dom_result = await executor.mark_page_elements()
        dom_elements = dom_result.get('elements', [])
        print(f"找到 {len(dom_elements)} 个可交互元素")

        # 截图
        screenshot_path = "test_books_page1.png"
        await executor.screenshot(screenshot_path)
        print(f"📸 截图保存: {screenshot_path}")

        # 读取截图为 base64
        import base64
        with open(screenshot_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("ascii")

        # 测试 VLM 提取
        print("\n🤖 测试 VLM 提取...")
        task = "提取书籍的名称和价格"
        list_data, raw_response = planner.extract_from_page(
            task=task,
            mode="list",
            annotated_image_base64=image_base64,
            current_url=url,
            elements=dom_elements,
        )

        print("\n📊 VLM 返回结果:")
        # 检查关键字段
        items = list_data.get("items", [])
        next_action = list_data.get("next", "stop")
        next_page_element_id = list_data.get("next_page_element_id")

        # 只显示关键字段，不显示完整的 items
        print(f"  - 提取了 {len(items)} 个书籍")
        print(f"  - next: {next_action}")
        print(f"  - next_page_element_id: {next_page_element_id}")

        # 显示前2个书籍
        if items:
            print(f"\n  前2个书籍:")
            for i, item in enumerate(items[:2], 1):
                print(f"    {i}. {item.get('title', 'N/A')} - {item.get('price', 'N/A')}")

        print(f"\n✅ 提取了 {len(items)} 个书籍")
        print(f"📄 下一步动作: {next_action}")
        print(f"🔗 下一页按钮ID: {next_page_element_id}")

        # 如果有下一页按钮ID，尝试查找
        if next_page_element_id:
            print(f"\n🔍 查找下一页按钮: {next_page_element_id}")
            found = False
            for elem in dom_elements:
                if elem.get('id') == next_page_element_id:
                    print(f"✅ 找到按钮: {elem.get('text', '')[:50]}")
                    found = True
                    break
            if not found:
                print(f"❌ 未找到按钮 {next_page_element_id}")

        # 手动查找 "next" 按钮
        print("\n🔍 手动查找翻页按钮...")
        next_keywords = ["next", "下一页", "›", "»"]
        for elem in dom_elements:
            elem_text = elem.get("text", "").lower()
            elem_id = elem.get("id", "")
            for keyword in next_keywords:
                if keyword in elem_text:
                    print(f"✅ 找到可能的翻页按钮:")
                    print(f"   ID: {elem_id}")
                    print(f"   Text: {elem.get('text', '')}")
                    print(f"   Tag: {elem.get('tagName', '')}")
                    print(f"   Href: {elem.get('attributes', {}).get('href', '')}")
                    break

        # 测试点击下一页
        if next_action == "next_page" and next_page_element_id:
            print(f"\n🖱️  尝试点击下一页按钮: {next_page_element_id}")
            success = await executor.click_element_by_id(next_page_element_id)
            if success:
                print("✅ 点击成功！")
                await executor.wait_for_stable(2000)

                # 检查URL是否变化
                new_url = await executor.get_url()
                print(f"📍 新URL: {new_url}")

                # 截图第二页
                screenshot_path2 = "test_books_page2.png"
                await executor.screenshot(screenshot_path2)
                print(f"📸 第二页截图: {screenshot_path2}")
            else:
                print("❌ 点击失败")

        print("\n✅ 测试完成！")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print("\n关闭浏览器...")
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
