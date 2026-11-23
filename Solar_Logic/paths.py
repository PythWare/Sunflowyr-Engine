from __future__ import annotations
from pathlib import Path

created: set[str] = set()

def ensure_directory(directory: Path) -> None:
    key = str(directory)
    if key in created:
        return
    directory.mkdir(parents=True, exist_ok=True)
    created.add(key)

def ensure_parent(path: Path) -> None:
    ensure_directory(path.parent)

def reset() -> None:
    created.clear()
