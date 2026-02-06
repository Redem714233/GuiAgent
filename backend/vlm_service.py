from __future__ import annotations

import json
import os
from typing import Any, Dict

from openai import OpenAI


class VLMService:
    def __init__(self) -> None:
        base_url = os.getenv("VLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        api_key = os.getenv("VLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("VLM_MODEL", "qwen3-vl-flash")
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def decide(
        self,
        *,
        task: str,
        elements: list[dict],
        annotated_image_base64: str,
        image_size: tuple[int, int] | None = None,
        plan_context: str | None = None,
    ) -> tuple[Dict[str, Any], str]:
        prompt = (
            "You decide the best action using the annotated image as the primary source. "
            "The image shows interactive elements with RED BOXES and ID NUMBERS (e.g., '48', '52'). "
            "Elements list provides additional context (text, type) but may be incomplete. "
            "\n\n"
            "IMPORTANT: When you see a red box with an ID number in the image, use that ID directly. "
            "For example, if you see a button with ID '48' in the image, return {'tool': 'click', 'id': 48}. "
            "\n\n"
            "Use exactly one tool from: click, type, press, wait, copy, goto, scroll. "
            "Return JSON with keys: 'tool', and optionally 'id', 'point', 'text', 'key', 'ms', 'url', 'scroll'. "
            "- 'id' is the NUMBER shown in the red box on the image (e.g., 48, not 'skyvern-48'). "
            "- 'point' is [x, y] normalized to [0,1] in image coordinates (use only if no ID is visible). "
            "- 'text' is the exact input for type. "
            "- 'key' is for press (e.g., 'Enter'). "
            "- 'ms' is for wait in milliseconds. "
            "- 'url' is for goto (must include scheme like https://). "
            "- 'scroll' is delta-y pixels; positive to scroll down. "
            "\n\n"
            "Decision priority: "
            "1. If you see a red box with ID in the image → use 'id' "
            "2. If no clear ID but you see the target → use 'point' "
            "3. If you can directly open a target site → use 'goto' with 'url' "
            "\n\n"
            "For typing tasks: "
            "- If the step says type/input/输入/enter/回车, you MUST return tool='type' with 'text' "
            "- Set 'key' to 'Enter' when it says press Enter/回车 "
            "- Do not return tool='click' for typing steps "
            "\n\n"
            "If unsure, return {'tool': 'click', 'point': [0.5, 0.5]}."
        )
        payload: Dict[str, Any] = {"task": task, "elements": elements, "image_size": image_size}
        if plan_context:
            payload["plan_context"] = plan_context
        data_url = f"data:image/png;base64,{annotated_image_base64}"

        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    ],
                },
            ],
            temperature=0.1,
        )
        content = resp.choices[0].message.content or ""
        return self._extract_json(content), content

    def _extract_json(self, text: str) -> Dict[str, Any]:
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except Exception:
            return {"id": None}

    def should_finish(
        self,
        *,
        task: str,
        annotated_image_base64: str,
        current_url: str,
    ) -> tuple[bool, dict]:
        prompt = (
            "Determine if the task is completed based on the current URL and screenshot. "
            "Return JSON with key 'done' as true/false. "
            "Only return true when the user request has fully completed, "
            "especially when the target website has been opened."
        )
        user_payload = {"task": task, "url": current_url}
        data_url = f"data:image/png;base64,{annotated_image_base64}"

        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)},
                    ],
                },
            ],
            temperature=0.1,
        )
        content = resp.choices[0].message.content or ""
        parsed = self._extract_json(content)
        return bool(parsed.get("done")), {
            "request": {"task": task, "url": current_url, "image": True},
            "response_raw": content,
            "response": parsed,
        }

    def plan_steps(
        self,
        *,
        task: str,
        annotated_image_base64: str | None = None,
        max_steps: int = 6,
    ) -> tuple[list[str], dict]:
        prompt = (
            "Decompose the user task into a short, ordered list of concrete UI steps. "
            "Each step must be a single action the agent can attempt on the current page. "
            "Prefer direct navigation when a target site is clear (e.g., 'goto https://example.com'). "
            "Only include search + typing steps when a direct URL is unknown. "
            "Keep steps minimal and explicit. "
            "Return JSON with key 'steps' as a list of strings. "
            "Do not include confirmations or meta commentary."
        )
        user_payload = {"task": task, "max_steps": max_steps}

        content_parts = [{"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)}]
        if annotated_image_base64:
            data_url = f"data:image/png;base64,{annotated_image_base64}"
            content_parts.insert(0, {"type": "image_url", "image_url": {"url": data_url}})

        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": content_parts},
            ],
            temperature=0.1,
        )
        content = resp.choices[0].message.content or ""
        parsed = self._extract_json(content)
        steps = parsed.get("steps") if isinstance(parsed, dict) else None
        if isinstance(steps, list):
            return [str(s) for s in steps if str(s).strip()][: max_steps or 6], {
                "request": {"task": task, "max_steps": max_steps, "image": bool(annotated_image_base64)},
                "response_raw": content,
                "response": parsed,
            }
        return [task], {
            "request": {"task": task, "max_steps": max_steps, "image": bool(annotated_image_base64)},
            "response_raw": content,
            "response": parsed,
        }

    def extract_task_spec(self, *, task: str) -> tuple[Dict[str, Any], str]:
        prompt = (
            "Convert the user request into a flexible task specification JSON. "
            "Analyze the task carefully and extract all relevant parameters.\n\n"
            "Return JSON with keys:\n"
            "- target_site (str, optional): The FULL target URL including path (e.g., 'https://github.com/trending', 'https://movie.douban.com/top250', 'https://news.sina.com.cn')\n"
            "  IMPORTANT: Include the full path, not just the domain!\n"
            "- count (int): Number of items to extract (default 10)\n"
            "- fields (list[str]): Data fields to extract, choose from:\n"
            "  ['title', 'url', 'content', 'summary', 'time', 'author', 'source', 'tags', 'likes', 'comments_count', 'views']\n"
            "- filters (dict, optional): Filtering criteria (e.g., {'date': 'today', 'category': 'tech'})\n"
            "- output (dict, optional): Output configuration\n"
            "  - format: 'excel' (default)\n"
            "  - path: filename if user specifies (e.g., 'news_data.xlsx')\n"
            "- strategy (dict, optional): Extraction strategy\n"
            "  - list_then_detail (bool): Whether to extract list first, then visit detail pages\n"
            "  - open_detail: 'same_tab' | 'new_tab' (how to open detail pages)\n"
            "  - scroll_strategy: 'auto' | 'manual'\n\n"
            "Examples:\n"
            "Task: '进入新浪新闻，复制今天的前10条新闻标题和内容'\n"
            "{\n"
            "  'target_site': 'https://news.sina.com.cn',\n"
            "  'count': 10,\n"
            "  'fields': ['title', 'url', 'summary', 'time', 'source'],\n"
            "  'filters': {'date': 'today'},\n"
            "  'output': {'format': 'excel'},\n"
            "  'strategy': {'list_then_detail': true, 'open_detail': 'same_tab'}\n"
            "}\n\n"
            "Task: '打开GitHub Trending，提取前5个项目'\n"
            "{\n"
            "  'target_site': 'https://github.com/trending',\n"
            "  'count': 5,\n"
            "  'fields': ['title', 'url', 'summary', 'author'],\n"
            "  'output': {'format': 'excel'},\n"
            "  'strategy': {'list_then_detail': false}\n"
            "}\n\n"
            "Task: '打开豆瓣电影Top250，提取前10部电影'\n"
            "{\n"
            "  'target_site': 'https://movie.douban.com/top250',\n"
            "  'count': 10,\n"
            "  'fields': ['title', 'url', 'summary'],\n"
            "  'output': {'format': 'excel'},\n"
            "  'strategy': {'list_then_detail': false}\n"
            "}\n\n"
            "Do not add commentary. Return only the JSON."
        )
        user_payload = {"task": task}
        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.1,
        )
        content = resp.choices[0].message.content or ""
        return self._extract_json(content), content

    def extract_from_page(
        self,
        *,
        task: str,
        mode: str,
        annotated_image_base64: str,
        current_url: str,
        elements: list = None,
    ) -> tuple[Dict[str, Any], str]:
        prompt = (
            "Extract structured data from the current page image for automation tasks. "
            "Return JSON only. Be precise and extract all visible relevant information.\n\n"
            "If mode is 'list', return:\n"
            "{\n"
            "  'items': [\n"
            "    {\n"
            "      'title': str,\n"
            "      'url': str,\n"
            "      'element_id': str,\n"
            "      'confidence': float,  // 0.0-1.0, confidence in element_id selection\n"
            "      'summary': str,\n"
            "      'time': str,\n"
            "      'author': str,\n"
            "      'rating': str,\n"
            "      'votes': str,\n"
            "      ...\n"
            "    },\n"
            "    ...\n"
            "  ],\n"
            "  'next': 'scroll' | 'next_page' | 'stop'\n"
            "}\n\n"
            "Field extraction rules (extract ONLY what is CLEARLY VISIBLE):\n"
            "- title: Main title/headline (required)\n"
            "- url: Link URL (ONLY if visible as text in the image)\n"
            "- element_id: Unique element identifier from the provided elements list (REQUIRED for clickable items)\n"
            "  * CRITICAL: Only select element IDs from the provided 'Available interactive elements' list below\n"
            "  * DO NOT imagine or create new element IDs\n"
            "  * Match the visible content (title, text) with the elements list\n"
            "  * Return the element_id (e.g., 'skyvern-48') that corresponds to the MAIN/PRIMARY clickable item\n"
            "  * Selection criteria (in priority order):\n"
            "    1. Element text EXACTLY matches or CONTAINS the item title\n"
            "    2. Element is an <a> tag (not <span>, <div>, or <button>)\n"
            "    3. Element href points to the main content (not metadata pages)\n"
            "    4. Element is LARGER in size (prefer width > 200px, height > 40px)\n"
            "    5. Element is HIGHER in visual hierarchy (closer to top of the item)\n"
            "  * AVOID selecting elements that are:\n"
            "    - Metadata links: 'stars', 'stargazers', 'forks', 'watchers', 'issues', 'pull requests'\n"
            "    - Social links: 'likes', 'comments', 'shares', 'follow', 'subscribe'\n"
            "    - Statistics: 'views', 'ratings', 'reviews', 'votes'\n"
            "    - Navigation: 'settings', 'profile', 'logout', 'help'\n"
            "    - Small elements (width < 100px or height < 30px) unless it's the only option\n"
            "  * If multiple elements match, choose the one with the LONGEST matching text\n"
            "  * Example: For 'bytedance/UI-TARS-desktop', if you see:\n"
            "    - skyvern-48 (a): \"bytedance/UI-TARS-desktop\" [href: /bytedance/UI-TARS-desktop] ✓ CORRECT\n"
            "    - skyvern-102 (a): \"233 stars\" [href: /bytedance/UI-TARS-desktop/stargazers] ✗ WRONG (metadata)\n"
            "    Choose skyvern-48 because it's the main title link, not metadata\n"
            "- confidence: Confidence score (0.0-1.0) for the element_id selection (REQUIRED if element_id is provided)\n"
            "  * 1.0 = Perfect match (element text exactly matches title, correct tag type, appropriate size)\n"
            "  * 0.8-0.9 = Good match (element text contains title, correct tag, good size)\n"
            "  * 0.6-0.7 = Acceptable match (partial text match, may be smaller or different tag)\n"
            "  * 0.4-0.5 = Uncertain match (weak text match, small element, or metadata concern)\n"
            "  * < 0.4 = Poor match (should avoid using this element)\n"
            "  * Use this to indicate your confidence in the element selection\n"
            "  * If confidence < 0.6, consider if there's a better element in the list\n"
            "- summary: Brief description/plot summary (NOT metadata like director, cast, year)\n"
            "- time: Publication date, upload time, or release year\n"
            "- author: Author name, director name, or channel name\n"
            "- rating: Numeric rating/score (e.g., '9.7', '4.5/5')\n"
            "- votes: Number of ratings/reviews (e.g., '3254985人评价', '1.2M views')\n"
            "- source: News source or platform name\n"
            "- tags: Category tags or genres\n\n"
            "Content-specific extraction:\n"
            "1. Movies/TV (豆瓣电影, IMDb, etc.):\n"
            "   - title: Movie title\n"
            "   - author: Director name ONLY (not full 'Director: Name' text)\n"
            "   - time: Release year (e.g., '1994')\n"
            "   - rating: Score number (e.g., '9.7')\n"
            "   - votes: Rating count (e.g., '3254985人评价')\n"
            "   - summary: Brief plot description ONLY (exclude director/cast/year info)\n"
            "   - tags: Genres (e.g., ['剧情', '犯罪'])\n\n"
            "2. News articles:\n"
            "   - title: Article headline\n"
            "   - author: Author or journalist name\n"
            "   - time: Publication date/time\n"
            "   - source: News outlet name\n"
            "   - summary: Article preview/excerpt\n\n"
            "3. Videos (YouTube, Bilibili, etc.):\n"
            "   - title: Video title\n"
            "   - author: Channel/uploader name\n"
            "   - time: Upload date\n"
            "   - votes: View count\n"
            "   - summary: Video description\n\n"
            "4. GitHub repositories:\n"
            "   - title: Repository name (e.g., 'owner/repo')\n"
            "   - author: Owner/organization name\n"
            "   - summary: Repository description\n"
            "   - votes: Total stars (e.g., '3,399 stars' or '3399')\n"
            "   - tags: Programming language ONLY (e.g., 'Python', 'TypeScript')\n"
            "   - DO NOT put 'stars today', 'GitHub Trending', or other metadata in tags\n"
            "   - DO NOT mix different fields together\n\n"
            "CRITICAL rules:\n"
            "- Only extract data that is CLEARLY VISIBLE in the image\n"
            "- DO NOT guess, infer, or fabricate any information\n"
            "- If a field is not visible, OMIT it entirely (no null/empty values)\n"
            "- For URLs: ABSOLUTELY FORBIDDEN to fabricate URLs. Only include if you can see the EXACT URL text in the image.\n"
            "  * DO NOT generate URLs based on patterns (e.g., doc-imkzueyv9876545.shtml)\n"
            "  * DO NOT infer URLs from titles or other content\n"
            "  * If URL is not visible as text, you MUST omit the 'url' field and provide 'element_id' instead\n"
            "- For element_id: REQUIRED if item is clickable - match the visible content with the provided elements list\n"
            "  * Look at the elements list provided in the context\n"
            "  * Find the element whose text matches the title or clickable content\n"
            "  * Return the element's unique_id (e.g., 'skyvern-48')\n"
            "  * Be precise in matching - use the text content to identify the correct element\n"
            "- Separate metadata from content (don't mix director/cast into summary)\n"
            "- Extract numbers without units when possible (rating: '9.7' not '★★★★☆ 9.7')\n"
            "- Each field should contain ONLY its designated data type (don't mix fields)\n"
            "- For tags: Extract as a simple string (e.g., 'Python') or omit if not applicable\n"
            "- Set 'next' to 'scroll' if more content might load below, 'next_page' if there's a next page button, 'stop' if at the end\n\n"
            "If mode is 'detail', return:\n"
            "{\n"
            "  'data': {\n"
            "    'title': str,\n"
            "    'content': str,\n"
            "    'author': str,\n"
            "    'time': str,\n"
            "    'rating': str,\n"
            "    'votes': str,\n"
            "    'tags': [str],\n"
            "    'comments_count': int,\n"
            "    'likes': int,\n"
            "    ...\n"
            "  }\n"
            "}\n"
            "- Extract detailed information from the current page\n"
            "- Follow the same field extraction rules as list mode\n"
            "- Include all relevant fields based on the page type\n\n"
            "Important:\n"
            "- Use current_url as base for resolving relative URLs\n"
            "- Extract text exactly as shown in the image\n"
            "- CRITICAL: Only extract data that is CLEARLY VISIBLE. DO NOT guess, infer, or fabricate information.\n"
            "- If a field is not visible, omit it entirely (don't use null or empty string)\n"
            "- Return empty items/data if nothing relevant is found"
        )
        user_payload = {"task": task, "mode": mode, "url": current_url}

        # Add elements list if provided
        if elements:
            # Build a simplified elements text for VLM
            elements_text = "Available interactive elements:\n"
            for elem in elements[:50]:  # Limit to first 50 elements to avoid token overflow
                elem_id = elem.get('id', '')
                tag = elem.get('tagName', '')
                text = elem.get('text', '')[:100]  # Limit text length
                href = elem.get('attributes', {}).get('href', '')

                elements_text += f"- {elem_id} ({tag}): \"{text}\""
                if href:
                    elements_text += f" [href: {href}]"
                elements_text += "\n"

            user_payload["elements"] = elements_text

        data_url = f"data:image/png;base64,{annotated_image_base64}"
        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)},
                    ],
                },
            ],
            temperature=0.1,
        )
        content = resp.choices[0].message.content or ""
        return self._extract_json(content), content

    def get_next_action(
        self,
        *,
        task: str,
        screenshot_base64: str,
        current_url: str,
        step_count: int,
        context: str = "",
    ) -> tuple[dict, str]:
        """
        获取下一步动作（用于 WebAgent）

        参考 AutoGLM 的提示词格式，返回动作字符串

        Args:
            task: 用户任务描述
            screenshot_base64: 当前页面截图（base64）
            current_url: 当前URL
            step_count: 当前步数
            context: 历史上下文（可选）

        Returns:
            ({"action": str, "thinking": str}, raw_response)
        """
        prompt = (
            "You are a web automation agent. Analyze the screenshot and decide the next action.\n\n"
            "Available actions:\n"
            "1. do(action=\"Tap\", element=[x, y]) - Click at relative coordinates (0-1000 range)\n"
            "   - x, y are relative coordinates from 0 to 1000\n"
            "   - Example: do(action=\"Tap\", element=[500, 300])\n\n"
            "2. do(action=\"Type\", text=\"...\") - Type text into focused input\n"
            "   - Example: do(action=\"Type\", text=\"search query\")\n\n"
            "3. do(action=\"Scroll\", direction=\"down\"|\"up\") - Scroll the page\n"
            "   - Example: do(action=\"Scroll\", direction=\"down\")\n\n"
            "4. do(action=\"Wait\", duration=\"N seconds\") - Wait for N seconds\n"
            "   - Example: do(action=\"Wait\", duration=\"2 seconds\")\n\n"
            "5. do(action=\"Extract\", fields=[\"field1\", \"field2\"]) - Extract data from current page\n"
            "   - Use this when you need to extract structured data\n"
            "   - Specify which fields to extract (e.g., [\"title\", \"rating\", \"author\"])\n"
            "   - Example: do(action=\"Extract\", fields=[\"title\", \"rating\"])\n\n"
            "6. finish(message=\"...\") - Complete the task with a message\n"
            "   - Example: finish(message=\"Task completed successfully\")\n\n"
            "IMPORTANT:\n"
            "- Use relative coordinates (0-1000) for Tap action\n"
            "- Use Extract action when the task asks to \"extract\", \"get\", \"collect\" data\n"
            "- Analyze the screenshot carefully before deciding\n"
            "- Return ONLY the action in the exact format shown above\n"
            "- Do NOT add any explanation or commentary\n\n"
            "Return JSON format:\n"
            "{\n"
            "  \"thinking\": \"Your analysis of the current state\",\n"
            "  \"action\": \"do(action=\\\"Tap\\\", element=[500, 300])\" or \"do(action=\\\"Extract\\\", fields=[\\\"title\\\", \\\"rating\\\"])\" or \"finish(message=\\\"...\\\")\"\n"
            "}\n"
        )

        user_payload = {
            "task": task,
            "current_url": current_url,
            "step": step_count,
        }
        if context:
            user_payload["context"] = context

        data_url = f"data:image/png;base64,{screenshot_base64}"

        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)},
                    ],
                },
            ],
            temperature=0.1,
        )

        content = resp.choices[0].message.content or ""
        return self._extract_json(content), content
