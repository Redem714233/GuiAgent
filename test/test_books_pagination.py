"""
完整测试翻页功能 - books.toscrape.com

测试完整的提取流程，包括翻页
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.extraction_engine import ExtractionEngine
from backend.planner import Planner
from backend.output_store import OutputStore


async def main():
    # 初始化
    executor = Executor()
    planner = Planner()
    output_store = OutputStore()
    engine = ExtractionEngine(
        executor=executor,
        parser_service=None,
        planner=planner,
        output_store=output_store,
        data_dir="./data"
    )

    try:
        await executor.start()
        print("✅ 浏览器已启动\n")

        # 测试任务：明确要求翻页
        task = "访问 https://books.toscrape.com/index.html，提取前25本书的名称和价格，然后翻到第二页，再提取2本书"

        print(f"📝 任务: {task}")
        print(f"🎯 目标: 提取 27 本书（第一页25本 + 第二页2本）\n")

        # 运行提取
        result = await engine.run_extraction(
            task=task,
            max_items=27,  # 需要翻页才能完成
            strategy={
                "max_scrolls": 5,  # 允许最多5次翻页
                "list_only": True
            }
        )

        # 显示结果
        print("\n" + "=" * 60)
        print("📊 提取结果")
        print("=" * 60)

        print(f"\n状态: {result['status']}")
        print(f"已提取: {result['items_extracted']} / {result['target_count']} 本书")

        if result['file_path']:
            print(f"文件路径: {result['file_path']}")

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
            print(f"\n📋 数据预览（前5本和最后2本）:")
            for idx, item in enumerate(result['items'][:5], 1):
                print(f"  {idx}. {item.get('title', 'N/A')} - {item.get('price', 'N/A')}")

            if len(result['items']) > 5:
                print("  ...")
                for idx, item in enumerate(result['items'][-2:], len(result['items']) - 1):
                    print(f"  {idx}. {item.get('title', 'N/A')} - {item.get('price', 'N/A')}")

        # 判断是否成功翻页
        print("\n" + "=" * 60)
        if result['items_extracted'] > 20:
            print("✅ 翻页功能正常！成功提取了超过20本书")
        else:
            print(f"⚠️  可能未翻页，只提取了 {result['items_extracted']} 本书")
            print("提示：检查 VLM 是否返回了 next='next_page'")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print("\n关闭浏览器...")
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
