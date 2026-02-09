"""
调试：显示页面上所有包含 'next' 的元素
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor


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

        # 滚动到底部再回到顶部
        print("\n📜 滚动到底部再回到顶部...")
        await executor.scroll_to_bottom()
        await executor.wait_for_stable(500)
        await executor.scroll_to_top()
        await executor.wait_for_stable(500)

        # 标记元素
        print("\n🔍 标记页面元素...")
        dom_result = await executor.mark_page_elements()
        dom_elements = dom_result.get('elements', [])
        print(f"找到 {len(dom_elements)} 个可交互元素")

        # 查找所有包含 "next" 的元素
        print("\n🔍 所有包含 'next' 的元素:")
        next_elements = []
        for elem in dom_elements:
            text = elem.get('text', '') or ''
            href = elem.get('attributes', {}).get('href', '') or ''
            if 'next' in text.lower() or 'next' in href.lower():
                next_elements.append(elem)

        for i, elem in enumerate(next_elements, 1):
            print(f"\n  {i}. ID: {elem.get('id')}")
            print(f"     Text: '{elem.get('text')}'")
            print(f"     Tag: {elem.get('tagName')}")
            print(f"     Href: {elem.get('attributes', {}).get('href', '')}")
            print(f"     Class: {elem.get('attributes', {}).get('class', '')}")
            print(f"     Position: x={elem.get('rect', {}).get('x')}, y={elem.get('rect', {}).get('y')}")

        # 查找所有链接
        print("\n\n🔗 所有链接元素 (a 标签):")
        links = [e for e in dom_elements if e.get('tagName') == 'a']
        print(f"共 {len(links)} 个链接")

        # 显示前 10 个链接
        for i, link in enumerate(links[:10], 1):
            print(f"\n  {i}. ID: {link.get('id')}")
            print(f"     Text: '{link.get('text')[:50]}'")
            print(f"     Href: {link.get('attributes', {}).get('href', '')}")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
