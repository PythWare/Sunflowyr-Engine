from __future__ import annotations
import os, queue, subprocess, threading, textwrap
from pathlib import Path
from typing import Any
from tkinter import filedialog, messagebox, simpledialog
from .asset_bundle_inputs import SUPPORTED_EXTENSIONS
from .asset_bundle_prefab import write_character_template
from .navmesh_from_glb import looks_like_navmesh_glb
from .unpack_sources import convert_unpacked_material, material_needs_conversion

COMPANION_SUFFIXES = (".tiles.bin", ".pvs.bin", ".u8.bin", ".u16le.bin", ".f32le.bin", ".settings.json", "_collision.bin")

def is_companion_name(lowered_name: str) -> bool:
    return any(lowered_name.endswith(suffix) for suffix in COMPANION_SUFFIXES)


def supported_asset_filter() -> str:
    from .asset_bundle_inputs import SUPPORTED_EXTENSIONS

    return ";".join("*" + suffix for suffix in sorted(SUPPORTED_EXTENSIONS))

BUTTON_HEIGHT = 26.0
BUTTON_GAP = 8.0
BUTTON_ROW_HEIGHT = BUTTON_HEIGHT + 8.0

def card_button_width(label: str) -> float:
    return max(78.0, min(148.0, 18.0 + len(str(label)) * 7.0))

def card_button_rows(
    buttons: list[tuple[str, str]],
    width: float,
) -> list[list[tuple[str, str, float]]]:
    usable = max(card_button_width(""), float(width) - 28.0)
    rows: list[list[tuple[str, str, float]]] = []
    row: list[tuple[str, str, float]] = []
    used = 0.0
    for action, label in buttons:
        button_width = card_button_width(label)
        needed = button_width if not row else button_width + BUTTON_GAP
        if row and used + needed > usable:
            rows.append(row)
            row = []
            used = 0.0
            needed = button_width
        row.append((action, label, button_width))
        used += needed
    if row:
        rows.append(row)
    return rows

