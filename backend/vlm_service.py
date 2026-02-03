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
    ) -> tuple[Dict[str, Any], str]:
        prompt = (
            "You decide the best action using the image as the primary source. "
            "Elements are optional hints and may be incomplete or noisy. "
            "Use exactly one tool from: click, type, press, wait, copy, goto. "
            "Return JSON with keys: 'tool', and optionally 'id', 'point', 'text', 'key', 'ms', 'url'. "
            "- 'id' must be copied from the provided elements list; NEVER invent an id. "
            "- 'point' is [x, y] normalized to [0,1] in image coordinates. "
            "- 'text' is the exact input for type. "
            "- 'key' is for press (e.g., 'Enter'). "
            "- 'ms' is for wait in milliseconds. "
            "- 'url' is for goto (must include scheme like https://). "
            "Prefer point over id unless you are confident an element id precisely matches the target. "
            "If you can directly open a target site, you may return tool='goto' with 'url'. "
            "If you cannot find a reliable element id, return a point. "
            "If the step says type/input/输入/enter/回车, you MUST return tool='type' with 'text', "
            "and set 'key' to 'Enter' when it says press Enter/回车. "
            "Do not return tool='click' for typing steps. "
            "Prefer image-first decisions; ignore elements if they conflict with the image. "
            "If unsure, return {'tool': 'click', 'point': [0.5, 0.5]}."
        )
        user_payload = {"task": task, "elements": elements, "image_size": image_size}
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

