"""
测试 DOM 标记 - 找到项目链接

专门查找 GitHub Trending 项目的链接
"""
import asyncio
from backend.executor import Executor
from dotenv import load_dotenv

load_dotenv()


async def test_find_project_links():
    """测试查找项目链接"""

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

        # 4. 查找项目链接（包含 "/" 的链接，通常是 owner/repo 格式）
        print("\n查找项目链接（包含 '/' 的文本）:")
        project_links = []

        for elem in result['elements']:
            if elem['tagName'] == 'a':
                text = elem.get('text', '').strip()
                # 项目链接通常包含 "/"，如 "owner/repo"
                if '/' in text and len(text) < 100:
                    project_links.append(elem)
                    if len(project_links) <= 5:  # 只显示前 5 个
                        print(f"\n{len(project_links)}. ID: {elem['id']}")
                        print(f"   文本: {text}")
                        print(f"   位置: ({elem['rect']['x']}, {elem['rect']['y']})")
                        href = elem['attributes'].get('href')
                        if href:
                            print(f"   href: {href}")

        print(f"\n✓ 找到 {len(project_links)} 个项目链接")

        # 5. 点击第一个项目链接
        if project_links:
            first_project = project_links[0]
            print(f"\n准备点击第一个项目:")
            print(f"  ID: {first_project['id']}")
            print(f"  文本: {first_project.get('text', '')}")
            print(f"  点击前 URL: {await executor.get_url()}")

            # 点击
            success = await executor.click_element_by_id(first_project['id'])

            if success:
                print("✓ 点击成功")
                await asyncio.sleep(3)

                # 检查点击后的 URL
                after_url = await executor.get_url()
                print(f"  点击后 URL: {after_url}")

                if "github.com" in after_url and after_url != "https://github.com/trending":
                    print("✓✓✓ 成功！进入了项目页面")
                    print("\n🎉 DOM 定位方法工作正常！")
                else:
                    print("⚠️ URL 变化不符合预期")
            else:
                print("❌ 点击失败")

    finally:
        await executor.stop()
        print("\n✓ 测试完成")


if __name__ == "__main__":
    asyncio.run(test_find_project_links())