class AssetBundleGeneratorCanvas:
    PANEL_TAG = "asset_bundle_generator_panel"
    NEW_PANEL_TAG = "asset_bundle_generator_panel_new"

    def __init__(self, host: Any) -> None:
        self.host = host
        self.bundle_name = "Sunflowyr New Asset Bundle"
        self.input_paths: list[str] = []
        self.staged_version = 0
        self.staged_counts: tuple[int, int] = (0, 0)
        self.staged_counts_key: tuple[int, int] | None = None
        self.output_path: str | None = None
        self.manifest_path: str | None = None
        self.replace_paths: list[str] = []
        self.taildata_paths: list[str] = []
        self.scan_mono = False
        self.map_output_path: str | None = None
        self.clone_folder: str | None = None
        self.job_kind = "bundle"
        self.active_field: str | None = None
        self.status = "Ready for usage."
        self.scroll_y = 0.0
        self.content_height = 1.0
        self.bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.drag_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.viewport: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.building = False
        self.process: subprocess.Popen[str] | None = None
        self.worker: threading.Thread | None = None
        self.pending_conversions: list[str] = []
        self.conversion_folder: str | None = None
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()

    def activate(self) -> None:
        self.scroll_y = self.clamp_scroll(self.scroll_y)
        self.status = "AssetBundle generator ready."

    def is_action(self, action: str) -> bool:
        return action.startswith("bundle:")

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.drag_bounds
        return x0 <= x <= x1 and y0 <= y <= y1

    def scroll_from_delta(self, start_scroll: float, dy: float) -> None:
        self.scroll_y = self.clamp_scroll(float(start_scroll) - float(dy))

    def scroll_by(self, amount: float) -> None:
        next_value = self.clamp_scroll(self.scroll_y + amount)
        if next_value != self.scroll_y:
            self.scroll_y = next_value
            self.redraw()

    def clamp_scroll(self, value: float) -> float:
        view_height = max(1.0, self.viewport[3] - self.viewport[1])
        maximum = max(0.0, self.content_height - view_height)
        return max(0.0, min(float(value), maximum))

    def draw(
        self,
        left: float,
        right: float,
        top: float,
        bottom: float,
        tag: str = PANEL_TAG,
        drag_bounds: tuple[float, float, float, float] | None = None,
    ) -> None:
        canvas = self.host.canvas
        before_items = set(canvas.find_all())
        self.bounds = (left, top, right, bottom)
        self.drag_bounds = drag_bounds or self.bounds
        scroll_right = right - 20.0
        self.viewport = (left, top + 52.0, scroll_right - 8.0, bottom - 34.0)

        content_left = left
        content_right = scroll_right - 8.0
        content_top = self.viewport[1] - self.scroll_y
        cursor = content_top
        cursor = self.draw_card(
            content_left,
            cursor,
            content_right - content_left,
            106.0,
            "Explaining",
            [
                "One bundle, one .json sidecar. U can stage anything.",
                "Prefabs carry GameObject trees (characters, particles, props), other assets standalone.",
                "Output: Windows UnityFS, serialized format 15, Unity 5.4.1p4, uncompressed.",
            ],
        )
        cursor = self.draw_settings_card(content_left, cursor, content_right - content_left)
        staged_lines = [f"{len(self.input_paths)} new source assets staged"]
        for path_text in self.input_paths[:6]:
            staged_lines.append("• " + self.shorten(Path(path_text).name, 78))
        if len(self.input_paths) > 6:
            staged_lines.append(f"• and {len(self.input_paths) - 6} more")
        cursor = self.draw_card(
            content_left,
            cursor,
            content_right - content_left,
            116.0 + min(6, len(self.input_paths)) * 19.0,
            "New Asset Inputs",
            staged_lines,
            [
                ("bundle:add_files", "Add Files"),
                ("bundle:add_folder", "Add Folder"),
                ("bundle:clear_files", "Clear"),
            ],
        )
        output_label = self.output_path or "Choose the output when Build AssetBundle is clicked"
        cursor = self.draw_card(
            content_left,
            cursor,
            content_right - content_left,
            170.0,
            "Build and BepInEx info",
            [
                self.shorten(output_label, 104),
                "The .json sidecar holds: assets[] (LoadAsset path + class), registrations[]",
                "(definitions to add to game databases), rebinds[] (where to put an asset).",
                "One generic plugin can read that sidecar and handle any Sunflowyr bundle.",
            ],
            [
                ("bundle:build", "Building" if self.building else "Build AssetBundle"),
                ("bundle:verify", "Verify Bundle"),
            ],
        )
        cursor = self.draw_character_card(content_left, cursor, content_right - content_left)
        cursor = self.draw_replacement_map_card(content_left, cursor, content_right - content_left)
        self.content_height = max(1.0, cursor - content_top)
        self.scroll_y = self.clamp_scroll(self.scroll_y)
        self.draw_scrollbar(scroll_right, self.viewport[1], 12.0, self.viewport[3] - self.viewport[1])

        canvas.create_text(
            left,
            top,
            anchor="nw",
            text="AssetBundle Generator",
            fill="#edf6f7",
            font=("Segoe UI", 13, "bold"),
        )
        canvas.create_text(
            left,
            top + 24,
            anchor="nw",
            text="Standalone RWBY GE AssetBundle creation.",
            fill="#b9c8cb",
            font=("Segoe UI", 9, "bold"),
        )
        status_text = self.host.middle_ellipsis(self.status, max(36, int((right - left) / 7.2)))
        canvas.create_text(
            left,
            bottom - 14,
            anchor="w",
            text=status_text,
            fill="#edf6f7",
            font=("Consolas", 9),
        )
        self.host.tag_canvas_items(before_items, tag)

    def redraw(self) -> None:
        x0, y0, x1, y1 = self.bounds
        if x1 <= x0 or y1 <= y0:
            self.host.draw()
            return
        self.host.hit_regions = [
            region for region in self.host.hit_regions if not self.is_action(region.name)
        ]
        canvas = self.host.canvas
        canvas.delete(self.NEW_PANEL_TAG)
        self.draw(x0, x1, y0, y1, tag=self.NEW_PANEL_TAG, drag_bounds=self.drag_bounds)
        canvas.delete(self.PANEL_TAG)
        canvas.addtag_withtag(self.PANEL_TAG, self.NEW_PANEL_TAG)
        canvas.dtag(self.NEW_PANEL_TAG, self.NEW_PANEL_TAG)

    def perform_action(self, action: str) -> None:
        if action == "bundle:field:name":
            self.active_field = "name"
            self.status = "Editing bundle name. Enter accepts, Escape closes editing."
        elif action == "bundle:add_files" and not self.building:
            paths = filedialog.askopenfilenames(
                parent=self.host.root,
                title="Select new audio, font, GLB mesh, .sanim clip, prefab/definition/material/sprite/physics JSON, image, text, or data assets",
                filetypes=[
                    ("Supported Assets", supported_asset_filter()),
                    ("All Files", "*.*"),
                ],
            )
            added = self.append_unique(paths)
            self.status = f"Staged {added} new asset(s)."
        elif action == "bundle:add_folder" and not self.building:
            folder = filedialog.askdirectory(
                parent=self.host.root,
                title="Select folder containing new source assets",
            )
            if folder:
                files = [
                    str(path)
                    for path in Path(folder).rglob("*")
                    if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
                ]
                added = self.append_unique(files)
                self.status = f"Staged {added} new asset(s) from {Path(folder).name}."
        elif action == "bundle:clear_files" and not self.building:
            self.input_paths.clear()
            self.invalidate_staged_counts()
            self.status = "Cleared staged new-asset inputs."
        elif action == "bundle:build" and not self.building:
            self.start_build()
            return
        elif action == "bundle:verify" and not self.building:
            self.start_verify()
            return
        elif action == "bundle:clone_character" and not self.building:
            self.start_character_clone()
            return
        elif action == "bundle:character_template" and not self.building:
            self.write_character_template()
        elif action == "bundle:add_replaced" and not self.building:
            paths = filedialog.askopenfilenames(
                parent=self.host.root,
                title="Select the Sunflowyr-exported originals these new assets replace",
                filetypes=[("Sunflowyr unpacked files", "*.*")],
            )
            added = self.append_unique_paths(self.replace_paths, paths)
            self.status = (
                f"Staged {added} replaced original(s)." if added else "No new replaced originals were staged."
            )
        elif action == "bundle:add_taildata" and not self.building:
            paths = filedialog.askopenfilenames(
                parent=self.host.root,
                title="Select the Sunflowyr taildata catalog for those originals",
                filetypes=[
                    ("Sunflowyr Taildata", "sunflowyr_taildata.json"),
                    ("JSON", "*.json"),
                    ("All Files", "*.*"),
                ],
            )
            added = self.append_unique_paths(self.taildata_paths, paths)
            self.status = f"Staged {added} taildata catalog(s)." if added else "No new catalogs were staged."
        elif action == "bundle:toggle_mono" and not self.building:
            self.scan_mono = not self.scan_mono
            self.status = (
                "MonoBehaviour scan on: finds Sprite/UI script references, slower."
                if self.scan_mono
                else "MonoBehaviour scan off."
            )
        elif action == "bundle:build_map" and not self.building:
            self.start_map_build()
            return
        elif action == "bundle:clear_replaced" and not self.building:
            self.replace_paths.clear()
            self.taildata_paths.clear()
            self.status = "Cleared asset-map inputs."
        elif action == "bundle:scroll_up":
            self.scroll_by(-72.0)
            return
        elif action == "bundle:scroll_down":
            self.scroll_by(72.0)
            return
        self.redraw()

    def handle_key(self, event: Any) -> bool:
        if self.active_field != "name":
            return False
        key = getattr(event, "keysym", "")
        if key == "Escape":
            self.active_field = None
            self.status = "Bundle name editing closed."
            self.redraw()
            return True
        if key in {"Return", "KP_Enter"}:
            self.active_field = None
            self.status = "Bundle name accepted."
            self.redraw()
            return True
        if key == "BackSpace":
            self.bundle_name = self.bundle_name[:-1]
            self.redraw()
            return True
        char = getattr(event, "char", "")
        if char and char.isprintable() and len(self.bundle_name) < 80:
            self.bundle_name += char
            self.redraw()
            return True
        return False

    def start_build(self) -> None:
        if not self.input_paths:
            messagebox.showinfo(
                "AssetBundle Generator",
                "Stage at least one audio, font, mesh, animation, JSON, image or data file first.",
                parent=self.host.root,
            )
            return
        if not self.bundle_name.strip():
            messagebox.showinfo(
                "AssetBundle Generator",
                "Enter a bundle name first.",
                parent=self.host.root,
            )
            return
        output_folder = self.host.output_root / "AssetBundles"
        output_folder.mkdir(parents=True, exist_ok=True)
        output = filedialog.asksaveasfilename(
            parent=self.host.root,
            title="Build RWBY GE AssetBundle",
            initialdir=str(output_folder),
            initialfile=self.safe_output_name(self.bundle_name) + ".bundle",
            defaultextension=".bundle",
            filetypes=[("Unity AssetBundle", "*.bundle"), ("All Files", "*.*")],
        )
        if not output:
            return
        self.output_path = output
        self.manifest_path = output + ".json"
        command = [
            self.host.python_executable(),
            str(self.host.project_root / "solar_cli.py"),
            "bundle-build",
            "--name",
            self.bundle_name,
            "--out",
            output,
            "--data",
            str(self.host.data_root),
        ]
        for path_text in self.replace_paths:
            command.extend(["--rebind", path_text])
        for path_text in self.taildata_paths:
            command.extend(["--taildata", path_text])
        if self.replace_paths and self.scan_mono:
            command.append("--rebind-scan-mono")
        for path_text in self.input_paths:
            command.extend(["--input", path_text])
        self.building = True
        self.job_kind = "bundle"
        self.status = "Building AssetBundle"
        self.worker = threading.Thread(target=self.run_build_process, args=(command,), daemon=True)
        self.worker.start()
        self.redraw()

    def start_character_clone(self) -> None:
        character = simpledialog.askstring(
            "Clone Game Character",
            "Character to clone (i.e., Ruby, Weiss, Blake, Yang, Jaune).\n"
            "Leave blank to list every character root in the log.",
            parent=self.host.root,
        )
        if character is None:
            return
        character = character.strip()
        folder = self.host.output_root / "ClonedCharacters" / (self.safe_output_name(character) or "list")
        command = [
            self.host.python_executable(),
            str(self.host.project_root / "solar_cli.py"),
            "prefab-clone",
            "--data",
            str(self.host.data_root),
        ]
        if character:
            complete = messagebox.askyesnocancel(
                "Clone Game Character",
                f"Clone {character} complete?\n\n"
                "Yes: everything it uses, needed for a playable character.\n"
                "No: the character only, for a quick model test.",
                parent=self.host.root,
            )
            if complete is None:
                return
            command.extend(["--character", character, "--out", str(folder)])
            command.extend(["--follow-refs", "-1"] if complete else ["--minimal"])
            self.clone_folder = str(folder)
            scope = "complete" if complete else "character-only"
            self.status = f"Cloning {character} ({scope}) this can take several minutes."
        else:
            command.append("--list")
            self.clone_folder = None
            self.status = "Listing character roots"
        self.building = True
        self.job_kind = "clone"
        self.worker = threading.Thread(target=self.run_build_process, args=(command,), daemon=True)
        self.worker.start()
        self.redraw()

    def stage_cloned_character(self) -> None:
        if not self.clone_folder:
            return
        folder = Path(self.clone_folder)
        if not folder.is_dir():
            return
        files = [
            str(path)
            for path in sorted(folder.rglob("*"))
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        added = self.append_unique(files)
        self.status = f"Cloned character staged: {added} files from {folder.name}."
        messagebox.showinfo(
            "Clone Game Character",
            f"Cloned into:\n{folder}\n\nStaged {added} files, press Build AssetBundle next.\n"
            "Details are in sunflowyr_clone_report.log.",
            parent=self.host.root,
        )

    def write_character_template(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.host.root,
            title="Choose a folder for the new character template JSONs",
        )
        if not folder:
            return
        target = Path(folder)
        try:
            written = write_character_template(target)
        except OSError as exc:
            self.status = f"Couldnt write template: {exc}"
            messagebox.showerror("Character Template", self.status, parent=self.host.root)
            return
        added = self.append_unique([str(path) for path in written])
        self.status = f"Wrote {len(written)} template files, staged {added}."
        messagebox.showinfo(
            "Character Template",
            "Wrote and staged:\n"
            + "\n".join(f"• {path.name}" for path in written)
            + "\n\nEdit the IDs and names, then Build AssetBundle.",
            parent=self.host.root,
        )
        self.redraw()

    def start_verify(self) -> None:
        initial = self.output_path or str(self.host.output_root / "AssetBundles")
        chosen = filedialog.askopenfilename(
            parent=self.host.root,
            title="Verify a built AssetBundle",
            initialdir=str(Path(initial).parent if self.output_path else initial),
            initialfile=Path(initial).name if self.output_path else "",
            filetypes=[("Unity AssetBundle", "*.bundle"), ("All Files", "*.*")],
        )
        if not chosen:
            return
        command = [
            self.host.python_executable(),
            str(self.host.project_root / "solar_cli.py"),
            "bundle-inspect",
            chosen,
        ]
        self.building = True
        self.job_kind = "verify"
        self.status = f"Verifying {Path(chosen).name}"
        self.worker = threading.Thread(target=self.run_build_process, args=(command,), daemon=True)
        self.worker.start()
        self.redraw()

    def start_map_build(self) -> None:
        if not self.manifest_path or not Path(self.manifest_path).is_file():
            messagebox.showinfo(
                "Asset Map",
                "Build the AssetBundle first, the map needs its .json manifest.",
                parent=self.host.root,
            )
            return
        if not self.replace_paths:
            messagebox.showinfo(
                "Asset Map",
                "Stage the unpacked original files these new assets replace.",
                parent=self.host.root,
            )
            return
        default_name = Path(self.output_path).stem if self.output_path else "assetmap"
        output = filedialog.asksaveasfilename(
            parent=self.host.root,
            title="Save BepInEx Asset Map",
            initialdir=str(Path(self.manifest_path).parent),
            initialfile=f"{default_name}_assetmap.json",
            defaultextension=".json",
            filetypes=[("Asset Map JSON", "*.json"), ("All Files", "*.*")],
        )
        if not output:
            return
        self.map_output_path = output
        command = [
            self.host.python_executable(),
            str(self.host.project_root / "solar_cli.py"),
            "bundle-map",
            "--data",
            str(self.host.data_root),
            "--manifest",
            self.manifest_path,
            "--out",
            output,
        ]
        for path_text in self.replace_paths:
            command.extend(["--replace", path_text])
        for path_text in self.taildata_paths:
            command.extend(["--taildata", path_text])
        if self.scan_mono:
            command.append("--scan-mono")
        self.building = True
        self.job_kind = "map"
        self.status = "Scanning scene references"
        self.worker = threading.Thread(target=self.run_build_process, args=(command,), daemon=True)
        self.worker.start()
        self.redraw()

    def run_build_process(self, command: list[str]) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.host.project_root),
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
                    text = line.strip()
                    if text:
                        self.events.put(("line", text))
            self.events.put(("done", process.wait()))
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            self.process = None

    def poll(self) -> bool:
        changed = False
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            changed = True
            if kind == "line":
                self.status = str(payload)
            elif kind == "done":
                if self.job_kind == "clone":
                    self.building = False
                    if int(payload) == 0:
                        if self.clone_folder:
                            self.stage_cloned_character()
                        else:
                            self.status = "Character list printed above, run again with a name."
                    else:
                        self.status = f"Character clone failed: {self.status}"
                        messagebox.showerror("Clone Game Character", self.status, parent=self.host.root)
                    continue
                if self.job_kind == "convert":
                    self.building = False
                    if int(payload) == 0:
                        self.stage_converted_prefab()
                    else:
                        self.status = f"Prefab rebuild failed: {self.status}"
                        messagebox.showerror("AssetBundle Generator", self.status, parent=self.host.root)
                    self.start_next_conversion()
                    continue
                if self.job_kind == "verify":
                    self.building = False
                    if int(payload) == 0:
                        self.status = "Bundle verified: " + self.status
                        messagebox.showinfo(
                            "Verify Bundle",
                            "The bundle is well formed. The summary is in the log above.",
                            parent=self.host.root,
                        )
                    else:
                        self.status = f"Bundle verification failed: {self.status}"
                        messagebox.showerror("Verify Bundle", self.status, parent=self.host.root)
                    continue
                was_map = self.job_kind == "map"
                title = "Asset Map" if was_map else "AssetBundle Generator"
                self.building = False
                if int(payload) == 0:
                    if was_map:
                        self.status = f"Built asset map: {Path(self.map_output_path or '').name}"
                        messagebox.showinfo(
                            title,
                            "BepInEx asset map built:\n"
                            f"{self.map_output_path}\n\n"
                            "Ship it next to the .bundle or stage it as a bundle input.",
                            parent=self.host.root,
                        )
                    else:
                        self.status = f"Built AssetBundle: {Path(self.output_path or '').name}"
                        messagebox.showinfo(
                            title,
                            f"AssetBundle and BepInEx handoff manifest built:\n{self.output_path}",
                            parent=self.host.root,
                        )
                else:
                    detail = self.status
                    self.status = f"{'Asset map' if was_map else 'AssetBundle'} build failed: {detail}"
                    messagebox.showerror(title, self.status, parent=self.host.root)
            elif kind == "error":
                if self.job_kind == "clone":
                    self.building = False
                    self.status = f"Character clone failed: {payload}"
                    messagebox.showerror("Clone Game Character", self.status, parent=self.host.root)
                    continue
                if self.job_kind == "convert":
                    self.building = False
                    self.status = f"Prefab rebuild failed: {payload}"
                    messagebox.showerror("AssetBundle Generator", self.status, parent=self.host.root)
                    self.start_next_conversion()
                    continue
                if self.job_kind == "verify":
                    self.building = False
                    self.status = f"Bundle verification failed: {payload}"
                    messagebox.showerror("Verify Bundle", self.status, parent=self.host.root)
                    continue
                was_map = self.job_kind == "map"
                self.building = False
                self.status = f"{'Asset map' if was_map else 'AssetBundle'} build failed: {payload}"
                messagebox.showerror(
                    "Asset Map" if was_map else "AssetBundle Generator",
                    self.status,
                    parent=self.host.root,
                )
        return changed

    def append_unique_paths(self, target: list[str], paths: Any) -> int:
        added = 0
        existing = {str(Path(path).resolve()).casefold() for path in target}
        for path_text in paths:
            path = Path(path_text).resolve()
            key = str(path).casefold()
            if path.is_file() and key not in existing:
                target.append(str(path))
                existing.add(key)
                added += 1
        return added

    def append_unique(self, paths: Any) -> int:
        added = 0
        conversions: list[tuple[str, str]] = []
        existing = {str(Path(path).resolve()).casefold() for path in self.input_paths}
        for path_text in paths:
            path = Path(path_text).resolve()
            key = str(path).casefold()
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if key in existing:
                continue
            lowered_name = path.name.casefold()
            if lowered_name == "sunflowyr_taildata.json" or is_companion_name(lowered_name):
                continue
            if path.suffix.lower() == ".glb":
                if looks_like_navmesh_glb(path):
                    conversions.append((str(path), "navmesh"))
                    continue
                if looks_like_hierarchy_glb(path):
                    conversions.append((str(path), "prefab"))
                    continue
            if material_needs_conversion(path, self.host.data_root):
                added += self.stage_unpacked_material(path)
                existing.add(key)
                continue
            self.input_paths.append(str(path))
            existing.add(key)
            added += 1
            self.invalidate_staged_counts()
        if conversions:
            self.queue_conversions(conversions)
        return added

    def stage_unpacked_material(self, path: Path) -> int:
        folder = self.host.output_root / "MaterialFromUnpack" / safe_folder_name(path.stem)
        try:
            result = convert_unpacked_material(path, folder)
        except Exception as exc:
            self.status = f"Couldnt rebuild {path.name} as a new material: {exc}"
            return 0
        added = self.append_unique([str(item) for item in result["files"]])
        self.status = f"Rebuilt {path.name} as a new material, staged {added} file(s) with its textures and shader."
        return added

    def queue_conversions(self, jobs: list[tuple[str, str]]) -> None:
        self.pending_conversions.extend(jobs)
        if not self.building:
            self.start_next_conversion()

    def start_next_conversion(self) -> None:
        if not self.pending_conversions or self.building:
            return
        source_text, kind = self.pending_conversions.pop(0)
        source = Path(source_text)
        if kind == "navmesh":
            folder = self.host.output_root / "NavMeshFromGlb" / safe_folder_name(source.stem)
            verb = "navmesh-from-glb"
            message = f"Rebuilding {source.name} into Detour tiles"
        else:
            folder = self.host.output_root / "PrefabFromGlb" / safe_folder_name(source.stem)
            verb = "prefab-from-glb"
            message = f"Rebuilding {source.name} into a stageable prefab"
        self.conversion_folder = str(folder)
        command = [
            self.host.python_executable(),
            str(self.host.project_root / "solar_cli.py"),
            verb,
            str(source),
            "--out",
            str(folder),
            "--data",
            str(self.host.data_root),
        ]
        self.building = True
        self.job_kind = "convert"
        self.status = message
        self.worker = threading.Thread(target=self.run_build_process, args=(command,), daemon=True)
        self.worker.start()
        self.redraw()

    def stage_converted_prefab(self) -> None:
        folder = Path(self.conversion_folder or "")
        was_navmesh = folder.parent.name == "NavMeshFromGlb"
        if not folder.is_dir():
            self.status = (
                "NavMesh rebuild produced no folder."
                if was_navmesh
                else "Prefab rebuild produced no folder."
            )
            return
        files = [
            str(item)
            for item in sorted(folder.rglob("*"))
            if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        added = self.append_unique(files)
        tail = "its rebuilt NavMeshData" if was_navmesh else "its prefab"
        self.status = f"Rebuilt {folder.name}: staged {added} asset(s) including {tail}."

    def staged_kind_counts(self) -> tuple[int, int]:
        signature = (self.staged_version, len(self.input_paths))
        if self.staged_counts_key == signature:
            return self.staged_counts
        prefabs = 0
        definitions = 0
        for path_text in self.input_paths:
            lowered = path_text.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
            if lowered.endswith(".prefab.json"):
                prefabs += 1
            elif lowered.endswith(".mono.json"):
                definitions += 1
        self.staged_counts = (prefabs, definitions)
        self.staged_counts_key = signature
        return self.staged_counts

    def invalidate_staged_counts(self) -> None:
        self.staged_version += 1

    def draw_character_card(self, x: float, y: float, width: float) -> float:
        prefabs, definitions = self.staged_kind_counts()
        lines = [
            "Optional authoring helper. A prefab JSON becomes a GameObject tree holding any",
            "RWBY GE component: renderers, particles, colliders, AudioSource, MonoBehaviour.",
            f"Staged: {prefabs} prefab JSON, {definitions} definition JSON",
            "skeletonFromGlb rebuilds a bone hierarchy from an exported GLB. bonesFromGlb",
            "wires the SkinnedMeshRenderer to it.",
            "Clone Game Character exports a whole game character which is hierarchy, bones, skinned",
            "mesh, materials, textures, scripts and stages it.",
        ]
        return self.draw_card(
            x,
            y,
            width,
            202.0,
            "Prefabs & Characters",
            lines,
            [
                ("bundle:clone_character", "Clone Game Character"),
                ("bundle:character_template", "Character Template"),
            ],
        )

    def draw_replacement_map_card(self, x: float, y: float, width: float) -> float:
        if self.manifest_path:
            manifest_label = Path(self.manifest_path).name
        else:
            manifest_label = "build an AssetBundle first"
        lines = [
            "Optional. Only needed when a staged asset should take over from one the game",
            "already uses. Stage the exported original, the sidecar then records where that",
            "reference lives so the plugin can repoint it. Game files are never modified.",
            f"Replaced originals: {len(self.replace_paths)} staged|taildata: {len(self.taildata_paths)}",
            f"MonoBehaviour scan: {'on (finds Sprite/UI fields, slower)' if self.scan_mono else 'off'}",
            f"Last manifest: {self.shorten(manifest_label, 74)}",
        ]
        for path_text in self.replace_paths[:4]:
            lines.append("• " + self.shorten(Path(path_text).name, 78))
        if len(self.replace_paths) > 4:
            lines.append(f"• and {len(self.replace_paths) - 4} more")
        height = 210.0 + min(5, len(self.replace_paths)) * 19.0
        return self.draw_card(
            x,
            y,
            width,
            height,
            "Rebind Map (BepInEx)",
            lines,
            [
                ("bundle:add_replaced", "Add Originals"),
                ("bundle:add_taildata", "Taildata"),
                ("bundle:toggle_mono", "Mono Scan"),
                ("bundle:build_map", "Build Map"),
                ("bundle:clear_replaced", "Clear"),
            ],
        )

    def draw_settings_card(self, x: float, y: float, width: float) -> float:
        height = 104.0
        self.draw_card(x, y, width, height, "Bundle Identity", [])
        entry_y = y + 42.0
        if self.is_y_visible(entry_y, entry_y + 42.0):
            active = self.active_field == "name"
            self.host.rounded_rect(
                x + 14,
                entry_y,
                x + width - 14,
                entry_y + 38,
                6,
                fill="#263235" if active else "#1f282b",
                outline="#edf6f7" if active else "#83989c",
                width=2 if active else 1,
            )
            self.host.canvas.create_text(
                x + 22,
                entry_y + 7,
                anchor="w",
                text="Bundle Name",
                fill="#e3bc4d",
                font=("Segoe UI", 8, "bold"),
            )
            value = self.host.middle_ellipsis(self.bundle_name or " ", max(20, int((width - 48) / 7.1)))
            self.host.canvas.create_text(
                x + 22,
                entry_y + 25,
                anchor="w",
                text=value,
                fill="#edf6f7",
                font=("Consolas", 9),
            )
            self.host.add_region(
                "bundle:field:name",
                (x + 14, entry_y, x + width - 14, entry_y + 38),
                enabled=not self.building,
            )
        return y + height + 12.0

    def draw_card(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        title: str,
        lines: list[str],
        buttons: list[tuple[str, str]] | None = None,
    ) -> float:
        rows = card_button_rows(buttons or [], width)
        height = height + max(0, len(rows) - 1) * BUTTON_ROW_HEIGHT
        view_top = self.viewport[1]
        view_bottom = self.viewport[3]
        card_top = max(y, view_top)
        card_bottom = min(y + height, view_bottom)
        if card_bottom - card_top >= 4.0:
            self.host.rounded_rect(
                x,
                card_top,
                x + width,
                card_bottom,
                8,
                fill="#20292b",
                outline="#9fb0b4",
                width=1,
            )
        if self.is_y_visible(y + 14, y + 31):
            self.host.canvas.create_text(
                x + 14,
                y + 14,
                anchor="nw",
                text=title,
                fill="#edf6f7",
                font=("Segoe UI", 10, "bold"),
            )
        line_y = y + 43.0
        for value in lines:
            wrapped = textwrap.wrap(str(value), max(28, int((width - 30) / 7.1)), break_long_words=False)
            for line in wrapped[:3]:
                if self.is_y_visible(line_y, line_y + 16.0):
                    self.host.canvas.create_text(
                        x + 14,
                        line_y,
                        anchor="nw",
                        text=line,
                        fill="#c8dadd",
                        font=("Consolas", 9),
                    )
                line_y += 17.0
        button_y = y + height - 36.0 - max(0, len(rows) - 1) * BUTTON_ROW_HEIGHT
        for row in rows:
            button_x = x + 14.0
            for action, label, button_width in row:
                self.draw_card_button(action, label, button_x, button_y, button_width)
                button_x += button_width + BUTTON_GAP
            button_y += BUTTON_ROW_HEIGHT
        return y + height + 12.0

    def draw_card_button(
        self,
        action: str,
        label: str,
        button_x: float,
        button_y: float,
        button_width: float,
    ) -> None:
        if not self.is_y_visible(button_y, button_y + BUTTON_HEIGHT):
            return
        hovered = self.host.hover_action == action
        self.host.rounded_rect(
            button_x,
            button_y,
            button_x + button_width,
            button_y + BUTTON_HEIGHT,
            7,
            fill="#dfe8e8" if hovered else "#253033",
            outline="#edf6f7" if hovered else "#94a7aa",
            width=2 if hovered else 1,
        )
        self.host.canvas.create_text(
            button_x + button_width / 2,
            button_y + BUTTON_HEIGHT / 2,
            text=label,
            fill="#1c2528" if hovered else "#edf6f7",
            font=("Segoe UI", 8, "bold"),
        )
        enabled = not self.building or action.startswith("bundle:scroll") or action.startswith("bundle:page")
        self.host.add_region(
            action,
            (button_x, button_y, button_x + button_width, button_y + BUTTON_HEIGHT),
            enabled=enabled,
        )

    def draw_scrollbar(self, x: float, y: float, width: float, height: float) -> None:
        self.host.canvas.create_rectangle(x, y, x + width, y + height, fill="#1a2426", outline="#829699")
        arrow_height = 22.0
        self.host.canvas.create_text(x + width / 2, y + 10, text="▲", fill="#edf6f7", font=("Segoe UI", 7))
        self.host.canvas.create_text(
            x + width / 2,
            y + height - 10,
            text="▼",
            fill="#edf6f7",
            font=("Segoe UI", 7),
        )
        track_top = y + arrow_height
        track_bottom = y + height - arrow_height
        track_height = max(1.0, track_bottom - track_top)
        view_height = max(1.0, self.viewport[3] - self.viewport[1])
        maximum = max(0.0, self.content_height - view_height)
        thumb_height = track_height if maximum <= 0 else max(28.0, track_height * view_height / self.content_height)
        thumb_y = track_top if maximum <= 0 else track_top + (track_height - thumb_height) * self.scroll_y / maximum
        self.host.canvas.create_rectangle(
            x + 2,
            thumb_y,
            x + width - 2,
            thumb_y + thumb_height,
            fill="#e3bc4d",
            outline="",
        )
        self.host.add_region("bundle:scroll_up", (x - 5, y, x + width + 5, y + arrow_height), enabled=maximum > 0)
        self.host.add_region(
            "bundle:scroll_down",
            (x - 5, y + height - arrow_height, x + width + 5, y + height),
            enabled=maximum > 0,
        )
        self.host.add_scroll_track(
            "bundle:scrollbar",
            (x, track_top, x + width, track_bottom),
            self.content_height,
            view_height,
            lambda: self.scroll_y,
            self.scroll_to,
            minimum_thumb=28.0,
        )

    def scroll_to(self, value: float) -> None:
        next_value = self.clamp_scroll(value)
        if next_value != self.scroll_y:
            self.scroll_y = next_value
            self.redraw()

    def is_y_visible(self, top: float, bottom: float) -> bool:
        return top >= self.viewport[1] and bottom <= self.viewport[3]

    def shorten(self, value: str, maximum: int) -> str:
        if len(value) <= maximum:
            return value
        left = max(1, (maximum - 3) // 2)
        right = max(1, maximum - 3 - left)
        return value[:left] + "..." + value[-right:]

    def safe_output_name(self, value: str) -> str:
        cleaned = "".join(char if char.isalnum() or char in " .-()" else "-" for char in value)
        return cleaned.strip(" .-") or "Sunflowyr New Asset Bundle"

def safe_folder_name(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in " -_." else "_" for c in str(value or "")).strip(" .")
    return cleaned[:80] or "Prefab"

def looks_like_hierarchy_glb(path: Path) -> bool:
    import struct

    try:
        with Path(path).open("rb") as handle:
            header = handle.read(12)
            if len(header) < 12 or struct.unpack_from("<I", header, 0)[0] != 0x46546C67:
                return False
            length, kind = struct.unpack("<II", handle.read(8))
            if kind != 0x4E4F534A:
                return False
            chunk = handle.read(min(length, 4_000_000))
    except (OSError, struct.error):
        return False
    if b"unity_animation_clip_path_id" in chunk:
        return False
    return b"unity_transform_path_id" in chunk or b"unity_game_object_path_id" in chunk
