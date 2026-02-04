from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

import pandas as pd

from backend.storage import ensure_dir, timestamp_name


class OutputStore:
    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir
        ensure_dir(self.output_dir)
        self.rows: List[Dict[str, Any]] = []
        self.last_extract: Optional[Dict[str, Any]] = None

    def reset(self) -> None:
        self.rows = []
        self.last_extract = None

    def set_last_extract(self, data: Dict[str, Any]) -> None:
        self.last_extract = data

    def append_row(self, row: Dict[str, Any]) -> None:
        self.rows.append(row)

    def save_excel(self, file_name: Optional[str] = None) -> str:
        if not self.rows:
            raise ValueError("No rows to save")
        if file_name:
            file_name = os.path.basename(file_name)
            if not file_name.lower().endswith(".xlsx"):
                file_name = f"{file_name}.xlsx"
        else:
            suffix = uuid.uuid4().hex[:6]
            file_name = timestamp_name(f"output_{suffix}", ext=".xlsx")
        path = os.path.join(self.output_dir, file_name)
        if os.path.exists(path):
            suffix = uuid.uuid4().hex[:6]
            file_name = timestamp_name(f"output_{suffix}", ext=".xlsx")
            path = os.path.join(self.output_dir, file_name)
        df = pd.DataFrame(self.rows)
        df.to_excel(path, index=False)
        return path

    def list_files(self) -> List[str]:
        ensure_dir(self.output_dir)
        files = [f for f in os.listdir(self.output_dir) if os.path.isfile(os.path.join(self.output_dir, f))]
        files.sort(reverse=True)
        return files

    def get_file_path(self, name: str) -> Optional[str]:
        safe_name = os.path.basename(name)
        path = os.path.join(self.output_dir, safe_name)
        if not os.path.exists(path):
            return None
        base = os.path.abspath(self.output_dir)
        full = os.path.abspath(path)
        if not full.startswith(base):
            return None
        return full

