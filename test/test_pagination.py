"""
测试翻页功能

使用示例：
python test_pagination.py
"""

import requests
import json
import time

API_BASE = "http://127.0.0.1:8000"


def test_pagination_extraction():
    """测试自动翻页数据提取"""

    print("=" * 60)
    print("测试场景：豆瓣电影 Top250（需要翻页）")
    print("=" * 60)

    # 测试任务：提取豆瓣电影Top250的前30部电影
    # 豆瓣每页显示25部，需要翻页到第2页
    task = "访问豆瓣电影Top250，提取前30部电影的信息，包括标题、评分、导演、年份"

    print(f"\n📝 任务描述: {task}")
    print(f"🎯 目标数量: 30条（需要翻页）")
    print(f"⚙️  配置: 使用标注图=True, 仅列表模式=True")

    # 发送提取请求
    print("\n🚀 开始提取...")
    response = requests.post(
        f"{API_BASE}/run_extraction",
        json={
            "task": task,
            "max_items": 30,
            "strategy": {
                "list_only": True  # 仅列表模式，不进入详情页
            },
            "use_omniparser": True
        }
    )

    if response.status_code != 200:
        print(f"❌ 请求失败: {response.status_code}")
        print(response.text)
        return

    result = response.json()

    # 显示结果
    print("\n" + "=" * 60)
    print("📊 提取结果")
    print("=" * 60)

    print(f"\n状态: {result['status']}")
    print(f"已提取: {result['items_extracted']} / {result['target_count']} 条")

    if result['file_path']:
        print(f"文件路径: {result['file_path']}")
        print(f"下载链接: {API_BASE}/files/{result['file_path']}")

    if result['errors']:
        print(f"\n⚠️  错误信息:")
        for error in result['errors']:
            print(f"  - {error}")

    # 显示执行进度
    if result['progress']:
        print(f"\n⏱️  执行进度:")
        for prog in result['progress']:
            stage = prog.get('stage', 'unknown')
            status = prog.get('status', 'unknown')
            items = prog.get('items', 0)
            print(f"  - {stage}: {status} ({items} items)")

    # 显示数据预览
    if result['items']:
        print(f"\n📋 数据预览（前5条）:")
        for idx, item in enumerate(result['items'][:5], 1):
            print(f"\n  {idx}. {item.get('title', 'N/A')}")
            for key, value in item.items():
                if key != 'title':
                    print(f"     {key}: {value}")

    print("\n" + "=" * 60)

    # 判断是否成功翻页
    if result['items_extracted'] > 25:
        print("✅ 翻页功能正常！成功提取了超过25条数据（第一页只有25条）")
    elif result['items_extracted'] == 25:
        print("⚠️  可能未翻页，只提取了第一页的25条数据")
    else:
        print(f"ℹ️  提取了 {result['items_extracted']} 条数据")


def test_pagination_progress():
    """测试提取进度查询"""

    print("\n" + "=" * 60)
    print("测试场景：查询提取进度")
    print("=" * 60)

    # 查询进度
    response = requests.get(f"{API_BASE}/extraction_progress")

    if response.status_code != 200:
        print(f"❌ 请求失败: {response.status_code}")
        return

    progress_data = response.json()

    print(f"\n正在提取: {progress_data['is_extracting']}")

    if progress_data['progress']:
        print(f"\n当前进度:")
        for prog in progress_data['progress']:
            stage = prog.get('stage', 'unknown')
            status = prog.get('status', 'unknown')
            current_action = prog.get('current_action', '')
            processed = prog.get('processed', 0)
            total = prog.get('total', 0)

            print(f"  - {stage}: {status}")
            if current_action:
                print(f"    动作: {current_action}")
            if total > 0:
                print(f"    进度: {processed}/{total}")


def test_simple_pagination():
    """测试简单的翻页场景（新浪新闻）"""

    print("\n" + "=" * 60)
    print("测试场景：新浪新闻（简单翻页）")
    print("=" * 60)

    task = "访问新浪新闻首页，提取前15条新闻的标题和链接"

    print(f"\n📝 任务描述: {task}")
    print(f"🎯 目标数量: 15条")

    response = requests.post(
        f"{API_BASE}/run_extraction",
        json={
            "task": task,
            "max_items": 15,
            "strategy": {
                "list_only": True
            },
            "use_omniparser": False  # 不使用标注图，更快
        }
    )

    if response.status_code != 200:
        print(f"❌ 请求失败: {response.status_code}")
        return

    result = response.json()

    print(f"\n状态: {result['status']}")
    print(f"已提取: {result['items_extracted']} / {result['target_count']} 条")

    if result['items']:
        print(f"\n📋 提取的新闻:")
        for idx, item in enumerate(result['items'], 1):
            title = item.get('title', 'N/A')
            url = item.get('url', 'N/A')
            print(f"  {idx}. {title}")
            print(f"     {url}")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("GUIAgent 翻页功能测试")
    print("=" * 60)

    print("\n⚠️  注意事项:")
    print("1. 确保后端服务已启动 (python start_backend.py)")
    print("2. 确保浏览器可以正常访问目标网站")
    print("3. 测试过程可能需要1-3分钟")

    input("\n按回车键开始测试...")

    # 测试1: 简单翻页（新浪新闻）
    try:
        test_simple_pagination()
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")

    print("\n" + "-" * 60)
    input("\n按回车键继续下一个测试...")

    # 测试2: 复杂翻页（豆瓣电影）
    try:
        test_pagination_extraction()
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")

    print("\n" + "=" * 60)
    print("测试完成！")
    print("=" * 60)
