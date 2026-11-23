from __future__ import annotations
import copy, os, shutil, subprocess, threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable
from PIL import Image, ImageDraw, ImageTk
from .asset_finder import fit_text
from .class_ids import class_name
from .mod_folder import (
    LOADER_LOG,
    ModInfo,
    preview_images,
    read_mod_info,
    write_mod_info,
    export_mod_zip,
    import_mod_zip,
    mod_of,
    remove_built_mod,
    ModCheck,
    ModEntry,
    LooseFormatError,
    SourceFile,
    build_source,
    check_mods,
    install_loader,
    is_game_data,
    loader_state,
    mods_root,
    read_loader_log,
    read_mod_list,
    save_source_catalog,
    scan_source,
    source_fingerprint,
    uninstall_loader,
    write_mod_list,
)
from .updates_panel import (
    CLEAR,
    HEADER_COLOUR,
    PANEL_BLUE,
    PANEL_PAD,
    SCALE,
    add_bar_regions,
    bar_geometry,
    glow,
    hovered_icon,
    load_font,
    panel_frame,
    render_icon_bar,
    gradient_fill,
    text_shadow,
    wrap_words,
)

WELL = (16, 92, 118)
DARK_TEXT = (14, 70, 88, 255)
WHITE = (255, 255, 255, 255)
SOFT = (255, 255, 255, 215)
COLUMN_WIDTH = 282.0
ROW_HEIGHT = 44.0
LIST_TOP = 50.0
STEAM_DATA = Path(r"C:\Program Files (x86)\Steam\steamapps\common\RWBY_GE\rwby-ge_Data")
TABS = (("files", "Mod Files"), ("mods", "Installed Mods"), ("info", "Mod Info"))
THUMB_SIZE = (120.0, 72.0)
INFO_FIELD_COLOUR = "#0e5068"
STATE_COLOURS = {
    "matched": (120, 230, 160),
    "new": (130, 200, 255),
    "companion": (200, 210, 215),
    "built": (120, 230, 160),
    "unmatched": (236, 210, 120),
    "failed": (242, 104, 104),
}
COLUMN_ACTIONS = ("loose:pick_source", "loose:pick_game", "loose:build", "loose:rescan", "loose:open_source", "loose:open_mods", "loose:import", "loose:loader")

def human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{size} bytes"

