"""
Simple pagination test - books.toscrape.com
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor


async def test_pagination():
    """Test pagination on books.toscrape.com"""

    executor = Executor()

    try:
        await executor.start()
        print("[OK] Browser started")

        # Visit test website
        url = "https://books.toscrape.com/index.html"
        print(f"\n[INFO] Visiting: {url}")
        await executor.goto(url)
        await executor.wait_for_stable(2000)

        # Mark elements
        print("\n[INFO] Marking page elements...")
        dom_result = await executor.mark_page_elements()
        elements = dom_result.get('elements', [])
        print(f"[OK] Marked {len(elements)} elements")

        # Find next button
        next_elements = [e for e in elements if 'next' in (e.get('text', '') or '').lower()]
        if next_elements:
            print(f"\n[OK] Found {len(next_elements)} next button(s):")
            for elem in next_elements:
                print(f"  - ID: {elem.get('id')}")
                print(f"    Text: {elem.get('text')}")
                print(f"    Href: {elem.get('attributes', {}).get('href', '')}")
        else:
            print("\n[ERROR] No next button found")
            return

        # Click next button
        next_id = next_elements[0].get('id')
        print(f"\n[INFO] Clicking next button: {next_id}")
        await executor.click_element_by_id(next_id)
        await executor.wait_for_stable(2000)

        # Check if we're on page 2
        current_url = executor._page.url
        print(f"\n[OK] Current URL: {current_url}")

        if 'page-2' in current_url:
            print("[SUCCESS] Successfully navigated to page 2!")
        else:
            print("[ERROR] Failed to navigate to page 2")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Pagination Test - books.toscrape.com")
    print("=" * 60)

    asyncio.run(test_pagination())
