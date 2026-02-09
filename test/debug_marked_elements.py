"""
Debug: Check if pagination buttons are included in marked elements
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.executor import Executor


async def main():
    executor = Executor()

    try:
        await executor.start()
        print("[OK] Browser started\n")

        # Visit test website
        url = "https://books.toscrape.com/index.html"
        print(f"[INFO] Visiting: {url}")
        await executor.goto(url)
        await executor.wait_for_stable(2000)

        # Mark page elements
        print("\n[INFO] Marking page elements...")

        # Listen to console messages
        console_messages = []
        def handle_console(msg):
            console_messages.append(msg.text)

        executor._page.on("console", handle_console)

        dom_result = await executor.mark_page_elements()
        elements = dom_result.get('elements', [])
        print(f"Marked {len(elements)} elements")

        # Show console messages (skip to avoid encoding issues)
        debug_messages = [msg for msg in console_messages if 'DEBUG' in msg or 'pagination' in msg.lower()]
        print(f"\n[DEBUG] Found {len(debug_messages)} debug messages (skipping display due to encoding)")

        # Search for elements containing "next"
        print("\n[INFO] Searching for 'next' elements:")

        # Print all elements
        print("\nAll elements:")
        for i, elem in enumerate(elements, 1):
            text = (elem.get('text', '') or '')[:30]
            href = (elem.get('attributes', {}).get('href', '') or '')[:50]
            elem_id = elem.get('id', '')
            priority = elem.get('priority', 0)
            print(f"  {i}. ID={elem_id}, priority={priority}, text='{text}', href='{href}'")

        next_elements = []
        for elem in elements:
            text = (elem.get('text', '') or '').lower()
            href = (elem.get('attributes', {}).get('href', '') or '').lower()
            if 'next' in text or 'next' in href:
                next_elements.append(elem)

        if next_elements:
            print(f"\n[OK] Found {len(next_elements)} elements containing 'next':")
            for i, elem in enumerate(next_elements, 1):
                print(f"\n  {i}. ID: {elem.get('id')}")
                print(f"     Text: '{elem.get('text')}'")
                print(f"     Tag: {elem.get('tagName')}")
                print(f"     Href: {elem.get('attributes', {}).get('href', '')}")
        else:
            print("\n[ERROR] No elements containing 'next' found")

        # Show element ID range
        ids = [elem.get('id', '') for elem in elements if elem.get('id')]
        if ids:
            print(f"\n[INFO] Element ID range:")
            print(f"   Min: {min(ids)}")
            print(f"   Max: {max(ids)}")
            print(f"   Total: {len(ids)}")

    except Exception as e:
        print(f"\n[ERROR] Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