class LooseBuilderPanel:
    def __init__(self, host: Any) -> None:
        self.host = host
        self.tab = "files"
        self.scroll = {name: 0.0 for name, label in TABS}
        self.content_height = 1.0
        self.sources: list[SourceFile] = []
        self.mods: list[ModEntry] = []
        self.check: ModCheck | None = None
        self.log_lines: list[str] = []
        self.loader = "missing"
        self.job = ""
        self.job_progress = 0.0
        self.job_thread: threading.Thread | None = None
        self.mods_thread: threading.Thread | None = None
        self.mods_dirty = False
        self.version = 0
        self.seen_state: tuple[Any, ...] = ()
        self.notice = ""
        self.bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.drag_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.viewport: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.geometry: dict[str, Any] = {}
        self.base_cache: dict[tuple[Any, ...], Image.Image] = {}
        self.layers: dict[str, tuple[Any, Image.Image]] = {}
        self.photo: ImageTk.PhotoImage | None = None
        self.item: int | None = None
        self.scanned_source: Path | None = None
        self.scan_memory: tuple[Path, tuple, list[SourceFile]] | None = None
        self.chosen_source: Path | None = None
        if self.host.settings.pop("loose_source", None) is not None:
            self.host.save_settings()
        self.display: int | None = None
        self.display_image = 0
        self.info_widgets: dict[str, Any] = {}
        self.info_items: list[int] = []
        self.info_loaded_for: str | None = None
        self.image_cache: dict[tuple[Any, ...], Image.Image] = {}

    @property
    def scroll_y(self) -> float:
        return self.scroll[self.tab]

    @scroll_y.setter
    def scroll_y(self, value: float) -> None:
        self.scroll[self.tab] = value

    def source(self) -> Path | None:
        return self.chosen_source

    def game_data(self) -> Path | None:
        text = str(self.host.settings.get("loose_game") or "")
        if text:
            return Path(text)
        for candidate in (Path(self.host.data_root), STEAM_DATA):
            if is_game_data(candidate):
                return candidate
        return None

    def remember(self, key: str, value: Path) -> None:
        self.host.settings[key] = str(value)
        self.host.save_settings()

    def busy(self) -> bool:
        return self.job_thread is not None and self.job_thread.is_alive()

    def touch(self) -> None:
        self.version += 1

    def activate(self) -> None:
        self.notice = ""
        self.refresh_game()
        source = self.source()
        if source is not None and source != self.scanned_source and not self.busy():
            self.start_scan()

    def refresh_game(self) -> None:
        game = self.game_data()
        self.loader = loader_state(game) if game is not None and is_game_data(game) else "missing"
        self.log_lines = read_loader_log(game).splitlines() if game is not None else []
        self.refresh_mods()
        self.touch()

    def refresh_mods(self) -> None:
        game = self.game_data()
        if game is None:
            self.mods, self.check = [], None
            return
        try:
            self.mods = read_mod_list(game)
        except OSError:
            self.mods = []
        if self.mods_thread is not None and self.mods_thread.is_alive():
            self.mods_dirty = True
            return
        entries = list(self.mods)
        self.mods_thread = threading.Thread(target=self.run_check, args=(game, entries), daemon=True)
        self.mods_thread.start()

    def run_check(self, game: Path, entries: list[ModEntry]) -> None:
        try:
            self.check = check_mods(game, entries)
        except (OSError, LooseFormatError) as exc:
            self.check = ModCheck(problems=[str(exc)])
        self.touch()

    def is_action(self, action: str) -> bool:
        return action.startswith("loose:")

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.viewport
        return x0 <= x <= x1 and y0 <= y <= y1

    def list_height(self) -> float:
        if not self.geometry:
            return 1.0
        lx0, ly0, lx1, ly1 = self.geometry["list"]
        return max(1.0, ly1 - ly0)

    def clamp_scroll(self, value: float) -> float:
        return max(0.0, min(float(value), max(0.0, self.content_height - self.list_height())))

    def scroll_from_delta(self, start_scroll: float, dy: float) -> None:
        self.scroll_y = self.clamp_scroll(float(start_scroll) - float(dy))

    def scroll_by(self, amount: float) -> None:
        self.scroll_to(self.scroll_y + amount)

    def scroll_to(self, value: float) -> None:
        next_value = self.clamp_scroll(value)
        if next_value != self.scroll_y:
            self.scroll_y = next_value
            self.redraw()

    def handle_key(self, event: Any) -> bool:
        keysym = str(getattr(event, "keysym", ""))
        if keysym in ("Up", "Down", "Prior", "Next"):
            step = ROW_HEIGHT if keysym in ("Up", "Down") else self.list_height()
            self.scroll_by(-step if keysym in ("Up", "Prior") else step)
            return True
        if keysym == "F5":
            self.perform_action("loose:rescan")
            return True
        return False

    def perform_action(self, action: str) -> None:
        if action.startswith("loose:tab:"):
            self.tab = action.split(":", 2)[2]
            self.display = None
            self.redraw()
        elif action.startswith("loose:display:"):
            self.display = int(action.rsplit(":", 1)[1])
            self.display_image = 0
            self.tab = "mods"
            self.redraw()
        elif action == "loose:display_close":
            self.display = None
            self.redraw()
        elif action == "loose:display_next":
            self.display_image += 1
            self.redraw()
        elif action.startswith("loose:display_image:"):
            self.display_image = int(action.rsplit(":", 1)[1])
            self.redraw()
        elif action == "loose:add_images":
            self.add_images()
        elif action.startswith("loose:drop_image:"):
            self.drop_image(int(action.rsplit(":", 1)[1]))
        elif action == "loose:pick_source":
            self.pick_source()
        elif action == "loose:pick_game":
            self.pick_game()
        elif action == "loose:rescan":
            if self.source() is not None and not self.busy():
                self.start_scan()
            self.refresh_game()
            self.redraw()
        elif action == "loose:build":
            self.start_build()
        elif action == "loose:open_source":
            source = self.source()
            if source is not None:
                self.reveal(source)
        elif action == "loose:open_mods":
            game = self.game_data()
            if game is not None:
                folder = mods_root(game)
                folder.mkdir(parents=True, exist_ok=True)
                self.reveal(folder)
        elif action == "loose:loader":
            self.toggle_loader()
        elif action == "loose:import":
            self.import_zip()
        elif action.startswith("loose:zip:"):
            self.export_zip(int(action.rsplit(":", 1)[1]))
        elif action.startswith("loose:remove:"):
            self.remove_mod(int(action.rsplit(":", 1)[1]))
        elif action.startswith(("loose:toggle:", "loose:up:", "loose:down:")):
            self.change_mods(action)

    def pick_source(self) -> None:
        if self.busy():
            return
        current = self.source()
        initial = current if current is not None and current.exists() else Path(self.host.project_root)
        selected = filedialog.askdirectory(parent=self.host.root, title="Select the folder with your edited files", initialdir=str(initial))
        if not selected:
            return
        self.chosen_source = Path(selected)
        self.tab = "files"
        self.scroll["files"] = 0.0
        self.start_scan()
        self.redraw()

    def pick_game(self) -> None:
        current = self.game_data()
        initial = current if current is not None and current.exists() else Path(self.host.data_root)
        selected = filedialog.askdirectory(parent=self.host.root, title="Select the rwby-ge_Data folder of the game install", initialdir=str(initial))
        if not selected:
            return
        chosen = Path(selected)
        if not is_game_data(chosen) and is_game_data(chosen / "rwby-ge_Data"):
            chosen = chosen / "rwby-ge_Data"
        if not is_game_data(chosen):
            self.notice = "That folder isnt rwby-ge_Data next to rwby-ge.exe"
            self.touch()
            self.redraw()
            return
        self.remember("loose_game", chosen)
        self.notice = ""
        self.refresh_game()
        self.redraw()

    def start_scan(self) -> None:
        source = self.source()
        if source is None or self.busy():
            return
        self.job = "scanning"
        self.job_progress = 0.0
        self.notice = ""
        self.job_thread = threading.Thread(target=self.run_scan, args=(source,), daemon=True)
        self.job_thread.start()
        self.touch()

    def set_progress(self, value: float) -> None:
        self.job_progress = value

    def scan_fresh(self, source: Path) -> None:
        output_root = Path(self.host.output_root)
        sources = scan_source(source, output_root, self.set_progress)
        save_source_catalog(source, sources, output_root)
        self.scan_memory = (source, source_fingerprint(source, output_root), copy.deepcopy(sources))
        self.sources = sources
        self.scanned_source = source

    def remembered_scan(self, source: Path) -> list[SourceFile] | None:
        if self.scan_memory is None or self.scan_memory[0] != source:
            return None
        if self.scan_memory[1] != source_fingerprint(source, Path(self.host.output_root)):
            return None
        return copy.deepcopy(self.scan_memory[2])

    def run_scan(self, source: Path) -> None:
        try:
            self.scan_fresh(source)
            ready = sum(1 for item in self.sources if item.record is not None or item.state == "new")
            self.notice = "" if ready else "No file here came from the unpack or is a new asset yet"
        except (OSError, LooseFormatError) as exc:
            self.sources = []
            self.notice = str(exc)
        self.job = ""
        self.touch()

    def start_build(self) -> None:
        source, game = self.source(), self.game_data()
        if self.busy():
            return
        if source is None:
            self.notice = "Pick the folder with your edited files first"
        elif game is None or not is_game_data(game):
            self.notice = "Pick the rwby-ge_Data folder of the game first"
        else:
            self.job = "building"
            self.job_progress = 0.0
            self.notice = ""
            self.tab = "files"
            self.job_thread = threading.Thread(target=self.run_build, args=(source, game), daemon=True)
            self.job_thread.start()
        self.touch()
        self.redraw()

    def run_build(self, source: Path, game: Path) -> None:
        try:
            remembered = self.remembered_scan(source)
            if remembered is None:
                self.job = "scanning"
                self.scan_fresh(source)
            else:
                self.sources = remembered
            self.job = "building"
            self.job_progress = 0.0
            self.touch()
            target, objects = build_source(source, self.sources, game, progress=self.set_progress, output_root=Path(self.host.output_root))
            skipped = sum(1 for item in self.sources if item.state == "unmatched")
            noun = "object" if len(objects) == 1 else "objects"
            self.notice = f"Built {target.name} with {len(objects)} {noun}" + (f", {skipped} left out" if skipped else "")
        except (OSError, LooseFormatError) as exc:
            failed = next((item for item in self.sources if item.state == "failed"), None)
            self.notice = f"{failed.path.name}: {failed.message}" if failed is not None else str(exc)
        self.job = ""
        self.refresh_mods()
        self.touch()

    def toggle_loader(self) -> None:
        game = self.game_data()
        if game is None or not is_game_data(game):
            self.notice = "Pick the rwby-ge_Data folder of the game first"
            self.touch()
            self.redraw()
            return
        try:
            if self.loader == "installed":
                notes = uninstall_loader(game)
            else:
                if self.loader == "other" and not messagebox.askyesno(
                    "Sunflowyr Engine",
                    "Another version.dll is installed next to rwby-ge.exe.\n\n"
                    "Its set aside and restored when the loader is removed. Continue?",
                    parent=self.host.root,
                ):
                    return
                notes = install_loader(game)
            self.notice = ". ".join(notes)
        except (OSError, LooseFormatError) as exc:
            self.notice = str(exc)
        self.refresh_game()
        self.redraw()

    def export_zip(self, index: int) -> None:
        if not 0 <= index < len(self.mods):
            return
        entry = self.mods[index]
        chosen = filedialog.asksaveasfilename(parent=self.host.root, title="Save the mod as a zip", initialdir=str(self.host.project_root), initialfile=f"{entry.name}.zip", defaultextension=".zip", filetypes=[("Zip archive", "*.zip")])
        if not chosen:
            return
        try:
            export_mod_zip(entry.folder, Path(chosen))
            self.notice = f"Saved {Path(chosen).name}. Players unzip it into Sunflowyr_Mods or use Import Zip"
        except (OSError, LooseFormatError) as exc:
            self.notice = f"Couldnt save the zip: {exc}"
        self.touch()
        self.redraw()

    def import_zip(self) -> None:
        game = self.game_data()
        if game is None or not is_game_data(game):
            self.notice = "Pick the rwby-ge_Data folder of the game first"
        else:
            chosen = filedialog.askopenfilename(parent=self.host.root, title="Pick a Sunflowyr mod zip", initialdir=str(self.host.project_root), filetypes=[("Zip archive", "*.zip")])
            if not chosen:
                return
            try:
                name = import_mod_zip(Path(chosen), game)
                self.notice = f"Installed {name}"
            except (OSError, LooseFormatError, ValueError) as exc:
                self.notice = f"Couldnt install the zip: {exc}"
            self.refresh_mods()
        self.touch()
        self.redraw()

    def remove_mod(self, index: int) -> None:
        game = self.game_data()
        if game is None or not 0 <= index < len(self.mods):
            return
        entry = self.mods[index]
        if not messagebox.askyesno("Sunflowyr Engine", f"Remove {entry.name} from Sunflowyr_Mods?\n\nYour mod folder isnt touched.", parent=self.host.root):
            return
        try:
            remove_built_mod(game, entry.name)
            self.notice = f"Removed {entry.name}"
        except (OSError, LooseFormatError) as exc:
            self.notice = f"Couldnt remove {entry.name}: {exc}"
        self.refresh_mods()
        self.touch()
        self.redraw()

    def change_mods(self, action: str) -> None:
        game = self.game_data()
        kind, text = action[len("loose:"):].split(":", 1)
        index = int(text)
        if game is None or not 0 <= index < len(self.mods):
            return
        entries = list(self.mods)
        if kind == "toggle":
            entries[index].enabled = not entries[index].enabled
        elif kind == "up" and index > 0:
            entries[index - 1], entries[index] = entries[index], entries[index - 1]
        elif kind == "down" and index < len(entries) - 1:
            entries[index + 1], entries[index] = entries[index], entries[index + 1]
        else:
            return
        try:
            write_mod_list(game, entries)
        except OSError as exc:
            self.notice = f"Couldnt save mods.txt: {exc}"
        self.refresh_mods()
        self.touch()
        self.redraw()

    def reveal(self, path: Path) -> None:
        try:
            if os.name == "nt":
                if path.is_file():
                    subprocess.Popen(f'explorer /select,"{path}"')
                else:
                    os.startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            self.notice = f"Couldnt open folder: {exc}"
            self.touch()
            self.redraw()

    def poll(self) -> bool:
        if self.mods_dirty and not (self.mods_thread is not None and self.mods_thread.is_alive()):
            self.mods_dirty = False
            self.refresh_mods()
        if self.host.screen_mode != "loose" or self.item is None or getattr(self.host, "closing", False):
            return False
        state = (self.version, self.job, int(self.job_progress * 40))
        if state != self.seen_state:
            self.seen_state = state
            self.redraw()
        return False

    def compute_geometry(self, width: int, height: int) -> dict[str, Any]:
        pad = PANEL_PAD
        bar, icons = bar_geometry(width, height)
        bottom = bar[1] - 14.0
        column = (pad, pad, pad + COLUMN_WIDTH, bottom)
        left, right = column[0], column[2]
        middle = (left + right) / 2.0
        top = pad + 12.0
        fields = {
            "loose:pick_source": ("Mod folder", (left, top + 80.0, right, top + 112.0)),
            "loose:pick_game": ("Game", (left, top + 140.0, right, top + 172.0)),
        }
        buttons = {
            "loose:build": ("Build Mod", (left, top + 214.0, middle - 5.0, top + 246.0)),
            "loose:rescan": ("Rescan", (middle + 5.0, top + 214.0, right, top + 246.0)),
            "loose:open_source": ("Open Folder", (left, top + 254.0, middle - 5.0, top + 286.0)),
            "loose:open_mods": ("Open Mods", (middle + 5.0, top + 254.0, right, top + 286.0)),
            "loose:import": ("Import Zip", (left, top + 294.0, right, top + 326.0)),
            "loose:loader": ({"installed": "Remove Loader", "outdated": "Update Loader"}.get(self.loader, "Install Loader"), (left, bottom - 32.0, right, bottom)),
        }
        well = (right + 18.0, pad, width - pad, bottom)
        font = load_font("segoeuib.ttf", 10)
        tabs = []
        x = well[0] + 14.0
        for name, label in TABS:
            tab_w = font.getlength(label) / SCALE + 30.0
            tabs.append((name, label, x, well[1] + 12.0, x + tab_w, well[1] + 38.0))
            x += tab_w + 6.0
        return {
            "bar": bar,
            "icons": icons,
            "column": column,
            "fields": fields,
            "buttons": buttons,
            "summary_y": top + 190.0,
            "loader_y": bottom - 66.0,
            "well": well,
            "tabs": tabs,
            "list": (well[0] + 8.0, well[1] + LIST_TOP, well[2] - 8.0, well[3] - 8.0),
        }

    def draw(self, hole: tuple[float, float, float, float], below_item: int | None) -> None:
        x0, y0, x1, y1 = (int(round(value)) for value in hole)
        width, height = max(1, x1 - x0), max(1, y1 - y0)
        if (x0, y0, x1, y1) != self.bounds:
            self.layers = {}
        self.bounds = (x0, y0, x1, y1)
        self.geometry = self.compute_geometry(width, height)
        lx0, ly0, lx1, ly1 = self.geometry["list"]
        self.viewport = (x0 + lx0, y0 + ly0, x0 + lx1, y0 + ly1)
        self.drag_bounds = self.viewport
        self.photo = ImageTk.PhotoImage(self.compose(width, height), master=self.host.root)
        self.item = self.host.canvas.create_image(x0, y0, image=self.photo, anchor="nw")
        if below_item is not None:
            self.host.canvas.tag_lower(self.item, below_item)
        self.info_items = []
        self.add_regions()
        self.place_info_widgets()

    def redraw(self) -> None:
        if self.item is None or not self.geometry or self.host.screen_mode != "loose":
            return
        x0, y0, x1, y1 = self.bounds
        self.geometry = self.compute_geometry(int(x1 - x0), int(y1 - y0))
        self.photo = ImageTk.PhotoImage(self.compose(int(x1 - x0), int(y1 - y0)), master=self.host.root)
        self.host.canvas.itemconfigure(self.item, image=self.photo)
        self.host.hit_regions = [region for region in self.host.hit_regions if not self.is_action(region.name)]
        self.add_regions()
        self.place_info_widgets()

    def add_regions(self) -> None:
        x0, y0 = self.bounds[0], self.bounds[1]
        add_bar_regions(self.host, (x0, y0), self.geometry["icons"], "globe")
        busy = self.busy()
        for action, (label, box) in self.geometry["fields"].items():
            self.host.add_region(action, (x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]), enabled=not busy)
        for action, (label, box) in self.geometry["buttons"].items():
            enabled = not busy or action in ("loose:open_source", "loose:open_mods")
            self.host.add_region(action, (x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]), enabled=enabled)
        if self.showing_display():
            for action, box in self.display_controls().items():
                self.host.add_region(action, (x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]))
            return
        for name, label, tx0, ty0, tx1, ty1 in self.geometry["tabs"]:
            self.host.add_region(f"loose:tab:{name}", (x0 + tx0, y0 + ty0, x0 + tx1, y0 + ty1))
        lx0, ly0, lx1, ly1 = self.geometry["list"]
        if self.tab == "info" and self.source() is not None:
            for action, box in self.info_controls().items():
                self.host.add_region(action, (x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]))
        if self.tab == "mods" and not busy:
            for index in range(len(self.mods)):
                top = ly0 + 4 + index * ROW_HEIGHT - self.scroll_y
                if top < ly0 or top + ROW_HEIGHT - 4 > ly1:
                    continue
                for action, box in self.mod_controls(index, top).items():
                    self.host.add_region(action, (x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]))
        self.content_height = self.measure_content()
        self.scroll_y = self.clamp_scroll(self.scroll_y)
        self.host.add_scroll_track(
            f"loose:scrollbar:{self.tab}",
            (x0 + lx1 - 9, y0 + ly0 + 4, x0 + lx1 - 1, y0 + ly1 - 4),
            self.content_height,
            ly1 - ly0,
            lambda: self.scroll_y,
            self.scroll_to,
        )

    def mod_controls(self, index: int, top: float) -> dict[str, tuple[float, float, float, float]]:
        lx0, ly0, lx1, ly1 = self.geometry["list"]
        centre = top + (ROW_HEIGHT - 4) / 2.0
        return {
            f"loose:toggle:{index}": (lx0 + 10, centre - 11, lx0 + 32, centre + 11),
            f"loose:display:{index}": (lx1 - 232, centre - 12, lx1 - 202, centre + 12),
            f"loose:zip:{index}": (lx1 - 196, centre - 12, lx1 - 150, centre + 12),
            f"loose:remove:{index}": (lx1 - 144, centre - 12, lx1 - 86, centre + 12),
            f"loose:up:{index}": (lx1 - 80, centre - 12, lx1 - 52, centre + 12),
            f"loose:down:{index}": (lx1 - 48, centre - 12, lx1 - 20, centre + 12),
        }

    def measure_content(self) -> float:
        if self.tab == "files":
            return 8.0 + len(self.sources) * ROW_HEIGHT
        if self.tab == "mods":
            return 8.0 + len(self.mods) * ROW_HEIGHT
        return 1.0

    def layer(self, name: str, key: tuple[Any, ...], render: Callable[[], Image.Image]) -> Image.Image:
        cached = self.layers.get(name)
        if cached is None or cached[0] != key:
            cached = (key, render())
            self.layers[name] = cached
        return cached[1]

    def compose(self, width: int, height: int) -> Image.Image:
        hover = self.host.hover_action
        image = self.base(width, height, hovered_icon(hover)).copy()
        column_hover = hover if hover in COLUMN_ACTIONS else ""
        cx0, cy0, cx1, cy1 = (int(value) for value in self.geometry["column"])
        progress = int(self.job_progress * 40)
        column_key = (self.version, self.job, progress, column_hover, self.loader, str(self.source()), str(self.game_data()), cx1 - cx0, cy1 - cy0)
        image.alpha_composite(self.layer("column", column_key, lambda: self.render_column(cx1 - cx0, cy1 - cy0, column_hover)), (cx0, cy0))
        wx0, wy0, wx1, wy1 = (int(value) for value in self.geometry["well"])
        well_hover = hover if hover.startswith(("loose:tab:", "loose:toggle:", "loose:up:", "loose:down:", "loose:zip:", "loose:remove:", "loose:display", "loose:add_images", "loose:drop_image:")) else ""
        self.content_height = self.measure_content()
        self.scroll_y = self.clamp_scroll(self.scroll_y)
        if self.showing_display():
            entry = self.mods[self.display]
            images = preview_images(entry.folder)
            stamps = tuple((str(path), path.stat().st_mtime_ns) for path in images)
            display_key = ("display", entry.name, stamps, self.display_image, well_hover, self.version, wx1 - wx0, wy1 - wy0)
            image.alpha_composite(self.layer("well", display_key, lambda: self.render_display(wx1 - wx0, wy1 - wy0, well_hover, entry, images)), (wx0, wy0))
            return image
        info_stamp = self.info_stamp() if self.tab == "info" else ()
        well_key = (self.version, self.tab, int(self.scroll_y), self.job, progress, well_hover, wx1 - wx0, wy1 - wy0, info_stamp)
        image.alpha_composite(self.layer("well", well_key, lambda: self.render_well(wx1 - wx0, wy1 - wy0, well_hover)), (wx0, wy0))
        return image

    def base(self, width: int, height: int, icon_hover: str) -> Image.Image:
        key = (width, height, icon_hover)
        if key not in self.base_cache:
            if any(cached[:2] != (width, height) for cached in self.base_cache):
                self.base_cache = {}
            s = SCALE
            image = Image.new("RGBA", (width, height), PANEL_BLUE + (255,))
            shapes = panel_frame(width, height)
            well = Image.new("RGBA", shapes.size, CLEAR)
            wx0, wy0, wx1, wy1 = (value * s for value in self.geometry["well"])
            ImageDraw.Draw(well).rounded_rectangle((wx0, wy0, wx1, wy1), 14 * s, fill=WELL + (255,), outline=(255, 255, 255, 230), width=2 * s)
            shapes.alpha_composite(glow(well, 7, 2))
            shapes.alpha_composite(well)
            image.alpha_composite(shapes.resize((width, height), Image.LANCZOS))
            image.alpha_composite(render_icon_bar(width, height, self.geometry["bar"], self.geometry["icons"], "globe", icon_hover))
            self.base_cache[key] = image
        return self.base_cache[key]

    def subtitle(self) -> str:
        if self.job == "scanning":
            return f"Matching files against the unpack  {int(self.job_progress * 100)}%"
        if self.job == "building":
            return f"Building the mod  {int(self.job_progress * 100)}%"
        if self.notice:
            return self.notice
        if self.source() is None:
            return "Build loose mods from edited unpack files"
        ready = sum(1 for item in self.sources if item.record is not None or item.state == "new")
        fresh = sum(1 for item in self.sources if item.added_id or item.state == "new")
        return f"{ready} of {len(self.sources)} files ready to build" + (f", {fresh} new" if fresh else "")

    def render_column(self, width: int, height: int, hover: str) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        ox, oy = self.geometry["column"][0], self.geometry["column"][1]
        shapes = Image.new("RGBA", size, CLEAR)
        text = Image.new("RGBA", size, CLEAR)
        plain = Image.new("RGBA", size, CLEAR)
        shape_draw = ImageDraw.Draw(shapes)
        text_draw = ImageDraw.Draw(text)
        plain_draw = ImageDraw.Draw(plain)
        label_font = load_font("segoeuib.ttf", 10)
        value_font = load_font("segoeui.ttf", 10)
        button_font = load_font("segoeuib.ttf", 10)
        text_draw.text((0, 0), "Loose Builder", font=load_font("segoeuiz.ttf", 24), fill=HEADER_COLOUR, anchor="lt")
        status = wrap_words(self.subtitle(), label_font, size[0] - 4 * s)
        if len(status) > 2:
            status = [status[0], fit_text(" ".join(status[1:]), label_font, size[0] - 4 * s)]
        for index, line in enumerate(status):
            text_draw.text((2 * s, (34 + index * 14) * s), line, font=label_font, fill=WHITE if self.notice and not self.job else SOFT, anchor="lt")
        busy = self.busy()
        values = {"loose:pick_source": self.source(), "loose:pick_game": self.game_data()}
        placeholders = {"loose:pick_source": "Pick the folder with your edited files", "loose:pick_game": "Pick the rwby-ge_Data of the game"}
        for action, (label, box) in self.geometry["fields"].items():
            bx0, by0, bx1, by1 = ((box[0] - ox) * s, (box[1] - oy) * s, (box[2] - ox) * s, (box[3] - oy) * s)
            text_draw.text((bx0 + 2 * s, by0 - 8 * s), label, font=label_font, fill=WHITE, anchor="lb")
            hovered = hover == action and not busy
            shape_draw.rounded_rectangle((bx0, by0, bx1, by1), (by1 - by0) / 2, fill=(255, 255, 255, 90 if hovered else 46), outline=(255, 255, 255, 240 if hovered else 180), width=2 * s)
            value = values[action]
            shown = str(value) if value is not None else placeholders[action]
            room = bx1 - bx0 - 28 * s
            shown = fit_text(shown, value_font, room) if value is None else self.fit_path(shown, value_font, room)
            text_draw.text((bx0 + 14 * s, (by0 + by1) / 2), shown, font=value_font, fill=WHITE if value is not None else (255, 255, 255, 170), anchor="lm")
        for action, (label, box) in self.geometry["buttons"].items():
            bx0, by0, bx1, by1 = ((box[0] - ox) * s, (box[1] - oy) * s, (box[2] - ox) * s, (box[3] - oy) * s)
            enabled = not busy or action in ("loose:open_source", "loose:open_mods")
            hovered = hover == action and enabled
            primary = action == "loose:build"
            fill = (255, 255, 255, 235) if hovered else (255, 255, 255, 120 if primary else 70)
            if not enabled:
                fill = (255, 255, 255, 30)
            shape_draw.rounded_rectangle((bx0, by0, bx1, by1), (by1 - by0) / 2, fill=fill, outline=(255, 255, 255, 245 if enabled else 120), width=2 * s)
            colour = DARK_TEXT if hovered else (WHITE if enabled else (255, 255, 255, 140))
            (plain_draw if hovered else text_draw).text(((bx0 + bx1) / 2, (by0 + by1) / 2), label, font=button_font, fill=colour, anchor="mm")
        summary_y = (self.geometry["summary_y"] - oy) * s
        game = self.game_data()
        enabled_mods = sum(1 for entry in self.mods if entry.enabled)
        mods_line = f"{enabled_mods} of {len(self.mods)} mods on" if game is not None else "No game folder picked"
        text_draw.text((2 * s, summary_y), mods_line, font=value_font, fill=SOFT, anchor="lm")
        loader_y = (self.geometry["loader_y"] - oy) * s
        text_draw.text((2 * s, loader_y), "Loader", font=load_font("segoeuiz.ttf", 13), fill=WHITE, anchor="lm")
        state_text = {
            "installed": "Installed next to rwby-ge.exe",
            "outdated": "An older loader build is installed",
            "other": "Another version.dll is in its place",
            "missing": "Not installed",
        }[self.loader]
        dot = (120, 230, 160, 255) if self.loader == "installed" else (236, 210, 120, 255)
        shape_draw.ellipse((64 * s, loader_y - 4 * s, 72 * s, loader_y + 4 * s), fill=dot, outline=(255, 255, 255, 230), width=s)
        text_draw.text((80 * s, loader_y), state_text, font=value_font, fill=SOFT, anchor="lm")
        image = glow(shapes, 4)
        image.alpha_composite(shapes)
        image.alpha_composite(text_shadow(text))
        image.alpha_composite(text)
        image.alpha_composite(plain)
        return image.resize((width, height), Image.LANCZOS)

    def fit_path(self, text: str, font: Any, room: float) -> str:
        if font.getlength(text) <= room:
            return text
        tail = text
        while tail and font.getlength("..." + tail) > room:
            tail = tail[1:]
        return "..." + tail

    def render_well(self, width: int, height: int, hover: str) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        ox, oy = self.geometry["well"][0], self.geometry["well"][1]
        shapes = Image.new("RGBA", size, CLEAR)
        text = Image.new("RGBA", size, CLEAR)
        plain = Image.new("RGBA", size, CLEAR)
        shape_draw = ImageDraw.Draw(shapes)
        text_draw = ImageDraw.Draw(text)
        plain_draw = ImageDraw.Draw(plain)
        tab_font = load_font("segoeuib.ttf", 10)
        for name, label, x0, y0, x1, y1 in self.geometry["tabs"]:
            box = ((x0 - ox) * s, (y0 - oy) * s, (x1 - ox) * s, (y1 - oy) * s)
            active = name == self.tab
            hovered = hover == f"loose:tab:{name}"
            fill = (255, 255, 255, 230) if active else (255, 255, 255, 80 if hovered else 30)
            shape_draw.rounded_rectangle(box, (box[3] - box[1]) / 2, fill=fill, outline=(255, 255, 255, 245 if active or hovered else 160), width=2 * s)
            (plain_draw if active else text_draw).text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label, font=tab_font, fill=DARK_TEXT if active else WHITE, anchor="mm")
        lx0, ly0, lx1, ly1 = self.geometry["list"]
        list_box = ((lx0 - ox) * s, (ly0 - oy) * s, (lx1 - ox) * s, (ly1 - oy) * s)
        rows = Image.new("RGBA", size, CLEAR)
        row_text = Image.new("RGBA", size, CLEAR)
        if self.tab == "files":
            self.render_files(rows, row_text, list_box)
        elif self.tab == "mods":
            self.render_mods(rows, row_text, list_box, hover)
        else:
            self.render_info(rows, row_text, list_box, hover)
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rectangle(list_box, fill=255)
        for layer in (rows, row_text):
            layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", size, 0), mask))
        if self.content_height > ly1 - ly0:
            track_x = list_box[2] - 7 * s
            shape_draw.rounded_rectangle((track_x, list_box[1] + 4 * s, track_x + 4 * s, list_box[3] - 4 * s), 2 * s, fill=(255, 255, 255, 70))
            track_h = list_box[3] - list_box[1] - 8 * s
            view = ly1 - ly0
            thumb_h = max(24 * s, track_h * view / self.content_height)
            thumb_y = list_box[1] + 4 * s + (track_h - thumb_h) * self.scroll_y / max(1.0, self.content_height - view)
            shape_draw.rounded_rectangle((track_x - s, thumb_y, track_x + 5 * s, thumb_y + thumb_h), 3 * s, fill=(255, 255, 255, 235))
        image = glow(shapes, 4)
        image.alpha_composite(shapes)
        image.alpha_composite(rows)
        image.alpha_composite(text_shadow(text))
        image.alpha_composite(text)
        image.alpha_composite(text_shadow(row_text))
        image.alpha_composite(row_text)
        image.alpha_composite(plain)
        return image.resize((width, height), Image.LANCZOS)

    def empty_message(self, draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], title: str, lines: list[str]) -> None:
        s = SCALE
        cx = (box[0] + box[2]) / 2
        cy = (box[1] + box[3]) / 2 - 30 * s
        draw.text((cx, cy), title, font=load_font("segoeuiz.ttf", 16), fill=WHITE, anchor="mm")
        body = load_font("segoeui.ttf", 10)
        y = cy + 30 * s
        for line in lines:
            for part in wrap_words(line, body, box[2] - box[0] - 60 * s):
                draw.text((cx, y), part, font=body, fill=SOFT, anchor="mm")
                y += 16 * s
            y += 6 * s

    def row_top(self, box: tuple[float, float, float, float], index: int) -> float:
        return box[1] + (4 + index * ROW_HEIGHT - self.scroll_y) * SCALE

    def visible_rows(self, box: tuple[float, float, float, float], count: int) -> range:
        view = (box[3] - box[1]) / SCALE
        first = max(0, int(self.scroll_y // ROW_HEIGHT))
        return range(first, min(count, first + int(view // ROW_HEIGHT) + 2))

    def render_files(self, rows: Image.Image, text: Image.Image, box: tuple[float, float, float, float]) -> None:
        s = SCALE
        row_draw = ImageDraw.Draw(rows)
        text_draw = ImageDraw.Draw(text)
        if self.job == "scanning" and not self.sources:
            self.empty_message(text_draw, box, "Matching files", ["Reading the unpack catalog to find where each file came from. The first scan of a folder takes a few seconds."])
            return
        if self.source() is None:
            self.empty_message(text_draw, box, "No mod folder yet", [
                "Copy the unpacked files you want to change into a folder of their own, keeping their unpack names (Name__path123).",
                "Edit them, pick that folder here, and press Build Mod. The folder name becomes the mod name.",
                'New files go in the same folder. In a JSON, point at one with {"file": "Name.png"} and at a game object with {"game": "Name"}, adding "type", "in" or "id" when the name is shared.',
            ])
            return
        if not self.sources:
            self.empty_message(text_draw, box, "This folder is empty", ["Put the edited unpack files in it, then press Rescan."])
            return
        name_font = load_font("segoeuiz.ttf", 12)
        info_font = load_font("segoeui.ttf", 9.5)
        right = box[2] - 14 * s
        for index in self.visible_rows(box, len(self.sources)):
            item = self.sources[index]
            top = self.row_top(box, index)
            row = (box[0] + 4 * s, top, right, top + (ROW_HEIGHT - 4) * s)
            if index % 2 == 0:
                row_draw.rounded_rectangle(row, 9 * s, fill=(255, 255, 255, 18))
            colour = STATE_COLOURS.get(item.state, (200, 200, 200)) + (255,)
            cy = top + 11 * s
            row_draw.ellipse((row[0] + 10 * s, cy - 4 * s, row[0] + 18 * s, cy + 4 * s), fill=colour, outline=(255, 255, 255, 230), width=s)
            left = row[0] + 28 * s
            room = right - left - 10 * s
            text_draw.text((left, cy), fit_text(item.relative, name_font, room), font=name_font, fill=WHITE, anchor="lm")
            if item.record is not None and item.state in ("matched", "built"):
                parts = [item.state.capitalize() if item.state == "built" else "", self.class_label(item), item.container, f"ID {item.path_id}" if item.path_id else "", human_size(item.size), item.message if item.state == "built" else ""]
                info = "|".join(part for part in parts if part)
            elif item.added_id and item.state == "built":
                parts = ["Built new", class_name(item.added_class) if item.added_class else "", item.added_container, f"ID {item.added_id}", human_size(item.size)]
                info = "|".join(part for part in parts if part)
            elif item.state == "new":
                info = f"New asset, gets its own ID when built|{human_size(item.size)}"
            else:
                info = item.message
            info_colour = (255, 214, 214, 255) if item.state == "failed" else SOFT
            text_draw.text((left, top + 27 * s), fit_text(info, info_font, room), font=info_font, fill=info_colour, anchor="lm")

    def class_label(self, item: SourceFile) -> str:
        return class_name(item.class_id) if item.class_id else ""

    def render_mods(self, rows: Image.Image, text: Image.Image, box: tuple[float, float, float, float], hover: str) -> None:
        s = SCALE
        row_draw = ImageDraw.Draw(rows)
        text_draw = ImageDraw.Draw(text)
        if self.game_data() is None:
            self.empty_message(text_draw, box, "No game folder", ["Pick the rwby-ge_Data folder of the game install on the left."])
            return
        if not self.mods:
            self.empty_message(text_draw, box, "No mods installed", ["Built mods land in Sunflowyr_Mods inside the game data folder. Mods from other people can be dropped in there too."])
            return
        overridden: dict[str, int] = {}
        missing: dict[str, int] = {}
        if self.check is not None:
            for earlier, later in self.check.overridden:
                key = str(earlier.payload).lower()
                overridden[key] = overridden.get(key, 0) + 1
            for item in self.check.missing:
                missing[str(item.payload).lower()] = 1
        name_font = load_font("segoeuiz.ttf", 12)
        info_font = load_font("segoeui.ttf", 9.5)
        arrow_font = load_font("segoeuib.ttf", 11)
        small_font = load_font("segoeuib.ttf", 9)
        collisions = self.id_collisions()
        ox, oy = self.geometry["well"][0], self.geometry["well"][1]
        for index in self.visible_rows(box, len(self.mods)):
            entry = self.mods[index]
            top = self.row_top(box, index)
            right = box[2] - 14 * s
            row = (box[0] + 4 * s, top, right, top + (ROW_HEIGHT - 4) * s)
            if index % 2 == 0:
                row_draw.rounded_rectangle(row, 9 * s, fill=(255, 255, 255, 18))
            controls = self.mod_controls(index, top / s + oy)
            for action, (x0, y0, x1, y1) in controls.items():
                rect = ((x0 - ox) * s, (y0 - oy) * s, (x1 - ox) * s, (y1 - oy) * s)
                hovered = hover == action
                if action.startswith("loose:toggle:"):
                    row_draw.rounded_rectangle(rect, 6 * s, fill=(255, 255, 255, 235 if entry.enabled else (70 if hovered else 25)), outline=(255, 255, 255, 245), width=2 * s)
                    if entry.enabled:
                        row_draw.line([(rect[0] + 5 * s, (rect[1] + rect[3]) / 2), (rect[0] + 9 * s, rect[3] - 6 * s), (rect[2] - 5 * s, rect[1] + 6 * s)], fill=DARK_TEXT, width=2 * s, joint="curve")
                elif action.startswith(("loose:zip:", "loose:remove:", "loose:display:")):
                    row_draw.rounded_rectangle(rect, (rect[3] - rect[1]) / 2, fill=(255, 255, 255, 235 if hovered else 50), outline=(255, 255, 255, 200), width=s)
                    label = "Zip" if action.startswith("loose:zip:") else ("D" if action.startswith("loose:display:") else "Remove")
                    text_draw.text(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), label, font=small_font, fill=DARK_TEXT if hovered else WHITE, anchor="mm")
                else:
                    first = action.startswith("loose:up:") and index == 0
                    last = action.startswith("loose:down:") and index == len(self.mods) - 1
                    alpha = 60 if first or last else (235 if hovered else 70)
                    row_draw.rounded_rectangle(rect, (rect[3] - rect[1]) / 2, fill=(255, 255, 255, alpha), outline=(255, 255, 255, 200), width=s)
                    glyph = "\u25b2" if action.startswith("loose:up:") else "\u25bc"
                    text_draw.text(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), glyph, font=arrow_font, fill=DARK_TEXT if hovered and alpha == 235 else WHITE, anchor="mm")
            left = box[0] + 46 * s
            room = right - left - 250 * s
            text_draw.text((left, top + 11 * s), fit_text(entry.name, name_font, room), font=name_font, fill=WHITE if entry.enabled else (255, 255, 255, 150), anchor="lm")
            noun = "object" if len(entry.objects) == 1 else "objects"
            parts = [f"{len(entry.objects)} {noun}", f"load order {index + 1}"]
            if entry.appends:
                parts.append(f"{sum(spec.count_add for spec in entry.appends)} list additions")
            fresh = [item.path_id for item in entry.objects if item.is_new]
            if fresh:
                parts.append(f"new IDs {min(fresh)} to {max(fresh)}")
            if entry.name.lower() in collisions:
                parts.append(f"new IDs collide with {collisions[entry.name.lower()]}")
            lost = sum(overridden.get(str(item.payload).lower(), 0) for item in entry.objects)
            unknown = sum(missing.get(str(item.payload).lower(), 0) for item in entry.objects)
            if not entry.enabled:
                parts.append("off")
            if lost:
                parts.append(f"{lost} replaced by a later mod")
            if unknown:
                parts.append(f"{unknown} not in the game files")
            text_draw.text((left, top + 27 * s), fit_text("|".join(parts), info_font, room), font=info_font, fill=SOFT, anchor="lm")

    def id_collisions(self) -> dict[str, str]:
        found: dict[str, str] = {}
        if self.check is None:
            return found
        for earlier, later in self.check.overridden:
            if earlier.is_new and later.is_new:
                first, second = mod_of(earlier.payload), mod_of(later.payload)
                if first and second and first.lower() != second.lower():
                    found[first.lower()] = second
                    found[second.lower()] = first
        return found

    def showing_display(self) -> bool:
        return self.display is not None and 0 <= self.display < len(self.mods)

    def display_controls(self) -> dict[str, tuple[float, float, float, float]]:
        wx0, wy0, wx1, wy1 = self.geometry["well"]
        controls = {"loose:display_next": (wx0, wy0, wx1, wy1), "loose:display_close": (wx0 + 14, wy0 + 12, wx0 + 86, wy0 + 40)}
        images = preview_images(self.mods[self.display].folder)
        if len(images) > 1:
            x = wx1 - 14
            for index in reversed(range(len(images))):
                controls[f"loose:display_image:{index}"] = (x - 72, wy0 + 12, x, wy0 + 56)
                x -= 80
        return controls

    def stretched(self, path: Path, size: tuple[int, int]) -> Image.Image:
        key = (str(path), path.stat().st_mtime_ns, size)
        cached = self.image_cache.get(key)
        if cached is None:
            if len(self.image_cache) > 24:
                self.image_cache = {}
            try:
                with Image.open(path) as source:
                    cached = source.convert("RGBA").resize(size, Image.LANCZOS)
            except OSError:
                cached = Image.new("RGBA", size, WELL + (255,))
            self.image_cache[key] = cached
        return cached

    def render_display(self, width: int, height: int, hover: str, entry: ModEntry, images: list[Path]) -> Image.Image:
        s = SCALE
        size = (width * s, height * s)
        if images:
            index = self.display_image % len(images)
            picture = self.stretched(images[index], size).copy()
        else:
            index = 0
            picture = gradient_fill(size).transpose(Image.Transpose.ROTATE_180)
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), 14 * s, fill=255)
        picture.putalpha(Image.composite(picture.getchannel("A"), Image.new("L", size, 0), mask))
        info = read_mod_info(entry.folder)
        band_height = int(size[1] * 0.46)
        ramp = Image.linear_gradient("L").resize((size[0], band_height)).point(lambda value: value * 225 // 255)
        band = Image.new("RGBA", (size[0], band_height), (6, 34, 46, 255))
        band.putalpha(Image.composite(ramp, Image.new("L", ramp.size, 0), mask.crop((0, size[1] - band_height, size[0], size[1]))))
        picture.alpha_composite(band, (0, size[1] - band_height))
        shapes = Image.new("RGBA", size, CLEAR)
        text = Image.new("RGBA", size, CLEAR)
        plain = Image.new("RGBA", size, CLEAR)
        shape_draw = ImageDraw.Draw(shapes)
        text_draw = ImageDraw.Draw(text)
        plain_draw = ImageDraw.Draw(plain)
        ox, oy = self.geometry["well"][0], self.geometry["well"][1]
        controls = self.display_controls()
        x0, y0, x1, y1 = ((value - offset) * s for value, offset in zip(controls["loose:display_close"], (ox, oy, ox, oy)))
        hovered = hover == "loose:display_close"
        shape_draw.rounded_rectangle((x0, y0, x1, y1), (y1 - y0) / 2, fill=(255, 255, 255, 235 if hovered else 90), outline=(255, 255, 255, 245), width=2 * s)
        (plain_draw if hovered else text_draw).text(((x0 + x1) / 2, (y0 + y1) / 2), "Back", font=load_font("segoeuib.ttf", 10), fill=DARK_TEXT if hovered else WHITE, anchor="mm")
        for action, box in controls.items():
            if not action.startswith("loose:display_image:"):
                continue
            number = int(action.rsplit(":", 1)[1])
            bx0, by0, bx1, by1 = (int((value - offset) * s) for value, offset in zip(box, (ox, oy, ox, oy)))
            thumb = self.stretched(images[number], (bx1 - bx0, by1 - by0))
            picture.alpha_composite(thumb, (bx0, by0))
            active = number == index
            shape_draw.rounded_rectangle((bx0, by0, bx1, by1), 6 * s, outline=(255, 255, 255, 255 if active or hover == action else 150), width=(3 if active else 2) * s)
        left = 26 * s
        bottom = size[1] - 22 * s
        lines: list[tuple[str, Any, tuple[int, int, int, int]]] = []
        meta = []
        if info.author:
            meta.append(f"by {info.author}")
        if info.version:
            meta.append(f"Version {info.version}")
        noun = "object" if len(entry.objects) == 1 else "objects"
        meta.append(f"{len(entry.objects)} {noun}")
        if entry.appends:
            meta.append(f"{sum(spec.count_add for spec in entry.appends)} list additions")
        body = load_font("segoeui.ttf", 11)
        description = [part for paragraph in (info.description or "No description yet. Add one in the Mod Info tab before building.").splitlines() for part in wrap_words(paragraph, body, size[0] - 2 * left)]
        room = max(1, int((band_height - 80 * s) // (17 * s)))
        if len(description) > room:
            description = description[:room - 1] + [description[room - 1].rstrip(".") + "..."]
        y = bottom
        for line in reversed(description):
            text_draw.text((left, y), line, font=body, fill=(255, 255, 255, 235), anchor="ls")
            y -= 17 * s
        y -= 8 * s
        text_draw.text((left, y), "|".join(meta), font=load_font("segoeuib.ttf", 10), fill=(255, 255, 255, 220), anchor="ls")
        y -= 24 * s
        text_draw.text((left, y), entry.name.replace("_", " "), font=load_font("segoeuiz.ttf", 24), fill=WHITE, anchor="ls")
        if len(images) > 1:
            hint_y = (max(box[3] for action, box in controls.items() if action.startswith("loose:display_image:")) - oy + 8) * s
            text_draw.text((size[0] - 16 * s, hint_y), f"{index + 1} of {len(images)}|click the image for the next one", font=load_font("segoeuib.ttf", 9), fill=(255, 255, 255, 220), anchor="rt")
        image = picture
        image.alpha_composite(glow(shapes, 3))
        image.alpha_composite(shapes)
        image.alpha_composite(text_shadow(text))
        image.alpha_composite(text)
        image.alpha_composite(plain)
        return image.resize((width, height), Image.LANCZOS)

    def info_layout(self) -> dict[str, Any]:
        lx0, ly0, lx1, ly1 = self.geometry["list"]
        x0, x1 = lx0 + 16.0, lx1 - 24.0
        middle = (x0 + x1) / 2.0
        return {
            "author": (x0, ly0 + 30.0, middle - 8.0, ly0 + 58.0),
            "version": (middle + 8.0, ly0 + 30.0, x1, ly0 + 58.0),
            "description": (x0, ly0 + 92.0, x1, ly0 + 196.0),
            "add": (x1 - 124.0, ly0 + 208.0, x1, ly0 + 236.0),
            "thumbs_top": ly0 + 246.0,
            "left": x0,
            "right": x1,
            "bottom": ly1 - 8.0,
        }

    def thumb_boxes(self) -> list[tuple[float, float, float, float]]:
        source = self.source()
        if source is None:
            return []
        layout = self.info_layout()
        width, height = THUMB_SIZE
        boxes = []
        x, y = layout["left"], layout["thumbs_top"]
        for unused in preview_images(source):
            if x + width > layout["right"]:
                x = layout["left"]
                y += height + 10.0
            if y + height > layout["bottom"]:
                break
            boxes.append((x, y, x + width, y + height))
            x += width + 10.0
        return boxes

    def info_controls(self) -> dict[str, tuple[float, float, float, float]]:
        controls = {"loose:add_images": self.info_layout()["add"]}
        for index, (x0, y0, x1, y1) in enumerate(self.thumb_boxes()):
            controls[f"loose:drop_image:{index}"] = (x1 - 22.0, y0 + 4.0, x1 - 4.0, y0 + 22.0)
        return controls

    def info_stamp(self) -> tuple[Any, ...]:
        source = self.source()
        if source is None:
            return ()
        return tuple((path.name, path.stat().st_mtime_ns) for path in preview_images(source))

    def render_info(self, rows: Image.Image, text: Image.Image, box: tuple[float, float, float, float], hover: str) -> None:
        s = SCALE
        row_draw = ImageDraw.Draw(rows)
        text_draw = ImageDraw.Draw(text)
        source = self.source()
        if source is None:
            self.empty_message(text_draw, box, "No mod folder yet", ["Pick the folder of your mod on the left, then fill in its author, version, description and preview images here."])
            return
        ox, oy = self.geometry["well"][0], self.geometry["well"][1]
        layout = self.info_layout()
        label_font = load_font("segoeuib.ttf", 10)

        def local(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
            return ((rect[0] - ox) * s, (rect[1] - oy) * s, (rect[2] - ox) * s, (rect[3] - oy) * s)

        for key, label in (("author", "Author"), ("version", "Version"), ("description", "Description")):
            rect = local(layout[key])
            text_draw.text((rect[0] + 2 * s, rect[1] - 7 * s), label, font=label_font, fill=WHITE, anchor="lb")
            row_draw.rounded_rectangle((rect[0] - 2 * s, rect[1] - 2 * s, rect[2] + 2 * s, rect[3] + 2 * s), 6 * s, fill=(14, 80, 104, 255), outline=(255, 255, 255, 170), width=s)
        add = local(layout["add"])
        hovered = hover == "loose:add_images"
        row_draw.rounded_rectangle(add, (add[3] - add[1]) / 2, fill=(255, 255, 255, 235 if hovered else 60), outline=(255, 255, 255, 230), width=2 * s)
        text_draw.text(((add[0] + add[2]) / 2, (add[1] + add[3]) / 2), "Add Images", font=label_font, fill=DARK_TEXT if hovered else WHITE, anchor="mm")
        images = preview_images(source)
        count = f"{len(images)} image" + ("" if len(images) == 1 else "s")
        text_draw.text((local(layout["description"])[0] + 2 * s, (add[1] + add[3]) / 2), f"Preview images|{count} in the preview folder", font=label_font, fill=WHITE, anchor="lm")
        boxes = self.thumb_boxes()
        if not images:
            hint = "Images added here are copied into the mod folder's preview folder. Theyre never built into the game, the D button on Installed Mods shows them."
            y = (layout["thumbs_top"] - oy + 18) * s
            for line in wrap_words(hint, load_font("segoeui.ttf", 10), box[2] - box[0] - 40 * s):
                text_draw.text((local(layout["description"])[0], y), line, font=load_font("segoeui.ttf", 10), fill=SOFT, anchor="lt")
                y += 16 * s
        for index, (path, rect) in enumerate(zip(images, boxes)):
            x0, y0, x1, y1 = (int(value) for value in local(rect))
            rows.alpha_composite(self.stretched(path, (x1 - x0, y1 - y0)), (x0, y0))
            row_draw.rounded_rectangle((x0, y0, x1, y1), 6 * s, outline=(255, 255, 255, 220), width=2 * s)
            drop = local(self.info_controls()[f"loose:drop_image:{index}"])
            over = hover == f"loose:drop_image:{index}"
            row_draw.ellipse(drop, fill=(242, 104, 104, 255) if over else (6, 34, 46, 220), outline=(255, 255, 255, 240), width=s)
            text_draw.text(((drop[0] + drop[2]) / 2, (drop[1] + drop[3]) / 2), "x", font=load_font("segoeuib.ttf", 9), fill=WHITE, anchor="mm")
        if len(boxes) < len(images):
            text_draw.text((box[2] - 24 * s, box[3] - 10 * s), f"{len(images) - len(boxes)} more not shown", font=load_font("segoeuib.ttf", 9), fill=SOFT, anchor="rb")
        text_draw.text((local(layout["author"])[0], (layout["author"][1] - oy - 22) * s), "Saved to mod_info.txt in the mod folder as you type", font=load_font("segoeui.ttf", 9), fill=SOFT, anchor="lb")

    def place_info_widgets(self) -> None:
        source = self.source()
        show = self.host.screen_mode == "loose" and self.tab == "info" and source is not None and not self.showing_display() and bool(self.geometry)
        if not show:
            for item in self.info_items:
                self.host.canvas.delete(item)
            self.info_items = []
            return
        self.create_info_widgets()
        self.load_info(source)
        if self.info_items:
            return
        layout = self.info_layout()
        bx, by = self.bounds[0], self.bounds[1]
        for key in ("author", "version", "description"):
            x0, y0, x1, y1 = layout[key]
            self.info_items.append(self.host.canvas.create_window(bx + x0, by + y0, window=self.info_widgets[key], anchor="nw", width=x1 - x0, height=y1 - y0))

    def create_info_widgets(self) -> None:
        if self.info_widgets:
            return
        style = {"bg": INFO_FIELD_COLOUR, "fg": "white", "insertbackground": "white", "relief": "flat", "font": ("Segoe UI", 10),
                 "highlightthickness": 0, "selectbackground": "#5fd0ea", "selectforeground": "#0e4658"}
        self.info_widgets["author"] = tk.Entry(self.host.canvas, **style)
        self.info_widgets["version"] = tk.Entry(self.host.canvas, **style)
        self.info_widgets["description"] = tk.Text(self.host.canvas, wrap="word", padx=6, pady=4, **style)
        for widget in self.info_widgets.values():
            widget.bind("<KeyRelease>", self.save_info)

    def load_info(self, source: Path) -> None:
        if self.info_loaded_for == str(source):
            return
        info = read_mod_info(source)
        for key in ("author", "version"):
            self.info_widgets[key].delete(0, "end")
            self.info_widgets[key].insert(0, getattr(info, key))
        self.info_widgets["description"].delete("1.0", "end")
        self.info_widgets["description"].insert("1.0", info.description)
        self.info_loaded_for = str(source)

    def save_info(self, event: Any = None) -> None:
        source = self.source()
        if source is None or self.info_loaded_for != str(source):
            return
        info = ModInfo(self.info_widgets["author"].get(), self.info_widgets["version"].get(), self.info_widgets["description"].get("1.0", "end-1c"))
        try:
            write_mod_info(source, info)
        except OSError as exc:
            self.notice = f"Couldnt save mod_info.txt: {exc}"
            self.touch()
            self.redraw()

    def add_images(self) -> None:
        source = self.source()
        if source is None:
            return
        chosen = filedialog.askopenfilenames(parent=self.host.root, title="Pick preview images", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tga")])
        if not chosen:
            return
        folder = source / "preview"
        folder.mkdir(parents=True, exist_ok=True)
        for name in chosen:
            path = Path(name)
            target = folder / path.name
            number = 2
            while target.exists() and target.resolve() != path.resolve():
                target = folder / f"{path.stem} ({number}){path.suffix}"
                number += 1
            if target.resolve() != path.resolve():
                shutil.copy2(path, target)
        self.notice = f"Added {len(chosen)} preview image" + ("" if len(chosen) == 1 else "s") + ", they ship with the mod on the next build"
        self.touch()
        self.redraw()

    def drop_image(self, index: int) -> None:
        source = self.source()
        images = preview_images(source) if source is not None else []
        if not 0 <= index < len(images):
            return
        if not messagebox.askyesno("Sunflowyr Engine", f"Remove {images[index].name} from the preview folder?", parent=self.host.root):
            return
        try:
            images[index].unlink()
        except OSError as exc:
            self.notice = f"Couldnt remove {images[index].name}: {exc}"
        self.touch()
        self.redraw()
