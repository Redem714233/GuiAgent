"""
测试点击逻辑

用于诊断为什么点击后跳到 about:blank
"""
import asyncio
import base64
import os
from backend.executor import Executor
from backend.vlm_service import VLMService
from dotenv import load_dotenv
load_dotenv()

async def test_github_trending():
    """测试 GitHub Trending 的点击"""

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

        # 3. 截图
        screenshot_path = "test_screenshot.png"
        await executor.screenshot(screenshot_path)
        print(f"✓ 截图已保存: {screenshot_path}")

        # 4. 读取截图
        with open(screenshot_path, "rb") as f:
            screenshot_base64 = base64.b64encode(f.read()).decode("ascii")

        # 5. 获取当前 URL
        current_url = await executor.get_url()
        print(f"✓ 当前 URL: {current_url}")

        # 6. 调用 VLM 提取列表
        print("\n正在调用 VLM 提取列表...")
        list_data, _ = vlm.extract_from_page(
            task="提取 GitHub Trending 项目",
            mode="list",
            annotated_image_base64=screenshot_base64,
            current_url=current_url,
        )

        items = list_data.get("items", [])
        print(f"✓ VLM 返回了 {len(items)} 个项目")

        # 7. 检查第一个项目
        if items:
            first_item = items[0]
            print(f"\n第一个项目:")
            print(f"  title: {first_item.get('title', 'N/A')}")
            print(f"  url: {first_item.get('url', 'N/A')}")
            print(f"  click_point: {first_item.get('click_point', 'N/A')}")

            # 8. 如果有 click_point，测试点击
            if "click_point" in first_item and first_item["click_point"]:
                click_point = first_item["click_point"]
                if isinstance(click_point, (list, tuple)) and len(click_point) == 2:
                    x, y = int(click_point[0]), int(click_point[1])

                    print(f"\n准备点击坐标: ({x}, {y})")
                    print("点击前 URL:", await executor.get_url())

                    # 点击
                    await executor.click_center((x, y))
                    print("✓ 已点击")

                    # 等待
                    await asyncio.sleep(3)

                    # 检查点击���的 URL
                    after_url = await executor.get_url()
                    print(f"点击后 URL: {after_url}")

                    if after_url == "about:blank":
                        print("❌ 问题确认：点击后跳到了 about:blank")
                    elif after_url == current_url:
                        print("⚠️ 点击后 URL 没有变化")
                    else:
                        print("✓ 点击成功，进入了新页面")

                        # 截图新页面
                        await executor.screenshot("test_after_click.png")
                        print("✓ 新页面截图已保存: test_after_click.png")

            # 9. 如果有 URL，测试导航
            if "url" in first_item and first_item["url"]:
                url = first_item["url"]
                print(f"\n测试导航到 URL: {url}")

                await executor.goto(url)
                await asyncio.sleep(3)

                after_url = await executor.get_url()
                print(f"导航后 URL: {after_url}")

                if after_url != "about:blank":
                    print("✓ URL 导航成功")
                else:
                    print("❌ URL 导航也失败了")

    finally:
        await executor.stop()
        print("\n✓ 测试完成")


if __name__ == "__main__":
    asyncio.run(test_github_trending())
