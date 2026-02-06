"""
测试 DOM Marker v3.0 的改进效果

简化版测试脚本，专注于核心功能验证
"""

import asyncio
import time
from backend.executor import Executor
from backend.visualizer import annotate_screenshot_base64
import base64
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()


async def test_dom_marker_v3():
    """测试 v3.0 版本的 DOM Marker"""

    print("=" * 80)
    print("DOM Marker v3.0 测试")
    print("=" * 80)

    executor = Executor()
    await executor.start()

    # 测试网站
    test_url = "https://github.com/trending"
    print(f"\n测试网站: {test_url}")

    try:
        await executor.goto(test_url)
        await asyncio.sleep(3)

        # 截图
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
            tmp_path = tmp_file.name

        try:
            await executor.screenshot(tmp_path)
            with open(tmp_path, 'rb') as f:
                screenshot_b64 = base64.b64encode(f.read()).decode('ascii')
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # 执行 DOM 标记
        print("\n执行 DOM 标记...")
        start_time = time.time()
        dom_result = await executor.mark_page_elements()
        mark_time = time.time() - start_time

        elements = dom_result.get('elements', [])

        print(f"\n✅ 标记完成！")
        print(f"  - 耗时: {mark_time:.3f}秒")
        print(f"  - 元素数量: {len(elements)}")

        # 元素类型统计
        tag_counts = {}
        for elem in elements:
            tag = elem.get('tagName', 'unknown')
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

        print(f"\n📊 元素类型分布:")
        for tag, count in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"  - {tag}: {count}")

        # 生成标注图片
        print(f"\n🎨 生成标注图片...")
        annotated_b64 = annotate_screenshot_base64(
            image_base64=screenshot_b64,
            elements=elements,
            max_elements=None
        )

        # 保存
        output_dir = Path("data/screenshots")
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"v3_test_{timestamp}.png"

        with open(output_path, 'wb') as f:
            f.write(base64.b64decode(annotated_b64))

        print(f"  - 保存路径: {output_path}")

        # 显示前 5 个元素
        print(f"\n🏆 前 5 个元素:")
        for i, elem in enumerate(elements[:5], 1):
            tag = elem.get('tagName', 'unknown')
            text = elem.get('text', '')[:40]
            print(f"  {i}. [{elem.get('id')}] <{tag}> \"{text}\"")

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()

    print("\n✅ 测试完成！")


if __name__ == "__main__":
    asyncio.run(test_dom_marker_v3())
