from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Optional

from .steel_excel_image_mapper import generate_excel_with_embedded_images
from .storage import ensure_dir


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def unzip_archive(zip_path: str, extract_dir: str) -> str:
    ensure_dir(extract_dir)
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        zip_file.extractall(extract_dir)
    return extract_dir


def find_picture_root(extract_dir: str) -> Optional[str]:
    root = Path(extract_dir)
    candidates = [path for path in root.rglob("picture") if path.is_dir()]
    if candidates:
        return str(candidates[0])

    for directory in [path for path in root.rglob("*") if path.is_dir()]:
        if any(child.suffix.lower() in IMAGE_EXTENSIONS for child in directory.iterdir() if child.is_file()):
            return str(directory)
    return None


def collect_image_filenames_from_dir(images_dir: str, limit: int = 5000) -> list[str]:
    path = Path(images_dir)
    if not path.exists() or not path.is_dir():
        return []

    names: list[str] = []
    for file in path.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        names.append(file.name)
        if len(names) >= max(1, int(limit or 5000)):
            break
    return names


def embed_images_to_excel(
    *,
    excel_path: str,
    images_dir: str,
    output_path: str,
    preferred_filenames: Optional[list[str]] = None,
    column_name: str = "原始图片",
    image_width: int = 160,
    image_height: int = 120,
) -> str:
    return str(
        generate_excel_with_embedded_images(
            excel_path=excel_path,
            images_dir=images_dir,
            output_path=output_path,
            reverse_mapping=True,
            preferred_filenames=list(preferred_filenames or []),
            column_name=column_name,
            image_width=image_width,
            image_height=image_height,
        )
    )

