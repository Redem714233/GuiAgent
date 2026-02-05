"""
测试 DOM 标记功能

验证 Skyvern 风格的 DOM 元素定位是否工作
"""
import asyncio
from backend.executor import Executor
from dotenv import load_dotenv

load_dotenv()


async def test_dom_marking():
    """测试 DOM 标记"""

    executor = Executor()

    try:
        # 1. 启动浏览器
        await executor.start()
        print("✓ 浏览器已启动")

        # 2. 导航到 GitHub Trending
        await executor.goto("https://github.com/trending")
        await asyncio.sleep(3)
        print("✓ 已导航到 GitHub Trending")

        # 3. 标记页面元素
        print("\n正在标记页面元素...")
        result = await executor.mark_page_elements()

        print(f"✓ 找到 {result['count']} 个可交互元素")
        print(f"  视口大小: {result['viewport']}")

        # 4. 显示前 10 个元素
        print("\n前 10 个可交互元素:")
        for i, elem in enumerate(result['elements'][:10], 1):
            print(f"\n{i}. ID: {elem['id']}")
            print(f"   标签: {elem['tagName']}")
            print(f"   文本: {elem.get('text', '')[:50]}")
            print(f"   位置: ({elem['rect']['x']}, {elem['rect']['y']})")
            print(f"   大小: {elem['rect']['width']}x{elem['rect']['height']}")

        # 5. 测试点击第一个链接
        if result['elements']:
            # 找到第一个 <a> 标签
            first_link = None
            for elem in result['elements']:
                if elem['tagName'] == 'a' and elem.get('text'):
                    first_link = elem
                    break

            if first_link:
                print(f"\n准备点击第一个链接:")
                print(f"  ID: {first_link['id']}")
                print(f"  文本: {first_link.get('text', '')[:50]}")
                print(f"  点击前 URL: {await executor.get_url()}")

                # 点击
                success = await executor.click_element_by_id(first_link['id'])

                if success:
                    print("✓ 点击成功")
                    await asyncio.sleep(3)

                    # 检查点击后的 URL
                    after_url = await executor.get_url()
                    print(f"  点击后 URL: {after_url}")

                    if after_url == "about:blank":
                        print("❌ 问题：点击后跳到了 about:blank")
                    elif after_url == "https://github.com/trending":
                        print("⚠️ 点击后 URL 没有变化")
                    else:
                        print("✓ 点击成功，进入了新页面")
                else:
                    print("❌ 点击失败")

    finally:
        await executor.stop()
        print("\n✓ 测试完成")


if __name__ == "__main__":
    asyncio.run(test_dom_marking())
