"""
Test VLM-based pagination detection
"""

import asyncio
import os
import sys
import base64
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor
from backend.vlm_service import VLMService


async def test_vlm_pagination():
    """Test if VLM can find the next button"""

    executor = Executor()
    vlm = VLMService()

    try:
        await executor.start()
        print("[OK] Browser started")

        # Visit test website
        url = "https://books.toscrape.com/index.html"
        print(f"\n[INFO] Visiting: {url}")
        await executor.goto(url)
        await executor.wait_for_stable(2000)

        # Mark elements and take screenshot
        print("\n[INFO] Marking page elements...")
        dom_result = await executor.mark_page_elements()
        elements = dom_result.get('elements', [])
        screenshot_base64 = dom_result.get('screenshot', '')

        print(f"[OK] Marked {len(elements)} elements")

        # Check if next button is in the list
        next_elements = [e for e in elements if 'next' in (e.get('text', '') or '').lower()]
        if next_elements:
            print(f"\n[OK] Next button found in elements:")
            for elem in next_elements:
                print(f"  - ID: {elem.get('id')}")
                print(f"    Text: {elem.get('text')}")
                print(f"    Priority: {elem.get('priority')}")
        else:
            print("\n[ERROR] Next button NOT found in elements")
            return

        # Ask VLM to find the next button
        print("\n[INFO] Asking VLM to find the next button...")

        task = "Click the 'next' button to go to the next page"

        response, raw = vlm.decide(
            task=task,
            annotated_image_base64=screenshot_base64,
            elements=elements
        )

        print(f"\n[VLM Response]")
        print(f"  Tool: {response.get('tool')}")
        print(f"  Element ID: {response.get('id')}")
        if 'point' in response:
            print(f"  Point: {response.get('point')}")

        # Check if VLM found the correct element
        vlm_element_id = response.get('id')
        if vlm_element_id:
            # VLM returns just the number (e.g., 37), we need to match with skyvern-37
            expected_id_num = int(next_elements[0].get('id').replace('skyvern-', ''))
            if vlm_element_id == expected_id_num:
                print(f"\n[SUCCESS] VLM correctly identified the next button!")
            else:
                print(f"\n[WARNING] VLM returned different element ID: {vlm_element_id}")
                print(f"  Expected: {expected_id_num}")
        else:
            print(f"\n[WARNING] VLM did not return an element ID")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("VLM Pagination Detection Test")
    print("=" * 60)

    asyncio.run(test_vlm_pagination())
