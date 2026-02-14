from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import unquote, urlparse


ClickCandidate = Callable[[], Awaitable[bool]]


@dataclass
class DownloadIntent:
    kind: str
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    file_extensions: list[str] = field(default_factory=list)
    content_type_hints: list[str] = field(default_factory=list)


def build_download_intent(
    *,
    kind: str,
    include_keywords: Optional[list[str]] = None,
    exclude_keywords: Optional[list[str]] = None,
) -> DownloadIntent:
    kind_lower = str(kind or "").strip().lower()
    if kind_lower == "excel":
        return DownloadIntent(
            kind="excel",
            include_keywords=list(include_keywords or ["导出", "excel", "xlsx", "download"]),
            exclude_keywords=list(exclude_keywords or ["图片", "视频", "zip"]),
            file_extensions=[".xlsx", ".xls", ".csv"],
            content_type_hints=[
                "spreadsheet",
                "excel",
                "csv",
                "application/vnd.ms-excel",
                "application/octet-stream",
            ],
        )

    return DownloadIntent(
        kind="zip",
        include_keywords=list(include_keywords or ["图片下载", "download", "zip", "图片"]),
        exclude_keywords=list(exclude_keywords or ["视频", "excel", "导出"]),
        file_extensions=[".zip", ".rar", ".7z"],
        content_type_hints=[
            "zip",
            "compressed",
            "application/octet-stream",
        ],
    )


