from __future__ import annotations
import time
from pathlib import Path

LOG_FILENAME = "sunflowyr_errors.log"
MAX_RECORDS = 20000

class Failure:
    __slots__ = ("stage", "source", "message", "unsupported")

    def __init__(self, stage: str, source: str, message: str, unsupported: bool) -> None:
        self.stage = stage
        self.source = source
        self.message = message
        self.unsupported = unsupported

collecting = False
records: list[Failure] = []
dropped = 0
log_root: Path | None = None
started_wall = 0.0
context: dict[str, str] = {}

def begin(root: Path, run_context: dict[str, object] | None = None) -> None:
    global collecting, dropped, log_root, started_wall
    records.clear()
    context.clear()
    context.update({str(key): str(value) for key, value in (run_context or {}).items()})
    dropped = 0
    log_root = Path(root)
    started_wall = time.time()
    collecting = True

def active() -> bool:
    return collecting

def record(stage: str, source: str, error: object, unsupported: bool = False) -> None:
    global dropped
    if not collecting:
        return
    if len(records) >= MAX_RECORDS:
        dropped += 1
        return
    records.append(Failure(str(stage), str(source), describe(error), bool(unsupported)))

def describe(error: object) -> str:
    if isinstance(error, BaseException):
        text = str(error).strip()
        return f"{type(error).__name__}: {text}" if text else type(error).__name__
    return str(error).strip()

def source_label(asset: object = None, obj: object = None, path: object = None) -> str:
    if path is not None:
        return str(path)
    container = getattr(asset, "relative_path", None) or str(asset or "")
    path_id = getattr(obj, "path_id", None)
    if container and path_id is not None:
        return f"{container}:{path_id}"
    return container or (str(path_id) if path_id is not None else "")

def counts() -> tuple[int, int]:
    return len(records) + dropped, len({item.stage for item in records})

def summary_lines(limit: int = 6) -> list[str]:
    if not records and not dropped:
        return []
    totals: dict[str, int] = {}
    for item in records:
        totals[item.stage] = totals.get(item.stage, 0) + 1
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"  {stage}: {count}" for stage, count in ranked[:limit]]
    if len(ranked) > limit:
        lines.append(f"  and {len(ranked) - limit} more kinds")
    if dropped:
        lines.append(f"  ({dropped} further failures not recorded, log is capped)")
    return lines

def finish() -> Path | None:
    global collecting
    if not collecting:
        return None
    collecting = False
    if not records and not dropped:
        if log_root is not None:
            stale = Path(log_root) / LOG_FILENAME
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass
        return None
    if log_root is None:
        return None
    path = Path(log_root) / LOG_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(), encoding="utf-8")
    except OSError:
        return None
    return path

def render() -> str:
    lines = [
        "Sunflowyr Engine, failures during this run",
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_wall)),
    ]
    lines.extend(f"{key}: {value}" for key, value in context.items())
    failed = sum(1 for item in records if not item.unsupported)
    unsupported = len(records) - failed
    lines.append(f"failures: {failed}   unsupported: {unsupported}")
    if dropped:
        lines.append(f"not recorded (log capped at {MAX_RECORDS}): {dropped}")
    lines.append("")
    by_stage: dict[str, list[Failure]] = {}
    for item in records:
        by_stage.setdefault(item.stage, []).append(item)
    for stage in sorted(by_stage):
        group = by_stage[stage]
        lines.append(f"{stage}  ({len(group)})")
        for item in group:
            mark = "unsupported" if item.unsupported else "failed"
            source = f"{item.source}  " if item.source else ""
            lines.append(f"    {source}{mark}: {item.message}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
