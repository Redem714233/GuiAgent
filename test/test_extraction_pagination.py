"""
Test complete extraction flow with pagination
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.vlm_service import VLMService
from backend.planner import Planner
from backend.output_store import OutputStore
from backend.extraction_engine import ExtractionEngine


async def test_extraction_with_pagination():
    """Test complete extraction flow with pagination"""

    executor = Executor()
    vlm = VLMService()
    planner = Planner(vlm)
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

        # Test task with pagination
        task = "访问https://books.toscrape.com/index.html，翻到第二页，提取2本书的名称和价格"

        print("\n" + "=" * 60)
        print("Testing extraction with pagination")
        print("=" * 60)
        print(f"\nTask: {task}\n")

        # Run extraction
        result = await engine.run_extraction(
            task=task,
            max_items=2,
            strategy={"list_only": True},
            use_omniparser=False
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

        # Check if we're on page 2
        current_url = await executor.get_url()
        print(f"\nFinal URL: {current_url}")

        if 'page-2' in current_url or 'page=2' in current_url:
            print("\n[SUCCESS] Successfully navigated to page 2!")
        else:
            print("\n[WARNING] Not on page 2, might have failed to paginate")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(test_extraction_with_pagination())
