"""
测试：先翻页再提取（选项B）
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.planner import Planner
from backend.output_store import OutputStore
from backend.extraction_engine import ExtractionEngine


async def test_pagination_then_extract():
    """测试：先翻到第二页，再从第二页提取数据"""

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

        # 测试任务：先翻到第二页，再提取
        task = "访问https://books.toscrape.com/index.html，翻到第二页，从第二页提取2本书的名称和价格"

        print("\n" + "=" * 60)
        print("Testing: Paginate THEN Extract (Option B)")
        print("=" * 60)
        print(f"\nTask: {task}\n")

        # 运行提取（启用反思机制）
        result = await engine.run_extraction(
            task=task,
            max_items=2,
            strategy={"list_only": True},
            use_omniparser=False,
            use_reflection=True,
        )

        print("\n" + "=" * 60)
        print("Extraction Result")
        print("=" * 60)
        print(f"Status: {result['status']}")
        print(f"Items extracted: {result['items_extracted']}")
        print(f"Target count: {result['target_count']}")

        print("\nProgress:")
        for step in result['progress']:
            stage = step.get('stage', 'unknown')
            status = step.get('status', 'unknown')
            print(f"  - {stage}: {status}")
            if stage == 'pre_pagination':
                print(f"    Target page: {step.get('target_page', 'N/A')}")
                print(f"    Final URL: {step.get('final_url', 'N/A')}")
            if 'items' in step:
                print(f"    Items: {step['items']}")
            if 'url' in step:
                print(f"    URL: {step['url']}")

        print("\nExtracted Items:")
        for i, item in enumerate(result.get('items', []), 1):
            print(f"\n  Item {i}:")
            for key, value in item.items():
                if not key.startswith('_'):
                    print(f"    {key}: {value}")

        # 检查最终URL
        current_url = await executor.get_url()
        print(f"\nFinal URL: {current_url}")

        # 验证结果
        if 'page-2' in current_url or 'page=2' in current_url:
            print("\n[SUCCESS] ✓ Successfully navigated to page 2!")

            # 检查提取的书籍是否来自第二页
            # 第一页的第一本书是 "A Light in the Attic"
            # 第二页的第一本书应该不同
            first_item_title = result['items'][0].get('title', '') if result['items'] else ''
            if first_item_title and first_item_title != "A Light in the Attic":
                print(f"[SUCCESS] ✓ Extracted data from page 2! First book: {first_item_title}")
            else:
                print(f"[WARNING] First book is '{first_item_title}', might be from page 1")
        else:
            print(f"\n[FAILED] ✗ Not on page 2. Final URL: {current_url}")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(test_pagination_then_extract())
