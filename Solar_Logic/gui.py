from __future__ import annotations
import json, os, queue, re, subprocess, sys, textwrap, threading, time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
import tkinter as tk
from tkinter import filedialog, messagebox
from . import __version__
from PIL import ImageTk
from .asset_bundle_generator import AssetBundleGeneratorCanvas
from .asset_finder import AssetFinderPanel
from .bundle_inspector import BundleInspectorPanel
from .loose_builder import LooseBuilderPanel
from .scroll_shell import render_markers, render_scroll_shell, split_scroll_caps
from .shader_compiler_panel import ShaderCompilerCanvas
from .updates_panel import UpdatesPanel

@dataclass(frozen=True, slots=True)
class ScrollLayoutTuning:
    transparent_window: bool = True
    transparent_color: str = "#010203"
    frameless_window: bool = True
    topmost_window: bool = True
    start_x: int = 80
    start_y: int = 60
    default_width: int = 1360
    default_height: int = 780
    min_width: int = 1120
    min_height: int = 640
    screen_fill: str = "#364144"
    screen_alpha: float = 0.86
    side_margin_min: float = 18.0
    side_margin_max: float = 42.0
    side_margin_scale: float = 0.035
    body_height_scale: float = 0.76
    body_height_from_width: float = 0.45
    body_height_min: float = 500.0
    body_height_padding: float = 82.0
    rail_width_min: float = 96.0
    rail_width_max: float = 136.0
    rail_width_scale: float = 0.1
    shell_ink: float = 3.0
    shell_corner_scale: float = 0.38
    bezel_min: float = 10.0
    bezel_scale: float = 0.022
    glass_lip_scale: float = 0.3
    diamond_half_width_scale: float = 0.2
    diamond_aspect: float = 1.55
    diamond_height: float = 0.5
    close_duration: float = 0.4
    close_hold: float = 0.3
    close_frame_ms: int = 15
    screen_side_inset: float = 6.0
    screen_vertical_pad: float = 8.0
    screen_content_pad: float = 22.0
    title_top_offset: float = 16.0
    top_folder_icon_offset: float = 250.0
    top_folder_icon_offset_min: float = 225.0
    top_folder_icon_gap: float = 84.0
    top_folder_icon_gap_min: float = 72.0
    top_folder_icon_y_offset: float = 16.0
    top_right_icon_width: float = 208.0
    top_right_icon_y_offset: float = 20.0
    status_width: float = 184.0
    status_top_offset: float = 54.0
    path_top_offset: float = 92.0
    path_width_scale: float = 0.52
    path_height: float = 46.0
    path_gap: float = 58.0
    options_gap: float = 24.0
    options_height: float = 166.0
    options_drag_threshold: float = 5.0
    options_row_height: float = 40.0
    options_visible_rows: int = 3
    bundle_drag_threshold: float = 10.0
    start_from_screen_bottom: float = 154.0
    dial_x_offset: float = 74.0
    dial_y_offset: float = 50.0
    dial_radius: float = 64.0
    dial_label_gap: float = 24.0
    log_x_offset: float = 172.0
    log_height: float = 134.0
    log_top_gap: float = 18.0
    log_height_max: float = 320.0

LAYOUT = ScrollLayoutTuning()

SETTINGS_PATH = Path(__file__).resolve().parent / "sunflowyr_settings.json"
BUNDLE_GENERATOR_SETTING = "bundle_generator_enabled"
BUNDLE_GENERATOR_NOTICE = (
    "The AssetBundle Generator needs more ingame tests with BepInEx. By enabling the AssetBundle Generator "
    "you are accepting it may not be as supported as loose mods are for now.\n\nEnable the AssetBundle Generator?"
)

LOG_LIMIT = 500
LOG_LINE_HEIGHT = 18.0
LOG_SCROLLBAR_WIDTH = 10.0
LOG_COLOURS = {"issue": "#f0b7ae", "result": "#dbe7c9", "info": "#c8dadd"}
LOG_ISSUE_WORDS = ("error", "failed", "failure", "unsupported", "could not", "cannot",
                   "missing", "skipped", "invalid", "traceback", "warning", "refused")
LOG_ISSUE_PATTERN = re.compile(r"\b(couldnt|cant|wouldnt|wasnt found)\b")
LOG_ZERO_COUNT = re.compile(r"^[A-Za-z][A-Za-z0-9 /()-]*:\s*0$")
LOG_ALWAYS_KEEP = ("scanned files", "scanned objects", "output")

def log_severity(line: str) -> str:
    text = line.strip()
    lowered = text.lower()
    if LOG_ZERO_COUNT.match(text) and not any(lowered.startswith(k) for k in LOG_ALWAYS_KEEP):
        return "noise"
    if any(word in lowered for word in LOG_ISSUE_WORDS) or LOG_ISSUE_PATTERN.search(lowered):
        return "issue"
    if ":" in text and text.rsplit(":", 1)[-1].strip().replace(",", "").isdigit():
        return "result"
    return "info"

def ease_in_out(value: float) -> float:
    if value < 0.5:
        return 4.0 * value ** 3
    return 1.0 - (2.0 - 2.0 * value) ** 3 / 2.0

@dataclass(slots=True)
class HitRegion:
    name: str
    bounds: tuple[float, float, float, float]
    enabled: bool = True

@dataclass(slots=True)
class ScrollTrack:
    bounds: tuple[float, float, float, float]
    content: float
    view: float
    minimum_thumb: float
    read: Callable[[], float]
    apply: Callable[[float], None]

class SolarScrollGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.project_root = Path(__file__).resolve().parent.parent
        self.data_root = self.default_data_root()
        self.output_root = self.project_root / "Solar_Output"
        self.settings: dict[str, Any] = {}
        self.load_settings()
        self.toggles = {
            "raw_objects": True,
            "full": True,
            "text_assets": True,
            "png": True,
            "sprites": True,
            "audio": True,
            "model_glb": True,
            "reconstructed_glb": True,
            "mono_json": True,
            "animation_package": True,
            "material_json": True,
            "fonts": True,
            "shaders": True,
            "textmesh_json": True,
            "ui_json": True,
            "map_component_json": True,
            "editable_asset_json": True,
            "gameobject_json": True,
        }

        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.process: subprocess.Popen[str] | None = None
        self.screen_window: tk.Toplevel | None = None
        self.screen_geometry: tuple[int, int, int, int] | None = None
        self.layout = LAYOUT
        self.window_transparency_enabled = False
        self.drag_data = {"active": False, "x": 0, "y": 0, "window_x": 0, "window_y": 0}
        self.left_press_data = {
            "active": False,
            "x": 0.0,
            "y": 0.0,
            "action": "",
            "in_options": False,
            "in_bundle_generator": False,
            "moved": False,
            "scroll_y": 0.0,
            "scroll_x": 0.0,
            "scrollbar": "",
            "grab": 0.0,
            "bundle_scroll_y": 0.0,
        }
        self.options_scroll_y = 0.0
        self.options_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.screen_mode = "home"
        self.topmost = self.layout.topmost_window
        self.window_background = (
            self.layout.transparent_color if os.name == "nt" and self.layout.transparent_window else "#101113"
        )
        self.running = False
        self.stop_requested = False
        self.status = "Ready"
        self.hover_action = ""
        self.hit_regions: list[HitRegion] = []
        self.scroll_tracks: dict[str, ScrollTrack] = {}
        self.log_lines: list[tuple[str, str]] = [
            ("Sunflowyr Engine ready.", "info"),
            ("The future of modding RWBY GE begins with Sunflowyr Engine.", "info"),
        ]
        self.log_scroll_y = 0.0
        self.log_pinned = True
        self.log_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.log_line_span = 0.0
        self.log_version = 0
        self.log_wrap_key: tuple[int, int] | None = None
        self.log_wrapped: list[tuple[str, str]] = []
        self.last_stats: dict[str, str] = {}
        self.needs_redraw = True
        self.pulse = 0
        self.width = self.layout.default_width
        self.height = self.layout.default_height
        self.shell_key: tuple[Any, ...] | None = None
        self.shell_image: ImageTk.PhotoImage | None = None
        self.shell_source = None
        self.shell_item: int | None = None
        self.shell_origin = (0, 0)
        self.closing = False
        self.closed = False
        self.close_job: str | None = None
        self.close_started = 0.0
        self.close_travel = 0.0
        self.close_layout: dict[str, Any] = {}
        self.close_masks: tuple[int, int] = (0, 0)
        self.close_pieces: list[tuple[ImageTk.PhotoImage, int, int, int, float, float]] = []

        self.root.title(f"Sunflowyr Engine {__version__}")
        if self.layout.frameless_window:
            self.root.overrideredirect(True)
        self.root.geometry(f"{self.width}x{self.height}+{self.layout.start_x}+{self.layout.start_y}")
        self.root.minsize(self.layout.min_width, self.layout.min_height)
        self.root.configure(bg=self.window_background)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.apply_windows_transparency()
        if self.layout.topmost_window:
            self.root.attributes("-topmost", True)

        self.canvas = tk.Canvas(
            self.root,
            bg=self.window_background,
            highlightthickness=0,
            bd=0,
            cursor="arrow",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self.on_resize)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", self.on_leave)
        self.canvas.bind("<ButtonPress-1>", self.on_left_press)
        self.canvas.bind("<B1-Motion>", self.on_left_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_release)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-3>", self.start_drag)
        self.canvas.bind("<B3-Motion>", self.do_drag)
        self.canvas.bind("<ButtonRelease-3>", self.stop_drag)
        self.canvas.bind("<Key>", self.on_key)
        self.root.bind("<Escape>", lambda event: self.on_close())
        self.root.bind("<F1>", self.toggle_topmost)
        self.canvas.focus_set()
        self.asset_bundle_generator = AssetBundleGeneratorCanvas(self)
        self.shader_compiler = ShaderCompilerCanvas(self)
        self.updates_panel = UpdatesPanel(self)
        self.asset_finder = AssetFinderPanel(self)
        self.loose_builder = LooseBuilderPanel(self)
        self.bundle_inspector = BundleInspectorPanel(self)
        self.screen_panels = {
            "inspector": self.bundle_inspector,
            "asset_bundle_generator": self.asset_bundle_generator,
            "shader_compiler": self.shader_compiler,
            "updates": self.updates_panel,
            "finder": self.asset_finder,
            "loose": self.loose_builder,
        }

        self.create_screen_window()
        self.draw()
        self.tick()
        self.root.after(500, lambda: self.updates_panel.warm(self.compute_layout()["hole"]))
        self.root.after(900, self.asset_finder.warm)

    def apply_windows_transparency(self) -> None:
        if os.name != "nt" or not self.layout.transparent_window:
            return
        try:
            self.root.wm_attributes("-transparentcolor", self.layout.transparent_color)
            self.window_transparency_enabled = True
        except tk.TclError:
            self.window_transparency_enabled = False

    def create_screen_window(self) -> None:
        if not self.window_transparency_enabled:
            return
        try:
            screen = tk.Toplevel(self.root)
            screen.withdraw()
            screen.overrideredirect(True)
            screen.configure(bg=self.layout.screen_fill)
            screen.attributes("-alpha", self.layout.screen_alpha)
            screen.attributes("-topmost", self.topmost)
            screen.bind("<Button-3>", self.start_drag)
            screen.bind("<B3-Motion>", self.do_drag)
            screen.bind("<ButtonRelease-3>", self.stop_drag)
            screen.bind("<ButtonPress-1>", self.forward_screen_left_press)
            screen.bind("<B1-Motion>", self.forward_screen_left_motion)
            screen.bind("<ButtonRelease-1>", self.forward_screen_left_release)
            screen.bind("<Escape>", lambda event: self.on_close())
            screen.bind("<F1>", self.toggle_topmost)
            self.screen_window = screen
            self.screen_geometry = None
        except tk.TclError:
            self.screen_window = None

    def default_data_root(self) -> Path:
        candidates = [
            self.project_root / "rwby-ge_Data",
            Path.cwd() / "rwby-ge_Data",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def on_resize(self, event: tk.Event) -> None:
        self.width = max(1, int(event.width))
        self.height = max(1, int(event.height))
        self.draw()

    def on_motion(self, event: tk.Event) -> None:
        if self.left_press_data["active"]:
            return
        action = self.action_at(event.x, event.y)
        if action != self.hover_action:
            old_action = self.hover_action
            self.hover_action = action
            self.canvas.configure(cursor="hand2" if action else "arrow")
            if self.is_options_action(action) or self.is_options_action(old_action):
                self.redraw_options_panel()
            else:
                panel = self.panel_for_action(action) or self.panel_for_action(old_action)
                if panel is not None:
                    panel.redraw()
                else:
                    self.draw()

    def on_leave(self, event: tk.Event) -> None:
        if self.hover_action:
            old_action = self.hover_action
            self.hover_action = ""
            self.canvas.configure(cursor="arrow")
            if self.is_options_action(old_action):
                self.redraw_options_panel()
            else:
                panel = self.panel_for_action(old_action)
                if panel is not None:
                    panel.redraw()
                else:
                    self.draw()

    def on_mousewheel(self, event: tk.Event) -> str | None:
        if self.screen_mode == "home" and self.inside_bounds(event.x, event.y, self.log_bounds):
            self.scroll_log_by(-3.0 if int(getattr(event, "delta", 0)) > 0 else 3.0)
            return "break"
        panel = self.active_panel()
        if panel is None:
            return None
        amount = -72.0 if int(getattr(event, "delta", 0)) > 0 else 72.0
        panel.scroll_by(amount)
        return "break"

    def on_left_press(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        scrollbar = self.action_at(event.x, event.y)
        if scrollbar in self.scroll_tracks:
            top, thumb = self.thumb_span(self.scroll_tracks[scrollbar])
            on_thumb = top <= event.y <= top + thumb
            grab = float(event.y) - top if on_thumb else thumb / 2.0
            self.left_press_data.update({"active": True, "action": "", "moved": False, "in_options": False,
                                         "in_bundle_generator": False, "scrollbar": scrollbar, "grab": grab})
            if not on_thumb:
                self.drag_scroll_track(scrollbar, float(event.y), grab)
            return
        in_options = self.screen_mode == "home" and self.inside_bounds(event.x, event.y, self.options_bounds)
        panel = self.active_panel()
        in_bundle_generator = panel is not None and panel.contains(event.x, event.y)
        self.left_press_data.update(
            {
                "active": True,
                "x": float(event.x),
                "y": float(event.y),
                "action": self.action_at(event.x, event.y),
                "in_options": in_options,
                "in_bundle_generator": in_bundle_generator,
                "moved": False,
                "scroll_y": float(self.options_scroll_y),
                "bundle_scroll_y": float(panel.scroll_y) if panel is not None else 0.0,
            }
        )
        if in_bundle_generator and hasattr(panel, "begin_drag"):
            panel.begin_drag()

    def on_left_motion(self, event: tk.Event) -> None:
        if not self.left_press_data["active"]:
            return
        if self.left_press_data["scrollbar"]:
            self.left_press_data["moved"] = True
            self.drag_scroll_track(str(self.left_press_data["scrollbar"]), float(event.y), float(self.left_press_data["grab"]))
            return
        dx = float(event.x) - float(self.left_press_data["x"])
        dy = float(event.y) - float(self.left_press_data["y"])
        threshold = self.layout.options_drag_threshold
        if self.left_press_data["in_options"]:
            if not self.left_press_data["moved"] and abs(dx) < threshold and abs(dy) < threshold:
                return
            self.left_press_data["moved"] = True
            self.options_scroll_y = self.snap_options_scroll(float(self.left_press_data["scroll_y"]) - dy)
            self.hover_action = ""
            self.canvas.configure(cursor="hand2")
            self.redraw_options_panel()
            return
        if self.left_press_data["in_bundle_generator"]:
            bundle_threshold = self.layout.bundle_drag_threshold
            if not self.left_press_data["moved"] and abs(dx) < bundle_threshold and abs(dy) < bundle_threshold:
                return
            self.left_press_data["moved"] = True
            panel = self.active_panel()
            if panel is not None and hasattr(panel, "drag_to"):
                panel.drag_to(dx, dy)
                self.hover_action = ""
                self.canvas.configure(cursor="fleur")
                panel.redraw()
            elif panel is not None:
                panel.scroll_from_delta(float(self.left_press_data["bundle_scroll_y"]), dy)
                self.hover_action = ""
                self.canvas.configure(cursor="hand2")
                panel.redraw()
            return
        if abs(dx) >= threshold or abs(dy) >= threshold:
            self.left_press_data["moved"] = True

    def on_left_release(self, event: tk.Event) -> None:
        if not self.left_press_data["active"]:
            return
        pressed_action = str(self.left_press_data["action"])
        was_scrollbar = bool(self.left_press_data["scrollbar"])
        was_options_drag = bool(self.left_press_data["in_options"]) and bool(self.left_press_data["moved"])
        was_bundle_drag = bool(self.left_press_data["in_bundle_generator"]) and bool(self.left_press_data["moved"])
        was_click_drag = bool(self.left_press_data["moved"])
        self.left_press_data.update(
            {
                "active": False,
                "x": 0.0,
                "y": 0.0,
                "action": "",
                "in_options": False,
                "in_bundle_generator": False,
                "moved": False,
                "scroll_y": self.options_scroll_y,
                "bundle_scroll_y": self.asset_bundle_generator.scroll_y,
                "scrollbar": "",
                "grab": 0.0,
            }
        )
        if was_scrollbar:
            return
        if was_options_drag:
            snapped = self.snap_options_scroll(self.options_scroll_y)
            if snapped != self.options_scroll_y:
                self.options_scroll_y = snapped
                self.redraw_options_panel()
            return
        if was_bundle_drag:
            return
        if was_click_drag:
            return
        action = self.action_at(event.x, event.y)
        if not action or action != pressed_action:
            return
        self.perform_action(action)

    def bundle_generator_enabled(self) -> bool:
        return bool(self.settings.get(BUNDLE_GENERATOR_SETTING))

    def open_bundle_generator(self) -> None:
        if not self.bundle_generator_enabled():
            if not messagebox.askyesno("AssetBundle Generator", BUNDLE_GENERATOR_NOTICE, parent=self.root):
                return
            self.settings[BUNDLE_GENERATOR_SETTING] = True
            self.save_settings()
        self.switch_screen_mode("asset_bundle_generator")

    def perform_action(self, action: str) -> None:
        if action == "show_asset_bundle_generator":
            self.open_bundle_generator()
        elif action == "show_shader_compiler":
            self.switch_screen_mode("shader_compiler")
        elif action == "show_home":
            self.switch_screen_mode("home")
        elif action == "show_updates":
            self.switch_screen_mode("updates")
        elif action == "show_finder":
            self.switch_screen_mode("finder")
        elif action.startswith("finder:"):
            self.asset_finder.perform_action(action)
        elif action == "show_loose":
            self.switch_screen_mode("loose")
        elif action.startswith("loose:"):
            self.loose_builder.perform_action(action)
        elif action == "show_inspector":
            self.switch_screen_mode("inspector")
        elif action.startswith("inspect:"):
            self.bundle_inspector.perform_action(action)
        elif action == "log:scroll_up":
            self.scroll_log_by(-1.0)
        elif action == "log:scroll_down":
            self.scroll_log_by(1.0)
        elif action == "browse_data":
            self.browse_data()
        elif action == "browse_output":
            self.browse_output()
        elif action == "start":
            self.start_unpack()
        elif action == "stop":
            self.stop_unpack()
        elif action.startswith("toggle:") and not self.running:
            key = action.split(":", 1)[1]
            if key == "full":
                enabled = not self.toggles["full"]
                for toggle_name in self.toggles:
                    self.toggles[toggle_name] = enabled
            else:
                self.toggles[key] = not self.toggles[key]
                self.toggles["full"] = all(
                    value for toggle_name, value in self.toggles.items() if toggle_name != "full"
                )
            self.redraw_options_panel()
        elif action.startswith("bundle:"):
            self.asset_bundle_generator.perform_action(action)
        elif action.startswith("shader:"):
            self.shader_compiler.perform_action(action)

    def active_panel(self):
        return self.screen_panels.get(self.screen_mode)

    def panel_for_action(self, action: str):
        if not action:
            return None
        for panel in self.screen_panels.values():
            if panel.is_action(action):
                return panel
        return None

    def switch_screen_mode(self, mode: str) -> None:
        self.screen_mode = mode
        self.hover_action = ""
        self.canvas.configure(cursor="arrow")
        panel = self.screen_panels.get(mode)
        if panel is not None:
            panel.activate()
        self.draw()

    def on_key(self, event: tk.Event) -> str | None:
        panel = self.active_panel()
        if panel is not None and panel.handle_key(event):
            return "break"
        if event.keysym == "Escape" and self.running:
            self.stop_unpack()
        return None

    def on_close(self) -> None:
        if self.closing:
            self.finish_close()
            return
        if self.asset_bundle_generator.building:
            if not messagebox.askyesno(
                "Sunflowyr Engine",
                "An AssetBundle build is still running. Stop it and close?",
            ):
                return
            process = self.asset_bundle_generator.process
            if process is not None and process.poll() is None:
                process.terminate()
        if self.running:
            if not messagebox.askyesno("Sunflowyr Engine", "An unpack is still running. Stop it and close?"):
                return
            self.stop_unpack()
        self.start_closing_animation()

    def start_closing_animation(self) -> None:
        self.closing = True
        self.release_input()
        self.remove_embedded_widgets()
        if self.shell_source is None or self.layout.close_duration <= 0:
            self.finish_close()
            return
        layout = self.compute_layout()
        ox, oy = self.shell_origin
        x0, y0, x1, y1 = layout["body"]
        cx_left, cx_right = layout["diamond"][:2]
        self.close_layout = layout
        self.close_travel = (cx_right - cx_left) / 2.0
        self.close_masks = (
            self.canvas.create_rectangle(-2, -2, x0, self.height + 2, fill=self.window_background, outline="", width=0),
            self.canvas.create_rectangle(x1, -2, self.width + 2, self.height + 2, fill=self.window_background, outline="", width=0),
        )
        (left_cap, left_x), (right_cap, right_x) = split_scroll_caps(self.shell_source, ox, oy, layout, self.window_background)
        (diamond, diamond_x, diamond_y), (seat, seat_x, seat_y) = render_markers(layout)
        moving = [
            (right_cap, right_x, oy, -1.0, 1.0),
            (left_cap, left_x, oy, 1.0, 1.0),
            (seat, seat_x, seat_y, -1.0, 0.0),
            (diamond, diamond_x, diamond_y, 1.0, 0.0),
        ]
        self.close_pieces = []
        for image, piece_x, piece_y, direction, overlap in moving:
            photo = ImageTk.PhotoImage(image, master=self.root)
            item = self.canvas.create_image(piece_x, piece_y, image=photo, anchor="nw")
            self.close_pieces.append((photo, item, piece_x, piece_y, direction, overlap))
        self.close_started = time.perf_counter()
        self.step_closing_animation()

    def step_closing_animation(self) -> None:
        if self.closed:
            return
        progress = min(1.0, (time.perf_counter() - self.close_started) / self.layout.close_duration)
        self.show_closing_frame(progress)
        if progress >= 1.0:
            self.close_job = self.root.after(int(self.layout.close_hold * 1000), self.finish_close)
        else:
            self.close_job = self.root.after(self.layout.close_frame_ms, self.step_closing_animation)

    def show_closing_frame(self, progress: float) -> None:
        eased = ease_in_out(max(0.0, min(1.0, progress)))
        shift = self.close_travel * eased
        x0, y0, x1, y1 = self.close_layout["body"]
        left_mask, right_mask = self.close_masks
        self.canvas.coords(left_mask, -2, -2, x0 + shift, self.height + 2)
        self.canvas.coords(right_mask, x1 - shift, -2, self.width + 2, self.height + 2)
        for photo, item, piece_x, piece_y, direction, overlap in self.close_pieces:
            self.canvas.coords(item, piece_x + (shift + overlap * eased) * direction, piece_y)
        corner = self.close_layout["corner"]
        self.fit_glass_between(x0 + shift + corner, x1 - shift - corner)

    def fit_glass_between(self, left: float, right: float) -> None:
        if self.screen_window is None:
            return
        gx0, gy0, gx1, gy1 = self.close_layout["glass"]
        gx0 = max(gx0, left)
        gx1 = min(gx1, right)
        try:
            if gx1 - gx0 < 2:
                self.screen_window.withdraw()
                return
            x = self.root.winfo_rootx() + int(round(gx0))
            y = self.root.winfo_rooty() + int(round(gy0))
            self.screen_window.geometry(f"{int(round(gx1 - gx0))}x{int(round(gy1 - gy0))}+{x}+{y}")
            self.screen_window.lower(self.root)
        except tk.TclError:
            pass

    def remove_embedded_widgets(self) -> None:
        for item in self.canvas.find_all():
            if self.canvas.type(item) == "window":
                self.canvas.delete(item)
        self.loose_builder.info_items = []
        self.root.update_idletasks()

    def release_input(self) -> None:
        for sequence in ("<Configure>", "<Motion>", "<Leave>", "<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>",
                         "<MouseWheel>", "<Button-3>", "<B3-Motion>", "<ButtonRelease-3>", "<Key>"):
            self.canvas.unbind(sequence)
            if self.screen_window is not None:
                self.screen_window.unbind(sequence)
        self.drag_data["active"] = False
        self.canvas.configure(cursor="arrow")

    def finish_close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.closing = True
        if self.close_job is not None:
            try:
                self.root.after_cancel(self.close_job)
            except tk.TclError:
                pass
        if self.screen_window is not None:
            self.screen_window.destroy()
        self.root.destroy()

    def start_drag(self, event: tk.Event) -> None:
        self.drag_data["active"] = True
        self.drag_data["x"] = int(event.x_root)
        self.drag_data["y"] = int(event.y_root)
        self.drag_data["window_x"] = int(self.root.winfo_rootx())
        self.drag_data["window_y"] = int(self.root.winfo_rooty())
        self.keep_root_above_screen()

    def do_drag(self, event: tk.Event) -> None:
        if not self.drag_data["active"]:
            return
        dx = int(event.x_root) - self.drag_data["x"]
        dy = int(event.y_root) - self.drag_data["y"]
        x = self.drag_data["window_x"] + dx
        y = self.drag_data["window_y"] + dy
        layout = self.compute_layout()
        if not self.move_overlay_windows(x, y, layout):
            self.root.geometry(f"+{x}+{y}")
            self.sync_screen_window(layout, root_x=x, root_y=y)
        self.keep_root_above_screen()

    def stop_drag(self, event: tk.Event) -> None:
        self.drag_data["active"] = False
        self.drag_data["x"] = 0
        self.drag_data["y"] = 0
        self.drag_data["window_x"] = 0
        self.drag_data["window_y"] = 0
        self.keep_root_above_screen()

    def forward_screen_left_press(self, event: tk.Event) -> None:
        self.keep_root_above_screen()
        self.on_left_press(self.screen_event_to_canvas_event(event))

    def forward_screen_left_motion(self, event: tk.Event) -> None:
        self.on_left_motion(self.screen_event_to_canvas_event(event))

    def forward_screen_left_release(self, event: tk.Event) -> None:
        self.on_left_release(self.screen_event_to_canvas_event(event))

    def screen_event_to_canvas_event(self, event: tk.Event) -> Any:
        sx0, sy0, sx1, sy1 = self.compute_layout()["glass"]
        return SimpleNamespace(
            x=float(sx0) + float(event.x),
            y=float(sy0) + float(event.y),
            x_root=getattr(event, "x_root", 0),
            y_root=getattr(event, "y_root", 0),
            keysym=getattr(event, "keysym", ""),
            char=getattr(event, "char", ""),
        )

    def keep_root_above_screen(self) -> None:
        if self.screen_window is None:
            return
        try:
            self.root.lift()
            self.screen_window.lower(self.root)
        except tk.TclError:
            pass

    def toggle_topmost(self, event: tk.Event | None = None) -> None:
        self.topmost = not self.topmost
        if self.screen_window is not None:
            self.screen_window.attributes("-topmost", self.topmost)
        self.root.attributes("-topmost", self.topmost)
        self.keep_root_above_screen()

    def move_overlay_windows(self, root_x: int, root_y: int, layout: dict[str, Any]) -> bool:
        if os.name != "nt" or self.screen_window is None:
            return False
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.BeginDeferWindowPos.argtypes = [ctypes.c_int]
            user32.BeginDeferWindowPos.restype = ctypes.c_void_p
            user32.DeferWindowPos.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.DeferWindowPos.restype = ctypes.c_void_p
            user32.EndDeferWindowPos.argtypes = [ctypes.c_void_p]
            user32.EndDeferWindowPos.restype = ctypes.c_bool

            root_hwnd = self.top_level_hwnd(self.root)
            screen_hwnd = self.top_level_hwnd(self.screen_window)
            sx0, sy0, sx1, sy1 = layout["glass"]
            screen_x = root_x + int(round(sx0))
            screen_y = root_y + int(round(sy0))
            screen_w = max(1, int(round(sx1 - sx0)))
            screen_h = max(1, int(round(sy1 - sy0)))
            flags = 0x0004 | 0x0010

            batch = user32.BeginDeferWindowPos(2)
            if not batch:
                return False
            batch = user32.DeferWindowPos(
                batch,
                ctypes.c_void_p(root_hwnd),
                None,
                int(root_x),
                int(root_y),
                int(self.width),
                int(self.height),
                flags,
            )
            if not batch:
                return False
            batch = user32.DeferWindowPos(
                batch,
                ctypes.c_void_p(screen_hwnd),
                None,
                int(screen_x),
                int(screen_y),
                screen_w,
                screen_h,
                flags,
            )
            if not batch:
                return False
            return bool(user32.EndDeferWindowPos(batch))
        except Exception:
            return False

    def top_level_hwnd(self, widget: tk.Misc) -> int:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = int(widget.winfo_id())
        parent = int(user32.GetParent(hwnd))
        return parent or hwnd

    def tick(self) -> None:
        if self.closing:
            return
        changed = self.asset_bundle_generator.poll()
        changed = self.shader_compiler.poll() or changed
        changed = self.asset_finder.poll() or changed
        changed = self.loose_builder.poll() or changed
        changed = self.bundle_inspector.poll() or changed
        changed = self.drain_worker_queue() or changed
        if self.running and not self.drag_data["active"]:
            self.pulse = (self.pulse + 1) % 240
            self.draw()
            self.needs_redraw = False
        elif changed or self.needs_redraw:
            self.draw()
            self.needs_redraw = False
        self.root.after(80, self.tick)

    def browse_data(self) -> None:
        if self.running:
            return
        initial = self.data_root if self.data_root.exists() else self.project_root
        selected = filedialog.askdirectory(title="Select rwby-ge_Data", initialdir=str(initial))
        if selected:
            self.data_root = Path(selected)
            self.add_log(f"Data root: {self.data_root}")
            self.save_settings()

    def browse_output(self) -> None:
        if self.running:
            return
        initial = self.output_root if self.output_root.exists() else self.project_root
        selected = filedialog.askdirectory(title="Select Solar output folder", initialdir=str(initial))
        if selected:
            self.output_root = Path(selected)
            self.add_log(f"Output root: {self.output_root}")
            self.save_settings()

    def load_settings(self) -> None:
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        self.settings = saved
        if isinstance(saved.get("data_root"), str) and saved["data_root"]:
            self.data_root = Path(saved["data_root"])
        if isinstance(saved.get("output_root"), str) and saved["output_root"]:
            self.output_root = Path(saved["output_root"])

    def save_settings(self) -> None:
        self.settings["data_root"] = str(self.data_root)
        self.settings["output_root"] = str(self.output_root)
        try:
            SETTINGS_PATH.write_text(json.dumps(self.settings, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            self.add_log(f"Could not save settings: {exc}")

    def start_unpack(self, only_file: str = "", output_root: Path | None = None, data_root: Path | None = None) -> None:
        if self.running:
            return
        output_root = output_root or self.output_root
        source_root = data_root or self.data_root
        if not source_root.exists():
            messagebox.showerror("Sunflowyr Engine", f"Data root doesnt exist:\n{source_root}")
            return
        if data_root is None and not (source_root / "globalgamemanagers").exists():
            if not messagebox.askyesno(
                "Sunflowyr Engine",
                "This folder doesnt look like rwby-ge_Data. Continue anyway?",
            ):
                return

        if not any(value for key, value in self.toggles.items() if key != "full"):
            messagebox.showinfo(
                "Sunflowyr Engine",
                "Select Raw, Full, or at least one editable export format.",
            )
            return

        cmd = self.build_unpack_command(only_file, output_root, source_root)
        self.running = True
        self.stop_requested = False
        self.last_stats = {}
        self.status = "Unpacking"
        self.reset_log([
            f"Launching unpack process for {only_file}." if only_file else "Launching unpack process.",
            f"Data: {source_root}",
            f"Output: {output_root}",
        ])
        self.worker_thread = threading.Thread(target=self.run_unpack_process, args=(cmd,), daemon=True)
        self.worker_thread.start()
        self.draw()

    def stop_unpack(self) -> None:
        if not self.running:
            return
        self.stop_requested = True
        self.status = "Stopping"
        process = self.process
        if process is not None and process.poll() is None:
            self.add_log("Stop requested.")
            process.terminate()
        else:
            self.add_log("Stop requested before process startup completed.")

    def build_unpack_command(self, only_file: str = "", output_root: Path | None = None, data_root: Path | None = None) -> list[str]:
        command = [
            self.python_executable(),
            str(self.project_root / "solar_cli.py"),
            "unpack",
            "--data",
            str(data_root or self.data_root),
            "--out",
            str(output_root or self.output_root),
        ]
        if only_file:
            command.extend(["--file", only_file])
        if self.toggles["full"]:
            command.append("--full")
            return command
        if self.toggles["raw_objects"]:
            command.append("--raw")
        editable_map = {
            "text_assets": "text-assets",
            "png": "png",
            "sprites": "sprites",
            "audio": "audio",
            "model_glb": "model-glb",
            "reconstructed_glb": "reconstructed-glb",
            "mono_json": "mono-json",
            "animation_package": "animation",
            "material_json": "material-json",
            "fonts": "fonts",
            "shaders": "shaders",
            "textmesh_json": "textmesh-json",
            "ui_json": "ui-json",
            "map_component_json": "map-json",
            "editable_asset_json": "asset-data",
            "gameobject_json": "gameobject-json",
        }
        for key, editable_name in editable_map.items():
            if self.toggles[key]:
                command.extend(["--editable", editable_name])
        return command

    def python_executable(self) -> str:
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            console_python = executable.with_name("python.exe")
            if console_python.exists():
                return str(console_python)
        return str(executable)

    def run_unpack_process(self, command: list[str]) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.project_root),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            self.process = process
            if process.stdout is not None:
                for line in process.stdout:
                    text = line.rstrip()
                    if text:
                        self.worker_queue.put(("log", text))
            return_code = process.wait()
            self.worker_queue.put(("done", return_code))
        except Exception as exc:
            self.worker_queue.put(("error", str(exc)))
        finally:
            self.process = None

    def drain_worker_queue(self) -> bool:
        changed = False
        while True:
            try:
                kind, payload = self.worker_queue.get_nowait()
            except queue.Empty:
                break
            changed = True
            if kind == "log":
                self.add_log(str(payload))
                self.parse_stat_line(str(payload))
            elif kind == "done":
                self.running = False
                if self.stop_requested:
                    self.status = "Stopped"
                    self.add_log("Unpack stopped.")
                elif int(payload) == 0:
                    self.status = "Complete"
                    self.add_log("Unpack complete.")
                else:
                    self.status = "Failed"
                    self.add_log(f"Unpack failed with exit code {payload}.")
            elif kind == "error":
                self.running = False
                self.status = "Failed"
                self.add_log(f"Process error: {payload}")
        return changed

    def parse_stat_line(self, line: str) -> None:
        if ":" not in line:
            return
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        tracked = {
            "Scanned files",
            "Scanned objects",
            "Raw object exports",
            "Raw object bytes",
            "Inlined resource objects",
            "Inlined resource bytes",
            "Raw inlining errors",
            "TextAsset exports",
            "PNG exports",
            "Sprite PNG exports",
            "Sprite JSON exports",
            "Audio exports",
            "Mesh GLB exports",
            "Scene GLB exports",
            "Prefab GLB exports",
            "Terrain GLB exports",
            "Terrain JSON exports",
            "NavMesh GLB exports",
            "NavMesh JSON exports",
            "MonoBehaviour JSON exports",
            "AnimationClip GLB exports",
            "Typed MonoBehaviour JSON exports",
            "Raw-fallback MonoBehaviour JSON exports",
            "Unsupported PNG conversions",
            "Unsupported Sprite conversions",
            "Unsupported audio conversions",
            "Unsupported Mesh GLB conversions",
            "Reconstructed GLB errors",
            "Unsupported MonoBehaviour JSON exports",
            "Unsupported AnimationClip GLB exports",
            "Unsupported map component JSON exports",
            "Map component JSON exports",
            "Unsupported editable asset JSON exports",
            "Editable asset JSON exports",
            "Missing resources skipped",
            "Errors",
            "Output",
        }
        if key in tracked:
            self.last_stats[key] = value

    def add_log(self, line: str) -> None:
        text = str(line).rstrip()
        if not text:
            return
        severity = log_severity(text)
        if severity == "noise":
            return
        self.log_lines.append((text, severity))
        if len(self.log_lines) > LOG_LIMIT:
            del self.log_lines[: len(self.log_lines) - LOG_LIMIT]
        self.log_version += 1
        self.needs_redraw = True

    def reset_log(self, lines: list[str]) -> None:
        self.log_lines = [(str(line), log_severity(str(line))) for line in lines]
        self.log_version += 1
        self.log_scroll_y = 0.0
        self.log_pinned = True
        self.needs_redraw = True

    def wrapped_log(self, panel_width: float) -> list[tuple[str, str]]:
        max_chars = max(20, int((panel_width - 28.0 - LOG_SCROLLBAR_WIDTH) / 7.1))
        key = (max_chars, self.log_version)
        if self.log_wrap_key == key:
            return self.log_wrapped
        wrapped: list[tuple[str, str]] = []
        for text, severity in self.log_lines:
            parts = textwrap.wrap(text, max_chars, break_long_words=False, break_on_hyphens=False)
            for part in parts or [""]:
                wrapped.append((part, severity))
        self.log_wrapped = wrapped
        self.log_wrap_key = key
        return wrapped

    def log_max_scroll(self) -> float:
        return max(0.0, self.log_line_span - self.log_visible_lines())

    def log_visible_lines(self) -> float:
        x0, y0, x1, y1 = self.log_bounds
        return max(1.0, (y1 - y0 - 42.0) / LOG_LINE_HEIGHT)

    def clamp_log_scroll(self, value: float) -> float:
        return max(0.0, min(float(value), self.log_max_scroll()))

    def scroll_log_by(self, lines: float) -> None:
        before = self.log_scroll_y
        self.log_scroll_y = self.clamp_log_scroll(self.log_scroll_y + lines)
        self.log_pinned = self.log_scroll_y >= self.log_max_scroll() - 0.01
        if self.log_scroll_y != before:
            self.needs_redraw = True
            self.draw()

    def draw(self) -> None:
        if self.closing:
            return
        self.canvas.delete("all")
        self.hit_regions = []
        self.scroll_tracks = {}
        layout = self.compute_layout()
        self.sync_screen_window(layout)
        if not self.window_transparency_enabled:
            self.draw_background()
        self.draw_scroll_shell(layout)
        self.draw_screen_ui(layout)

    def sync_screen_window(
        self,
        layout: dict[str, Any],
        root_x: int | None = None,
        root_y: int | None = None,
    ) -> None:
        if self.screen_window is None:
            return
        sx0, sy0, sx1, sy1 = layout["glass"]
        width = max(1, int(round(sx1 - sx0)))
        height = max(1, int(round(sy1 - sy0)))
        base_x = self.root.winfo_rootx() if root_x is None else root_x
        base_y = self.root.winfo_rooty() if root_y is None else root_y
        x = base_x + int(round(sx0))
        y = base_y + int(round(sy0))
        geometry = (width, height, x, y)
        try:
            if geometry != self.screen_geometry:
                self.screen_window.geometry(f"{width}x{height}+{x}+{y}")
                self.screen_window.configure(bg=self.layout.screen_fill)
                self.screen_window.attributes("-alpha", self.layout.screen_alpha)
                self.screen_window.deiconify()
                self.screen_geometry = geometry
            if not self.drag_data["active"]:
                self.screen_window.lower(self.root)
        except tk.TclError:
            self.screen_geometry = None

    def compute_layout(self) -> dict[str, tuple[float, float, float, float] | float]:
        cfg = self.layout
        w = float(self.width)
        h = float(self.height)
        margin_x = max(cfg.side_margin_min, min(cfg.side_margin_max, w * cfg.side_margin_scale))
        body_h = min(h * cfg.body_height_scale, w * cfg.body_height_from_width)
        body_h = max(cfg.body_height_min, min(body_h, h - cfg.body_height_padding))
        y0 = (h - body_h) * 0.50
        y1 = y0 + body_h
        x0 = margin_x
        x1 = w - margin_x
        rail_w = max(cfg.rail_width_min, min(cfg.rail_width_max, (x1 - x0) * cfg.rail_width_scale))
        bezel = max(cfg.bezel_min, body_h * cfg.bezel_scale)
        hole = (x0 + rail_w, y0 + bezel, x1 - rail_w, y1 - bezel)
        lip = rail_w * cfg.glass_lip_scale
        glass = (hole[0] - lip, hole[1] - 2.0, hole[2] + lip, hole[3] + 2.0)
        screen = (
            hole[0] + cfg.screen_side_inset,
            hole[1] + cfg.screen_vertical_pad,
            hole[2] - cfg.screen_side_inset,
            hole[3] - cfg.screen_vertical_pad,
        )
        half_w = rail_w * cfg.diamond_half_width_scale
        half_h = half_w * cfg.diamond_aspect
        diamond = (hole[0], hole[2], y0 + body_h * cfg.diamond_height, half_w, half_h)
        return {
            "body": (x0, y0, x1, y1),
            "left_rail": (x0, y0, x0 + rail_w, y1),
            "right_rail": (x1 - rail_w, y0, x1, y1),
            "hole": hole,
            "glass": glass,
            "screen": screen,
            "diamond": diamond,
            "rail_w": rail_w,
            "ink": cfg.shell_ink,
            "corner": rail_w * cfg.shell_corner_scale,
        }

    def draw_background(self) -> None:
        w = self.width
        h = self.height
        self.canvas.create_rectangle(0, 0, w, h, fill="#101113", outline="")
        for i in range(9):
            y = h * (0.14 + i * 0.095)
            self.canvas.create_line(0, y, w, y + 18, fill="#171a1c", width=1)
        for x in range(-40, w + 80, 82):
            self.canvas.create_line(x, 0, x + 130, h, fill="#131619", width=1)

    def draw_scroll_shell(self, layout: dict[str, Any]) -> None:
        x0, y0, x1, y1 = layout["body"]
        hx0, hy0, hx1, hy1 = layout["hole"]

        if not self.window_transparency_enabled:
            self.rounded_rect(x0 + 8, y0 + 12, x1 - 8, y1 + 14, layout["corner"], fill="#08090a", outline="", width=0)
        if not self.window_transparency_enabled or self.screen_window is None:
            self.canvas.create_rectangle(hx0, hy0, hx1, hy1, fill=self.layout.screen_fill, outline="")
        key = (self.width, self.height)
        if key != self.shell_key or self.shell_image is None:
            image, ox, oy = render_scroll_shell(layout)
            self.shell_image = ImageTk.PhotoImage(image, master=self.root)
            self.shell_source = image
            self.shell_origin = (ox, oy)
            self.shell_key = key
        self.shell_item = self.canvas.create_image(*self.shell_origin, image=self.shell_image, anchor="nw")

    def draw_screen_ui(self, layout: dict[str, Any]) -> None:
        cfg = self.layout
        sx0, sy0, sx1, sy1 = layout["screen"]
        screen_w = sx1 - sx0
        pad = cfg.screen_content_pad
        left = sx0 + pad
        right = sx1 - pad
        top = sy0 + cfg.title_top_offset

        if self.screen_mode == "updates":
            self.updates_panel.draw(layout["hole"], self.shell_item)
            return
        if self.screen_mode == "finder":
            self.asset_finder.draw(layout["hole"], self.shell_item)
            return
        if self.screen_mode == "loose":
            self.loose_builder.draw(layout["hole"], self.shell_item)
            return
        if self.screen_mode == "inspector":
            self.bundle_inspector.draw(layout["hole"], self.shell_item)
            return
        self.draw_top_glyphs(sy0, left, right)
        self.canvas.create_text(
            left,
            top,
            anchor="nw",
            text="Sunflowyr Engine",
            fill="#edf6f7",
            font=("Segoe UI", 19, "bold"),
        )
        self.canvas.create_text(
            left,
            top + 30,
            anchor="nw",
            text="RWBY GE Modding Software",
            fill="#aebec1",
            font=("Segoe UI", 10),
        )

        panel = self.active_panel()
        if panel is not None:
            panel.draw(
                left,
                right,
                top + cfg.path_top_offset,
                sy1 - cfg.screen_content_pad,
                drag_bounds=(sx0, sy0, sx1, sy1),
            )
            return

        status_x = right - cfg.status_width
        self.draw_status_chip(status_x, top + cfg.status_top_offset, cfg.status_width, 30.0)

        field_y = top + cfg.path_top_offset
        field_w = screen_w * cfg.path_width_scale
        self.draw_path_field(left, field_y, field_w, cfg.path_height, "RWBY Data", self.data_root, "browse_data")
        self.draw_path_field(left, field_y + cfg.path_gap, field_w, cfg.path_height, "Output", self.output_root, "browse_output")

        option_x = left + field_w + cfg.options_gap
        option_y = field_y
        self.draw_options(option_x, option_y, right - option_x)
        options_bottom = option_y + cfg.options_height

        start_y = sy1 - cfg.start_from_screen_bottom
        dial_bottom = cfg.dial_y_offset + cfg.dial_radius + cfg.dial_label_gap + 10.0
        start_y = min(start_y, sy1 - cfg.screen_content_pad - dial_bottom)
        self.draw_start_dial(left + cfg.dial_x_offset, start_y + cfg.dial_y_offset, cfg.dial_radius)

        log_x = left + cfg.log_x_offset
        log_bottom = start_y + cfg.log_height
        available = log_bottom - (options_bottom + cfg.log_top_gap)
        log_h = max(cfg.log_height, min(cfg.log_height_max, available))
        self.draw_log_panel(log_x, log_bottom - log_h, right - log_x, log_h)

    def top_folder_slot_positions(self, left: float, right: float) -> list[float]:
        cfg = self.layout
        icon_width = 70.0
        limit = right - cfg.top_right_icon_width - 18
        default_start = left + cfg.top_folder_icon_offset
        floor = left + cfg.top_folder_icon_offset_min
        for count in (3, 2, 1):
            gap = cfg.top_folder_icon_gap
            span = gap * (count - 1) + icon_width
            start = default_start
            if start + span > limit and count > 1:
                gap = max(cfg.top_folder_icon_gap_min, (limit - start - icon_width) / (count - 1))
                span = gap * (count - 1) + icon_width
            if start + span > limit:
                start = max(floor, limit - span)
            if start + span <= limit:
                return [start + gap * index for index in range(count)]
        return []

    def draw_top_glyphs(self, sy0: float, left: float, right: float) -> None:
        cfg = self.layout
        icon_y = sy0 + cfg.top_folder_icon_y_offset
        right_icon_start = right - cfg.top_right_icon_width
        folder_slots = self.top_folder_slot_positions(left, right)
        for idx, x in enumerate(folder_slots):
            colours = ((10, "#8e9ea1"), (5, "#bccbcd"), (0, "#eef6f7"))
            if idx == 2 and not self.bundle_generator_enabled():
                colours = ((10, "#5b6668"), (5, "#6d797b"), (0, "#86918f"))
            for offset, colour in colours:
                self.rounded_rect(x + offset, icon_y + offset, x + 54 + offset, icon_y + 34 + offset, 5, colour, "", 0)
            if idx == 2:
                self.add_region(
                    "show_asset_bundle_generator",
                    (x, icon_y, x + 70, icon_y + 50),
                    enabled=not self.running,
                )
            elif idx == 1:
                self.add_region(
                    "show_shader_compiler",
                    (x, icon_y, x + 70, icon_y + 50),
                    enabled=not self.running,
                )
            else:
                self.add_region(
                    "show_inspector",
                    (x, icon_y, x + 70, icon_y + 50),
                    enabled=not self.running,
                )

        x = right_icon_start
        y = sy0 + cfg.top_right_icon_y_offset
        self.canvas.create_line(x, y + 14, x + 14, y, x + 28, y + 14, fill="#dbe7e9", width=2)
        self.canvas.create_rectangle(x + 5, y + 14, x + 23, y + 28, outline="#dbe7e9", width=2)
        self.add_region("show_home", (x - 4, y - 4, x + 32, y + 32), enabled=True)
        x += 38
        self.canvas.create_oval(x, y, x + 28, y + 28, outline="#dbe7e9", width=2)
        self.canvas.create_oval(x + 8, y + 1, x + 20, y + 27, outline="#dbe7e9", width=1)
        self.canvas.create_line(x + 1, y + 14, x + 27, y + 14, fill="#dbe7e9", width=1)
        self.canvas.create_line(x + 4, y + 7, x + 24, y + 7, fill="#dbe7e9", width=1)
        self.canvas.create_line(x + 4, y + 21, x + 24, y + 21, fill="#dbe7e9", width=1)
        self.add_region("show_loose", (x - 4, y - 4, x + 32, y + 32), enabled=not self.running)
        x += 40
        self.canvas.create_oval(x, y + 2, x + 20, y + 22, outline="#dbe7e9", width=2)
        self.canvas.create_line(x + 18, y + 20, x + 29, y + 29, fill="#dbe7e9", width=2)
        self.add_region("show_finder", (x - 4, y - 2, x + 32, y + 32), enabled=not self.running)
        x += 42
        self.canvas.create_rectangle(x, y + 4, x + 30, y + 26, outline="#dbe7e9", width=2)
        self.canvas.create_line(x, y + 4, x + 15, y + 17, x + 30, y + 4, fill="#dbe7e9", width=2)
        self.add_region("show_updates", (x - 4, y - 4, x + 34, y + 32), enabled=not self.running)

    def draw_status_chip(self, x: float, y: float, w: float, h: float) -> None:
        color = "#67d8d8"
        if self.status == "Complete":
            color = "#83de86"
        elif self.status in {"Failed", "Stopped"}:
            color = "#e06c75"
        elif self.status == "Stopping":
            color = "#e6bf57"
        self.rounded_rect(x, y, x + w, y + h, 10, fill="#20292b", outline="#84999d", width=1)
        self.canvas.create_oval(x + 12, y + 9, x + 23, y + 20, fill=color, outline="")
        self.canvas.create_text(
            x + 34,
            y + h / 2,
            anchor="w",
            text=self.status,
            fill="#edf6f7",
            font=("Segoe UI", 9, "bold"),
        )

    def draw_path_field(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        label: str,
        path: Path,
        action: str,
    ) -> None:
        hovered = self.hover_action == action
        outline = "#e7f4f6" if hovered else "#9fb0b4"
        self.rounded_rect(x, y, x + w, y + h, 6, fill="#253033", outline=outline, width=2 if hovered else 1)
        self.canvas.create_text(x + 14, y + 9, anchor="w", text=label, fill="#b9c8cb", font=("Segoe UI", 8, "bold"))
        max_chars = max(18, int((w - 82) / 7.2))
        self.canvas.create_text(
            x + 14,
            y + 29,
            anchor="w",
            text=self.middle_ellipsis(str(path), max_chars),
            fill="#edf6f7",
            font=("Consolas", 10),
        )
        bx = x + w - 48
        by = y + 9
        self.rounded_rect(bx, by, bx + 34, by + 28, 5, fill="#dfe8e8" if hovered else "#aebfc1", outline="", width=0)
        self.canvas.create_rectangle(bx + 8, by + 12, bx + 26, by + 22, fill="#324044", outline="")
        self.canvas.create_polygon(bx + 8, by + 12, bx + 14, by + 8, bx + 21, by + 12, fill="#324044", outline="")
        self.add_region(action, (x, y, x + w, y + h), enabled=not self.running)

    def draw_options(self, x: float, y: float, w: float, tag: str = "options_panel") -> None:
        before_items = set(self.canvas.find_all())
        self.options_bounds = (x, y, x + w, y + self.layout.options_height)
        self.options_scroll_y = self.clamp_options_scroll(self.options_scroll_y)
        self.rounded_rect(x, y, x + w, y + self.layout.options_height, 8, fill="#20292b", outline="#9fb0b4", width=1)
        self.canvas.create_text(x + 16, y + 14, anchor="w", text="Unpack Set", fill="#edf6f7", font=("Segoe UI", 10, "bold"))
        self.canvas.create_text(
            x + w - 16,
            y + 14,
            anchor="e",
            text="Raw/Editable",
            fill="#e3bc4d",
            font=("Segoe UI", 8, "bold"),
        )
        rows = self.export_toggle_rows()
        col_w = (w - 34) / 2
        content_y = y + 42
        visible_y1 = content_y + self.layout.options_visible_rows * self.layout.options_row_height
        for index, (key, label) in enumerate(rows):
            col = index % 2
            row = index // 2
            toggle_y = content_y + row * self.layout.options_row_height - self.options_scroll_y
            if toggle_y < content_y or toggle_y + 28 > visible_y1:
                continue
            self.draw_toggle(x + 16 + col * col_w, toggle_y, col_w - 10, 28, key, label)
        self.tag_canvas_items(before_items, tag)

    def redraw_options_panel(self) -> None:
        x0, y0, x1, y1 = self.options_bounds
        if x1 <= x0:
            self.draw()
            return
        self.hit_regions = [region for region in self.hit_regions if not self.is_options_action(region.name)]
        new_tag = "__options_panel_new"
        self.canvas.delete(new_tag)
        self.draw_options(x0, y0, x1 - x0, tag=new_tag)
        self.canvas.delete("options_panel")
        self.canvas.addtag_withtag("options_panel", new_tag)
        self.canvas.dtag(new_tag, new_tag)

    def export_toggle_rows(self) -> list[tuple[str, str]]:
        return [
            ("raw_objects", "Raw"),
            ("full", "Full"),
            ("text_assets", "TextAsset"),
            ("png", "PNG"),
            ("sprites", "Sprites"),
            ("audio", "Audio"),
            ("model_glb", "Model GLB"),
            ("reconstructed_glb", "Scene/Prefab"),
            ("mono_json", "Mono JSON"),
            ("animation_package", "Animation"),
            ("material_json", "Mat JSON"),
            ("fonts", "Fonts"),
            ("shaders", "Shaders"),
            ("textmesh_json", "TextMesh"),
            ("ui_json", "UI JSON"),
            ("map_component_json", "Map JSON"),
            ("editable_asset_json", "Asset Data"),
            ("gameobject_json", "GameObj JSON"),
        ]

    def max_options_scroll(self) -> float:
        row_count = (len(self.export_toggle_rows()) + 1) // 2
        hidden_rows = max(0, row_count - self.layout.options_visible_rows)
        return float(hidden_rows * self.layout.options_row_height)

    def clamp_options_scroll(self, value: float) -> float:
        return max(0.0, min(float(value), self.max_options_scroll()))

    def snap_options_scroll(self, value: float) -> float:
        row_height = self.layout.options_row_height
        if row_height <= 0:
            return self.clamp_options_scroll(value)
        return self.clamp_options_scroll(round(float(value) / row_height) * row_height)

    def draw_toggle(self, x: float, y: float, w: float, h: float, key: str, label: str) -> None:
        action = f"toggle:{key}"
        value = self.toggles[key]
        hovered = self.hover_action == action
        fill = "#dfe8e8" if value else "#263235"
        outline = "#edf6f7" if hovered else "#94a7aa"
        self.rounded_rect(x, y, x + w, y + h, 7, fill=fill, outline=outline, width=2 if hovered else 1)
        box_fill = "#d3a11d" if value else "#121719"
        self.canvas.create_rectangle(x + 8, y + 7, x + 21, y + 20, fill=box_fill, outline="#6d7d80")
        if value:
            self.canvas.create_line(x + 10, y + 14, x + 14, y + 18, x + 20, y + 9, fill="#fff7c7", width=2)
        self.canvas.create_text(
            x + 30,
            y + h / 2,
            anchor="w",
            text=label,
            fill="#1c2528" if value else "#edf6f7",
            font=("Segoe UI", 8, "bold"),
        )
        self.add_region(action, (x, y, x + w, y + h), enabled=not self.running)

    def draw_start_dial(self, cx: float, cy: float, r: float) -> None:
        action = "stop" if self.running else "start"
        hovered = self.hover_action == action
        ring = "#e8f7f9" if hovered else "#b9d0d5"
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=ring, width=2)
        self.canvas.create_oval(cx - r + 12, cy - r + 12, cx + r - 12, cy + r - 12, outline="#9fb8bd", width=2)
        for i in range(16):
            angle = i * 22.5
            if i % 2 == 0:
                continue
            x0, y0 = self.point(cx, cy, r + 7, angle)
            x1, y1 = self.point(cx, cy, r + 16, angle)
            self.canvas.create_line(x0, y0, x1, y1, fill="#dceff2", width=2)
        if self.running:
            start = (self.pulse * 3) % 360
            self.canvas.create_arc(cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4, start=start, extent=82, style=tk.ARC, outline="#e4bd4e", width=5)
        else:
            self.canvas.create_polygon(cx - 14, cy - 22, cx - 14, cy + 22, cx + 24, cy, fill="#e3bc4d", outline="")
        label = "Stop" if self.running else "Start"
        self.canvas.create_text(
            cx,
            cy + r + self.layout.dial_label_gap,
            text=label,
            fill="#edf6f7",
            font=("Segoe UI", 10, "bold"),
        )
        self.add_region(action, (cx - r - 18, cy - r - 18, cx + r + 18, cy + r + self.layout.dial_label_gap + 18), enabled=True)

    def draw_log_panel(self, x: float, y: float, w: float, h: float) -> None:
        self.log_bounds = (x, y, x + w, y + h)
        self.rounded_rect(x, y, x + w, y + h, 6, fill="#1f282b", outline="#9fb0b4", width=1)
        wrapped = self.wrapped_log(w)
        self.log_line_span = float(len(wrapped))
        visible = self.log_visible_lines()
        maximum = self.log_max_scroll()
        if self.log_pinned:
            self.log_scroll_y = maximum
        else:
            self.log_scroll_y = self.clamp_log_scroll(self.log_scroll_y)

        issues = sum(1 for _, severity in self.log_lines if severity == "issue")
        heading = "Signal" if not issues else f"Signal - {issues} issue(s)"
        self.canvas.create_text(
            x + 14, y + 12, anchor="w", text=heading,
            fill="#edf6f7" if not issues else "#f0b7ae", font=("Segoe UI", 9, "bold"),
        )
        if maximum > 0:
            self.canvas.create_text(
                x + w - LOG_SCROLLBAR_WIDTH - 16, y + 12, anchor="e",
                text=f"{int(self.log_scroll_y) + int(visible)}/{len(wrapped)}",
                fill="#8fa3a7", font=("Consolas", 8),
            )

        first = int(self.log_scroll_y)
        y_line = y + 32
        for text, severity in wrapped[first : first + int(visible)]:
            self.canvas.create_text(
                x + 14,
                y_line,
                anchor="w",
                text=text,
                fill=LOG_COLOURS.get(severity, "#c8dadd"),
                font=("Consolas", 9),
            )
            y_line += LOG_LINE_HEIGHT
        self.draw_log_scrollbar(x + w - LOG_SCROLLBAR_WIDTH - 6, y + 26, h - 32, visible, len(wrapped))

    def draw_log_scrollbar(self, x: float, y: float, height: float, visible: float, total: int) -> None:
        width = LOG_SCROLLBAR_WIDTH
        maximum = self.log_max_scroll()
        self.canvas.create_rectangle(x, y, x + width, y + height, fill="#1a2426", outline="#829699")
        arrow = 14.0
        self.canvas.create_text(x + width / 2, y + 7, text="\u25b2", fill="#edf6f7", font=("Segoe UI", 6))
        self.canvas.create_text(x + width / 2, y + height - 7, text="\u25bc", fill="#edf6f7", font=("Segoe UI", 6))
        track_top = y + arrow
        track_bottom = y + height - arrow
        track = max(1.0, track_bottom - track_top)
        if maximum <= 0 or total <= 0:
            thumb_height, thumb_y = track, track_top
        else:
            thumb_height = max(18.0, track * visible / total)
            thumb_y = track_top + (track - thumb_height) * (self.log_scroll_y / maximum)
        self.canvas.create_rectangle(
            x + 2, thumb_y, x + width - 2, thumb_y + thumb_height,
            fill="#e3bc4d" if maximum > 0 else "#3b474a", outline="",
        )
        self.add_region("log:scroll_up", (x - 4, y, x + width + 4, track_top), enabled=maximum > 0)
        self.add_region("log:scroll_down", (x - 4, track_bottom, x + width + 4, y + height), enabled=maximum > 0)
        self.add_scroll_track(
            "log:scrollbar",
            (x, track_top, x + width, track_bottom),
            float(total),
            float(visible),
            lambda: self.log_scroll_y,
            lambda value: self.scroll_log_by(value - self.log_scroll_y),
            minimum_thumb=18.0,
        )

    def add_region(self, name: str, bounds: tuple[float, float, float, float], enabled: bool = True) -> None:
        if enabled:
            self.hit_regions.append(HitRegion(name, bounds, enabled))

    def add_scroll_track(
        self,
        name: str,
        bounds: tuple[float, float, float, float],
        content: float,
        view: float,
        read: Callable[[], float],
        apply: Callable[[float], None],
        minimum_thumb: float = 24.0,
    ) -> None:
        if content <= view:
            self.scroll_tracks.pop(name, None)
            return
        self.scroll_tracks[name] = ScrollTrack(bounds, float(content), float(view), minimum_thumb, read, apply)
        x0, y0, x1, y1 = bounds
        self.add_region(name, (x0 - 4, y0, x1 + 4, y1))

    def thumb_span(self, track: ScrollTrack) -> tuple[float, float]:
        x0, y0, x1, y1 = track.bounds
        length = max(1.0, y1 - y0)
        thumb = min(length, max(track.minimum_thumb, length * track.view / track.content))
        maximum = max(1.0, track.content - track.view)
        return y0 + (length - thumb) * max(0.0, min(1.0, track.read() / maximum)), thumb

    def drag_scroll_track(self, name: str, y: float, grab: float) -> None:
        track = self.scroll_tracks.get(name)
        if track is None:
            return
        top, thumb = self.thumb_span(track)
        room = max(1.0, track.bounds[3] - track.bounds[1] - thumb)
        fraction = max(0.0, min(1.0, (y - grab - track.bounds[1]) / room))
        track.apply(fraction * (track.content - track.view))

    def action_at(self, x: float, y: float) -> str:
        for region in reversed(self.hit_regions):
            x0, y0, x1, y1 = region.bounds
            if region.enabled and x0 <= x <= x1 and y0 <= y <= y1:
                return region.name
        return ""

    def is_options_action(self, action: str) -> bool:
        return action.startswith("toggle:")

    def tag_canvas_items(self, before_items: set[int], tag: str) -> None:
        for item in set(self.canvas.find_all()) - before_items:
            self.canvas.addtag_withtag(tag, item)

    def inside_bounds(self, x: float, y: float, bounds: tuple[float, float, float, float]) -> bool:
        x0, y0, x1, y1 = bounds
        return x0 <= x <= x1 and y0 <= y <= y1

    def rounded_rect(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        radius: float,
        fill: str,
        outline: str,
        width: int,
    ) -> None:
        radius = max(1.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
        points = [
            x0 + radius,
            y0,
            x1 - radius,
            y0,
            x1,
            y0,
            x1,
            y0 + radius,
            x1,
            y1 - radius,
            x1,
            y1,
            x1 - radius,
            y1,
            x0 + radius,
            y1,
            x0,
            y1,
            x0,
            y1 - radius,
            x0,
            y0 + radius,
            x0,
            y0,
        ]
        self.canvas.create_polygon(points, smooth=True, splinesteps=18, fill=fill, outline=outline, width=width)

    def middle_ellipsis(self, value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        if max_chars <= 8:
            return value[:max_chars]
        left = (max_chars - 3) // 2
        right = max_chars - 3 - left
        return f"{value[:left]}...{value[-right:]}"

    def point(self, cx: float, cy: float, radius: float, degrees: float) -> tuple[float, float]:
        import math

        radians = math.radians(degrees - 90)
        return cx + radius * math.cos(radians), cy + radius * math.sin(radians)

def main() -> int:
    root = tk.Tk()
    SolarScrollGui(root)
    root.mainloop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
