from __future__ import annotations
import os, queue, textwrap, threading
from pathlib import Path
from typing import Any
from tkinter import filedialog, messagebox, simpledialog
from .shader_compiler import (
    DEFAULT_TARGET,
    SHADER_TARGETS,
    ShaderCompilerError,
    compile_folder,
    compiler_available,
    discover_hlsl_sources,
    log_path_for,
)

class ShaderCompilerCanvas:
    PANEL_TAG = "shader_compiler_panel"
    NEW_PANEL_TAG = "shader_compiler_panel_new"

    def __init__(self, host: Any) -> None:
        self.host = host
        self.source_folder: str | None = None
        self.output_folder: str | None = None
        self.target = DEFAULT_TARGET
        self.entry_point: str | None = None
        self.status = "Ready for usage."
        self.scroll_y = 0.0
        self.content_height = 1.0
        self.bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.drag_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.viewport: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.compiling = False
        self.worker: threading.Thread | None = None
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.last_summary: list[str] = ["No shaders compiled yet."]
        self.last_log: str | None = None
        self.source_count = 0
        self.package_folder: str | None = None

    def activate(self) -> None:
        self.scroll_y = self.clamp_scroll(self.scroll_y)
        available, message = compiler_available()
        self.status = (
            "Shader compiler ready. " + message
            if available
            else "Shader compiler unavailable: " + message
        )

    def is_action(self, action: str) -> bool:
        return action.startswith("shader:")

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

    def handle_key(self, event: Any) -> bool:
        return False

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
                "Compiles HLSL to DXBC with d3dcompiler_47.dll, the same backend Unity uses.",
                "Each shader writes a .dxbc plus a .reflection.json naming every constant and",
                "its offset, the table a RWBY GE Shader object carries for its properties.",
            ],
        )

        folder_label = self.source_folder or "No folder selected"
        source_lines = [self.shorten(folder_label, 104)]
        if self.source_folder:
            source_lines.append(f"{self.source_count} HLSL files found (subfolders included)")
            source_lines.append(".hlsli, .inc and .cginc are treated as includes and skipped.")
        else:
            source_lines.append("Pick a folder of .hlsl files, every shader in it is compiled.")
        cursor = self.draw_card(
            content_left,
            cursor,
            content_right - content_left,
            140.0,
            "HLSL Source",
            source_lines,
            [
                ("shader:select_folder", "Select HLSL Folder"),
                ("shader:select_output", "Output Folder"),
                ("shader:clear", "Clear"),
            ],
        )

        entry_label = self.entry_point or "auto-detect"
        cursor = self.draw_card(
            content_left,
            cursor,
            content_right - content_left,
            152.0,
            "Compiler",
            [
                f"Profile: {self.target}    Entry point: {entry_label}",
                "A file named *.ps.hlsl builds as a pixel shader and *.vs.hlsl as a vertex",
                "shader whatever the profile says, the profile covers everything else.",
                "Auto-detect tries main, frag, vert, PSMain, VSMain and the other usual names.",
            ],
            [
                ("shader:cycle_target", f"Profile: {self.target}"),
                ("shader:set_entry", "Entry Point"),
                ("shader:compile", "Compiling…" if self.compiling else "Compile Shaders"),
            ],
        )

        package_label = self.package_folder or "No shader package opened"
        cursor = self.draw_card(
            content_left,
            cursor,
            content_right - content_left,
            152.0,
            "Shader Packages",
            [
                self.shorten(package_label, 104),
                "A .sshader is a zip. Open one to unpack it into a folder of plain files:",
                "script.shader for the ShaderLab text and code/ holding each program's",
                "bytecode. Drop a compiled .dxbc over one then Repack to build the .sshader.",
            ],
            [
                ("shader:open_package", "Open Package"),
                ("shader:repack_package", "Repack Package"),
                ("shader:open_folder", "Show Folder"),
            ],
        )

        result_lines = list(self.last_summary[:5])
        cursor = self.draw_card(
            content_left,
            cursor,
            content_right - content_left,
            116.0 + min(5, len(self.last_summary)) * 19.0,
            "Results",
            result_lines,
            [("shader:open_log", "Open Log")] if self.last_log else [],
        )

        self.content_height = max(1.0, cursor - content_top)
        self.scroll_y = self.clamp_scroll(self.scroll_y)
        self.draw_scrollbar(scroll_right, self.viewport[1], 12.0, self.viewport[3] - self.viewport[1])

        canvas.create_text(
            left, top, anchor="nw", text="Shader Compiler",
            fill="#edf6f7", font=("Segoe UI", 13, "bold"),
        )
        canvas.create_text(
            left, top + 24, anchor="nw",
            text="HLSL to DXBC for RWBY GE, no Unity editor required.",
            fill="#b9c8cb", font=("Segoe UI", 9, "bold"),
        )
        status_text = self.host.middle_ellipsis(self.status, max(36, int((right - left) / 7.2)))
        canvas.create_text(
            left, bottom - 14, anchor="w", text=status_text,
            fill="#edf6f7", font=("Consolas", 9),
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
        if action == "shader:scroll_up":
            self.scroll_by(-60.0)
            return
        if action == "shader:scroll_down":
            self.scroll_by(60.0)
            return
        if self.compiling:
            return
        if action == "shader:select_folder":
            self.select_source_folder()
        elif action == "shader:select_output":
            self.select_output_folder()
        elif action == "shader:clear":
            self.source_folder = None
            self.output_folder = None
            self.source_count = 0
            self.last_summary = ["No shaders compiled yet."]
            self.last_log = None
            self.status = "Cleared."
            self.redraw()
        elif action == "shader:cycle_target":
            index = SHADER_TARGETS.index(self.target) if self.target in SHADER_TARGETS else 0
            self.target = SHADER_TARGETS[(index + 1) % len(SHADER_TARGETS)]
            self.status = f"Default profile is now {self.target}."
            self.redraw()
        elif action == "shader:set_entry":
            self.set_entry_point()
        elif action == "shader:compile":
            self.start_compile()
        elif action == "shader:open_log":
            self.open_log()
        elif action == "shader:open_package":
            self.open_package()
        elif action == "shader:repack_package":
            self.repack_package()
        elif action == "shader:open_folder":
            self.show_package_folder()

    def open_package(self) -> None:
        from .shader_package import ShaderPackageError, extract_shader_package

        selected = filedialog.askopenfilename(
            title="Open a Sunflowyr shader package",
            initialdir=str(self.host.output_root),
            filetypes=[("Sunflowyr shader package", "*.sshader"), ("All files", "*.*")],
        )
        if not selected:
            return
        source = Path(selected)
        folder = Path(self.host.output_root) / "ShaderPackages" / source.stem
        try:
            result = extract_shader_package(source, folder)
        except ShaderPackageError as error:
            messagebox.showerror("Shader Compiler", str(error), parent=self.host.root)
            self.status = "Shader package could not be opened."
            self.redraw()
            return
        self.package_folder = result["folder"]
        programs = result["programCount"]
        self.status = f"Opened {source.name}: {result['members']} files, {programs} programs."
        self.last_summary = [
            f"Unpacked {source.name}",
            self.shorten("Folder: " + result["folder"], 100),
            f"{result['members']} files, {programs} compiled programs",
            "Edit script.shader, or replace a file under code/ with compiled bytecode.",
            "Then press Repack Package to rebuild the .sshader.",
        ]
        if not result["hasPrograms"]:
            self.last_summary.append("This package predates the editable code format.")
        self.redraw()

    def repack_package(self) -> None:
        from .shader_package import ShaderPackageError, build_shader_package

        initial = self.package_folder or str(self.host.output_root)
        selected = filedialog.askdirectory(
            title="Select an unpacked shader package folder", initialdir=initial
        )
        if not selected:
            return
        folder = Path(selected)
        destination = folder.with_name(folder.name + ".sshader")
        try:
            result = build_shader_package(folder, destination)
        except ShaderPackageError as error:
            messagebox.showerror("Shader Compiler", str(error), parent=self.host.root)
            self.status = "Shader package couldnt be rebuilt."
            self.redraw()
            return
        refreshed = result["tablesRefreshed"]
        self.package_folder = str(folder)
        self.status = f"Rebuilt {destination.name} ({result['byteLength']:,} bytes)."
        self.last_summary = [
            f"Rebuilt {destination.name}",
            self.shorten("Path: " + result["package"], 100),
            f"{result['members']} member(s), {result['byteLength']:,} bytes",
            (
                f"{refreshed} parameter tables regenerated from replaced bytecode"
                if refreshed
                else "No replaced bytecode found, parameter tables left as they were"
            ),
            "Stage this .sshader in the AssetBundle generator to ship it.",
        ]
        self.redraw()

    def show_package_folder(self) -> None:
        if not self.package_folder:
            messagebox.showinfo(
                "Shader Compiler",
                "Open a shader package first, its folder is shown here.",
                parent=self.host.root,
            )
            return
        folder = Path(self.package_folder)
        if not folder.is_dir():
            messagebox.showinfo(
                "Shader Compiler", "That folder no longer exists.", parent=self.host.root
            )
            return
        try:
            os.startfile(str(folder))
        except OSError as error:
            messagebox.showerror(
                "Shader Compiler",
                "The folder couldnt be opened:\n%s\n\nIt is at:\n%s" % (error, folder),
                parent=self.host.root,
            )

    def select_source_folder(self) -> None:
        initial = self.source_folder or str(self.host.output_root)
        selected = filedialog.askdirectory(
            title="Select a folder of HLSL shaders", initialdir=initial
        )
        if not selected:
            return
        self.source_folder = selected
        try:
            self.source_count = len(discover_hlsl_sources(Path(selected)))
        except OSError:
            self.source_count = 0
        if self.source_count == 0:
            self.status = "That folder holds no .hlsl files."
            messagebox.showwarning(
                "Shader Compiler",
                "No .hlsl files were found in:\n\n%s\n\nSubfolders are searched too. Files "
                "ending .hlsli, .inc or .cginc are treated as includes and skipped." % selected,
                parent=self.host.root,
            )
        else:
            self.status = f"{self.source_count} HLSL files ready to compile."
        self.redraw()

    def select_output_folder(self) -> None:
        initial = self.output_folder or self.source_folder or str(self.host.output_root)
        selected = filedialog.askdirectory(
            title="Select where compiled shaders should go", initialdir=initial
        )
        if not selected:
            return
        self.output_folder = selected
        self.status = "Output folder set."
        self.redraw()

    def set_entry_point(self) -> None:
        answer = simpledialog.askstring(
            "Shader Compiler",
            "Entry point function name.\nLeave blank to auto-detect (main, frag, vert, PSMain).",
            initialvalue=self.entry_point or "",
            parent=self.host.root,
        )
        if answer is None:
            return
        answer = answer.strip()
        self.entry_point = answer or None
        self.status = f"Entry point: {self.entry_point or 'auto-detect'}."
        self.redraw()

    def start_compile(self) -> None:
        if not self.source_folder:
            messagebox.showinfo(
                "Shader Compiler",
                "Select a folder of HLSL shaders first.",
                parent=self.host.root,
            )
            return
        available, message = compiler_available()
        if not available:
            messagebox.showerror("Shader Compiler", message, parent=self.host.root)
            self.status = "Shader compiler unavailable."
            self.redraw()
            return
        source = Path(self.source_folder)
        output = Path(self.output_folder) if self.output_folder else source / "compiled"
        self.compiling = True
        self.status = f"Compiling shaders in {source.name}"
        self.worker = threading.Thread(
            target=self.run_compile, args=(source, output, self.target, self.entry_point), daemon=True
        )
        self.worker.start()
        self.redraw()

    def run_compile(self, source: Path, output: Path, target: str, entry_point: str | None) -> None:
        try:
            result = compile_folder(source, output, target, entry_point)
            self.events.put(("done", result))
        except ShaderCompilerError as error:
            self.events.put(("error", str(error)))
        except Exception as error:
            self.events.put(("error", f"{type(error).__name__}: {error}"))

    def poll(self) -> bool:
        changed = False
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            changed = True
            self.compiling = False
            if kind == "error":
                self.status = "Shader compilation failed."
                self.last_summary = [str(payload)]
                messagebox.showerror("Shader Compiler", str(payload), parent=self.host.root)
                continue
            result = payload
            self.last_log = str(result.log_path) if result.log_path else None
            summary = [
                f"{len(result.compiled)} compiled, {len(result.failed)} failed "
                f"(of {result.total} file(s))",
                self.shorten("Output: " + str(result.output_folder), 100),
            ]
            for compiled in result.compiled[:3]:
                summary.append(
                    "• %s -> %s, %d bytes" % (compiled.source.name, compiled.target, compiled.byte_length)
                )
            for failed in result.failed[:3]:
                summary.append("• Failed " + failed.source.name)
            self.last_summary = summary
            if result.total == 0:
                self.status = "No .hlsl files were found in that folder."
                messagebox.showwarning(
                    "Shader Compiler",
                    "No .hlsl files were found in:\n\n%s" % result.folder,
                    parent=self.host.root,
                )
            elif result.failed:
                self.status = f"{len(result.compiled)} compiled, {len(result.failed)} failed."
                detail = "\n\n".join(
                    "%s:\n%s" % (item.source.name, item.messages.strip()[:600])
                    for item in result.failed[:4]
                )
                messagebox.showerror(
                    "Shader Compiler",
                    "%d shader(s) compiled, %d failed.\n\n%s\n\nFull log:\n%s"
                    % (len(result.compiled), len(result.failed), detail, result.log_path),
                    parent=self.host.root,
                )
            else:
                self.status = (
                    f"{len(result.compiled)} shaders compiled into {result.output_folder}."
                )
        return changed

    def open_log(self) -> None:
        if not self.last_log:
            return
        path = Path(self.last_log)
        if not path.is_file():
            messagebox.showinfo(
                "Shader Compiler", "The log file is no longer there.", parent=self.host.root
            )
            return
        try:
            os.startfile(str(path))
        except OSError as error:
            messagebox.showerror(
                "Shader Compiler",
                "The log couldnt be opened:\n%s\n\nIt is at:\n%s" % (error, path),
                parent=self.host.root,
            )

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
        view_top = self.viewport[1]
        view_bottom = self.viewport[3]
        card_top = max(y, view_top)
        card_bottom = min(y + height, view_bottom)
        if card_bottom - card_top >= 4.0:
            self.host.rounded_rect(
                x, card_top, x + width, card_bottom, 8,
                fill="#20292b", outline="#9fb0b4", width=1,
            )
        if self.is_y_visible(y + 14, y + 31):
            self.host.canvas.create_text(
                x + 14, y + 14, anchor="nw", text=title,
                fill="#edf6f7", font=("Segoe UI", 10, "bold"),
            )
        line_y = y + 43.0
        for value in lines:
            wrapped = textwrap.wrap(str(value), max(28, int((width - 30) / 7.1)), break_long_words=False)
            for line in wrapped[:3]:
                if self.is_y_visible(line_y, line_y + 16.0):
                    self.host.canvas.create_text(
                        x + 14, line_y, anchor="nw", text=line,
                        fill="#c8dadd", font=("Consolas", 9),
                    )
                line_y += 17.0
        button_y = y + height - 36.0
        button_x = x + 14.0
        for action, label in buttons or []:
            button_width = max(78.0, min(168.0, 18.0 + len(label) * 7.0))
            if self.is_y_visible(button_y, button_y + 26.0):
                hovered = self.host.hover_action == action
                self.host.rounded_rect(
                    button_x, button_y, button_x + button_width, button_y + 26.0, 7,
                    fill="#dfe8e8" if hovered else "#253033",
                    outline="#edf6f7" if hovered else "#94a7aa",
                    width=2 if hovered else 1,
                )
                self.host.canvas.create_text(
                    button_x + button_width / 2, button_y + 13.0, text=label,
                    fill="#1c2528" if hovered else "#edf6f7",
                    font=("Segoe UI", 8, "bold"),
                )
                enabled = not self.compiling or action.startswith("shader:scroll") or action.startswith("shader:page")
                self.host.add_region(
                    action,
                    (button_x, button_y, button_x + button_width, button_y + 26.0),
                    enabled=enabled,
                )
            button_x += button_width + 8.0
        return y + height + 12.0

    def draw_scrollbar(self, x: float, y: float, width: float, height: float) -> None:
        self.host.canvas.create_rectangle(x, y, x + width, y + height, fill="#1a2426", outline="#829699")
        arrow_height = 22.0
        self.host.canvas.create_text(x + width / 2, y + 10, text="▲", fill="#edf6f7", font=("Segoe UI", 7))
        self.host.canvas.create_text(
            x + width / 2, y + height - 10, text="▼", fill="#edf6f7", font=("Segoe UI", 7)
        )
        track_top = y + arrow_height
        track_bottom = y + height - arrow_height
        track_height = max(1.0, track_bottom - track_top)
        view_height = max(1.0, self.viewport[3] - self.viewport[1])
        maximum = max(0.0, self.content_height - view_height)
        thumb_height = track_height if maximum <= 0 else max(28.0, track_height * view_height / self.content_height)
        thumb_y = track_top if maximum <= 0 else track_top + (track_height - thumb_height) * self.scroll_y / maximum
        self.host.canvas.create_rectangle(
            x + 2, thumb_y, x + width - 2, thumb_y + thumb_height, fill="#e3bc4d", outline=""
        )
        self.host.add_region("shader:scroll_up", (x - 5, y, x + width + 5, y + arrow_height), enabled=maximum > 0)
        self.host.add_region(
            "shader:scroll_down",
            (x - 5, y + height - arrow_height, x + width + 5, y + height),
            enabled=maximum > 0,
        )
        self.host.add_scroll_track(
            "shader:scrollbar",
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
