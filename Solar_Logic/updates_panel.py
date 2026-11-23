from __future__ import annotations
import os, re, threading
from functools import lru_cache
from pathlib import Path
from typing import Any
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

SCALE = 2
HERE = Path(__file__).resolve().parent
NOTES_PATH = HERE / "patch_notes.txt"
ICON_PATH = HERE / "icon" / "icon.png"
FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
RELEASE_PATTERN = re.compile(r"^\[(.+?)\]\s*(.*)$")
CLEAR = (0, 0, 0, 0)
GRADIENT = ((126, 230, 243), (94, 210, 231), (78, 198, 223), (46, 164, 196))
PANEL_BLUE = (86, 204, 228)
HEADER_COLOUR = (191, 152, 217, 255)
TEXT_SHADOW = (10, 84, 104, 150)
PANEL_PAD = 26.0
BAR_HEIGHT = 74.0
HEADER_HEIGHT = 58.0
FEED_MARGIN = 10.0
TAG_HEIGHT = 40.0
BUBBLE_GAP = 22.0
FADE = 16
ICON_NAMES = ("home", "globe", "search", "mail")
BAR_ACTIONS = {"home": "show_home", "globe": "show_loose", "search": "show_finder", "mail": "show_updates"}

def load_font(name: str, size: float) -> ImageFont.FreeTypeFont:
    return cached_font(name, int(size * SCALE), threading.get_ident())

@lru_cache(maxsize=256)
def cached_font(name: str, pixels: int, thread_id: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_DIR / name), pixels)
    except OSError:
        return ImageFont.load_default(pixels)

def read_patch_notes(path: Path = NOTES_PATH) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    if not path.exists():
        return [{"version": "SE", "label": "Now", "lines": [f"No patch notes yet. Add them to {path.name}."]}]
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("#"):
            continue
        match = RELEASE_PATTERN.match(line.strip())
        if match:
            releases.append({"version": match.group(1).strip(), "label": match.group(2).strip(), "lines": []})
        elif releases:
            releases[-1]["lines"].append(line)
    return releases or [{"version": "SE", "label": "Now", "lines": [f"{path.name} has no [version] entries yet."]}]

def wrap_words(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and font.getlength(trial) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]

def layout_note(lines: list[str], font: ImageFont.FreeTypeFont, max_width: float, line_height: float) -> tuple[list[tuple[str, float, float, bool]], float]:
    placed: list[tuple[str, float, float, bool]] = []
    y = 0.0
    pending = 0.0
    for raw in lines:
        text = raw.strip()
        if not text:
            if placed:
                pending = line_height * 0.5
            continue
        y += pending
        pending = 0.0
        bullet = text.startswith(("- ", "* "))
        indent = line_height * 0.9 if bullet else 0.0
        body = text[2:].strip() if bullet else text
        for index, part in enumerate(wrap_words(body, font, max_width - indent)):
            placed.append((part, indent, y, bullet and index == 0))
            y += line_height
    return placed, max(y, line_height)

