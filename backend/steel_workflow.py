from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class WorkflowStepConfig:
    key: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class SteelWorkflowConfig:
    navigate: WorkflowStepConfig = field(default_factory=lambda: WorkflowStepConfig(key="navigate"))
    set_date: WorkflowStepConfig = field(default_factory=lambda: WorkflowStepConfig(key="set_date"))
    filter_status: WorkflowStepConfig = field(default_factory=lambda: WorkflowStepConfig(key="filter_status"))
    wait_ready: WorkflowStepConfig = field(default_factory=lambda: WorkflowStepConfig(key="wait_ready"))
    download_excel: WorkflowStepConfig = field(default_factory=lambda: WorkflowStepConfig(key="download_excel"))
    download_images: WorkflowStepConfig = field(default_factory=lambda: WorkflowStepConfig(key="download_images"))
    unzip_images: WorkflowStepConfig = field(default_factory=lambda: WorkflowStepConfig(key="unzip_images"))
    embed_excel: WorkflowStepConfig = field(default_factory=lambda: WorkflowStepConfig(key="embed_excel"))


def build_steel_workflow_config(
    *,
    status_filter: str,
    image_modes: Optional[list[dict[str, Any]]],
    download_images_enabled: bool = True,
    embed_excel_enabled: bool = True,
) -> SteelWorkflowConfig:
    has_status = bool(str(status_filter or "").strip())
    has_images = bool(image_modes) and bool(download_images_enabled)
    has_embed = bool(embed_excel_enabled) and has_images
    return SteelWorkflowConfig(
        filter_status=WorkflowStepConfig(key="filter_status", enabled=has_status),
        download_images=WorkflowStepConfig(key="download_images", enabled=has_images),
        unzip_images=WorkflowStepConfig(key="unzip_images", enabled=has_images),
        embed_excel=WorkflowStepConfig(key="embed_excel", enabled=has_embed),
    )
