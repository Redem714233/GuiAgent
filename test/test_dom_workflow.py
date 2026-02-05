"""
测试完整的 DOM 定位流程

1. 标记页面元素
2. 将元素列表发送给 VLM
3. VLM 返回 element_id
4. 通过 element_id 点击
"""
import asyncio
import base64
from backend.executor import Executor
from backend.vlm_service import VLMService
from dotenv import load_dotenv

load_dotenv()


async def test_full_dom_workflow():
    """测试完整的 DOM 定位工作流"""

    executor = Executor()
    vlm = VLMService()

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
        dom_result = await executor.mark_page_elements()
        elements = dom_result['elements']
        print(f"✓ 找到 {len(elements)} 个可交互元素")

        # 4. 过滤出项目链接（包含 trending 的链接）
        project_links = []
        for elem in elements:
            if elem['tagName'] == 'a':
                href = elem['attributes'].get('href', '')
                text = elem.get('text', '')
                # GitHub Trending 项目链接通常包含 /owner/repo 格式
                if href and '/' in href and text and len(text) > 5:
                    # 排除导航链接
                    if 'trending' not in href.lower() and 'github.com' not in href.lower():
                        project_links.append(elem)

        print(f"\n找到 {len(project_links)} 个项目链接")

        # 5. 显示前 5 个项目
        print("\n前 5 个项目:")
        for i, elem in enumerate(project_links[:5], 1):
            print(f"\n{i}. ID: {elem['id']}")
            print(f"   文本: {elem.get('text', '')[:50]}")
            print(f"   href: {elem['attributes'].get('href', '')}")
            print(f"   位置: ({elem['rect']['x']}, {elem['rect']['y']})")

        # 6. 测试点击第一个项目
        if project_links:
            first_project = project_links[0]
            print(f"\n准备点击第一个项目:")
            print(f"  ID: {first_project['id']}")
            print(f"  文本: {first_project.get('text', '')[:50]}")
            print(f"  点击前 URL: {await executor.get_url()}")

            # 点击
            success = await executor.click_element_by_id(first_project['id'])

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
                    print("✓ 点击成功，进入了项目页面")

                    # 截图新页面
                    await executor.screenshot("test_project_page.png")
                    print("✓ 项目页面截图已保存: test_project_page.png")
            else:
                print("❌ 点击失败")

        # 7. 测试将元素列表转换为 HTML
        print("\n\n测试元素列表转换为 HTML:")
        html = executor.dom_service.elements_to_html(project_links[:5])
        print(html[:500])  # 显示前 500 字符

    finally:
        await executor.stop()
        print("\n✓ 测试完成")


if __name__ == "__main__":
    asyncio.run(test_full_dom_workflow())
