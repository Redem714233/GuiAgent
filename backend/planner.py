from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from openai import OpenAI

from backend.schemas import Element, PlanRequest, PlanResponse


class Planner:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")
        self._vlm_provider = os.getenv("VLM_PROVIDER", "").strip().lower()
        self._disable_text_llm = os.getenv("DISABLE_TEXT_LLM", "1").strip().lower() in {"1", "true", "yes", "on"}
        self.client: Optional[OpenAI] = None
        if not self._disable_text_llm:
            base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL")
            if base_url:
                self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=base_url)
            else:
                self.client = OpenAI()
        self._vlm: Optional["VLMService"] = None

    def _client_get(self) -> OpenAI:
        if self.client is None:
            raise RuntimeError("Text LLM is disabled")
        return self.client

    def _compact_elements(
        self, elements: List[Element], image_size: Optional[tuple[int, int]]
    ) -> list[dict]:
        return [
            {
                "id": e.id,
                "type": e.type,
                "content": e.content,
                "pos": self._get_semantic_pos(e.bbox, image_size),
            }
            for e in elements
        ]

    def plan(self, request: PlanRequest) -> PlanResponse:
        self._last_query = None
        elements = request.elements
        llm_action, debug = self._llm_select(
            request.task,
            elements,
            annotated_image_base64=request.annotated_image_base64,
            image_size=request.image_size,
        )
        if llm_action:
            self._last_query = llm_action.get("query")
            return PlanResponse(
                target_id=llm_action.get("target_id"),
                target_point=llm_action.get("target_point"),
                action_tool=llm_action.get("action_tool"),
                action_text=llm_action.get("action_text"),
                action_key=llm_action.get("action_key"),
                action_ms=llm_action.get("action_ms"),
                action_url=llm_action.get("action_url"),
                reason="llm",
                query=self._last_query,
                debug=debug,
            )
        fallback_choice = self._rule_select(request.task, elements)
        return PlanResponse(target_id=fallback_choice, reason="rule", debug=debug)

    def _llm_select(
        self,
        task: str,
        elements: List[Element],
        *,
        annotated_image_base64: Optional[str] = None,
        image_size: Optional[tuple[int, int]] = None,
    ) -> tuple[Optional[dict], Optional[dict]]:
        if not elements and not annotated_image_base64:
            return None, None
        compact = self._compact_elements(elements, image_size)
        disable_elements = os.getenv("VLM_DISABLE_ELEMENTS", "0").strip().lower() in {"1", "true", "yes", "on"}
        if disable_elements:
            compact = []
        if self._vlm_provider in {"qwen3-vl", "dashscope"} and annotated_image_base64:
            try:
                if self._vlm is None:
                    from backend.vlm_service import VLMService

                    self._vlm = VLMService()
                resp, raw = self._vlm.decide(
                    task=task,
                    elements=compact,
                    annotated_image_base64=annotated_image_base64,
                    image_size=image_size,
                )
                action = self._parse_action(resp, image_size)
                debug = {
                    "provider": self._vlm_provider,
                    "model": os.getenv("VLM_MODEL", ""),
                    "request": {
                        "task": task,
                        "elements": compact,
                        "image": "annotated_image_base64",
                        "elements_disabled": disable_elements,
                    },
                    "response": resp,
                    "response_raw": raw,
                }
                return action, debug
            except Exception as exc:
                return None, {
                    "provider": self._vlm_provider,
                    "model": os.getenv("VLM_MODEL", ""),
                    "request": {"task": task, "elements": compact, "image": "annotated_image_base64"},
                    "error": str(exc),
                }

        if self._disable_text_llm:
            return None, None

        system_prompt = (
            "You pick exactly one UI element id to accomplish the user task. "
            "Return JSON with keys 'id' and optional 'query'. "
            "If the task includes search, choose an input only when you must type. "
            "If the task includes 'click/open/enter/visit' a site after searching, "
            "prefer a non-input element that matches the target site. "
            "If the task explicitly involves typing (type/input/enter), you must set 'query' "
            "to the exact text to type, without extra instruction words. "
            "If the task does not involve typing, omit 'query'. "
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
                return self._parse_action(parsed, image_size, default_tool="click"), {
                    "provider": "text",
                    "request": user_payload,
                    "response": parsed,
                }
            return None, {"provider": "text", "request": user_payload, "response": content}
        except Exception:
            return None, {"provider": "text", "request": user_payload, "response": content}

    def _parse_action(
        self,
        resp: dict,
        image_size: Optional[tuple[int, int]],
        default_tool: Optional[str] = None,
    ) -> Optional[dict]:
        if not isinstance(resp, dict):
            return None
        tool = resp.get("tool") or default_tool
        target_id = resp.get("id")
        if isinstance(target_id, str) and target_id.isdigit():
            target_id = int(target_id)
        elif not isinstance(target_id, int):
            target_id = None
        point = resp.get("point")
        target_point = None
        if isinstance(point, list) and len(point) == 2 and image_size:
            img_w, img_h = image_size
            px = float(point[0])
            py = float(point[1])
            # Accept normalized [0,1] or absolute pixel coordinates from VLM.
            if 0.0 <= px <= 1.0 and 0.0 <= py <= 1.0:
                px = px * img_w
                py = py * img_h
            px = max(0, min(int(px), img_w - 1))
            py = max(0, min(int(py), img_h - 1))
            target_point = (px, py)
        action_text = resp.get("text") or resp.get("query")
        action = {
            "action_tool": tool,
            "action_text": action_text,
            "action_key": resp.get("key"),
            "action_ms": resp.get("ms") if isinstance(resp.get("ms"), int) else None,
            "action_url": resp.get("url"),
            "target_id": target_id,
            "target_point": target_point,
            "query": resp.get("query"),
        }
        if tool is None and action["target_id"] is None and action["target_point"] is None:
            return None
        return action

    def _rule_select(self, task: str, elements: List[Element]) -> Optional[int]:
        task_lower = task.lower()
        target_domain = self._extract_domain(task)
        if target_domain:
            action_keywords = ["点击", "进入", "打开", "访问", "open", "visit", "enter", "go"]
            if any(k in task_lower for k in action_keywords):
                aliases = self._domain_aliases(target_domain)
                for elem in elements:
                    if elem.type == "dom_input":
                        continue
                    content_lower = (elem.content or "").lower()
                    if any(alias in content_lower for alias in aliases):
                        return elem.id

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

    def _extract_domain(self, task: str) -> Optional[str]:
        match = re.search(r"\b([\w.-]+\.(?:com|cn|net|org|io|tv))\b", task, re.IGNORECASE)
        if match:
            return match.group(1).lower()

        alias_map = {
            "bilibili": "bilibili.com",
            "哔哩哔哩": "bilibili.com",
        }
        for key, domain in alias_map.items():
            if key.lower() in task.lower():
                return domain
        return None

    def _domain_aliases(self, target_domain: str) -> list[str]:
        alias_map = {
            "bilibili.com": ["bilibili", "哔哩哔哩", "b站"],
        }
        aliases = [target_domain]
        aliases.extend(alias_map.get(target_domain, []))
        return [a.lower() for a in aliases]

    def should_finish(
        self,
        task: str,
        elements: List[Element],
        current_url: Optional[str] = None,
        *,
        annotated_image_base64: Optional[str] = None,
        image_size: Optional[tuple[int, int]] = None,
    ) -> bool:
        target_domain = self._extract_domain(task)
        if target_domain and current_url:
            return target_domain in current_url.lower()
        if target_domain and current_url is not None:
            return False

        if self._vlm_provider in {"qwen3-vl", "dashscope"} and annotated_image_base64:
            try:
                if self._vlm is None:
                    from backend.vlm_service import VLMService

                    self._vlm = VLMService()
                done, debug = self._vlm.should_finish(
                    task=task,
                    annotated_image_base64=annotated_image_base64,
                    current_url=current_url or "",
                )
                self._last_finish_debug = {
                    "provider": self._vlm_provider,
                    "model": os.getenv("VLM_MODEL", ""),
                    **(debug or {}),
                }
                return done
            except Exception as exc:
                self._last_finish_debug = {
                    "provider": self._vlm_provider,
                    "model": os.getenv("VLM_MODEL", ""),
                    "error": str(exc),
                }
                return False

        if self._disable_text_llm:
            self._last_finish_debug = {
                "provider": "none",
                "reason": "text_llm_disabled_or_vlm_unavailable",
                "request": {"task": task, "url": current_url or "", "image": bool(annotated_image_base64)},
            }
            return False

        system_prompt = (
            "Determine if the task is completed based on the current URL and screenshot. "
            "Return JSON with key 'done' as true/false. "
            "Only return true when the user request has fully completed, "
            "especially when the target website has been opened."
        )
        user_payload = {"task": task, "url": current_url or ""}
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
            self._last_finish_debug = {
                "provider": "text",
                "request": {"task": task, "url": current_url or "", "image": bool(annotated_image_base64)},
                "response": parsed,
            }
            return bool(parsed.get("done"))
        except Exception:
            self._last_finish_debug = {
                "provider": "text",
                "request": {"task": task, "url": current_url or "", "image": bool(annotated_image_base64)},
                "response": content,
            }
            return False

    def plan_steps(
        self,
        task: str,
        max_steps: int = 6,
        *,
        annotated_image_base64: Optional[str] = None,
    ) -> list[str]:
        if self._vlm_provider in {"qwen3-vl", "dashscope"}:
            try:
                if self._vlm is None:
                    from backend.vlm_service import VLMService

                    self._vlm = VLMService()
                steps, debug = self._vlm.plan_steps(
                    task=task,
                    annotated_image_base64=annotated_image_base64,
                    max_steps=max_steps or 6,
                )
                self._last_plan_debug = {
                    "provider": self._vlm_provider,
                    "model": os.getenv("VLM_MODEL", ""),
                    **(debug or {}),
                }
                return steps
            except Exception as exc:
                self._last_plan_debug = {
                    "provider": self._vlm_provider,
                    "model": os.getenv("VLM_MODEL", ""),
                    "error": str(exc),
                }
                pass

        if self._disable_text_llm:
            self._last_plan_debug = None
            return [task]

        system_prompt = (
            "Decompose the user task into a short, ordered list of concrete UI steps. "
            "Each step must be a single action the agent can attempt on the current page. "
            "Keep steps minimal and explicit (e.g., 'click the search box', 'type bilibili and press Enter', "
            "'click the bilibili.com result'). "
            "Return JSON with key 'steps' as a list of strings. "
            "Do not include confirmations or meta commentary."
        )
        user_payload = {"task": task, "max_steps": max_steps}
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
            if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                return [str(step) for step in parsed["steps"]]
            return []
        except Exception:
            return []

    def _get_semantic_pos(self, bbox: tuple[int, int, int, int], image_size: Optional[tuple[int, int]]) -> str:
        if not image_size:
            return "unknown"
        img_w, img_h = image_size
        if img_w <= 0 or img_h <= 0:
            return "unknown"
        x, y, w, h = bbox
        cx = x + w / 2
        cy = y + h / 2
        h_zone = "left" if cx < img_w / 3 else "right" if cx > img_w * 2 / 3 else "center"
        v_zone = "top" if cy < img_h / 3 else "bottom" if cy > img_h * 2 / 3 else "center"
        return f"{v_zone}-{h_zone}"