class GenericDownloadSkill:
    def __init__(
        self,
        *,
        ensure_page: Callable[[], Awaitable[None]],
        get_page: Callable[[], Any],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._ensure_page = ensure_page
        self._get_page = get_page
        self._logger = logger or logging.getLogger(__name__)

    async def download_with_click_candidates(
        self,
        *,
        intent: DownloadIntent,
        save_path: str,
        click_candidates: list[ClickCandidate],
        timeout_ms: int = 60000,
        allow_link_probe: bool = True,
        preserve_download_filename: bool = False,
    ) -> str:
        await self._ensure_page()
        page = self._get_page()
        if page is None:
            raise RuntimeError("Download skill unavailable: page is None")

        started = time.monotonic()
        total_timeout_s = max(5.0, float(timeout_ms or 60000) / 1000.0)
        per_candidate_timeout_ms = int(min(timeout_ms, 10000))

        last_error: Optional[Exception] = None
        for candidate in click_candidates:
            if time.monotonic() - started > total_timeout_s:
                break
            try:
                async with page.expect_download(timeout=per_candidate_timeout_ms) as download_info:
                    clicked = await candidate()
                    if not clicked:
                        raise RuntimeError("click candidate returned False")

                download = await download_info.value
                final_save_path = self._resolve_save_path(
                    save_path=save_path,
                    suggested_filename=getattr(download, "suggested_filename", None),
                    preserve_download_filename=preserve_download_filename,
                )
                await download.save_as(final_save_path)
                if os.path.exists(final_save_path) and os.path.getsize(final_save_path) > 0:
                    return final_save_path
            except Exception as exc:
                last_error = exc
                self._logger.debug(f"expect_download strategy failed: {exc}")

        for candidate in click_candidates:
            if time.monotonic() - started > total_timeout_s:
                break
            try:
                saved = await self._capture_attachment_response_after_click(
                    page=page,
                    candidate=candidate,
                    intent=intent,
                    save_path=save_path,
                    timeout_ms=min(timeout_ms, 8000),
                    preserve_download_filename=preserve_download_filename,
                )
                if saved:
                    return saved
            except Exception as exc:
                last_error = exc
                self._logger.debug(f"response-capture strategy failed: {exc}")

        if allow_link_probe and (time.monotonic() - started <= total_timeout_s):
            try:
                saved = await self._download_from_discovered_links(
                    page=page,
                    intent=intent,
                    save_path=save_path,
                    max_fetches=8,
                    request_timeout_ms=3000,
                    preserve_download_filename=preserve_download_filename,
                )
                if saved:
                    return saved
            except Exception as exc:
                last_error = exc
                self._logger.debug(f"link-probe strategy failed: {exc}")

        raise RuntimeError(f"download skill failed for kind={intent.kind}: {last_error}")

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        name = os.path.basename(str(filename or "").strip())
        if not name:
            return ""
        name = re.sub(r"[<>:\"/\\\\|?*]+", "_", name).strip(" .")
        return name

    def _resolve_save_path(
        self,
        *,
        save_path: str,
        suggested_filename: Any,
        preserve_download_filename: bool,
    ) -> str:
        if not preserve_download_filename:
            return save_path
        sanitized = self._sanitize_filename(str(suggested_filename or ""))
        if not sanitized:
            return save_path
        parent = os.path.dirname(save_path) or "."
        return os.path.join(parent, sanitized)

    @classmethod
    def _guess_filename_from_response(cls, *, response_url: Any, headers: dict[str, Any]) -> str:
        content_disposition = str((headers or {}).get("content-disposition", "") or "")
        if content_disposition:
            match = re.search(
                r'filename\\*?=(?:UTF-8\'\')?\"?([^\";]+)\"?',
                content_disposition,
                flags=re.IGNORECASE,
            )
            if match:
                guessed_name = unquote(str(match.group(1) or "").strip())
                sanitized = cls._sanitize_filename(guessed_name)
                if sanitized:
                    return sanitized

        parsed = urlparse(str(response_url or ""))
        file_name = unquote(os.path.basename(parsed.path or ""))
        return cls._sanitize_filename(file_name)

    async def _capture_attachment_response_after_click(
        self,
        *,
        page: Any,
        candidate: ClickCandidate,
        intent: DownloadIntent,
        save_path: str,
        timeout_ms: int,
        preserve_download_filename: bool = False,
    ) -> Optional[str]:
        hit_event = asyncio.Event()
        hit_box: dict[str, Any] = {"response": None}

        def _on_response(response: Any) -> None:
            if hit_box.get("response") is not None:
                return
            try:
                if self._looks_like_download_response(response=response, intent=intent):
                    hit_box["response"] = response
                    hit_event.set()
            except Exception:
                return

        page.on("response", _on_response)
        try:
            clicked = await candidate()
            if not clicked:
                return None
            await asyncio.wait_for(hit_event.wait(), timeout=max(3.0, timeout_ms / 1000.0))
            response = hit_box.get("response")
            if response is None:
                return None
            body = await response.body()
            if not body:
                return None
            resolved_path = self._resolve_save_path(
                save_path=save_path,
                suggested_filename=self._guess_filename_from_response(
                    response_url=getattr(response, "url", ""),
                    headers=(response.headers or {}),
                ),
                preserve_download_filename=preserve_download_filename,
            )
            with open(resolved_path, "wb") as file_obj:
                file_obj.write(body)
            return resolved_path
        finally:
            with contextlib.suppress(Exception):
                page.remove_listener("response", _on_response)

    async def _download_from_discovered_links(
        self,
        *,
        page: Any,
        intent: DownloadIntent,
        save_path: str,
        max_fetches: int = 8,
        request_timeout_ms: int = 3000,
        preserve_download_filename: bool = False,
    ) -> Optional[str]:
        discovered = await page.evaluate(
            """
            () => {
              const toAbsolute = (url) => {
                try { return new URL(url, window.location.href).toString(); }
                catch (_) { return ''; }
              };

              const rows = [];
              const nodes = Array.from(document.querySelectorAll('a[href], button[data-url], [data-download-url], [download], [data-href]'));
              for (const node of nodes) {
                const href = node.getAttribute('href') || node.getAttribute('data-url') || node.getAttribute('data-download-url') || node.getAttribute('data-href') || '';
                if (!href) continue;
                const text = (node.innerText || node.textContent || '').trim();
                const abs = toAbsolute(href);
                if (!abs) continue;
                rows.push({ url: abs, text });
              }
              return rows.slice(0, 300);
            }
            """
        )

        if not isinstance(discovered, list):
            return None

        fetched = 0
        for row in discovered:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            text = str(row.get("text") or "").strip().lower()
            if not url:
                continue

            haystack = f"{url.lower()} {text}"
            if intent.include_keywords and not any(keyword.lower() in haystack for keyword in intent.include_keywords):
                continue
            if intent.exclude_keywords and any(keyword.lower() in haystack for keyword in intent.exclude_keywords):
                continue

            try:
                resp = await page.request.get(url, timeout=max(1000, int(request_timeout_ms or 3000)))
                ok_attr = getattr(resp, "ok", False)
                ok_value = ok_attr() if callable(ok_attr) else bool(ok_attr)
                if not ok_value:
                    continue
                headers = {str(k).lower(): str(v).lower() for k, v in (resp.headers or {}).items()}
                content_type = headers.get("content-type", "")
                if intent.content_type_hints and not any(h in content_type for h in intent.content_type_hints):
                    parsed_path = urlparse(url).path.lower()
                    if intent.file_extensions and not any(parsed_path.endswith(ext) for ext in intent.file_extensions):
                        continue
                body = await resp.body()
                if not body:
                    continue
                resolved_path = self._resolve_save_path(
                    save_path=save_path,
                    suggested_filename=self._guess_filename_from_response(
                        response_url=url,
                        headers=(resp.headers or {}),
                    ),
                    preserve_download_filename=preserve_download_filename,
                )
                with open(resolved_path, "wb") as file_obj:
                    file_obj.write(body)
                return resolved_path
            except Exception:
                continue
            finally:
                fetched += 1
                if fetched >= max(1, int(max_fetches or 8)):
                    break

        return None

    def _looks_like_download_response(self, *, response: Any, intent: DownloadIntent) -> bool:
        try:
            url = str(response.url or "").lower()
            headers = {str(k).lower(): str(v).lower() for k, v in (response.headers or {}).items()}
            content_disposition = headers.get("content-disposition", "")
            content_type = headers.get("content-type", "")
            if "attachment" in content_disposition:
                return True
            if any(hint in content_type for hint in intent.content_type_hints):
                return True

            path = urlparse(url).path.lower()
            if any(path.endswith(ext) for ext in intent.file_extensions):
                return True
            return False
        except Exception:
            return False
