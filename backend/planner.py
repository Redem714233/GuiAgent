from __future__ import annotations

import json
import os
from typing import List, Optional

from openai import OpenAI

from backend.schemas import Element, PlanRequest, PlanResponse


class Planner:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")
        base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL")
        if base_url:
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=base_url)
        else:
            self.client = OpenAI()

    def _client_get(self) -> OpenAI:
        return self.client

    def plan(self, request: PlanRequest) -> PlanResponse:
        self._last_query = None
        elements = request.elements
        llm_choice = self._llm_select(request.task, elements)
        if llm_choice is not None:
            return PlanResponse(target_id=llm_choice, reason="llm", query=self._last_query)
        fallback_choice = self._rule_select(request.task, elements)
        return PlanResponse(target_id=fallback_choice, reason="rule")

    def _llm_select(self, task: str, elements: List[Element]) -> Optional[int]:
        if not elements:
            return None
        compact = [{"id": e.id, "content": e.content} for e in elements]
        system_prompt = (
            "You pick exactly one UI element id to accomplish the user task. "
            "Return JSON with keys 'id' and optional 'query'. "
            "If unsure, return {'id': null}."
        )
        user_payload = {"task": task, "elements": compact}
        try:
            resp = self._client_get().chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                temperature=0.1,
            )
        except Exception:
            resp = self._client_get().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                temperature=0.1,
            )
        content = resp.choices[0].message.content or ""
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                self._last_query = parsed.get("query")
                return parsed.get("id")
            return None
        except Exception:
            return None

    def _rule_select(self, task: str, elements: List[Element]) -> Optional[int]:
        task_lower = task.lower()
        for elem in elements:
            if not elem.content:
                continue
            if elem.content.lower() in task_lower:
                return elem.id

        search_keywords = ["search", "搜索", "检索", "查询", "百度一下", "搜", "输入", "input"]
        if any(k in task_lower for k in search_keywords):
            dom_inputs = [e for e in elements if e.type == "dom_input"]
            if dom_inputs:
                return dom_inputs[0].id

            best_id = None
            best_score = -1.0
            for elem in elements:
                x, y, w, h = elem.bbox
                if w <= 0 or h <= 0:
                    continue
                ratio = w / max(h, 1)
                # Prefer wide, short rectangles (search bars)
                score = ratio * w
                if score > best_score:
                    best_score = score
                    best_id = elem.id
            return best_id

        return None

    def should_finish(self, task: str, elements: List[Element]) -> bool:
        if not elements:
            return False
        compact = [{"id": e.id, "content": e.content} for e in elements]
        system_prompt = (
            "Determine if the task is completed based on the current UI elements. "
            "Return JSON with key 'done' as true/false."
        )
        user_payload = {"task": task, "elements": compact}
        try:
            resp = self._client_get().chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                temperature=0.1,
            )
        except Exception:
            resp = self._client_get().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                temperature=0.1,
            )
        content = resp.choices[0].message.content or ""
        try:
            parsed = json.loads(content)
            return bool(parsed.get("done"))
        except Exception:
            return False