def glow(layer: Image.Image, radius: float, strength: int = 1, scale: int = SCALE) -> Image.Image:
    result = Image.new("RGBA", layer.size, CLEAR)
    region_box = padded_bbox(layer, int(radius * scale * 3))
    if region_box is None:
        return result
    alpha = layer.getchannel("A").crop(region_box)
    factor = 4
    small = alpha.reduce(factor).filter(ImageFilter.GaussianBlur(radius * scale / factor))
    blurred = small.resize(alpha.size, Image.BILINEAR)
    if strength > 1:
        blurred = blurred.point(lambda value: 255 - (255 - value) ** strength // 255 ** (strength - 1))
    piece = Image.new("RGBA", alpha.size, (255, 255, 255, 0))
    piece.putalpha(blurred)
    result.paste(piece, region_box[:2])
    return result

def padded_bbox(layer: Image.Image, reach: int) -> tuple[int, int, int, int] | None:
    box = layer.getchannel("A").getbbox()
    if box is None:
        return None
    x0, y0, x1, y1 = box
    return (max(0, x0 - reach), max(0, y0 - reach), min(layer.width, x1 + reach), min(layer.height, y1 + reach))

def gradient_fill(size: tuple[int, int]) -> Image.Image:
    vertical = Image.linear_gradient("L").resize(size)
    horizontal = Image.linear_gradient("L").transpose(Image.Transpose.TRANSPOSE).resize(size)
    top_left, top_right, bottom_left, bottom_right = (Image.new("RGBA", size, colour + (255,)) for colour in GRADIENT)
    top = Image.composite(top_right, top_left, horizontal)
    bottom = Image.composite(bottom_right, bottom_left, horizontal)
    return Image.composite(bottom, top, vertical)

def draw_icon(draw: ImageDraw.ImageDraw, name: str, cx: float, cy: float, size: float, colour: tuple[int, int, int, int]) -> None:
    k = size / 28.0
    x = cx - 14.0 * k
    y = cy - 14.0 * k
    w = max(2, int(2.6 * k))

    def p(px: float, py: float) -> tuple[float, float]:
        return (x + px * k, y + py * k)

    if name == "home":
        draw.line([p(0, 14), p(14, 0), p(28, 14)], fill=colour, width=w, joint="curve")
        draw.rectangle((*p(5, 14), *p(23, 28)), outline=colour, width=w)
        draw.rectangle((*p(11.5, 19), *p(16.5, 28)), fill=colour)
    elif name == "globe":
        draw.ellipse((*p(0, 0), *p(28, 28)), outline=colour, width=w)
        draw.ellipse((*p(8, 1), *p(20, 27)), outline=colour, width=max(1, w // 2))
        draw.line([p(1, 14), p(27, 14)], fill=colour, width=max(1, w // 2))
        draw.line([p(4, 7), p(24, 7)], fill=colour, width=max(1, w // 2))
        draw.line([p(4, 21), p(24, 21)], fill=colour, width=max(1, w // 2))
    elif name == "search":
        draw.ellipse((*p(0, 1), *p(20, 21)), outline=colour, width=w)
        draw.line([p(17, 18), p(28, 29)], fill=colour, width=int(w * 1.5))
    elif name == "mail":
        draw.rectangle((*p(-1, 4), *p(29, 25)), outline=colour, width=w)
        draw.line([p(-1, 4), p(14, 16), p(29, 4)], fill=colour, width=w, joint="curve")

def hovered_icon(action: str) -> str:
    for name, bar_action in BAR_ACTIONS.items():
        if bar_action == action:
            return name
    return ""

def bar_geometry(width: int, height: int) -> tuple[tuple[float, float, float, float], list[tuple[float, float]]]:
    pad = PANEL_PAD
    bar = (pad, height - pad - BAR_HEIGHT, width - pad, height - pad)
    bar_w = bar[2] - bar[0]
    icons = [(bar[0] + bar_w * (index + 0.5) / len(ICON_NAMES), (bar[1] + bar[3]) / 2.0) for index in range(len(ICON_NAMES))]
    return bar, icons

def add_bar_regions(host: Any, origin: tuple[float, float], icons: list[tuple[float, float]], active: str) -> None:
    x0, y0 = origin
    for name, (cx, cy) in zip(ICON_NAMES, icons):
        action = BAR_ACTIONS.get(name)
        if action and name != active:
            host.add_region(action, (x0 + cx - 32, y0 + cy - 28, x0 + cx + 32, y0 + cy + 28), enabled=name == "home" or not host.running)

def render_icon_bar(width: int, height: int, bar: tuple[float, float, float, float], icons: list[tuple[float, float]], active: str, hover: str) -> Image.Image:
    s = SCALE
    size = (width * s, height * s)
    shapes = Image.new("RGBA", size, CLEAR)
    draw = ImageDraw.Draw(shapes)
    bx0, by0, bx1, by1 = (value * s for value in bar)
    draw.rounded_rectangle((bx0, by0, bx1, by1), 14 * s, fill=(255, 255, 255, 46), outline=(255, 255, 255, 170), width=2 * s)
    boxes = Image.new("RGBA", size, CLEAR)
    box_draw = ImageDraw.Draw(boxes)
    icon_layer = Image.new("RGBA", size, CLEAR)
    icon_draw = ImageDraw.Draw(icon_layer)
    for name, (cx, cy) in zip(ICON_NAMES, icons):
        cx *= s
        cy *= s
        if name == active or name == hover:
            fill = (255, 255, 255, 70) if name == active else (255, 255, 255, 40)
            box_draw.rounded_rectangle((cx - 34 * s, cy - 27 * s, cx + 34 * s, cy + 27 * s), 10 * s, fill=fill, outline=(255, 255, 255, 235), width=2 * s)
        draw_icon(icon_draw, name, cx, cy, 30 * s, (255, 255, 255, 255))
    shapes.alpha_composite(glow(shapes, 6))
    shapes.alpha_composite(glow(boxes, 7, 2))
    shapes.alpha_composite(boxes)
    shapes.alpha_composite(glow(icon_layer, 4))
    shapes.alpha_composite(icon_layer)
    return shapes.resize((width, height), Image.LANCZOS)

def text_shadow(text: Image.Image, scale: int = SCALE) -> Image.Image:
    result = Image.new("RGBA", text.size, CLEAR)
    region_box = padded_bbox(text, 8 * scale)
    if region_box is None:
        return result
    region = text.crop(region_box)
    shadow = Image.new("RGBA", region.size, TEXT_SHADOW)
    shadow.putalpha(region.getchannel("A").point(lambda value: value * TEXT_SHADOW[3] // 255))
    offset = Image.new("RGBA", region.size, CLEAR)
    offset.alpha_composite(shadow, (scale, 2 * scale))
    result.paste(offset.filter(ImageFilter.GaussianBlur(1.6 * scale)), region_box[:2])
    return result

def panel_frame(width: int, height: int) -> Image.Image:
    s = SCALE
    size = (width * s, height * s)
    frame = Image.new("RGBA", size, CLEAR)
    ImageDraw.Draw(frame).rounded_rectangle((5 * s, 5 * s, size[0] - 5 * s, size[1] - 5 * s), 10 * s, outline=(255, 255, 255, 150), width=2 * s)
    image = glow(frame, 5, 2)
    image.alpha_composite(frame)
    return image

class UpdatesPanel:
    def __init__(self, host: Any) -> None:
        self.host = host
        self.scroll_y = 0.0
        self.content_height = 1.0
        self.bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.drag_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.viewport: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.geometry: dict[str, Any] = {}
        self.releases: list[dict[str, Any]] = []
        self.notes_stamp: float | None = None
        self.base_cache: dict[tuple[Any, ...], Image.Image] = {}
        self.background_key: tuple[int, int] | None = None
        self.background: Image.Image | None = None
        self.feed_key: tuple[Any, ...] | None = None
        self.feed: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.item: int | None = None

    def activate(self) -> None:
        self.reload_notes()
        self.scroll_y = 0.0

    def reload_notes(self) -> None:
        stamp = NOTES_PATH.stat().st_mtime if NOTES_PATH.exists() else None
        if stamp != self.notes_stamp or not self.releases:
            self.releases = read_patch_notes()
            self.notes_stamp = stamp
            self.feed_key = None

    def is_action(self, action: str) -> bool:
        return action.startswith("updates:")

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.viewport
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
        return max(0.0, min(float(value), max(0.0, self.content_height - view_height)))

    def handle_key(self, event: Any) -> bool:
        if event.keysym in ("Up", "Down", "Prior", "Next"):
            step = 48.0 if event.keysym in ("Up", "Down") else self.viewport[3] - self.viewport[1]
            self.scroll_by(-step if event.keysym in ("Up", "Prior") else step)
            return True
        return False

    def perform_action(self, action: str) -> None:
        pass

    def poll(self) -> bool:
        return False

    def compute_geometry(self, width: int, height: int) -> dict[str, Any]:
        pad = PANEL_PAD
        bar, icons = bar_geometry(width, height)
        content_top = pad
        content_bottom = bar[1] - 18.0
        available = content_bottom - content_top
        diameter = max(96.0, min(250.0, width * 0.24, available * 0.8))
        portrait = (pad + 26.0 + diameter / 2.0, content_top + available / 2.0, diameter)
        column = (portrait[0] + diameter / 2.0 + 44.0, width - pad - 18.0)
        view = (column[0], content_top + HEADER_HEIGHT, column[1], content_bottom)
        return {"bar": bar, "portrait": portrait, "column": column, "view": view, "icons": icons, "header_y": content_top}

    def warm(self, hole: tuple[float, float, float, float]) -> None:
        x0, y0, x1, y1 = (int(round(value)) for value in hole)
        width, height = max(1, x1 - x0), max(1, y1 - y0)
        if self.background_key == (width, height):
            return
        self.geometry = self.compute_geometry(width, height)
        self.reload_notes()
        threading.Thread(target=self.render_cache, args=(width, height), daemon=True).start()

    def render_cache(self, width: int, height: int) -> None:
        self.base(width, height, "")
        self.feed_image()

    def draw(self, hole: tuple[float, float, float, float], below_item: int | None) -> None:
        x0, y0, x1, y1 = (int(round(value)) for value in hole)
        width, height = max(1, x1 - x0), max(1, y1 - y0)
        self.bounds = (x0, y0, x1, y1)
        self.geometry = self.compute_geometry(width, height)
        vx0, vy0, vx1, vy1 = self.geometry["view"]
        self.viewport = (x0 + vx0, y0 + vy0, x0 + vx1, y0 + vy1)
        self.drag_bounds = self.viewport
        self.reload_notes()
        image = self.compose(width, height)
        self.photo = ImageTk.PhotoImage(image, master=self.host.root)
        self.item = self.host.canvas.create_image(x0, y0, image=self.photo, anchor="nw")
        if below_item is not None:
            self.host.canvas.tag_lower(self.item, below_item)
        add_bar_regions(self.host, (x0, y0), self.geometry["icons"], "mail")
        track_x = x0 + int(self.geometry["column"][1]) + 10
        self.host.add_scroll_track(
            "updates:scrollbar",
            (track_x - 1, y0 + vy0, track_x + 6, y0 + vy1),
            self.content_height,
            vy1 - vy0,
            lambda: self.scroll_y,
            self.scroll_to,
        )

    def scroll_to(self, value: float) -> None:
        next_value = self.clamp_scroll(value)
        if next_value != self.scroll_y:
            self.scroll_y = next_value
            self.redraw()

    def redraw(self) -> None:
        if self.item is None or not self.geometry:
            self.host.draw()
            return
        x0, y0, x1, y1 = self.bounds
        image = self.compose(int(x1 - x0), int(y1 - y0))
        self.photo = ImageTk.PhotoImage(image, master=self.host.root)
        self.host.canvas.itemconfigure(self.item, image=self.photo)

    def compose(self, width: int, height: int) -> Image.Image:
        image = self.base(width, height, hovered_icon(self.host.hover_action)).copy()
        feed = self.feed_image()
        vx0, vy0, vx1, vy1 = (int(round(value)) for value in self.geometry["view"])
        view_h = max(1, vy1 - vy0)
        self.content_height = float(feed.height)
        self.scroll_y = self.clamp_scroll(self.scroll_y)
        top = int(round(self.scroll_y))
        crop = feed.crop((0, top, feed.width, top + view_h))
        fade = Image.new("L", (1, view_h), 255)
        for index in range(min(FADE, view_h // 2)):
            value = int(255 * (index + 1) / (FADE + 1))
            if top > 0:
                fade.putpixel((0, index), value)
            if top + view_h < feed.height:
                fade.putpixel((0, view_h - 1 - index), value)
        crop.putalpha(Image.composite(crop.getchannel("A"), Image.new("L", crop.size, 0), fade.resize(crop.size)))
        image.alpha_composite(crop, (vx0 - int(FEED_MARGIN), vy0))
        if feed.height > view_h:
            track_x = int(self.geometry["column"][1]) + 10
            bar = Image.new("RGBA", (7, view_h), CLEAR)
            draw = ImageDraw.Draw(bar)
            draw.rounded_rectangle((1, 0, 5, view_h - 1), 2, fill=(255, 255, 255, 70))
            thumb_h = max(24, int(view_h * view_h / feed.height))
            thumb_y = int((view_h - thumb_h) * self.scroll_y / max(1.0, feed.height - view_h))
            draw.rounded_rectangle((0, thumb_y, 6, thumb_y + thumb_h), 3, fill=(255, 255, 255, 230))
            image.alpha_composite(bar, (track_x - 1, vy0))
        return image

    def base(self, width: int, height: int, hover: str) -> Image.Image:
        key = (width, height, hover)
        if key not in self.base_cache:
            if self.background_key != (width, height):
                self.background = self.render_background(width, height)
                self.background_key = (width, height)
                self.base_cache = {}
            image = self.background.copy()
            image.alpha_composite(render_icon_bar(width, height, self.geometry["bar"], self.geometry["icons"], "mail", hover))
            self.base_cache[key] = image
        return self.base_cache[key]

    def render_background(self, width: int, height: int) -> Image.Image:
        background = Image.new("RGBA", (width, height), PANEL_BLUE + (255,))
        cx, cy, diameter = self.geometry["portrait"]
        reach = diameter * 0.62
        halo = Image.new("RGBA", (width, height), CLEAR)
        ImageDraw.Draw(halo).ellipse((cx - reach, cy - reach, cx + reach, cy + reach), fill=(255, 255, 255, 110))
        background.alpha_composite(glow(halo, 34, scale=1))
        s = SCALE
        size = (width * s, height * s)
        image = panel_frame(width, height)
        cx, cy, radius = cx * s, cy * s, diameter * s / 2.0
        image.alpha_composite(self.render_portrait(size, cx, cy, radius))
        text = Image.new("RGBA", size, CLEAR)
        draw = ImageDraw.Draw(text)
        column_x0 = self.geometry["column"][0] * s
        header_y = self.geometry["header_y"] * s
        draw.text((column_x0, header_y), "Sunflowyr Engine updates", font=load_font("segoeuiz.ttf", 27), fill=HEADER_COLOUR, anchor="lt")
        image.alpha_composite(text_shadow(text))
        image.alpha_composite(text)
        background.alpha_composite(image.resize((width, height), Image.LANCZOS))
        return background

    def render_portrait(self, size: tuple[int, int], cx: float, cy: float, radius: float) -> Image.Image:
        s = SCALE
        layer = Image.new("RGBA", size, CLEAR)
        ring = Image.new("RGBA", size, CLEAR)
        ImageDraw.Draw(ring).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(255, 255, 255, 255), width=3 * s)
        layer.alpha_composite(glow(ring, 7, 3))
        diameter = int(radius * 2)
        face = gradient_fill((diameter, diameter)).transpose(Image.Transpose.ROTATE_180)
        face.alpha_composite(Image.new("RGBA", face.size, (255, 255, 255, 120)))
        if ICON_PATH.exists():
            icon = Image.open(ICON_PATH).convert("RGBA")
            side = min(icon.size)
            left = (icon.width - side) // 2
            top = (icon.height - side) // 2
            icon = icon.crop((left, top, left + side, top + side)).resize((diameter, diameter), Image.LANCZOS)
            face.alpha_composite(icon)
        mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
        face.putalpha(Image.composite(face.getchannel("A"), Image.new("L", face.size, 0), mask))
        layer.alpha_composite(face, (int(cx - radius), int(cy - radius)))
        layer.alpha_composite(ring)
        return layer

    def feed_image(self) -> Image.Image:
        column_w = int(round(self.geometry["column"][1] - self.geometry["column"][0] + 2 * FEED_MARGIN))
        key = (column_w, self.notes_stamp, len(self.releases))
        if key != self.feed_key or self.feed is None:
            self.feed = self.render_feed(column_w)
            self.feed_key = key
        return self.feed

    def render_feed(self, width: int) -> Image.Image:
        s = SCALE
        margin = FEED_MARGIN * s
        body = load_font("segoeuiz.ttf", 16)
        version_font = load_font("segoeuib.ttf", 15)
        label_font = load_font("segoeuib.ttf", 9)
        line_height = 16 * 1.5 * s
        pad_x, pad_y = 22 * s, 16 * s
        full_w = width * s
        bubble_w = full_w - 2 * margin
        blocks = []
        y = margin
        for release in self.releases:
            placed, text_h = layout_note(release["lines"] or ["No notes for this version."], body, bubble_w - 2 * pad_x, line_height)
            bubble_h = text_h + 2 * pad_y
            blocks.append((release, placed, y, bubble_h))
            y += TAG_HEIGHT * s + bubble_h + BUBBLE_GAP * s
        total_h = int(y - BUBBLE_GAP * s + margin)
        size = (int(full_w), max(1, total_h))
        bubbles = Image.new("RGBA", size, CLEAR)
        bubble_draw = ImageDraw.Draw(bubbles)
        text = Image.new("RGBA", size, CLEAR)
        text_draw = ImageDraw.Draw(text)
        right = full_w - margin
        for release, placed, top, bubble_h in blocks:
            version = release["version"]
            version_text = version if version[:1].lower() == "v" or not version[:1].isdigit() else f"v{version}"
            text_draw.text((right - 6 * s, top + 2 * s), version_text, font=version_font, fill=(255, 255, 255, 255), anchor="rt")
            if release["label"]:
                text_draw.text((right - 6 * s, top + 22 * s), release["label"], font=label_font, fill=(255, 255, 255, 220), anchor="rt")
            box_top = top + TAG_HEIGHT * s
            bubble_draw.rounded_rectangle((margin, box_top, right, box_top + bubble_h), 16 * s, fill=(255, 255, 255, 52), outline=(255, 255, 255, 190), width=2 * s)
            for part, indent, line_y, bullet in placed:
                text_x = margin + pad_x + indent
                text_y = box_top + pad_y + line_y
                if bullet:
                    dot_y = text_y + line_height * 0.5
                    dot_x = margin + pad_x + line_height * 0.3
                    text_draw.ellipse((dot_x - 3 * s, dot_y - 3 * s, dot_x + 3 * s, dot_y + 3 * s), fill=(255, 255, 255, 255))
                text_draw.text((text_x, text_y + line_height * 0.5), part, font=body, fill=(255, 255, 255, 255), anchor="lm")
        image = glow(bubbles, 6)
        image.alpha_composite(bubbles)
        image.alpha_composite(text_shadow(text))
        image.alpha_composite(text)
        return image.resize((max(1, size[0] // s), max(1, size[1] // s)), Image.LANCZOS)
