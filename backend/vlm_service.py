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
            "You decide the best action using the image as the primary source. "
            "Elements are optional hints and may be incomplete or noisy. "
            "Use exactly one tool from: click, type, press, wait, copy, goto, scroll. "
            "Return JSON with keys: 'tool', and optionally 'id', 'point', 'text', 'key', 'ms', 'url', 'scroll'. "
            "- 'id' must be copied from the provided elements list; NEVER invent an id. "
            "- 'point' is [x, y] normalized to [0,1] in image coordinates. "
            "- 'text' is the exact input for type. "
            "- 'key' is for press (e.g., 'Enter'). "
            "- 'ms' is for wait in milliseconds. "
            "- 'url' is for goto (must include scheme like https://). "
            "- 'scroll' is delta-y pixels; positive to scroll down. "
            "Prefer point over id unless you are confident an element id precisely matches the target. "
            "If you can directly open a target site, you may return tool='goto' with 'url'. "
            "If you cannot find a reliable element id, return a point. "
            "If the step says type/input/输入/enter/回车, you MUST return tool='type' with 'text', "
            "and set 'key' to 'Enter' when it says press Enter/回车. "
            "Do not return tool='click' for typing steps. "
            "Prefer image-first decisions; ignore elements if they conflict with the image. "
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
    ) -> tuple[Dict[str, Any], str]:
        prompt = (
            "Extract structured data from the current page image for automation tasks. "
            "Return JSON only. Be precise and extract all visible relevant information.\n\n"
            "If mode is 'list', return:\n"
            "{\n"
            "  'items': [\n"
            "    {'title': str, 'url': str, 'summary': str, 'time': str, 'author': str, 'rating': str, 'votes': str, ...},\n"
            "    ...\n"
            "  ],\n"
            "  'next': 'scroll' | 'next_page' | 'stop'\n"
            "}\n\n"
            "Field extraction rules (extract ONLY what is CLEARLY VISIBLE):\n"
            "- title: Main title/headline (required)\n"
            "- url: Link URL (omit if not visible as text)\n"
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
            "- For URLs: Only include if you can see the actual URL text\n"
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
