"""
测试 Extract Data 模式集成反思机制
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.planner import Planner
from backend.output_store import OutputStore
from backend.extraction_engine import ExtractionEngine


async def test_extraction_with_reflection():
    """测试 Extract Data 模式使用反思机制翻页"""

    executor = Executor()
    planner = Planner()
    output_store = OutputStore()
    data_dir = "data"

    engine = ExtractionEngine(
        executor=executor,
        parser_service=None,
        planner=planner,
        output_store=output_store,
        data_dir=data_dir
    )

    try:
        await executor.start()
        print("[OK] Browser started")

        # 测试任务：翻页并提取数据
        task = "访问https://books.toscrape.com/index.html，翻到第二页，提取2本书的名称和价格"

        print("\n" + "=" * 60)
        print("Testing Extract Data with Reflection Mechanism")
        print("=" * 60)
        print(f"\nTask: {task}\n")

        # 运行提取（启用反思机制）
        result = await engine.run_extraction(
            task=task,
            max_items=2,
            strategy={"list_only": True},
            use_omniparser=False,
            use_reflection=True,  # 启用反思机制
        )

        print("\n" + "=" * 60)
        print("Extraction Result")
        print("=" * 60)
        print(f"Status: {result['status']}")
        print(f"Items extracted: {result['items_extracted']}")
        print(f"Target count: {result['target_count']}")
        print(f"Errors: {len(result['errors'])}")

        if result['errors']:
            print("\nErrors:")
            for error in result['errors']:
                print(f"  - {error}")

        print("\nProgress:")
        for step in result['progress']:
            stage = step.get('stage', 'unknown')
            status = step.get('status', 'unknown')
            print(f"  - {stage}: {status}")
            if 'items' in step:
                print(f"    Items: {step['items']}")
            if 'url' in step:
                print(f"    URL: {step['url']}")

        print("\nExtracted Items:")
        for i, item in enumerate(result.get('items', []), 1):
            print(f"\n  Item {i}:")
            for key, value in item.items():
                if not key.startswith('_'):  # Skip internal fields
                    print(f"    {key}: {value}")

        # 检查是否在第二页
        current_url = await executor.get_url()
        print(f"\nFinal URL: {current_url}")

        if 'page-2' in current_url or 'page=2' in current_url:
            print("\n[SUCCESS] ✓ Successfully navigated to page 2 with reflection mechanism!")
            print("Reflection mechanism verified pagination was successful.")
        else:
            print(f"\n[WARNING] Not on page 2. Final URL: {current_url}")
            print("Reflection mechanism may have detected pagination failure.")

        # 检查是否有文件保存
        if result.get('file_path'):
            print(f"\n[OK] Data saved to: {result['file_path']}")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


async def test_extraction_without_reflection():
    """测试 Extract Data 模式不使用反思机制（对比）"""

    executor = Executor()
    planner = Planner()
    output_store = OutputStore()
    data_dir = "data"

    engine = ExtractionEngine(
        executor=executor,
        parser_service=None,
        planner=planner,
        output_store=output_store,
        data_dir=data_dir
    )

    try:
        await executor.start()
        print("[OK] Browser started")

        # 测试任务：翻页并提取数据
        task = "访问https://books.toscrape.com/index.html，翻到第二页，提取2本书的名称和价格"

        print("\n" + "=" * 60)
        print("Testing Extract Data WITHOUT Reflection (for comparison)")
        print("=" * 60)
        print(f"\nTask: {task}\n")

        # 运行提取（不使用反思机制）
        result = await engine.run_extraction(
            task=task,
            max_items=2,
            strategy={"list_only": True},
            use_omniparser=False,
            use_reflection=False,  # 禁用反思机制
        )

        print("\n" + "=" * 60)
        print("Extraction Result (Without Reflection)")
        print("=" * 60)
        print(f"Status: {result['status']}")
        print(f"Items extracted: {result['items_extracted']}")

        # 检查是否在第二页
        current_url = await executor.get_url()
        print(f"\nFinal URL: {current_url}")

        if 'page-2' in current_url or 'page=2' in current_url:
            print("\n[OK] Navigated to page 2 (without reflection)")
        else:
            print(f"\n[WARNING] Not on page 2 (without reflection)")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    print("=" * 60)
    print("Test 1: Extract Data WITH Reflection Mechanism")
    print("=" * 60)
    asyncio.run(test_extraction_with_reflection())

    print("\n\n")
    print("=" * 60)
    print("Test 2: Extract Data WITHOUT Reflection (for comparison)")
    print("=" * 60)
    asyncio.run(test_extraction_without_reflection())
