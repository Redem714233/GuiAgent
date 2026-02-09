"""
调试：查看页面底部的翻页按钮
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

        # 1. 标记顶部的元素
        print("\n🔍 标记顶部视口的元素...")
        dom_result = await executor.mark_page_elements()
        top_elements = dom_result.get('elements', [])
        print(f"顶部视口: {len(top_elements)} 个元素")

        # 查找 next
        next_in_top = [e for e in top_elements if 'next' in (e.get('text', '') or '').lower()]
        print(f"包含 'next' 的元素: {len(next_in_top)}")

        # 2. 滚动到底部
        print("\n📜 滚动到底部...")
        await executor.scroll_to_bottom()
        await executor.wait_for_stable(1000)

        # 3. 标记底部的元素
        print("\n🔍 标记底部视口的元素...")
        dom_result = await executor.mark_page_elements()
        bottom_elements = dom_result.get('elements', [])
        print(f"底部视口: {len(bottom_elements)} 个元素")

        # 查找 next
        next_in_bottom = [e for e in bottom_elements if 'next' in (e.get('text', '') or '').lower()]
        print(f"包含 'next' 的元素: {len(next_in_bottom)}")

        if next_in_bottom:
            print("\n✅ 找到翻页按钮:")
            for elem in next_in_bottom:
                print(f"  - ID: {elem.get('id')}")
                print(f"    Text: '{elem.get('text')}'")
                print(f"    Tag: {elem.get('tagName')}")
                print(f"    Href: {elem.get('attributes', {}).get('href', '')}")
        else:
            print("\n❌ 底部也没有找到翻页按钮")

        # 4. 使用 JavaScript 直接查找
        print("\n🔍 使用 JavaScript 直接查找包含 'next' 的链接...")
        result = await executor._page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a'));
                const nextLinks = links.filter(a => {
                    const text = a.textContent.toLowerCase();
                    const href = a.href.toLowerCase();
                    return text.includes('next') || href.includes('next');
                });
                return nextLinks.map(a => ({
                    text: a.textContent.trim(),
                    href: a.href,
                    class: a.className,
                    visible: a.offsetParent !== null
                }));
            }
        """)

        if result:
            print(f"✅ 找到 {len(result)} 个包含 'next' 的链接:")
            for link in result:
                print(f"  - Text: '{link['text']}'")
                print(f"    Href: {link['href']}")
                print(f"    Class: {link['class']}")
                print(f"    Visible: {link['visible']}")
        else:
            print("❌ 没有找到包含 'next' 的链接")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
