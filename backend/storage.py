from __future__ import annotations

import base64
import os
import time
from typing import Tuple


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def timestamp_name(prefix: str, ext: str = ".png") -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}{ext}"


def save_base64_image(image_b64: str, path: str) -> None:
    raw = base64.b64decode(image_b64)
    ensure_dir(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(raw)

