"""确定性 JSON 规范化与内容摘要，仅依赖标准库。"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容的 SHA-256 摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
