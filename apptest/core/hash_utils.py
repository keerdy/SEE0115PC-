from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def md5_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    md5 = hashlib.md5()
    file_path = Path(path)
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()
