from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_profiles() -> list[dict[str, Any]]:
    return [
        {
            "name": "default",
            "match": {"default": True},
            "download": {
                "excel": {
                    "include": ["数据导出", "导出", "excel", "xlsx", "download"],
                    "exclude": ["视频", "图片", "zip"],
                },
                "zip": {
                    "include": ["图片下载", "图片", "zip", "download"],
                    "exclude": ["视频", "excel", "数据导出"],
                },
            },
        }
    ]


def _normalize_profile_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        profiles = payload.get("profiles")
        if isinstance(profiles, list):
            return [item for item in profiles if isinstance(item, dict)]
    return []


@lru_cache(maxsize=1)
def load_workflow_profiles() -> list[dict[str, Any]]:
    custom_path = str(os.getenv("WORKFLOW_PROFILES_FILE", "")).strip()
    candidate = Path(custom_path) if custom_path else (_project_root() / "data" / "workflow_profiles.json")

    profiles = _default_profiles()
    if not candidate.exists() or not candidate.is_file():
        return profiles

    try:
        with candidate.open("r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
        loaded = _normalize_profile_list(payload)
        if loaded:
            return loaded
    except Exception:
        return profiles

    return profiles


def _match_profile(profile: dict[str, Any], target_url: str) -> bool:
    match = profile.get("match")
    if not isinstance(match, dict):
        return False
    if bool(match.get("default")):
        return False

    target = str(target_url or "").strip().lower()
    parsed = urlparse(target)
    host = str(parsed.hostname or "").lower()
    path = str(parsed.path or "").lower()

    host_contains = [str(item).lower() for item in (match.get("host_contains") or []) if str(item).strip()]
    path_contains = [str(item).lower() for item in (match.get("path_contains") or []) if str(item).strip()]
    url_contains = [str(item).lower() for item in (match.get("url_contains") or []) if str(item).strip()]

    host_ok = not host_contains or any(token in host for token in host_contains)
    path_ok = not path_contains or any(token in path for token in path_contains)
    url_ok = not url_contains or any(token in target for token in url_contains)
    return host_ok and path_ok and url_ok


def resolve_workflow_profile(target_url: Optional[str]) -> dict[str, Any]:
    profiles = load_workflow_profiles()
    target = str(target_url or "").strip()

    for profile in profiles:
        if _match_profile(profile, target):
            return profile

    for profile in profiles:
        match = profile.get("match")
        if isinstance(match, dict) and bool(match.get("default")):
            return profile

    return _default_profiles()[0]


def get_download_keywords(
    profile: Optional[dict[str, Any]],
    *,
    kind: str,
    default_include: list[str],
    default_exclude: list[str],
) -> tuple[list[str], list[str]]:
    if not isinstance(profile, dict):
        return list(default_include), list(default_exclude)

    download_cfg = profile.get("download")
    if not isinstance(download_cfg, dict):
        return list(default_include), list(default_exclude)

    kind_cfg = download_cfg.get(kind)
    if not isinstance(kind_cfg, dict):
        return list(default_include), list(default_exclude)

    include = kind_cfg.get("include")
    exclude = kind_cfg.get("exclude")

    include_keywords = [str(item).strip() for item in (include or []) if str(item).strip()]
    exclude_keywords = [str(item).strip() for item in (exclude or []) if str(item).strip()]

    return (
        include_keywords or list(default_include),
        exclude_keywords or list(default_exclude),
    )

