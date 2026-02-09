"""
Find the actual next button in the DOM
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

        url = "https://books.toscrape.com/index.html"
        print(f"[INFO] Visiting: {url}")
        await executor.goto(url)
        await executor.wait_for_stable(2000)

        # Find all elements containing "next"
        result = await executor._page.evaluate("""
            () => {
                const allElements = document.querySelectorAll('*');
                const nextElements = [];

                for (const elem of allElements) {
                    const text = (elem.textContent || '').toLowerCase().trim();
                    const href = (elem.href || '').toLowerCase();
                    const className = (elem.className || '').toLowerCase();

                    if (text.includes('next') || href.includes('next') || className.includes('next')) {
                        nextElements.push({
                            tag: elem.tagName,
                            text: elem.textContent ? elem.textContent.substring(0, 50) : '',
                            href: elem.href || '',
                            className: elem.className || '',
                            id: elem.id || '',
                            outerHTML: elem.outerHTML.substring(0, 200)
                        });
                    }
                }

                return nextElements;
            }
        """)

        print(f"\n[INFO] Found {len(result)} elements containing 'next':\n")
        for i, elem in enumerate(result, 1):
            print(f"{i}. Tag: {elem['tag']}")
            print(f"   Text: {elem['text']}")
            print(f"   Href: {elem['href']}")
            print(f"   Class: {elem['className']}")
            print(f"   HTML: {elem['outerHTML']}")
            print()

    except Exception as e:
        print(f"\n[ERROR] Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
