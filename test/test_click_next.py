"""
直接测试点击翻页按钮
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

        # 标记元素
        print("\n🔍 标记页面元素...")
        dom_result = await executor.mark_page_elements()
        dom_elements = dom_result.get('elements', [])
        print(f"找到 {len(dom_elements)} 个可交互元素")

        # 滚动到页面底部，让翻页按钮进入视口
        print("\n📜 滚动到页面底部...")
        try:
            await executor.scroll_to_bottom()
            await executor.wait_for_stable(1000)
            print("✅ 滚动完成")

            # 重新标记元素
            print("\n🔍 重新标记页面元素...")
            dom_result = await executor.mark_page_elements()
            dom_elements = dom_result.get('elements', [])
            print(f"找到 {len(dom_elements)} 个可交互元素")
        except Exception as e:
            print(f"⚠️  滚动失败: {e}")

        # 查找 "next" 按钮
        print("\n🔍 查找 'next' 按钮...")
        next_buttons = []
        for elem in dom_elements:
            text = elem.get('text', '').lower()
            if 'next' in text:
                next_buttons.append(elem)

        if not next_buttons:
            print("❌ 未找到 'next' 按钮")
            return

        print(f"✅ 找到 {len(next_buttons)} 个包含 'next' 的元素:")
        for i, elem in enumerate(next_buttons, 1):
            print(f"\n  {i}. ID: {elem.get('id')}")
            print(f"     Text: {elem.get('text')}")
            print(f"     Tag: {elem.get('tagName')}")
            print(f"     Href: {elem.get('attributes', {}).get('href', '')}")
            print(f"     Class: {elem.get('attributes', {}).get('class', '')}")

        # 选择正确的 next 按钮（应该是翻页的，不是分类的）
        next_button = None
        for elem in next_buttons:
            href = elem.get('attributes', {}).get('href', '')
            text = elem.get('text', '').strip().lower()
            # 翻页按钮的特征：href 包含 "page-" 或者 text 只是 "next"
            if 'page-' in href or text == 'next':
                next_button = elem
                break

        if not next_button:
            # 如果没找到，就用第一个
            next_button = next_buttons[0]

        print(f"\n🎯 选择的按钮:")
        print(f"   ID: {next_button.get('id')}")
        print(f"   Text: {next_button.get('text')}")
        print(f"   Href: {next_button.get('attributes', {}).get('href', '')}")

        # 尝试点击
        button_id = next_button.get('id')
        print(f"\n🖱️  尝试点击按钮: {button_id}")

        success = await executor.click_element_by_id(button_id)

        if success:
            print("✅ 点击成功！")
            await executor.wait_for_stable(2000)

            # 检查 URL 变化
            new_url = await executor.get_url()
            print(f"📍 新URL: {new_url}")

            if new_url != url:
                print("✅ 成功跳转到第二页！")

                # 截图
                await executor.screenshot("page2.png")
                print("📸 第二页截图: page2.png")
            else:
                print("⚠️  URL 没有变化")
        else:
            print("❌ 点击失败")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
