from __future__ import annotations
import os, re, shutil, subprocess, threading, time, zlib
from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from tkinter import filedialog
from PIL import Image, ImageDraw, ImageTk
from .asset_finder import fit_text, human_size
from .binary import EndianReader
from .bundle_extract import extract_bundle, read_unity_fs, read_unity_web
from .class_ids import COMPONENT_CLASS_IDS, class_name
from .objects import read_object_name
from .serialized import SerializedAsset, discover_serialized_files, is_serialized_file, load_serialized_asset, read_serialized_asset
from .updates_panel import (
    CLEAR,
    HEADER_COLOUR,
    PANEL_BLUE,
    PANEL_PAD,
    SCALE,
    add_bar_regions,
    bar_geometry,
    draw_icon,
    glow,
    hovered_icon,
    load_font,
    panel_frame,
    render_icon_bar,
    text_shadow,
)

DARK_TEXT = (14, 70, 88, 255)
WHITE = (255, 255, 255, 255)
COLUMN_WIDTH = 262.0
FILE_ROW = 42
OBJECT_ROW = 36
CARET_SECONDS = 0.53
BUNDLE_SIGNATURES = (b"UnityFS", b"UnityWeb", b"UnityRaw")
STREAM_SUFFIXES = (".ress", ".resource")
SINGLE_UNPACK_FOLDER = "single_unpacks"
BUNDLE_FILES_FOLDER = "bundle_files"
TYPE_COLOURS = (
    (255, 214, 102), (126, 217, 156), (255, 158, 128), (186, 160, 255), (120, 200, 255),
    (255, 140, 190), (160, 230, 220), (240, 190, 120), (200, 220, 140), (150, 180, 255),
)

@dataclass(slots=True)
class FileSummary:
    key: str
    label: str
    path: Path
    root: Path
    kind: str
    size: int
    objects: int
    in_data: bool

@dataclass(slots=True)
class InspectedFile:
    summary: FileSummary
    info: str
    rows: list[tuple[str, str, int, int, str]]
    type_counts: list[tuple[str, int]] = field(default_factory=list)

def type_colour(type_name: str) -> tuple[int, int, int]:
    return TYPE_COLOURS[zlib.crc32(type_name.encode("utf-8")) % len(TYPE_COLOURS)]

