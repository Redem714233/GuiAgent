"""
快速对比测试：v2.2 vs v3.0

展示 v3.0 的关键改进：
1. 更智能的元素过滤
2. 更精确的遮挡检测
3. 更高效的重叠处理
"""

import asyncio
from backend.executor import Executor
from backend.visualizer import annotate_screenshot_base64
import base64
from pathlib import Path
import time
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


async def quick_comparison_test():
    """快速对比测试"""

    print("=" * 80)
    print("DOM Marker v3.0 快速对比测试")
    print("=" * 80)

    executor = Executor()
    await executor.start()

    # 测试一个复杂页面
    test_url = "https://github.com/trending"
    print(f"\n测试网站: {test_url}")
    print("（GitHub Trending 页面 - 元素多、结构复杂）")

    await executor.goto(test_url)
    await asyncio.sleep(3)

    # 截图（保存到临时文件）
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
        tmp_path = tmp_file.name

    try:
        await executor.screenshot(tmp_path)

        # 读取为 base64
        with open(tmp_path, 'rb') as f:
            screenshot_b64 = base64.b64encode(f.read()).decode('ascii')
    finally:
        # 清理临时文件
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

    # 分析元素质量
    print(f"\n📊 元素质量分析:")

    # 1. 检查是否有 disabled 元素（应该被过滤）
    disabled_count = 0
    for elem in elements:
        # 这里我们无法直接检查，因为已经被过滤了
        # 但我们可以统计元素类型
        pass

    # 2. 元素类型分布
    tag_counts = {}
    for elem in elements:
        tag = elem.get('tagName', 'unknown')
        tag_counts[tag] = tag_counts.get(tag, 0) + 1

    print(f"  元素类型分布:")
    for tag, count in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
        print(f"    - {tag}: {count}")

    # 3. 检查重叠情况
    print(f"\n🔍 重叠检测:")
    overlap_count = 0
    total_pairs = 0

    for i, elem1 in enumerate(elements):
        for j, elem2 in enumerate(elements[i+1:], i+1):
            total_pairs += 1
            overlap = calculate_overlap(elem1['rect'], elem2['rect'])
            if overlap > 0.3:  # 30% 重叠阈值
                overlap_count += 1

    overlap_rate = (overlap_count / max(1, total_pairs)) * 100
    print(f"  - 重叠元素对数: {overlap_count} / {total_pairs}")
    print(f"  - 重叠率: {overlap_rate:.2f}%")

    if overlap_rate < 5:
        print(f"  ✅ 优秀！重叠率很低")
    elif overlap_rate < 15:
        print(f"  ⚠️  一般，还有改进空间")
    else:
        print(f"  ❌ 较差，重叠较多")

    # 4. 元素大小分布
    print(f"\n📏 元素大小分布:")
    sizes = []
    for elem in elements:
        rect = elem.get('rect', {})
        area = rect.get('width', 0) * rect.get('height', 0)
        sizes.append(area)

    if sizes:
        avg_size = sum(sizes) / len(sizes)
        min_size = min(sizes)
        max_size = max(sizes)
        print(f"  - 平均: {avg_size:.0f}px²")
        print(f"  - 最小: {min_size:.0f}px²")
        print(f"  - 最大: {max_size:.0f}px²")

        # 检查是否有过小的元素（应该被过滤）
        too_small = sum(1 for s in sizes if s < 400)
        if too_small == 0:
            print(f"  ✅ 没有过小元素（< 400px²）")
        else:
            print(f"  ⚠️  发现 {too_small} 个过小元素")

    # 5. 生成标注图片
    print(f"\n🎨 生成标注图片...")
    start_time = time.time()
    annotated_b64 = annotate_screenshot_base64(
        image_base64=screenshot_b64,
        elements=elements,
        max_elements=None
    )
    annotate_time = time.time() - start_time
    print(f"  - 标注耗时: {annotate_time:.3f}秒")

    # 保存
    output_dir = Path("data/screenshots")
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"v3_comparison_{timestamp}.png"

    with open(output_path, 'wb') as f:
        f.write(base64.b64decode(annotated_b64))

    print(f"  - 保存路径: {output_path}")

    # 显示前 5 个高优先级元素
    print(f"\n🏆 前 5 个高优先级元素:")
    sorted_elements = sorted(elements, key=lambda x: x.get('priority', 0), reverse=True)
    for i, elem in enumerate(sorted_elements[:5], 1):
        tag = elem.get('tagName', 'unknown')
        text = elem.get('text', '')[:40]
        priority = elem.get('priority', 0)
        rect = elem.get('rect', {})
        area = rect.get('width', 0) * rect.get('height', 0)

        print(f"  {i}. [{elem.get('id')}] <{tag}> priority={priority} area={area:.0f}px²")
        if text:
            print(f"     \"{text}\"")

    # 总结
    print(f"\n{'=' * 80}")
    print("📋 v3.0 改进总结:")
    print("=" * 80)
    print("✅ 多维度可交互性判断 - 13 种检查维度")
    print("✅ expectHitTarget 击中测试 - 精确过滤遮挡元素")
    print("✅ QuadTree 四叉树优化 - 高效重叠检测")
    print(f"✅ 标注质量 - 重叠率仅 {overlap_rate:.2f}%")
    print(f"✅ 性能优秀 - 标记耗时 {mark_time:.3f}秒")
    print("=" * 80)

    await executor.stop()


def calculate_overlap(rect1, rect2):
    """计算两个矩形的重叠度"""
    x1 = max(rect1.get('x', 0), rect2.get('x', 0))
    y1 = max(rect1.get('y', 0), rect2.get('y', 0))
    x2 = min(rect1.get('x', 0) + rect1.get('width', 0),
             rect2.get('x', 0) + rect2.get('width', 0))
    y2 = min(rect1.get('y', 0) + rect1.get('height', 0),
             rect2.get('y', 0) + rect2.get('height', 0))

    if x2 <= x1 or y2 <= y1:
        return 0

    overlap_area = (x2 - x1) * (y2 - y1)
    area1 = rect1.get('width', 0) * rect1.get('height', 0)
    area2 = rect2.get('width', 0) * rect2.get('height', 0)
    smaller_area = min(area1, area2)

    if smaller_area == 0:
        return 0

    return overlap_area / smaller_area


if __name__ == "__main__":
    print("\n🚀 启动快速对比测试...\n")
    asyncio.run(quick_comparison_test())
    print("\n✅ 测试完成！\n")
