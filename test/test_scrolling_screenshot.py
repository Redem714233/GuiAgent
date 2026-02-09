"""
测试滚动截图功能
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.scrolling_screenshot import take_scrolling_screenshot
import base64


async def main():
    executor = Executor()

    try:
        await executor.start()
        print("✅ 浏览器已启动\n")

        # 访问测试网站
        url = "https://books.toscrape.com/index.html"
        print(f"📖 访问: {url}")
        await executor.goto(url)
        await executor.wait_for_stable(2000)

        # 使用滚动截图
        print("\n📸 开始滚动截图...")
        image_base64, elements = await take_scrolling_screenshot(executor, max_scrolls=5)

        print(f"\n✅ 滚动截图完成！")
        print(f"   - 捕获了 {len(elements)} 个元素")

        # 保存截图
        screenshot_path = "scrolling_screenshot.png"
        with open(screenshot_path, "wb") as f:
            f.write(base64.b64decode(image_base64))
        print(f"   - 截图已保存: {screenshot_path}")

        # 查找包含 "next" 的元素
        print("\n🔍 查找包含 'next' 的元素:")
        next_elements = []
        for elem in elements:
            text = elem.get('text', '') or ''
            href = elem.get('attributes', {}).get('href', '') or ''
            # 调试：打印所有链接的文本
            if elem.get('tagName') == 'a' and text:
                print(f"  Link: '{text[:30]}' -> {href[:50]}")
            if 'next' in text.lower() or 'next' in href.lower():
                next_elements.append(elem)

        if next_elements:
            print(f"✅ 找到 {len(next_elements)} 个包含 'next' 的元素:")
            for i, elem in enumerate(next_elements, 1):
                print(f"\n  {i}. ID: {elem.get('id')}")
                print(f"     Text: '{elem.get('text')}'")
                print(f"     Tag: {elem.get('tagName')}")
                print(f"     Href: {elem.get('attributes', {}).get('href', '')}")
                print(f"     Scroll Y: {elem.get('_scroll_y', 0)}")
        else:
            print("❌ 未找到包含 'next' 的元素")

        # 显示所有链接
        print(f"\n🔗 所有链接元素: {len([e for e in elements if e.get('tagName') == 'a'])} 个")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
