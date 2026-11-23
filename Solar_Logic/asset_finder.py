from __future__ import annotations
import json, os, re, subprocess, threading, time
from pathlib import Path
from typing import Any, Callable
from PIL import Image, ImageDraw, ImageTk
from .taildata import EXTERNAL_TAILDATA_FILENAME
from .updates_panel import (
    CLEAR,
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

INDEX_FILENAME = "sunflowyr_finder_index.tsv"
INDEX_HEADER = "sunflowyr-finder 2"
SECTION_MARK = "\x1e"
ROW_HEIGHT = 38
SEARCH_DELAY_MS = 70
CARET_SECONDS = 0.53
THUMB_LIMIT = 32
DARK_TEXT = (14, 70, 88, 255)
WHITE = (255, 255, 255, 255)
CATEGORIES = (
    ("models", "Models", (204, 52, 68)),
    ("textures", "Textures", (236, 246, 255)),
    ("audio", "Audio", (238, 186, 46)),
    ("animation", "Animation", (124, 88, 172)),
    ("materials", "Materials", (212, 134, 58)),
    ("shaders", "Shaders", (226, 96, 160)),
    ("text", "Text and UI", (58, 160, 108)),
    ("data", "Data", (58, 92, 126)),
    ("raw", "Raw", (34, 44, 58)),
)
CATEGORY_LABELS = {key: label for key, label, colour in CATEGORIES}
CATEGORY_COLOURS = {key: colour for key, label, colour in CATEGORIES}
CONVERTED_CATEGORIES = {
    "mesh_glb": "models",
    "glb": "models",
    "png": "textures",
    "sprite_png": "textures",
    "audio": "audio",
    "animation": "animation",
    "material_json": "materials",
    "shader": "shaders",
    "text_asset": "text",
    "textmesh_json": "text",
    "ui_json": "text",
    "font": "text",
    "font_json": "text",
}
PATH_ID_PATTERN = re.compile(r"__path(-?\d+)")
QUERY_WORD_PATTERN = re.compile(r'bundle:"[^"]*"?|"[^"]*"?|\S+')
CLASS_FOLDER_PATTERN = re.compile(r"^\d+_(.+)$")
CONTAINER_PREFIX = b'      "container": "'
RESOURCE_PREFIX = b'      "resourceContainer": "'

def index_line(relative: str, container: str, resource: str) -> str:
    if resource and resource.lower() != container.lower():
        return f"{relative}\t{container}\t{resource}"
    return f"{relative}\t{container}"

def line_files(line: str) -> list[str]:
    return line.split("\t")[1:]

def query_words(query: str) -> list[str]:
    words = []
    for match in QUERY_WORD_PATTERN.finditer(query.lower()):
        word = match.group(0).replace('"', "")
        if word.startswith("bundle:"):
            word = "bundle:" + word[len("bundle:"):].replace("\\", "/").strip("/")
        if word and word != "bundle:":
            words.append(word)
    return words

def file_matches(line: str, wanted: str) -> bool:
    return any(field == wanted or field.rsplit("/", 1)[-1] == wanted for field in line_files(line))

def bundle_query(name: str) -> str:
    return f'bundle:"{name.lower()}"' if " " in name else f"bundle:{name.lower()}"

def category_for(relative: str) -> str:
    if relative.startswith("raw_objects/"):
        return "raw"
    parts = relative.split("/", 2)
    if len(parts) > 1 and parts[0] == "converted":
        return CONVERTED_CATEGORIES.get(parts[1], "data")
    return "data"

def describe(line: str) -> dict[str, str]:
    fields = line.split("\t")
    relative = fields[0]
    container = fields[1] if len(fields) > 1 else ""
    resource = fields[2] if len(fields) > 2 else ""
    parts = relative.split("/")
    filename = parts[-1]
    stem, dot, extension = filename.rpartition(".")
    if not dot:
        stem, extension = filename, ""
    match = PATH_ID_PATTERN.search(stem)
    folder = parts[-2] if len(parts) > 1 else ""
    class_match = CLASS_FOLDER_PATTERN.match(folder)
    return {
        "line": line,
        "relative": relative,
        "container": container,
        "resource": resource,
        "name": (stem[: match.start()] if match else stem) or filename,
        "extension": extension.lower(),
        "path_id": match.group(1) if match else "",
        "class_name": class_match.group(1) if class_match else folder,
        "category": category_for(relative),
    }

def decode_json_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if "\\" in text:
        try:
            text = json.loads(f'"{text}"')
        except ValueError:
            pass
    return text.replace("\t", " ").replace("\n", " ")

def path_id_matches(line: str, needle: str) -> bool:
    index = line.find(needle)
    while index != -1:
        following = line[index + len(needle): index + len(needle) + 1]
        if following in (".", "_", "\t", ""):
            return True
        index = line.find(needle, index + 1)
    return False

def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"

def fit_text(text: str, font: Any, max_width: float) -> str:
    if font.getlength(text) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.getlength(text[:middle] + "...") <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "..."

class FinderIndex:
    def __init__(self) -> None:
        self.state = "idle"
        self.progress = 0.0
        self.message = ""
        self.root: Path | None = None
        self.sections: dict[str, tuple[str, str]] = {}
        self.counts: dict[str, int] = {}
        self.total = 0
        self.thread: threading.Thread | None = None

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def is_current(self, output_root: Path) -> bool:
        index_path = output_root / INDEX_FILENAME
        taildata = output_root / EXTERNAL_TAILDATA_FILENAME
        try:
            if not index_path.is_file() or index_path.stat().st_mtime < taildata.stat().st_mtime:
                return False
            with index_path.open("r", encoding="utf-8") as handle:
                return handle.readline().strip() == INDEX_HEADER
        except OSError:
            return False

    def ensure(self, output_root: Path, force: bool = False, build: bool = True) -> None:
        output_root = Path(output_root)
        if self.busy():
            return
        if not force and self.root == output_root and self.state == "ready" and self.is_current(output_root):
            return
        if not build and not self.is_current(output_root):
            return
        self.root = output_root
        self.state = "building"
        self.progress = 0.0
        self.message = ""
        self.thread = threading.Thread(target=self.prepare, args=(output_root, force), daemon=True)
        self.thread.start()

    def prepare(self, output_root: Path, force: bool) -> None:
        try:
            taildata = output_root / EXTERNAL_TAILDATA_FILENAME
            index_path = output_root / INDEX_FILENAME
            if not taildata.is_file():
                self.sections, self.counts, self.total = {}, {}, 0
                self.state = "missing"
                return
            if force or not self.is_current(output_root):
                self.write_index(taildata, index_path)
            self.load_index(index_path)
            self.state = "ready"
        except (OSError, ValueError) as exc:
            self.message = str(exc)
            self.state = "failed"

    def write_index(self, taildata: Path, index_path: Path) -> None:
        size = max(1, taildata.stat().st_size)
        grouped: dict[str, list[str]] = {key: [] for key, label, colour in CATEGORIES}
        relative: str | None = None
        container = ""
        resource = ""
        done = 0
        counter = 0
        with taildata.open("rb") as handle:
            for line in handle:
                done += len(line)
                counter += 1
                if counter & 0xFFFF == 0:
                    self.progress = 0.92 * done / size
                if line.startswith(b'    "'):
                    stripped = line.strip()
                    if stripped.endswith(b"{"):
                        if relative is not None:
                            grouped[category_for(relative)].append(index_line(relative, container, resource))
                        key = stripped[1: stripped.rfind(b'":')]
                        relative = decode_json_text(key) if b"/" in key else None
                        container = ""
                        resource = ""
                elif relative is not None and line.startswith(CONTAINER_PREFIX):
                    container = decode_json_text(line.strip()[len(CONTAINER_PREFIX) - 6:].rstrip(b",").rstrip(b'"'))
                elif relative is not None and line.startswith(RESOURCE_PREFIX):
                    resource = decode_json_text(line.strip()[len(RESOURCE_PREFIX) - 6:].rstrip(b",").rstrip(b'"'))
        if relative is not None:
            grouped[category_for(relative)].append(index_line(relative, container, resource))
        temporary = index_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as out:
            out.write(INDEX_HEADER + "\n")
            for key, label, colour in CATEGORIES:
                out.write(f"{SECTION_MARK}{key}\n")
                lines = sorted(grouped[key], key=str.lower)
                if lines:
                    out.write("\n".join(lines) + "\n")
        os.replace(temporary, index_path)
        self.progress = 0.96

    def load_index(self, index_path: Path) -> None:
        text = index_path.read_text(encoding="utf-8")
        sections: dict[str, tuple[str, str]] = {}
        counts: dict[str, int] = {}
        for chunk in text.split(SECTION_MARK)[1:]:
            name, separator, body = chunk.partition("\n")
            body = body.rstrip("\n")
            body = body + "\n" if body else body
            lower = body.lower()
            if len(lower) != len(body):
                lower = "".join(character.lower() if len(character.lower()) == 1 else character for character in body)
            sections[name] = (lower, body)
            counts[name] = body.count("\n")
        self.sections = sections
        self.counts = counts
        self.total = sum(counts.values())
        self.progress = 1.0

    def search(self, query: str, category: str, limit: int | None = None, stop: Callable[[], bool] | None = None) -> tuple[list[str], bool]:
        words = query_words(query)
        if not words or self.state != "ready":
            return [], False
        bundles = [word[len('bundle:'):] for word in words if word.startswith("bundle:") and len(word) > len("bundle:")]
        terms = [word for word in words if not word.startswith("bundle:")]
        names = [category] if category in self.sections else [key for key, label, colour in CATEGORIES]
        results: list[str] = []
        seen: set[str] = set()
        if len(terms) == 1 and terms[0].isdigit() and not bundles:
            needle = f"__path{terms[0]}"
            if self.scan(names, needle, lambda line: path_id_matches(line, needle), results, seen, limit, stop):
                return results, True
        if not terms and not bundles:
            return [], False
        if terms:
            anchor: str | tuple[str, ...] = max(terms, key=len)
        else:
            anchor = tuple(f"{lead}{bundles[0]}{tail}" for lead in ("\t", "/") for tail in ("\t", "\n"))
        full = self.scan(names, anchor, lambda line: all(term in line for term in terms) and all(file_matches(line, bundle) for bundle in bundles), results, seen, limit, stop)
        return results, full

    def locate(self, folder: str, path_id: int, container: str, section: str = "data") -> str | None:
        lower, original = self.sections.get(section, ("", ""))
        needle = f"__path{path_id}"
        wanted = container.lower()
        prefix = folder.lower()
        position = lower.find(needle)
        while position != -1:
            start = lower.rfind("\n", 0, position) + 1
            end = lower.find("\n", position)
            if end == -1:
                end = len(lower)
            line = lower[start:end]
            if line.startswith(prefix) and line_files(line)[:1] == [wanted] and path_id_matches(line, needle):
                return original[start:end].partition("\t")[0]
            position = lower.find(needle, end)
        return None

    def scan(self, names: list[str], anchor: str | tuple[str, ...], accept: Callable[[str], bool], results: list[str], seen: set[str], limit: int | None, stop: Callable[[], bool] | None = None) -> bool:
        checked = 0
        anchors = (anchor,) if isinstance(anchor, str) else anchor
        upcoming: list[int] = []

        def find(text: str, start: int) -> int:
            for slot, needle in enumerate(anchors):
                if upcoming[slot] != -1 and upcoming[slot] < start:
                    upcoming[slot] = text.find(needle, start)
            found = [value for value in upcoming if value != -1]
            return min(found) if found else -1

        for name in names:
            lower, original = self.sections.get(name, ("", ""))
            upcoming[:] = [lower.find(needle) for needle in anchors]
            found = [value for value in upcoming if value != -1]
            position = min(found) if found else -1
            while position != -1:
                checked += 1
                if stop is not None and checked & 0xFFF == 0 and stop():
                    return False
                start = lower.rfind("\n", 0, position) + 1
                end = lower.find("\n", position)
                if end == -1:
                    end = len(lower)
                if accept(lower[start:end]):
                    line = original[start:end]
                    if line not in seen:
                        seen.add(line)
                        results.append(line)
                        if limit is not None and len(results) >= limit:
                            return True
                position = find(lower, end)
        return False

class AssetFinderPanel:
    def __init__(self, host: Any) -> None:
        self.host = host
        self.index = FinderIndex()
        self.query = ""
        self.searched_query = ""
        self.category = "all"
        self.results: list[str] = []
        self.results_full = False
        self.result_version = 0
        self.search_ms = 0.0
        self.selected = -1
        self.scroll_y = 0.0
        self.content_height = 1.0
        self.bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.drag_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.viewport: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.geometry: dict[str, Any] = {}
        self.layers: dict[str, tuple[Any, Image.Image]] = {}
        self.chip_cache: dict[tuple[Any, ...], Image.Image] = {}
        self.base_cache: dict[tuple[Any, ...], Image.Image] = {}
        self.thumbs: dict[str, Image.Image | None] = {}
        self.photo: ImageTk.PhotoImage | None = None
        self.item: int | None = None
        self.search_job: str | None = None
        self.search_token = 0
        self.searching = False
        self.finished_search: tuple[int, str, list[str], bool, float] | None = None
        self.caret_on = True
        self.caret_time = time.perf_counter()
        self.caret_x = 0.0
        self.notice = ""
        self.seen_state: tuple[str, int] = ("", -1)

    def activate(self) -> None:
        self.notice = ""
        self.index.ensure(self.host.output_root)

    def warm(self) -> None:
        self.index.ensure(self.host.output_root, build=False)

    def is_action(self, action: str) -> bool:
        return action.startswith("finder:")

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.viewport
        return x0 <= x <= x1 and y0 <= y <= y1

    def scroll_from_delta(self, start_scroll: float, dy: float) -> None:
        self.scroll_y = self.clamp_scroll(float(start_scroll) - float(dy))

    def scroll_by(self, amount: float) -> None:
        step = ROW_HEIGHT * 3 if amount > 0 else -ROW_HEIGHT * 3
        next_value = self.clamp_scroll(self.scroll_y + step)
        if next_value != self.scroll_y:
            self.scroll_y = next_value
            self.redraw()

    def clamp_scroll(self, value: float) -> float:
        view_height = max(1.0, self.viewport[3] - self.viewport[1])
        return max(0.0, min(float(value), max(0.0, self.content_height - view_height)))

    def selected_entry(self) -> dict[str, str] | None:
        if 0 <= self.selected < len(self.results):
            return describe(self.results[self.selected])
        return None

    def selected_path(self) -> Path | None:
        entry = self.selected_entry()
        if entry is None or self.index.root is None:
            return None
        return self.index.root / entry["relative"]

    def handle_key(self, event: Any) -> bool:
        keysym = str(getattr(event, "keysym", ""))
        char = str(getattr(event, "char", ""))
        control = bool(int(getattr(event, "state", 0) or 0) & 0x4)
        if keysym == "Escape":
            if self.query:
                self.set_query("")
                return True
            return False
        if self.index.state != "ready":
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
            page = max(1, int((self.viewport[3] - self.viewport[1]) // ROW_HEIGHT))
            step = {"Up": -1, "Down": 1, "Prior": -page, "Next": page}[keysym]
            self.select(max(0, min(len(self.results) - 1, self.selected + step)))
            return True
        if keysym in ("Return", "KP_Enter"):
            self.perform_action("finder:open")
            return True
        if control and keysym.lower() == "v":
            try:
                pasted = self.host.root.clipboard_get()
            except Exception:
                pasted = ""
            self.set_query((self.query + " ".join(str(pasted).split()))[:120])
            return True
        if control and keysym.lower() == "c":
            self.perform_action("finder:copy")
            return True
        if char and char.isprintable() and not control and len(self.query) < 120:
            self.set_query(self.query + char)
            return True
        return False

    def preset(self, text: str) -> None:
        self.category = "all"
        self.set_query(text)

    def set_query(self, text: str) -> None:
        self.query = text
        self.caret_on = True
        self.caret_time = time.perf_counter()
        if self.search_job is not None:
            self.host.root.after_cancel(self.search_job)
        self.search_job = self.host.root.after(SEARCH_DELAY_MS, self.run_search)
        self.redraw()

    def run_search(self) -> None:
        self.search_job = None
        self.search_token += 1
        token, query, category = self.search_token, self.query, self.category
        self.searching = True
        threading.Thread(target=self.search_worker, args=(token, query, category), daemon=True).start()
        self.redraw()

    def search_worker(self, token: int, query: str, category: str) -> None:
        started = time.perf_counter()
        results, full = self.index.search(query, category, None, lambda: token != self.search_token)
        if token == self.search_token:
            self.finished_search = (token, query, results, full, (time.perf_counter() - started) * 1000.0)

    def apply_search(self) -> None:
        token, query, results, full, elapsed = self.finished_search
        self.finished_search = None
        if token != self.search_token:
            return
        self.searching = False
        self.results, self.results_full = results, full
        self.searched_query = query.strip()
        self.search_ms = elapsed
        self.result_version += 1
        self.selected = 0 if self.results else -1
        self.scroll_y = 0.0
        self.content_height = float(len(self.results) * ROW_HEIGHT + 16)
        self.notice = ""
        self.redraw()

    def select(self, index: int) -> None:
        if not self.results:
            return
        self.selected = index
        row_top = index * ROW_HEIGHT
        view_h = self.viewport[3] - self.viewport[1] - 16
        if row_top < self.scroll_y:
            self.scroll_y = float(row_top)
        elif row_top + ROW_HEIGHT > self.scroll_y + view_h:
            self.scroll_y = self.clamp_scroll(row_top + ROW_HEIGHT - view_h)
        self.notice = ""
        self.redraw()

    def perform_action(self, action: str) -> None:
        if action.startswith("finder:chip:"):
            self.category = action.split(":", 2)[2]
            if self.search_job is not None:
                self.host.root.after_cancel(self.search_job)
            self.run_search()
        elif action.startswith("finder:row:"):
            self.select(int(action.split(":", 2)[2]))
        elif action == "finder:open":
            path = self.selected_path()
            if path is not None:
                self.reveal(path)
        elif action == "finder:copy":
            path = self.selected_path()
            if path is not None:
                self.host.root.clipboard_clear()
                self.host.root.clipboard_append(str(path))
                self.notice = "Path copied"
                self.redraw()
        elif action == "finder:reindex":
            self.index.ensure(self.host.output_root, force=True)
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
            self.notice = f"Could not open folder: {exc}"
        self.redraw()

    def poll(self) -> bool:
        if self.host.screen_mode != "finder" or self.item is None or getattr(self.host, "closing", False):
            return False
        if self.finished_search is not None:
            self.apply_search()
            return False
        state = (self.index.state, int(self.index.progress * 100))
        if state != self.seen_state:
            became_ready = state[0] == "ready" and self.seen_state[0] != "ready"
            self.seen_state = state
            if became_ready and self.query:
                self.run_search()
            else:
                self.redraw()
            return False
        now = time.perf_counter()
        if self.index.state == "ready" and now - self.caret_time >= CARET_SECONDS:
            self.caret_on = not self.caret_on
            self.caret_time = now
            self.redraw()
        return False

    def compute_geometry(self, width: int, height: int) -> dict[str, Any]:
        pad = PANEL_PAD
        bar, icons = bar_geometry(width, height)
        search = (pad, pad, width - pad, pad + 50.0)
        chip_y = search[3] + 12.0
        chip_h = 30.0
        font = load_font("segoeuib.ttf", 11)
        chips = []
        x = pad
        for key, label in [("all", "All")] + [(key, label) for key, label, colour in CATEGORIES]:
            chip_w = font.getlength(label) / SCALE + (40.0 if key != "all" else 28.0)
            chips.append((key, label, x, x + chip_w))
            x += chip_w + 8.0
        body_top = chip_y + chip_h + 14.0
        body_bottom = bar[1] - 14.0
        split = pad + (width - 2 * pad) * 0.6
        return {
            "bar": bar,
            "icons": icons,
            "search": search,
            "chips": chips,
            "chip_row": (pad, chip_y, width - pad, chip_y + chip_h),
            "results": (pad, body_top, split - 9.0, body_bottom),
            "details": (split + 9.0, body_top, width - pad, body_bottom),
        }

    def draw(self, hole: tuple[float, float, float, float], below_item: int | None) -> None:
        x0, y0, x1, y1 = (int(round(value)) for value in hole)
        width, height = max(1, x1 - x0), max(1, y1 - y0)
        if (x0, y0, x1, y1) != self.bounds:
            self.layers = {}
        self.bounds = (x0, y0, x1, y1)
        self.geometry = self.compute_geometry(width, height)
        rx0, ry0, rx1, ry1 = self.geometry["results"]
        self.viewport = (x0 + rx0, y0 + ry0, x0 + rx1, y0 + ry1)
        self.drag_bounds = self.viewport
        self.photo = ImageTk.PhotoImage(self.compose(width, height), master=self.host.root)
        self.item = self.host.canvas.create_image(x0, y0, image=self.photo, anchor="nw")
        if below_item is not None:
            self.host.canvas.tag_lower(self.item, below_item)
        self.add_regions()

    def redraw(self) -> None:
        if self.item is None or not self.geometry or self.host.screen_mode != "finder":
            return
        x0, y0, x1, y1 = self.bounds
        self.photo = ImageTk.PhotoImage(self.compose(int(x1 - x0), int(y1 - y0)), master=self.host.root)
        self.host.canvas.itemconfigure(self.item, image=self.photo)
        self.host.hit_regions = [region for region in self.host.hit_regions if not self.is_action(region.name)]
        self.add_regions()

    def add_regions(self) -> None:
        x0, y0 = self.bounds[0], self.bounds[1]
        add_bar_regions(self.host, (x0, y0), self.geometry["icons"], "search")
        if self.index.state != "ready":
            return
        cy0, cy1 = self.geometry["chip_row"][1], self.geometry["chip_row"][3]
        for key, label, cx0, cx1 in self.geometry["chips"]:
            self.host.add_region(f"finder:chip:{key}", (x0 + cx0, y0 + cy0, x0 + cx1, y0 + cy1))
        rx0, ry0, rx1, ry1 = self.geometry["results"]
        first = int(self.scroll_y // ROW_HEIGHT)
        for index in range(first, min(len(self.results), first + int((ry1 - ry0) // ROW_HEIGHT) + 2)):
            top = ry0 + 8 + index * ROW_HEIGHT - self.scroll_y
            bottom = top + ROW_HEIGHT - 4
            if bottom <= ry0 + 4 or top >= ry1 - 4:
                continue
            self.host.add_region(f"finder:row:{index}", (x0 + rx0 + 8, y0 + max(top, ry0 + 4), x0 + rx1 - 16, y0 + min(bottom, ry1 - 4)))
        for action, box in self.button_boxes().items():
            self.host.add_region(action, (x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]))
        if self.searched_query and self.results:
            self.host.add_scroll_track(
                "finder:scrollbar",
                (x0 + rx1 - 13, y0 + ry0 + 8, x0 + rx1 - 3, y0 + ry1 - 8),
                self.content_height,
                ry1 - ry0,
                lambda: self.scroll_y,
                self.scroll_to,
            )

    def scroll_to(self, value: float) -> None:
        next_value = self.clamp_scroll(value)
        if next_value != self.scroll_y:
            self.scroll_y = next_value
            self.redraw()

    def button_boxes(self) -> dict[str, tuple[float, float, float, float]]:
        dx0, dy0, dx1, dy1 = self.geometry["details"]
        if self.selected_entry() is not None:
            middle = (dx0 + dx1) / 2.0
            return {
                "finder:open": (dx0 + 16, dy1 - 46, middle - 6, dy1 - 14),
                "finder:copy": (middle + 6, dy1 - 46, dx1 - 16, dy1 - 14),
            }
        return {"finder:reindex": (dx1 - 136, dy1 - 42, dx1 - 16, dy1 - 14)}

    def layer(self, name: str, key: tuple[Any, ...], render: Callable[[], Image.Image]) -> Image.Image:
        cached = self.layers.get(name)
        if cached is None or cached[0] != key:
            cached = (key, render())
            self.layers[name] = cached
        return cached[1]

    def compose(self, width: int, height: int) -> Image.Image:
        hover = self.host.hover_action
        icon_hover = hovered_icon(hover)
        image = self.base(width, height, icon_hover).copy()
        chip_hover = hover[len("finder:chip:"):] if hover.startswith("finder:chip:") else ""
        row = self.geometry["chip_row"]
        image.alpha_composite(self.chips(width, chip_hover), (0, int(row[1]) - 8))
        sx0, sy0, sx1, sy1 = (int(value) for value in self.geometry["search"])
        status = self.status_text()
        query_image = self.layer("query", (self.query, self.index.state, status, sx1 - sx0), lambda: self.render_query(sx1 - sx0, sy1 - sy0, status))
        image.alpha_composite(query_image, (sx0, sy0))
        rx0, ry0, rx1, ry1 = (int(value) for value in self.geometry["results"])
        progress = int(self.index.progress * 100)
        results_key = (self.result_version, int(self.scroll_y), self.selected, self.index.state, progress, rx1 - rx0, ry1 - ry0)
        image.alpha_composite(self.layer("results", results_key, lambda: self.render_results(rx1 - rx0, ry1 - ry0)), (rx0, ry0))
        dx0, dy0, dx1, dy1 = (int(value) for value in self.geometry["details"])
        button_hover = hover if hover in ("finder:open", "finder:copy", "finder:reindex") else ""
        details_key = (self.result_version, self.selected, self.index.state, progress, self.notice, button_hover, dx1 - dx0, dy1 - dy0)
        image.alpha_composite(self.layer("details", details_key, lambda: self.render_details(dx1 - dx0, dy1 - dy0, button_hover)), (dx0, dy0))
        overlay = ImageDraw.Draw(image)
        if self.index.state == "ready" and self.caret_on:
            caret_x = sx0 + self.caret_x
            overlay.line((caret_x, sy0 + 14, caret_x, sy1 - 14), fill=WHITE, width=2)
        if hover.startswith("finder:row:"):
            index = int(hover.split(":", 2)[2])
            if index != self.selected:
                top = ry0 + 8 + index * ROW_HEIGHT - self.scroll_y
                if ry0 + 4 <= top and top + ROW_HEIGHT - 4 <= ry1 - 4:
                    hover_box = Image.new("RGBA", (rx1 - rx0 - 24, ROW_HEIGHT - 4), (255, 255, 255, 34))
                    image.alpha_composite(hover_box, (rx0 + 8, int(top)))
        return image

    def status_text(self) -> str:
        state = self.index.state
        if state == "building":
            return f"Indexing  {int(self.index.progress * 100)}%"
        if state == "missing":
            return "No unpack found"
        if state == "failed":
            return "Index failed"
        if state != "ready":
            return ""
        if self.searching:
            return "Searching"
        if not self.searched_query:
            return f"{self.index.total:,} assets indexed"
        count = f"{len(self.results):,}+" if self.results_full else f"{len(self.results):,}"
        noun = "match" if len(self.results) == 1 and not self.results_full else "matches"
        timing = "<1" if self.search_ms < 1 else f"{self.search_ms:.0f}"
        return f"{count} {noun}|{timing} ms"

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
            for name in ("results", "details"):
                bx0, by0, bx1, by1 = (value * s for value in self.geometry[name])
                draw.rounded_rectangle((bx0, by0, bx1, by1), 14 * s, fill=(255, 255, 255, 34), outline=(255, 255, 255, 150), width=2 * s)
            shapes.alpha_composite(glow(boxes, 6))
            shapes.alpha_composite(boxes)
            icon = Image.new("RGBA", shapes.size, CLEAR)
            draw_icon(ImageDraw.Draw(icon), "search", sx0 + 30 * s, (sy0 + sy1) / 2, 22 * s, WHITE)
            shapes.alpha_composite(glow(icon, 3))
            shapes.alpha_composite(icon)
            image.alpha_composite(shapes.resize((width, height), Image.LANCZOS))
            image.alpha_composite(render_icon_bar(width, height, self.geometry["bar"], self.geometry["icons"], "search", icon_hover))
            self.base_cache[key] = image
        return self.base_cache[key]

    def chips(self, width: int, hover: str) -> Image.Image:
        key = (width, self.category, hover, self.index.state == "ready")
        if key not in self.chip_cache:
            if len(self.chip_cache) > 48:
                self.chip_cache = {}
            s = SCALE
            row_x0, row_y0, row_x1, row_y1 = self.geometry["chip_row"]
            offset = row_y0 - 8
            size = (width * s, int((row_y1 - row_y0 + 16) * s))
            shapes = Image.new("RGBA", size, CLEAR)
            text = Image.new("RGBA", size, CLEAR)
            shape_draw = ImageDraw.Draw(shapes)
            text_draw = ImageDraw.Draw(text)
            font = load_font("segoeuib.ttf", 11)
            enabled = self.index.state == "ready"
            for chip_key, label, x0, x1 in self.geometry["chips"]:
                box = (x0 * s, (row_y0 - offset) * s, x1 * s, (row_y1 - offset) * s)
                active = chip_key == self.category
                if active:
                    fill, outline, colour = (255, 255, 255, 235), (255, 255, 255, 255), DARK_TEXT
                elif chip_key == hover:
                    fill, outline, colour = (255, 255, 255, 90), (255, 255, 255, 240), WHITE
                else:
                    fill, outline, colour = (255, 255, 255, 40 if enabled else 20), (255, 255, 255, 170 if enabled else 90), WHITE if enabled else (255, 255, 255, 140)
                shape_draw.rounded_rectangle(box, (box[3] - box[1]) / 2, fill=fill, outline=outline, width=2 * s)
                text_x = box[0] + 14 * s
                if chip_key != "all":
                    dot = CATEGORY_COLOURS[chip_key] + (255,)
                    cy = (box[1] + box[3]) / 2
                    shape_draw.ellipse((text_x, cy - 4 * s, text_x + 8 * s, cy + 4 * s), fill=dot, outline=(255, 255, 255, 230), width=s)
                    text_x += 14 * s
                text_draw.text((text_x, (box[1] + box[3]) / 2), label, font=font, fill=colour, anchor="lm")
            layer = glow(shapes, 4)
            layer.alpha_composite(shapes)
            layer.alpha_composite(text)
            self.chip_cache[key] = layer.resize((width, size[1] // s), Image.LANCZOS)
        return self.chip_cache[key]

    def render_query(self, width: int, height: int, status: str) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        text = Image.new("RGBA", size, CLEAR)
        draw = ImageDraw.Draw(text)
        query_font = load_font("segoeuiz.ttf", 17)
        status_font = load_font("segoeuib.ttf", 10)
        left = 56 * s
        status_w = status_font.getlength(status) if status else 0
        draw.text((size[0] - 26 * s, size[1] / 2), status, font=status_font, fill=(255, 255, 255, 235), anchor="rm")
        room = size[0] - left - status_w - 52 * s
        if self.query:
            shown = self.query
            while shown and query_font.getlength(shown) > room:
                shown = shown[1:]
            draw.text((left, size[1] / 2), shown, font=query_font, fill=WHITE, anchor="lm")
            self.caret_x = (left + query_font.getlength(shown)) / s + 3
        else:
            hint = "Search by name, type, bundle or path ID" if self.index.state == "ready" else "Preparing the asset index"
            draw.text((left, size[1] / 2), hint, font=query_font, fill=(255, 255, 255, 150), anchor="lm")
            self.caret_x = left / s
        image = text_shadow(text)
        image.alpha_composite(text)
        return image.resize((width, height), Image.LANCZOS)

    def render_results(self, width: int, height: int) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        shapes = Image.new("RGBA", size, CLEAR)
        highlight = Image.new("RGBA", size, CLEAR)
        text = Image.new("RGBA", size, CLEAR)
        plain = Image.new("RGBA", size, CLEAR)
        shape_draw = ImageDraw.Draw(shapes)
        text_draw = ImageDraw.Draw(text)
        plain_draw = ImageDraw.Draw(plain)
        state = self.index.state
        if state != "ready":
            self.render_index_state(shapes, text, size)
        elif not self.searched_query:
            self.render_tips(text_draw, size)
        elif not self.results:
            message_font = load_font("segoeuiz.ttf", 15)
            text_draw.text((size[0] / 2, size[1] / 2), f"No assets match {self.searched_query}", font=message_font, fill=WHITE, anchor="mm")
        else:
            name_font = load_font("segoeuiz.ttf", 13)
            info_font = load_font("segoeui.ttf", 9.5)
            badge_font = load_font("segoeuib.ttf", 8.5)
            highlight_draw = ImageDraw.Draw(highlight)
            first = int(self.scroll_y // ROW_HEIGHT)
            last = min(len(self.results), first + int(height // ROW_HEIGHT) + 2)
            right = size[0] - 16 * s
            for index in range(first, last):
                entry = describe(self.results[index])
                top = (8 + index * ROW_HEIGHT - self.scroll_y) * s
                box = (8 * s, top, right, top + (ROW_HEIGHT - 4) * s)
                selected = index == self.selected
                row_draw = plain_draw if selected else text_draw
                if selected:
                    highlight_draw.rounded_rectangle(box, 9 * s, fill=(255, 255, 255, 232), outline=(255, 255, 255, 255), width=2 * s)
                elif index % 2 == 0:
                    shape_draw.rounded_rectangle(box, 9 * s, fill=(255, 255, 255, 18))
                colour = CATEGORY_COLOURS.get(entry["category"], (58, 92, 126))
                badge = (box[0] + 8 * s, top + 9 * s, box[0] + 54 * s, top + 25 * s)
                badge_draw = plain_draw if selected else shape_draw
                badge_draw.rounded_rectangle(badge, 8 * s, fill=colour + (255,), outline=(255, 255, 255, 220), width=s)
                if selected or sum(colour) > 600:
                    badge_draw.rounded_rectangle(badge, 8 * s, outline=(14, 70, 88, 160), width=s)
                badge_colour = DARK_TEXT if sum(colour) > 600 else WHITE
                plain_draw.text(((badge[0] + badge[2]) / 2, (badge[1] + badge[3]) / 2), (entry["extension"] or "file").upper()[:5], font=badge_font, fill=badge_colour, anchor="mm")
                text_left = badge[2] + 12 * s
                room = right - text_left - 10 * s
                row_draw.text((text_left, top + 11 * s), fit_text(entry["name"], name_font, room), font=name_font, fill=DARK_TEXT if selected else WHITE, anchor="lm")
                info = "|".join(part for part in (entry["class_name"], entry["container"], f"ID {entry['path_id']}" if entry["path_id"] else "") if part)
                row_draw.text((text_left, top + 26 * s), fit_text(info, info_font, room), font=info_font, fill=(14, 70, 88, 215) if selected else (255, 255, 255, 215), anchor="lm")
            if self.content_height > height:
                track_x = size[0] - 10 * s
                shape_draw.rounded_rectangle((track_x, 8 * s, track_x + 4 * s, size[1] - 8 * s), 2 * s, fill=(255, 255, 255, 70))
                track_h = size[1] - 16 * s
                thumb_h = max(24 * s, track_h * height / self.content_height)
                thumb_y = 8 * s + (track_h - thumb_h) * self.scroll_y / max(1.0, self.content_height - height)
                shape_draw.rounded_rectangle((track_x - s, thumb_y, track_x + 5 * s, thumb_y + thumb_h), 3 * s, fill=(255, 255, 255, 235))
        image = glow(highlight, 5)
        image.alpha_composite(shapes)
        image.alpha_composite(highlight)
        image.alpha_composite(text_shadow(text))
        image.alpha_composite(text)
        image.alpha_composite(plain)
        return image.resize((width, height), Image.LANCZOS)

    def render_index_state(self, shapes: Image.Image, text: Image.Image, size: tuple[int, int]) -> None:
        s = SCALE
        draw = ImageDraw.Draw(shapes)
        text_draw = ImageDraw.Draw(text)
        cx, cy = size[0] / 2, size[1] / 2 - 18 * s
        state = self.index.state
        title_font = load_font("segoeuiz.ttf", 16)
        body_font = load_font("segoeui.ttf", 10)
        if state == "building":
            radius = 38 * s
            ring = (cx - radius, cy - radius, cx + radius, cy + radius)
            draw.ellipse(ring, outline=(255, 255, 255, 70), width=5 * s)
            draw.arc(ring, -90, -90 + max(4.0, 360.0 * self.index.progress), fill=WHITE, width=5 * s)
            text_draw.text((cx, cy), f"{int(self.index.progress * 100)}%", font=load_font("segoeuib.ttf", 15), fill=WHITE, anchor="mm")
            text_draw.text((cx, cy + radius + 22 * s), "Indexing assets", font=title_font, fill=WHITE, anchor="mm")
            text_draw.text((cx, cy + radius + 44 * s), "First search builds a cached index, later visits load it in a moment.", font=body_font, fill=(255, 255, 255, 220), anchor="mm")
        else:
            root = self.host.output_root
            title = "No unpack found" if state == "missing" else "The asset index could not be built"
            detail = f"Run an unpack into {root} first." if state == "missing" else self.index.message[:90]
            text_draw.text((cx, cy), title, font=title_font, fill=WHITE, anchor="mm")
            text_draw.text((cx, cy + 26 * s), detail, font=body_font, fill=(255, 255, 255, 220), anchor="mm")

    def render_tips(self, draw: ImageDraw.ImageDraw, size: tuple[int, int]) -> None:
        s = SCALE
        title_font = load_font("segoeuiz.ttf", 16)
        body_font = load_font("segoeuib.ttf", 10.5)
        x = 26 * s
        y = 30 * s
        draw.text((x, y), "Find anything in the unpack", font=title_font, fill=WHITE, anchor="lm")
        tips = (
            ("ruby", "a name finds every asset with it"),
            ("ruby png", "more words narrow the search"),
            ("4191", "a number finds that Unity path ID"),
            ("sharedassets0", "a bundle name lists its contents"),
            ("bundle:level5", "only that exact file, .resS and .resource work too"),
            ("texture2d", "a Unity type lists every asset of it"),
        )
        for index, (example, meaning) in enumerate(tips):
            row_y = y + (34 + index * 30) * s
            pill_w = body_font.getlength(example) + 24 * s
            draw.rounded_rectangle((x, row_y - 11 * s, x + pill_w, row_y + 11 * s), 11 * s, fill=(255, 255, 255, 60), outline=(255, 255, 255, 220), width=s)
            draw.text((x + pill_w / 2, row_y), example, font=body_font, fill=WHITE, anchor="mm")
            draw.text((x + pill_w + 14 * s, row_y), meaning, font=body_font, fill=(255, 255, 255, 225), anchor="lm")

    def render_details(self, width: int, height: int, button_hover: str) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        shapes = Image.new("RGBA", size, CLEAR)
        text = Image.new("RGBA", size, CLEAR)
        shape_draw = ImageDraw.Draw(shapes)
        text_draw = ImageDraw.Draw(text)
        title_font = load_font("segoeuiz.ttf", 15)
        label_font = load_font("segoeuib.ttf", 9)
        value_font = load_font("segoeui.ttf", 10)
        entry = self.selected_entry() if self.index.state == "ready" else None
        art = Image.new("RGBA", size, CLEAR)
        if entry is not None:
            path = self.index.root / entry["relative"] if self.index.root is not None else None
            preview = 108 * s
            px, py = 16 * s, 16 * s
            colour = CATEGORY_COLOURS.get(entry["category"], (58, 92, 126))
            shape_draw.rounded_rectangle((px, py, px + preview, py + preview), 12 * s, fill=colour + (200,), outline=(255, 255, 255, 240), width=2 * s)
            thumb = self.thumbnail(path, preview - 12 * s) if path is not None and entry["extension"] == "png" else None
            if thumb is not None:
                art.alpha_composite(thumb, (int(px + (preview - thumb.width) / 2), int(py + (preview - thumb.height) / 2)))
            else:
                badge_colour = DARK_TEXT if sum(colour) > 600 else WHITE
                text_draw.text((px + preview / 2, py + preview / 2), (entry["extension"] or "file").upper()[:5], font=load_font("segoeuib.ttf", 20), fill=badge_colour, anchor="mm")
            info_x = px + preview + 14 * s
            room = size[0] - info_x - 14 * s
            name_lines = self.wrap_name(entry["name"], title_font, room)
            for index, line in enumerate(name_lines):
                text_draw.text((info_x, py + 10 * s + index * 21 * s), line, font=title_font, fill=WHITE, anchor="lm")
            tag_y = py + 10 * s + len(name_lines) * 21 * s + 6 * s
            label = CATEGORY_LABELS.get(entry["category"], "Data")
            tag_w = label_font.getlength(label) + 18 * s
            shape_draw.rounded_rectangle((info_x, tag_y - 9 * s, info_x + tag_w, tag_y + 9 * s), 9 * s, fill=colour + (255,), outline=(255, 255, 255, 230), width=s)
            text_draw.text((info_x + tag_w / 2, tag_y), label, font=label_font, fill=DARK_TEXT if sum(colour) > 600 else WHITE, anchor="mm")
            size_text = human_size(path.stat().st_size) if path is not None and path.exists() else "File missing"
            folder = str(Path(entry["relative"]).parent).replace("\\", "/")
            rows = (
                ("Type", entry["class_name"]),
                ("Bundle", entry["container"] or "Unknown"),
            ) + ((("Data in", entry["resource"]),) if entry["resource"] else ()) + (
                ("Path ID", entry["path_id"] or "None"),
                ("Size", size_text),
                ("Folder", folder),
            )
            row_y = py + preview + 22 * s
            for label_text, value in rows:
                text_draw.text((px, row_y), label_text, font=label_font, fill=(255, 255, 255, 200), anchor="lm")
                text_draw.text((px + 64 * s, row_y), fit_text(value, value_font, size[0] - px - 80 * s), font=value_font, fill=WHITE, anchor="lm")
                row_y += 19 * s
        elif self.index.state == "ready":
            self.render_index_summary(shape_draw, text_draw, size)
        buttons = {"finder:open": "Open Folder", "finder:copy": "Copy Path", "finder:reindex": "Rebuild Index"}
        if self.index.state == "ready":
            dx0, dy0 = self.geometry["details"][0], self.geometry["details"][1]
            for action, box in self.button_boxes().items():
                bx0, by0, bx1, by1 = ((box[0] - dx0) * s, (box[1] - dy0) * s, (box[2] - dx0) * s, (box[3] - dy0) * s)
                fill = (255, 255, 255, 235) if action == button_hover else (255, 255, 255, 70)
                shape_draw.rounded_rectangle((bx0, by0, bx1, by1), (by1 - by0) / 2, fill=fill, outline=(255, 255, 255, 245), width=2 * s)
                text_draw.text(((bx0 + bx1) / 2, (by0 + by1) / 2), buttons[action], font=load_font("segoeuib.ttf", 10), fill=DARK_TEXT if action == button_hover else WHITE, anchor="mm")
        if self.notice:
            text_draw.text((size[0] / 2, size[1] - 56 * s), self.notice, font=load_font("segoeuiz.ttf", 10), fill=WHITE, anchor="mm")
        image = glow(shapes, 5)
        image.alpha_composite(shapes)
        image.alpha_composite(art)
        image.alpha_composite(text_shadow(text))
        image.alpha_composite(text)
        return image.resize((width, height), Image.LANCZOS)

    def render_index_summary(self, shape_draw: ImageDraw.ImageDraw, text_draw: ImageDraw.ImageDraw, size: tuple[int, int]) -> None:
        s = SCALE
        x = 18 * s
        text_draw.text((x, 26 * s), "Asset Index", font=load_font("segoeuiz.ttf", 15), fill=WHITE, anchor="lm")
        text_draw.text((size[0] - 18 * s, 26 * s), f"{self.index.total:,}", font=load_font("segoeuib.ttf", 15), fill=WHITE, anchor="rm")
        counts = self.index.counts
        largest = max([1] + list(counts.values()))
        label_font = load_font("segoeuib.ttf", 9)
        count_font = load_font("segoeui.ttf", 9)
        bar_x = x + 86 * s
        bar_room = size[0] - bar_x - 70 * s
        for index, (key, label, colour) in enumerate(CATEGORIES):
            row_y = (54 + index * 20) * s
            count = counts.get(key, 0)
            text_draw.text((x, row_y), label, font=label_font, fill=(255, 255, 255, 230), anchor="lm")
            shape_draw.rounded_rectangle((bar_x, row_y - 4 * s, bar_x + bar_room, row_y + 4 * s), 4 * s, fill=(255, 255, 255, 40))
            fill_w = max(8 * s, bar_room * (count / largest) ** 0.5) if count else 0
            if fill_w:
                shape_draw.rounded_rectangle((bar_x, row_y - 4 * s, bar_x + fill_w, row_y + 4 * s), 4 * s, fill=colour + (255,), outline=(255, 255, 255, 200), width=s)
            text_draw.text((size[0] - 18 * s, row_y), f"{count:,}", font=count_font, fill=WHITE, anchor="rm")

    def wrap_name(self, name: str, font: Any, room: float) -> list[str]:
        words = name.replace("_", "_ ").split()
        lines: list[str] = []
        current = ""
        for word in words:
            trial = (current + word) if current.endswith("_") else (f"{current} {word}".strip())
            if current and font.getlength(trial) > room:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        if len(lines) > 2:
            lines = [lines[0], fit_text(" ".join(lines[1:]), font, room)]
        return [fit_text(line, font, room) for line in lines] or [""]

    def thumbnail(self, path: Path, box: int) -> Image.Image | None:
        key = f"{path}|{box}"
        if key not in self.thumbs:
            if len(self.thumbs) >= THUMB_LIMIT:
                self.thumbs.pop(next(iter(self.thumbs)))
            try:
                with Image.open(path) as source:
                    source.draft("RGBA", (box, box))
                    picture = source.convert("RGBA")
                picture.thumbnail((box, box), Image.LANCZOS)
                self.thumbs[key] = picture
            except (OSError, ValueError):
                self.thumbs[key] = None
        return self.thumbs[key]