def same_path_key(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(str(path))

def file_kind(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return ""
    if head.startswith(BUNDLE_SIGNATURES):
        return "bundle"
    return "serialized" if is_serialized_file(path) else ""

def owner_name(asset: SerializedAsset, reader: EndianReader, byte_start: int, game_objects: dict[int, str]) -> str:
    try:
        reader.seek(byte_start)
        file_id = reader.i32()
        path_id = reader.i64() if asset.format_version >= 14 else reader.i32()
    except Exception:
        return ""
    return game_objects.get(path_id, "") if file_id == 0 else ""

def read_rows(asset: SerializedAsset, handle: Any, where: str) -> list[tuple[str, str, int, int, str]]:
    reader = EndianReader(handle, asset.endian)
    game_objects = {obj.path_id: read_object_name(asset, reader, obj) for obj in asset.objects if obj.class_id == 1}
    rows = []
    for obj in asset.objects:
        if obj.class_id == 1:
            name = game_objects.get(obj.path_id, "")
        elif obj.class_id in COMPONENT_CLASS_IDS and obj.class_id != 114:
            name = owner_name(asset, reader, obj.byte_start, game_objects)
        else:
            name = read_object_name(asset, reader, obj)
            if not name and obj.class_id == 114:
                name = owner_name(asset, reader, obj.byte_start, game_objects)
        type_name = class_name(obj.class_id)
        search = f"{name}\t{type_name}\t{obj.path_id}\t{where}".lower()
        rows.append((name, type_name, obj.path_id, obj.byte_size, search))
    return rows

def finish(summary: FileSummary, info: str, rows: list[tuple[str, str, int, int, str]]) -> InspectedFile:
    rows.sort(key=lambda row: (row[1].lower(), row[0].lower() or "￿", row[2]))
    counts = Counter(row[1] for row in rows)
    return InspectedFile(summary=summary, info=info, rows=rows, type_counts=counts.most_common())

def inspect_serialized(summary: FileSummary) -> InspectedFile:
    with summary.path.open("rb") as handle:
        asset = read_serialized_asset(handle, summary.path, summary.root)
        rows = read_rows(asset, handle, summary.label)
    trees = "Type trees" if asset.enable_type_tree else "No type trees"
    externals = len(asset.externals)
    info = f"Unity {asset.unity_version}|Format {asset.format_version}|{trees}|{externals} external file{'s' if externals != 1 else ''}|{human_size(summary.size)}"
    return finish(summary, info, rows)

def inspect_bundle(summary: FileSummary) -> InspectedFile:
    data = summary.path.read_bytes()
    signature = data.split(b"\0", 1)[0].decode("ascii", "replace")
    nodes, blocks = read_unity_fs(data) if signature == "UnityFS" else read_unity_web(data)
    rows: list[tuple[str, str, int, int, str]] = []
    versions = []
    for node in nodes:
        if node.path.lower().endswith(STREAM_SUFFIXES):
            continue
        payload = BytesIO(blocks[node.offset:node.offset + node.size])
        try:
            asset = read_serialized_asset(payload, Path(node.path), Path("."))
        except Exception:
            continue
        versions.append(asset.unity_version)
        rows.extend(read_rows(asset, payload, node.path))
    version = f"Unity {versions[0]}|  " if versions else ""
    info = f"{signature} bundle|{version}{len(nodes)} file{'s' if len(nodes) != 1 else ''} inside|{human_size(summary.size)}"
    return finish(summary, info, rows)

DATA_FOLDER_MARKERS = ("globalgamemanagers", "mainData", "data.unity3d", "level0", "resources.assets")

def is_unity_data_folder(root: Path) -> bool:
    return root.is_dir() and any((root / marker).is_file() for marker in DATA_FOLDER_MARKERS)

def summarize(path: Path, root: Path) -> FileSummary:
    asset = load_serialized_asset(path, root)
    return FileSummary(key=str(path), label=asset.relative_path, path=path, root=root, kind="serialized",
                       size=path.stat().st_size, objects=len(asset.objects), in_data=True)

class BundleInspectorPanel:
    def __init__(self, host: Any) -> None:
        self.host = host
        self.catalog: list[FileSummary] = []
        self.opened: list[FileSummary] = []
        self.scan_state = "idle"
        self.scan_root: Path | None = None
        self.scan_progress = 0.0
        self.scan_message = ""
        self.scan_token = 0
        self.selected_key = ""
        self.inspected: dict[str, InspectedFile] = {}
        self.loading: set[str] = set()
        self.finished: list[tuple[str, InspectedFile | None, str]] = []
        self.load_errors: dict[str, str] = {}
        self.query = ""
        self.type_filter = "all"
        self.chip_start = 0
        self.chip_next = 0
        self.filtered: list[tuple[str, str, int, int, str]] = []
        self.filter_key: tuple[Any, ...] = ()
        self.filter_version = 0
        self.selected_row = -1
        self.file_scroll = 0.0
        self.row_scroll = 0.0
        self.drag_target = "rows"
        self.bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.drag_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.viewport: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.geometry: dict[str, Any] = {}
        self.base_cache: dict[tuple[Any, ...], Image.Image] = {}
        self.layers: dict[str, tuple[Any, Image.Image]] = {}
        self.photo: ImageTk.PhotoImage | None = None
        self.item: int | None = None
        self.caret_on = True
        self.caret_time = time.perf_counter()
        self.caret_x = 0.0
        self.notice = ""
        self.unpack_job: tuple[str, Path, Path | None] | None = None
        self.seen_state: tuple[Any, ...] = ()

    @property
    def scroll_y(self) -> float:
        return self.file_scroll if self.drag_target == "files" else self.row_scroll

    def files(self) -> list[FileSummary]:
        return self.catalog + self.opened

    def summary(self) -> FileSummary | None:
        return next((item for item in self.files() if item.key == self.selected_key), None)

    def current(self) -> InspectedFile | None:
        return self.inspected.get(self.selected_key)

    def activate(self) -> None:
        self.notice = ""
        self.start_scan()

    def start_scan(self, force: bool = False) -> None:
        root = Path(self.host.data_root)
        if not force and self.scan_root == root and self.scan_state in ("scanning", "ready"):
            return
        self.scan_root = root
        self.scan_state = "scanning"
        self.scan_progress = 0.0
        self.scan_message = ""
        self.scan_token += 1
        self.catalog = []
        self.inspected = {key: value for key, value in self.inspected.items() if not value.summary.in_data}
        if self.selected_key not in {item.key for item in self.opened}:
            self.selected_key = ""
        self.file_scroll = 0.0
        threading.Thread(target=self.scan, args=(root, self.scan_token), daemon=True).start()

    def scan(self, root: Path, token: int) -> None:
        try:
            paths = discover_serialized_files(root) if is_unity_data_folder(root) else []
            if not paths:
                if token == self.scan_token:
                    self.scan_state = "missing"
                return
            found = []
            for index, path in enumerate(paths):
                try:
                    found.append(summarize(path, root))
                except Exception:
                    continue
                if token != self.scan_token:
                    return
                self.catalog = list(found)
                self.scan_progress = (index + 1) / len(paths)
            if token == self.scan_token:
                self.scan_state = "ready"
        except Exception as exc:
            if token == self.scan_token:
                self.scan_message = f"{type(exc).__name__}: {exc}"
                self.scan_state = "failed"

    def request(self, summary: FileSummary) -> None:
        key = summary.key
        if key in self.inspected or key in self.loading or key in self.load_errors:
            return
        self.loading.add(key)
        threading.Thread(target=self.load, args=(summary,), daemon=True).start()

    def load(self, summary: FileSummary) -> None:
        try:
            result = inspect_bundle(summary) if summary.kind == "bundle" else inspect_serialized(summary)
            self.finished.append((summary.key, result, ""))
        except Exception as exc:
            self.finished.append((summary.key, None, f"{type(exc).__name__}: {exc}"))
        finally:
            self.loading.discard(summary.key)

    def collect_finished(self) -> None:
        while self.finished:
            key, result, error = self.finished.pop(0)
            if result is not None:
                result.summary.objects = len(result.rows)
                self.inspected[key] = result
            else:
                self.load_errors[key] = error

    def select_file(self, key: str) -> None:
        if key == self.selected_key:
            return
        self.selected_key = key
        self.type_filter = "all"
        self.chip_start = 0
        self.row_scroll = 0.0
        self.selected_row = -1
        self.notice = ""
        self.load_errors.pop(key, None)
        self.forget_old_files()
        summary = self.summary()
        if summary is not None:
            self.request(summary)
        self.redraw()

    def forget_old_files(self) -> None:
        for key in [key for key in self.inspected if key != self.selected_key]:
            self.inspected.pop(key, None)

    def visible_rows(self) -> list[tuple[str, str, int, int, str]]:
        inspected = self.current()
        key = (id(inspected), self.query, self.type_filter)
        if key != self.filter_key:
            rows = inspected.rows if inspected is not None else []
            terms = self.query.lower().split()
            self.filtered = [
                row for row in rows
                if (self.type_filter == "all" or row[1] == self.type_filter) and all(term in row[4] for term in terms)
            ]
            self.filter_key = key
            self.filter_version += 1
            self.selected_row = 0 if self.filtered else -1
            self.row_scroll = 0.0
        return self.filtered

    def selected_object(self) -> tuple[str, str, int, int, str] | None:
        rows = self.visible_rows()
        return rows[self.selected_row] if 0 <= self.selected_row < len(rows) else None

    def is_action(self, action: str) -> bool:
        return action.startswith("inspect:")

    def screen_box(self, name: str) -> tuple[float, float, float, float]:
        x0, y0 = self.bounds[0], self.bounds[1]
        bx0, by0, bx1, by1 = self.geometry.get(name, (0.0, 0.0, 0.0, 0.0))
        return (x0 + bx0, y0 + by0, x0 + bx1, y0 + by1)

    def contains(self, x: float, y: float) -> bool:
        for name in ("files", "rows"):
            x0, y0, x1, y1 = self.screen_box(name)
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.drag_target = name
                return True
        return False

    def content_height(self, name: str) -> float:
        if name == "files":
            return float(len(self.files()) * FILE_ROW + 12)
        return float(len(self.visible_rows()) * OBJECT_ROW + 12)

    def clamp_scroll(self, name: str, value: float) -> float:
        x0, y0, x1, y1 = self.geometry.get(name, (0.0, 0.0, 0.0, 1.0))
        return max(0.0, min(float(value), max(0.0, self.content_height(name) - (y1 - y0))))

    def set_scroll(self, name: str, value: float) -> bool:
        value = self.clamp_scroll(name, value)
        if name == "files":
            changed = value != self.file_scroll
            self.file_scroll = value
        else:
            changed = value != self.row_scroll
            self.row_scroll = value
        return changed

    def scroll_from_delta(self, start_scroll: float, dy: float) -> None:
        self.set_scroll(self.drag_target, float(start_scroll) - float(dy))

    def scroll_by(self, amount: float) -> None:
        canvas = self.host.canvas
        x = canvas.winfo_pointerx() - canvas.winfo_rootx()
        y = canvas.winfo_pointery() - canvas.winfo_rooty()
        fx0, fy0, fx1, fy1 = self.screen_box("files")
        target = "files" if fx0 <= x <= fx1 and fy0 <= y <= fy1 else "rows"
        step = (FILE_ROW if target == "files" else OBJECT_ROW) * 3
        current = self.file_scroll if target == "files" else self.row_scroll
        if self.set_scroll(target, current + (step if amount > 0 else -step)):
            self.redraw()

    def handle_key(self, event: Any) -> bool:
        keysym = str(getattr(event, "keysym", ""))
        char = str(getattr(event, "char", ""))
        control = bool(int(getattr(event, "state", 0) or 0) & 0x4)
        if self.current() is None:
            return False
        if keysym == "Escape":
            if self.query:
                self.set_query("")
                return True
            return False
        if keysym == "BackSpace":
            if control:
                trimmed = self.query.rstrip()
                self.set_query(trimmed[: trimmed.rfind(" ") + 1])
            else:
                self.set_query(self.query[:-1])
            return True
        if keysym == "Delete":
            self.set_query("")
            return True
        if keysym in ("Up", "Down", "Prior", "Next"):
            rows = self.visible_rows()
            if rows:
                x0, y0, x1, y1 = self.geometry["rows"]
                page = max(1, int((y1 - y0) // OBJECT_ROW))
                step = {"Up": -1, "Down": 1, "Prior": -page, "Next": page}[keysym]
                self.select_row(max(0, min(len(rows) - 1, self.selected_row + step)))
            return True
        if keysym in ("Return", "KP_Enter"):
            self.perform_action("inspect:find")
            return True
        if control and keysym.lower() == "v":
            try:
                pasted = self.host.root.clipboard_get()
            except Exception:
                pasted = ""
            self.set_query((self.query + " ".join(str(pasted).split()))[:120])
            return True
        if char and char.isprintable() and not control and len(self.query) < 120:
            self.set_query(self.query + char)
            return True
        return False

    def set_query(self, text: str) -> None:
        self.query = text
        self.caret_on = True
        self.caret_time = time.perf_counter()
        self.notice = ""
        self.redraw()

    def select_row(self, index: int) -> None:
        self.selected_row = index
        x0, y0, x1, y1 = self.geometry["rows"]
        view_h = y1 - y0 - 12
        top = index * OBJECT_ROW
        if top < self.row_scroll:
            self.row_scroll = float(top)
        elif top + OBJECT_ROW > self.row_scroll + view_h:
            self.row_scroll = self.clamp_scroll("rows", top + OBJECT_ROW - view_h)
        self.notice = ""
        self.redraw()

    def perform_action(self, action: str) -> None:
        if action.startswith("inspect:file:"):
            self.select_file(action.split(":", 2)[2])
        elif action.startswith("inspect:row:"):
            self.select_row(int(action.split(":", 2)[2]))
        elif action == "inspect:chip:+more":
            self.chip_start = self.chip_next
            self.redraw()
        elif action.startswith("inspect:chip:"):
            self.type_filter = action.split(":", 2)[2]
            self.redraw()
        elif action == "inspect:browse":
            self.browse()
        elif action == "inspect:unpack":
            self.unpack()
        elif action == "inspect:stop":
            self.host.stop_unpack()
        elif action == "inspect:output":
            self.open_unpack()
        elif action == "inspect:find":
            self.find_in_unpack()
        elif action == "inspect:open":
            summary = self.summary()
            if summary is not None:
                self.reveal(summary.path)

    def browse(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.host.root,
            title="Open a Unity bundle or serialized file",
            initialdir=str(self.host.project_root),
            filetypes=[("All files", "*.*"), ("Unity bundles", "*.bundle *.unity3d"), ("Serialized files", "*.assets")],
        )
        if not chosen:
            return
        path = Path(chosen)
        wanted = same_path_key(path)
        existing = next((item for item in self.files() if same_path_key(item.path) == wanted), None)
        if existing is not None:
            self.select_file(existing.key)
            return
        kind = file_kind(path)
        if not kind:
            self.notice = f"{path.name} isnt a Unity bundle or serialized file"
            self.redraw()
            return
        summary = FileSummary(key=str(path), label=path.name, path=path, root=path.parent, kind=kind,
                              size=path.stat().st_size, objects=-1, in_data=False)
        self.opened.append(summary)
        self.load_errors.pop(summary.key, None)
        self.select_file(summary.key)
        self.set_scroll("files", self.content_height("files"))

    def unpack(self) -> None:
        summary = self.summary()
        if summary is None or self.host.running:
            return
        output_root = self.unpack_output(summary)
        staging = None
        if summary.kind == "bundle":
            staging = output_root / BUNDLE_FILES_FOLDER
            try:
                extract_bundle(summary.path, staging)
            except Exception as exc:
                shutil.rmtree(staging, ignore_errors=True)
                self.notice = f"Could not open the bundle: {exc}"[:90]
                self.redraw()
                return
            self.host.start_unpack(output_root=output_root, data_root=staging)
        elif summary.in_data:
            self.host.start_unpack(only_file=summary.label, output_root=output_root)
        else:
            self.host.start_unpack(only_file=summary.path.name, output_root=output_root, data_root=summary.path.parent)
        if self.host.running:
            self.unpack_job = (summary.label, output_root, staging)
            self.notice = ""
        elif staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        self.redraw()

    def unpack_output(self, summary: FileSummary) -> Path:
        folder = re.sub(r"[^A-Za-z0-9._-]+", "_", summary.label)
        return Path(self.host.output_root) / SINGLE_UNPACK_FOLDER / folder

    def finish_unpack(self) -> None:
        label, output_root, staging = self.unpack_job
        self.unpack_job = None
        if staging is not None and self.host.status != "Complete":
            shutil.rmtree(staging, ignore_errors=True)
        relative = f"{SINGLE_UNPACK_FOLDER}/{output_root.name}"
        self.notice = {
            "Complete": f"Finished unpack. Saved to {relative}",
            "Stopped": f"Unpack of {label} stopped.",
        }.get(self.host.status, f"Unpack of {label} failed. The home screen log has the details.")
        self.redraw()

    def status_line(self) -> str:
        if self.host.running:
            if self.unpack_job is not None:
                return f"Currently unpacking {self.unpack_job[0]}"
            return "An unpack is running from the home screen"
        return self.notice

    def find_in_unpack(self) -> None:
        summary = self.summary()
        row = self.selected_object()
        if summary is None or row is None or not summary.in_data:
            return
        self.host.switch_screen_mode("finder")
        from .asset_finder import bundle_query

        self.host.asset_finder.preset(f"{bundle_query(summary.label)} {row[2]}")

    def open_unpack(self) -> None:
        summary = self.summary()
        if summary is None:
            return
        folder = self.unpack_output(summary)
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                subprocess.Popen(["xdg-open", str(folder)])
            self.notice = "Opened the unpack in Explorer"
        except OSError as exc:
            self.notice = f"Couldnt open folder: {exc}"
        self.redraw()

    def reveal(self, path: Path) -> None:
        try:
            if os.name == "nt":
                if path.exists():
                    subprocess.Popen(f'explorer /select,"{path}"')
                else:
                    os.startfile(str(path.parent))
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
            self.notice = "Opened in Explorer"
        except OSError as exc:
            self.notice = f"Couldnt open folder: {exc}"
        self.redraw()

    def poll(self) -> bool:
        if self.host.screen_mode != "inspector" or self.item is None or getattr(self.host, "closing", False):
            return False
        self.collect_finished()
        if self.unpack_job is not None and not self.host.running:
            self.finish_unpack()
        if not self.selected_key and self.catalog:
            self.select_file(self.catalog[0].key)
        self.forget_old_files()
        state = (
            self.scan_state,
            int(self.scan_progress * 50),
            len(self.catalog),
            self.selected_key in self.inspected,
            self.selected_key in self.load_errors,
            len(self.loading),
            self.host.running,
        )
        if state != self.seen_state:
            self.seen_state = state
            self.redraw()
            return False
        now = time.perf_counter()
        if self.current() is not None and now - self.caret_time >= CARET_SECONDS:
            self.caret_on = not self.caret_on
            self.caret_time = now
            self.redraw()
        return False

    def button_specs(self) -> list[tuple[str, str, bool]]:
        summary = self.summary()
        if summary is None:
            return []
        loaded = self.current() is not None
        specs = [("inspect:open", "Open Folder", True)]
        if not self.host.running and self.unpack_output(summary).is_dir():
            specs.append(("inspect:output", "Open Unpack", True))
        if summary.in_data:
            specs.append(("inspect:find", "Find in Unpack", loaded and self.selected_object() is not None))
        if self.host.running:
            specs.append(("inspect:stop", "Stop Unpack", True))
        else:
            specs.append(("inspect:unpack", "Unpack Bundle" if summary.kind == "bundle" else "Unpack File", True))
        return specs

    def compute_geometry(self, width: int, height: int) -> dict[str, Any]:
        pad = PANEL_PAD
        bar, icons = bar_geometry(width, height)
        bottom = bar[1] - 14.0
        column = (pad, pad, pad + COLUMN_WIDTH, bottom)
        right_x0 = column[2] + 18.0
        right_x1 = width - pad
        search = (right_x0, pad + 48.0, right_x1, pad + 86.0)
        chip_y0, chip_y1 = pad + 96.0, pad + 122.0
        chips = []
        inspected = self.current()
        if inspected is not None:
            font = load_font("segoeuib.ttf", 10)
            types = inspected.type_counts
            all_w = font.getlength("All") / SCALE + 22.0
            more_w = font.getlength("More types") / SCALE + 22.0
            chips.append(("all", "All", right_x0, right_x0 + all_w))
            x = right_x0 + all_w + 6.0
            index = self.chip_start if self.chip_start < len(types) else 0
            while index < len(types):
                name, count = types[index]
                label = f"{name}  {count:,}"
                chip_w = font.getlength(label) / SCALE + 34.0
                reserve = more_w + 6.0 if index < len(types) - 1 or self.chip_start > 0 else 0.0
                if x + chip_w > right_x1 - reserve:
                    break
                chips.append((name, label, x, x + chip_w))
                x += chip_w + 6.0
                index += 1
            if index < len(types) or self.chip_start > 0:
                chips.append(("+more", "More types", x, x + more_w))
            self.chip_next = index if index < len(types) else 0
        buttons = []
        bx = right_x1
        for action, label, enabled in reversed(self.button_specs()):
            button_w = load_font("segoeuib.ttf", 10).getlength(label) / SCALE + 34.0
            buttons.insert(0, (action, label, enabled, (bx - button_w, bottom - 32.0, bx, bottom)))
            bx -= button_w + 8.0
        return {
            "bar": bar,
            "icons": icons,
            "column": column,
            "files": (pad, pad + 58.0, pad + COLUMN_WIDTH, bottom - 44.0),
            "browse": (pad, bottom - 32.0, pad + COLUMN_WIDTH, bottom),
            "header": (right_x0, pad, right_x1, chip_y1),
            "search": search,
            "chip_row": (right_x0, chip_y0, right_x1, chip_y1),
            "chips": chips,
            "rows": (right_x0, pad + 132.0, right_x1, bottom - 44.0),
            "footer": (right_x0, bottom - 32.0, right_x1, bottom),
            "buttons": buttons,
        }

    def draw(self, hole: tuple[float, float, float, float], below_item: int | None) -> None:
        x0, y0, x1, y1 = (int(round(value)) for value in hole)
        width, height = max(1, x1 - x0), max(1, y1 - y0)
        if (x0, y0, x1, y1) != self.bounds:
            self.layers = {}
        self.bounds = (x0, y0, x1, y1)
        self.geometry = self.compute_geometry(width, height)
        rx0, ry0, rx1, ry1 = self.geometry["rows"]
        self.viewport = (x0 + rx0, y0 + ry0, x0 + rx1, y0 + ry1)
        self.drag_bounds = self.viewport
        self.photo = ImageTk.PhotoImage(self.compose(width, height), master=self.host.root)
        self.item = self.host.canvas.create_image(x0, y0, image=self.photo, anchor="nw")
        if below_item is not None:
            self.host.canvas.tag_lower(self.item, below_item)
        self.add_regions()

    def redraw(self) -> None:
        if self.item is None or not self.geometry or self.host.screen_mode != "inspector":
            return
        x0, y0, x1, y1 = self.bounds
        self.geometry = self.compute_geometry(int(x1 - x0), int(y1 - y0))
        self.photo = ImageTk.PhotoImage(self.compose(int(x1 - x0), int(y1 - y0)), master=self.host.root)
        self.host.canvas.itemconfigure(self.item, image=self.photo)
        self.host.hit_regions = [region for region in self.host.hit_regions if not self.is_action(region.name)]
        self.add_regions()

    def add_regions(self) -> None:
        x0, y0 = self.bounds[0], self.bounds[1]
        add_bar_regions(self.host, (x0, y0), self.geometry["icons"], "")
        box = self.geometry["browse"]
        self.host.add_region("inspect:browse", (x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]))
        fx0, fy0, fx1, fy1 = self.geometry["files"]
        for index, summary in enumerate(self.files()):
            top = fy0 + 6 + index * FILE_ROW - self.file_scroll
            bottom = top + FILE_ROW - 4
            if bottom <= fy0 + 4 or top >= fy1 - 4:
                continue
            self.host.add_region(f"inspect:file:{summary.key}", (x0 + fx0 + 6, y0 + max(top, fy0 + 4), x0 + fx1 - 14, y0 + min(bottom, fy1 - 4)))
        cy0, cy1 = self.geometry["chip_row"][1], self.geometry["chip_row"][3]
        for key, label, cx0, cx1 in self.geometry["chips"]:
            self.host.add_region(f"inspect:chip:{key}", (x0 + cx0, y0 + cy0, x0 + cx1, y0 + cy1))
        rx0, ry0, rx1, ry1 = self.geometry["rows"]
        rows = self.visible_rows()
        first = int(self.row_scroll // OBJECT_ROW)
        for index in range(first, min(len(rows), first + int((ry1 - ry0) // OBJECT_ROW) + 2)):
            top = ry0 + 6 + index * OBJECT_ROW - self.row_scroll
            bottom = top + OBJECT_ROW - 4
            if bottom <= ry0 + 4 or top >= ry1 - 4:
                continue
            self.host.add_region(f"inspect:row:{index}", (x0 + rx0 + 6, y0 + max(top, ry0 + 4), x0 + rx1 - 14, y0 + min(bottom, ry1 - 4)))
        for action, label, enabled, (bx0, by0, bx1, by1) in self.geometry["buttons"]:
            if enabled:
                self.host.add_region(action, (x0 + bx0, y0 + by0, x0 + bx1, y0 + by1))
        for name in ("files", "rows"):
            sx0, sy0, sx1, sy1 = self.screen_box(name)
            self.host.add_scroll_track(
                f"inspect:scrollbar:{name}",
                (sx1 - 13, sy0 + 8, sx1 - 3, sy1 - 8),
                self.content_height(name),
                sy1 - sy0,
                lambda name=name: self.file_scroll if name == "files" else self.row_scroll,
                lambda value, name=name: self.scroll_to(name, value),
            )

    def scroll_to(self, name: str, value: float) -> None:
        if self.set_scroll(name, value):
            self.redraw()

    def layer(self, name: str, key: tuple[Any, ...], render: Callable[[], Image.Image]) -> Image.Image:
        cached = self.layers.get(name)
        if cached is None or cached[0] != key:
            cached = (key, render())
            self.layers[name] = cached
        return cached[1]

    def compose(self, width: int, height: int) -> Image.Image:
        hover = self.host.hover_action
        image = self.base(width, height, hovered_icon(hover)).copy()
        rows = self.visible_rows()
        geometry = self.geometry
        inspected = self.current()
        cx0, cy0, cx1, cy1 = (int(value) for value in geometry["column"])
        browse_hover = hover == "inspect:browse"
        title_key = (self.scan_state, int(self.scan_progress * 100), len(self.catalog), browse_hover, cx1 - cx0, cy1 - cy0, self.subtitle())
        image.alpha_composite(self.layer("title", title_key, lambda: self.render_title(cx1 - cx0, cy1 - cy0, browse_hover)), (cx0, cy0))
        fx0, fy0, fx1, fy1 = (int(value) for value in geometry["files"])
        loaded_counts = tuple(item.objects for item in self.opened)
        files_key = (self.scan_state, len(self.catalog), len(self.opened), self.selected_key, self.file_scroll, loaded_counts, fx1 - fx0, fy1 - fy0)
        image.alpha_composite(self.layer("files", files_key, lambda: self.render_files(fx1 - fx0, fy1 - fy0)), (fx0, fy0))
        hx0, hy0, hx1, hy1 = (int(value) for value in geometry["header"])
        chip_hover = hover[len("inspect:chip:"):] if hover.startswith("inspect:chip:") else ""
        header_key = (self.selected_key, inspected is not None, self.query, self.type_filter, len(rows), chip_hover, tuple(chip[0] for chip in geometry["chips"]), hx1 - hx0)
        image.alpha_composite(self.layer("header", header_key, lambda: self.render_header(hx1 - hx0, hy1 - hy0, chip_hover)), (hx0, hy0))
        rx0, ry0, rx1, ry1 = (int(value) for value in geometry["rows"])
        state = (self.selected_key in self.loading, self.load_errors.get(self.selected_key, ""), self.scan_state, inspected is not None)
        rows_key = (self.filter_version, self.row_scroll, self.selected_row, state, rx1 - rx0, ry1 - ry0)
        image.alpha_composite(self.layer("rows", rows_key, lambda: self.render_rows(rx1 - rx0, ry1 - ry0)), (rx0, ry0))
        ox0, oy0, ox1, oy1 = (int(value) for value in geometry["footer"])
        button_hover = hover if any(spec[0] == hover for spec in geometry["buttons"]) else ""
        spin = int(time.perf_counter() * 8) % 12 if self.host.running else -1
        footer_key = (tuple((spec[0], spec[2]) for spec in geometry["buttons"]), button_hover, self.status_line(), spin, ox1 - ox0)
        image.alpha_composite(self.layer("footer", footer_key, lambda: self.render_footer(ox1 - ox0, oy1 - oy0, button_hover)), (ox0, oy0))
        overlay = ImageDraw.Draw(image)
        if inspected is not None and self.caret_on:
            sx0, sy0, sx1, sy1 = geometry["search"]
            caret = sx0 + self.caret_x
            overlay.line((caret, sy0 + 11, caret, sy1 - 11), fill=WHITE, width=2)
        if hover.startswith("inspect:file:") and hover[len("inspect:file:"):] != self.selected_key:
            index = next((position for position, item in enumerate(self.files()) if item.key == hover[len("inspect:file:"):]), -1)
            self.hover_box(image, geometry["files"], index, FILE_ROW, self.file_scroll)
        if hover.startswith("inspect:row:"):
            index = int(hover.split(":", 2)[2])
            if index != self.selected_row:
                self.hover_box(image, geometry["rows"], index, OBJECT_ROW, self.row_scroll)
        return image

    def hover_box(self, image: Image.Image, box: tuple[float, float, float, float], index: int, row_height: int, scroll: float) -> None:
        if index < 0:
            return
        x0, y0, x1, y1 = (int(value) for value in box)
        top = int(y0 + 6 + index * row_height - scroll)
        if y0 + 4 <= top and top + row_height - 4 <= y1 - 4:
            image.alpha_composite(Image.new("RGBA", (x1 - x0 - 20, row_height - 4), (255, 255, 255, 34)), (x0 + 6, top))

    def base(self, width: int, height: int, icon_hover: str) -> Image.Image:
        key = (width, height, icon_hover)
        if key not in self.base_cache:
            if any(cached[:2] != (width, height) for cached in self.base_cache):
                self.base_cache = {}
            s = SCALE
            image = Image.new("RGBA", (width, height), PANEL_BLUE + (255,))
            shapes = panel_frame(width, height)
            boxes = Image.new("RGBA", shapes.size, CLEAR)
            draw = ImageDraw.Draw(boxes)
            sx0, sy0, sx1, sy1 = (value * s for value in self.geometry["search"])
            draw.rounded_rectangle((sx0, sy0, sx1, sy1), (sy1 - sy0) / 2, fill=(255, 255, 255, 58), outline=(255, 255, 255, 220), width=2 * s)
            for name in ("files", "rows"):
                bx0, by0, bx1, by1 = (value * s for value in self.geometry[name])
                draw.rounded_rectangle((bx0, by0, bx1, by1), 14 * s, fill=(255, 255, 255, 34), outline=(255, 255, 255, 150), width=2 * s)
            shapes.alpha_composite(glow(boxes, 6))
            shapes.alpha_composite(boxes)
            icon = Image.new("RGBA", shapes.size, CLEAR)
            draw_icon(ImageDraw.Draw(icon), "search", sx0 + 26 * s, (sy0 + sy1) / 2, 18 * s, WHITE)
            shapes.alpha_composite(glow(icon, 3))
            shapes.alpha_composite(icon)
            image.alpha_composite(shapes.resize((width, height), Image.LANCZOS))
            image.alpha_composite(render_icon_bar(width, height, self.geometry["bar"], self.geometry["icons"], "", icon_hover))
            self.base_cache[key] = image
        return self.base_cache[key]

    def draw_button(self, shape_draw: ImageDraw.ImageDraw, text_draw: ImageDraw.ImageDraw, plain_draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], label: str, hovered: bool, enabled: bool) -> None:
        bx0, by0, bx1, by1 = box
        if not enabled:
            shape_draw.rounded_rectangle(box, (by1 - by0) / 2, fill=(255, 255, 255, 24), outline=(255, 255, 255, 110), width=2 * SCALE)
            text_draw.text(((bx0 + bx1) / 2, (by0 + by1) / 2), label, font=load_font("segoeuib.ttf", 10), fill=(255, 255, 255, 130), anchor="mm")
            return
        shape_draw.rounded_rectangle(box, (by1 - by0) / 2, fill=(255, 255, 255, 235 if hovered else 70), outline=(255, 255, 255, 245), width=2 * SCALE)
        (plain_draw if hovered else text_draw).text(((bx0 + bx1) / 2, (by0 + by1) / 2), label, font=load_font("segoeuib.ttf", 10), fill=DARK_TEXT if hovered else WHITE, anchor="mm")

    def flatten(self, shapes: Image.Image, text: Image.Image, plain: Image.Image, width: int, height: int, highlight: Image.Image | None = None) -> Image.Image:
        image = glow(highlight, 5) if highlight is not None else glow(shapes, 4)
        image.alpha_composite(shapes)
        if highlight is not None:
            image.alpha_composite(highlight)
        image.alpha_composite(text_shadow(text))
        image.alpha_composite(text)
        image.alpha_composite(plain)
        return image.resize((width, height), Image.LANCZOS)

    def subtitle(self) -> str:
        summary = self.summary()
        if summary is not None and not summary.in_data:
            if summary.key in self.load_errors:
                return f"Could not read {summary.label}"[:70]
            inspected = self.inspected.get(summary.key)
            if inspected is None:
                return f"Reading {summary.label}"[:70]
            return f"{summary.label}  {len(inspected.rows)} objects"[:70]
        return {
            "scanning": f"Reading file headers  {int(self.scan_progress * 100)}%",
            "ready": f"{len(self.catalog)} files in RWBY Data",
            "missing": "The saved data folder has no game files, use Open Bundle",
            "failed": "File headers could not be read",
        }.get(self.scan_state, "Opening RWBY Data")

    def render_title(self, width: int, height: int, browse_hover: bool) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        ox, oy = self.geometry["column"][0], self.geometry["column"][1]
        shapes, text, plain = (Image.new("RGBA", size, CLEAR) for index in range(3))
        text_draw = ImageDraw.Draw(text)
        text_draw.text((0, 0), "Bundle Inspector", font=load_font("segoeuiz.ttf", 24), fill=HEADER_COLOUR, anchor="lt")
        text_draw.text((2 * s, 34 * s), self.subtitle(), font=load_font("segoeuib.ttf", 10), fill=(255, 255, 255, 225), anchor="lt")
        bx0, by0, bx1, by1 = self.geometry["browse"]
        box = ((bx0 - ox) * s, (by0 - oy) * s, (bx1 - ox) * s, (by1 - oy) * s)
        self.draw_button(ImageDraw.Draw(shapes), text_draw, ImageDraw.Draw(plain), box, "Open Bundle", browse_hover, True)
        return self.flatten(shapes, text, plain, width, height)

    def render_files(self, width: int, height: int) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        shapes, highlight, text, plain = (Image.new("RGBA", size, CLEAR) for index in range(4))
        shape_draw = ImageDraw.Draw(shapes)
        highlight_draw = ImageDraw.Draw(highlight)
        text_draw = ImageDraw.Draw(text)
        plain_draw = ImageDraw.Draw(plain)
        files = self.files()
        if not files:
            title = {"missing": "No game files here", "failed": "Could not read RWBY Data"}.get(self.scan_state, "Reading file headers")
            detail = {
                "missing": "Pick the RWBY Data folder on the home screen.",
                "failed": self.scan_message[:60],
            }.get(self.scan_state, "This takes a few seconds.")
            text_draw.text((size[0] / 2, size[1] / 2 - 10 * s), title, font=load_font("segoeuiz.ttf", 13), fill=WHITE, anchor="mm")
            text_draw.text((size[0] / 2, size[1] / 2 + 12 * s), detail, font=load_font("segoeui.ttf", 9), fill=(255, 255, 255, 220), anchor="mm")
            return self.flatten(shapes, text, plain, width, height, highlight)
        name_font = load_font("segoeuiz.ttf", 12)
        info_font = load_font("segoeui.ttf", 9)
        right = size[0] - 14 * s
        first = int(self.file_scroll // FILE_ROW)
        last = min(len(files), first + int(height // FILE_ROW) + 2)
        for index in range(first, last):
            summary = files[index]
            top = (6 + index * FILE_ROW - self.file_scroll) * s
            box = (6 * s, top, right, top + (FILE_ROW - 4) * s)
            selected = summary.key == self.selected_key
            draw = plain_draw if selected else text_draw
            if selected:
                highlight_draw.rounded_rectangle(box, 9 * s, fill=(255, 255, 255, 232), outline=(255, 255, 255, 255), width=2 * s)
            elif index % 2 == 0:
                shape_draw.rounded_rectangle(box, 9 * s, fill=(255, 255, 255, 18))
            room = right - box[0] - 24 * s
            draw.text((box[0] + 12 * s, top + 12 * s), fit_text(summary.label, name_font, room), font=name_font, fill=DARK_TEXT if selected else WHITE, anchor="lm")
            count = summary.objects
            parts = ["Bundle" if summary.kind == "bundle" else ("Game file" if summary.in_data else "Serialized file")]
            if count >= 0:
                parts.append(f"{count:,} object{'s' if count != 1 else ''}")
            parts.append(human_size(summary.size))
            draw.text((box[0] + 12 * s, top + 27 * s), "  |  ".join(parts), font=info_font, fill=(14, 70, 88, 215) if selected else (255, 255, 255, 215), anchor="lm")
        self.draw_scrollbar(shape_draw, size, height, self.content_height("files"), self.file_scroll)
        return self.flatten(shapes, text, plain, width, height, highlight)

    def draw_scrollbar(self, draw: ImageDraw.ImageDraw, size: tuple[int, int], height: int, content: float, scroll: float) -> None:
        if content <= height:
            return
        s = SCALE
        track_x = size[0] - 10 * s
        draw.rounded_rectangle((track_x, 8 * s, track_x + 4 * s, size[1] - 8 * s), 2 * s, fill=(255, 255, 255, 70))
        track_h = size[1] - 16 * s
        thumb_h = max(24 * s, track_h * height / content)
        thumb_y = 8 * s + (track_h - thumb_h) * scroll / max(1.0, content - height)
        draw.rounded_rectangle((track_x - s, thumb_y, track_x + 5 * s, thumb_y + thumb_h), 3 * s, fill=(255, 255, 255, 235))

    def render_header(self, width: int, height: int, chip_hover: str) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        ox, oy = self.geometry["header"][0], self.geometry["header"][1]
        shapes, text, plain = (Image.new("RGBA", size, CLEAR) for index in range(3))
        shape_draw = ImageDraw.Draw(shapes)
        text_draw = ImageDraw.Draw(text)
        plain_draw = ImageDraw.Draw(plain)
        summary = self.summary()
        inspected = self.current()
        title = summary.label if summary is not None else "No file selected"
        text_draw.text((2 * s, 2 * s), fit_text(title, load_font("segoeuiz.ttf", 17), size[0] - 8 * s), font=load_font("segoeuiz.ttf", 17), fill=WHITE, anchor="lt")
        if inspected is not None:
            info = inspected.info
        elif summary is not None and summary.key in self.load_errors:
            info = "This file couldnt be read"
        elif summary is not None:
            info = "Reading objects"
        else:
            info = "Pick a file on the left or open a bundle from anywhere."
        text_draw.text((3 * s, 30 * s), fit_text(info, load_font("segoeuib.ttf", 9), size[0] - 8 * s), font=load_font("segoeuib.ttf", 9), fill=(255, 255, 255, 220), anchor="lm")
        sx0, sy0, sx1, sy1 = self.geometry["search"]
        search_y = ((sy0 + sy1) / 2 - oy) * s
        left = (sx0 - ox + 46) * s
        query_font = load_font("segoeuiz.ttf", 14)
        status_font = load_font("segoeuib.ttf", 10)
        status = ""
        if inspected is not None:
            rows = self.visible_rows()
            total = len(inspected.rows)
            status = f"{len(rows):,} of {total:,} objects" if len(rows) != total else f"{total:,} objects"
        status_w = status_font.getlength(status) if status else 0
        text_draw.text(((sx1 - ox - 22) * s, search_y), status, font=status_font, fill=(255, 255, 255, 235), anchor="rm")
        room = (sx1 - ox - 22) * s - left - status_w - 24 * s
        if self.query and inspected is not None:
            shown = self.query
            while shown and query_font.getlength(shown) > room:
                shown = shown[1:]
            text_draw.text((left, search_y), shown, font=query_font, fill=WHITE, anchor="lm")
            self.caret_x = (left + query_font.getlength(shown)) / s - (sx0 - ox) + 3
        else:
            hint = "Type to filter by name, type/path ID" if inspected is not None else "Waiting for a file"
            text_draw.text((left, search_y), hint, font=query_font, fill=(255, 255, 255, 150), anchor="lm")
            self.caret_x = left / s - (sx0 - ox)
        chip_font = load_font("segoeuib.ttf", 10)
        cy0, cy1 = self.geometry["chip_row"][1], self.geometry["chip_row"][3]
        for key, label, x0, x1 in self.geometry["chips"]:
            box = ((x0 - ox) * s, (cy0 - oy) * s, (x1 - ox) * s, (cy1 - oy) * s)
            active = key == self.type_filter
            hovered = key == chip_hover
            fill = (255, 255, 255, 235) if active else (255, 255, 255, 90 if hovered else 40)
            shape_draw.rounded_rectangle(box, (box[3] - box[1]) / 2, fill=fill, outline=(255, 255, 255, 240 if active or hovered else 170), width=2 * s)
            text_x = box[0] + 11 * s
            mid = (box[1] + box[3]) / 2
            if key not in ("all", "+more"):
                shape_draw.ellipse((text_x, mid - 4 * s, text_x + 8 * s, mid + 4 * s), fill=type_colour(key) + (255,), outline=(255, 255, 255, 230), width=s)
                text_x += 14 * s
            (plain_draw if active else text_draw).text((text_x, mid), label, font=chip_font, fill=DARK_TEXT if active else WHITE, anchor="lm")
        return self.flatten(shapes, text, plain, width, height)

    def render_rows(self, width: int, height: int) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        shapes, highlight, text, plain = (Image.new("RGBA", size, CLEAR) for index in range(4))
        shape_draw = ImageDraw.Draw(shapes)
        highlight_draw = ImageDraw.Draw(highlight)
        text_draw = ImageDraw.Draw(text)
        plain_draw = ImageDraw.Draw(plain)
        summary = self.summary()
        inspected = self.current()
        rows = self.visible_rows()
        title_font = load_font("segoeuiz.ttf", 15)
        body_font = load_font("segoeui.ttf", 10)
        cx, cy = size[0] / 2, size[1] / 2
        if inspected is None:
            if summary is not None and summary.key in self.load_errors:
                title, detail = "This file couldnt be read", self.load_errors[summary.key][:90]
            elif summary is not None:
                title, detail = "Reading objects", "Big files like sharedassets0 take a moment."
                radius = 26 * s
                ring = (cx - radius, cy - 52 * s - radius, cx + radius, cy - 52 * s + radius)
                shape_draw.ellipse(ring, outline=(255, 255, 255, 70), width=4 * s)
                shape_draw.arc(ring, -90, 30, fill=WHITE, width=4 * s)
            elif self.scan_state == "missing":
                title, detail = "No game files found", f"{self.host.data_root} has no Unity serialized files."
            else:
                title, detail = "Reading RWBY Data", "File headers load first, then pick a file."
            text_draw.text((cx, cy), title, font=title_font, fill=WHITE, anchor="mm")
            text_draw.text((cx, cy + 24 * s), detail, font=body_font, fill=(255, 255, 255, 220), anchor="mm")
        elif not rows:
            message = f"No objects match {self.query.strip()}" if self.query.strip() else "This file has no objects"
            text_draw.text((cx, cy), message, font=title_font, fill=WHITE, anchor="mm")
        else:
            name_font = load_font("segoeuiz.ttf", 12)
            info_font = load_font("segoeui.ttf", 9)
            right = size[0] - 14 * s
            first = int(self.row_scroll // OBJECT_ROW)
            last = min(len(rows), first + int(height // OBJECT_ROW) + 2)
            for index in range(first, last):
                name, type_name, path_id, byte_size, search = rows[index]
                top = (6 + index * OBJECT_ROW - self.row_scroll) * s
                box = (6 * s, top, right, top + (OBJECT_ROW - 4) * s)
                selected = index == self.selected_row
                draw = plain_draw if selected else text_draw
                if selected:
                    highlight_draw.rounded_rectangle(box, 9 * s, fill=(255, 255, 255, 232), outline=(255, 255, 255, 255), width=2 * s)
                elif index % 2 == 0:
                    shape_draw.rounded_rectangle(box, 9 * s, fill=(255, 255, 255, 18))
                dot_x = box[0] + 14 * s
                mid = (box[1] + box[3]) / 2
                (plain_draw if selected else shape_draw).ellipse((dot_x - 5 * s, mid - 5 * s, dot_x + 5 * s, mid + 5 * s), fill=type_colour(type_name) + (255,), outline=(14, 70, 88, 200) if selected else (255, 255, 255, 230), width=s)
                text_left = dot_x + 14 * s
                info = f"{type_name}|Path ID {path_id}|{human_size(byte_size)}"
                info_w = info_font.getlength(info)
                draw.text((right - 12 * s, mid), info, font=info_font, fill=(14, 70, 88, 215) if selected else (255, 255, 255, 215), anchor="rm")
                room = right - 24 * s - info_w - text_left
                draw.text((text_left, mid), fit_text(name or "Unnamed", name_font, room), font=name_font, fill=(DARK_TEXT if selected else WHITE) if name else ((14, 70, 88, 170) if selected else (255, 255, 255, 150)), anchor="lm")
            self.draw_scrollbar(shape_draw, size, height, self.content_height("rows"), self.row_scroll)
        return self.flatten(shapes, text, plain, width, height, highlight)

    def render_footer(self, width: int, height: int, button_hover: str) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        ox, oy = self.geometry["footer"][0], self.geometry["footer"][1]
        shapes, text, plain = (Image.new("RGBA", size, CLEAR) for index in range(3))
        shape_draw = ImageDraw.Draw(shapes)
        text_draw = ImageDraw.Draw(text)
        plain_draw = ImageDraw.Draw(plain)
        buttons = self.geometry["buttons"]
        for action, label, enabled, (bx0, by0, bx1, by1) in buttons:
            box = ((bx0 - ox) * s, (by0 - oy) * s, (bx1 - ox) * s, (by1 - oy) * s)
            self.draw_button(shape_draw, text_draw, plain_draw, box, label, action == button_hover, enabled)
        status = self.status_line()
        if status:
            room = ((buttons[0][3][0] if buttons else ox + width) - ox - 16) * s
            text_x = 4 * s
            if self.host.running:
                cy = size[1] / 2
                start = int(time.perf_counter() * 8) % 12 * 30
                shape_draw.arc((text_x, cy - 8 * s, text_x + 16 * s, cy + 8 * s), start, start + 100, fill=WHITE, width=3 * s)
                text_x += 24 * s
                room -= 24 * s
            text_draw.text((text_x, size[1] / 2), fit_text(status, load_font("segoeuiz.ttf", 10), room), font=load_font("segoeuiz.ttf", 10), fill=WHITE, anchor="lm")
        return self.flatten(shapes, text, plain, width, height)
