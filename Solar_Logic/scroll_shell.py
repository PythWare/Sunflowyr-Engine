from __future__ import annotations
import math
from typing import Any
from PIL import Image, ImageChops, ImageColor, ImageDraw

SUPERSAMPLE = 3
INK = (20, 23, 25, 255)
CAP_WHITE = (246, 248, 247, 255)
SEAM = (168, 176, 178, 255)
GLASS_LINE = (218, 230, 233, 255)
CLEAR = (0, 0, 0, 0)
DIAMOND_INK = (70, 46, 4, 255)
DIAMOND_FACETS = ((254, 226, 124, 255), (238, 188, 54, 255), (178, 122, 14, 255), (210, 152, 28, 255))
DIAMOND_TABLE = (230, 176, 40, 255)
DIAMOND_GLINT = (255, 246, 200, 255)
SEAT_EDGE = (140, 148, 150, 255)
SEAT_FACETS = ((164, 172, 174, 255), (190, 196, 197, 255), (238, 241, 241, 255), (214, 219, 220, 255))
SEAT_TABLE = (206, 211, 212, 255)

def render_scroll_shell(layout: dict[str, Any]) -> tuple[Image.Image, int, int]:
    x0, y0, x1, y1 = layout["body"]
    rail_w = float(layout["rail_w"])
    ink = float(layout["ink"])
    radius = float(layout["corner"])
    pad = 4.0
    ox = int(math.floor(x0 - pad))
    oy = int(math.floor(y0 - pad))
    width = int(math.ceil(x1 + pad)) - ox
    height = int(math.ceil(y1 + pad)) - oy
    s = SUPERSAMPLE

    def box(bounds: tuple[float, float, float, float], grow: float = 0.0) -> tuple[float, float, float, float]:
        bx0, by0, bx1, by1 = bounds
        return ((bx0 - ox - grow) * s, (by0 - oy - grow) * s, (bx1 - ox + grow) * s, (by1 - oy + grow) * s)

    def pt(x: float, y: float) -> tuple[float, float]:
        return ((x - ox) * s, (y - oy) * s)

    image = Image.new("RGBA", (width * s, height * s), CLEAR)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box(layout["body"]), radius * s, fill=INK)
    draw.rounded_rectangle(box(layout["body"], -ink), (radius - ink) * s, fill=CAP_WHITE)
    image = shade_shell(image, layout, ox, oy)
    draw = ImageDraw.Draw(image)

    lx1 = layout["left_rail"][2]
    rx0 = layout["right_rail"][0]
    for seam_x in (lx1, rx0):
        draw.line([pt(seam_x, y0 + ink), pt(seam_x, y1 - ink)], fill=SEAM, width=2 * s)

    hole = layout["hole"]
    draw.rounded_rectangle(box(hole, 2.0), 12 * s, fill=INK)
    draw.rounded_rectangle(box(hole), 10 * s, fill=CLEAR)
    draw.rounded_rectangle(box(hole, -3.0), 7 * s, outline=GLASS_LINE, width=s)

    cx_left, cx_right, cy, half_w, half_h = layout["diamond"]
    draw_diamond_seat(draw, pt, cx_right, cy, half_w, half_h)
    draw_gold_diamond(draw, pt, cx_left, cy, half_w, half_h)
    return downsample(image, width, height), ox, oy

def render_markers(layout: dict[str, Any]) -> list[tuple[Image.Image, int, int]]:
    cx_left, cx_right, cy, half_w, half_h = layout["diamond"]
    s = SUPERSAMPLE
    markers = []
    for cx, painter in ((cx_left, draw_gold_diamond), (cx_right, draw_diamond_seat)):
        ox = int(math.floor(cx - half_w - 6.0))
        oy = int(math.floor(cy - half_h - 6.0))
        width = int(math.ceil(cx + half_w + 6.0)) - ox
        height = int(math.ceil(cy + half_h + 6.0)) - oy
        image = Image.new("RGBA", (width * s, height * s), CLEAR)
        painter(ImageDraw.Draw(image), lambda x, y, ox=ox, oy=oy: ((x - ox) * s, (y - oy) * s), cx, cy, half_w, half_h)
        markers.append((downsample(image, width, height), ox, oy))
    return markers

def downsample(image: Image.Image, width: int, height: int) -> Image.Image:
    image = image.resize((width, height), Image.LANCZOS)
    image.putalpha(image.getchannel("A").point(lambda value: 255 if value >= 128 else 0))
    return image

def split_scroll_caps(image: Image.Image, ox: int, oy: int, layout: dict[str, Any], backdrop: str) -> list[tuple[Image.Image, int]]:
    x0, y0, x1, y1 = layout["body"]
    split_left = int(round(layout["left_rail"][2])) - ox
    split_right = int(round(layout["right_rail"][0])) - ox
    top = int(math.ceil(y0 - oy)) + 1
    bottom = int(math.floor(y1 - oy)) - 1
    fill = ImageColor.getrgb(backdrop)[:3] + (255,)
    pieces = []
    for left, right, ink_x in ((0, split_left, split_left - 2), (split_right, image.width, 0)):
        crop = image.crop((left, 0, right, image.height))
        piece = Image.new("RGBA", crop.size, fill)
        piece.alpha_composite(crop)
        ImageDraw.Draw(piece).rectangle((ink_x, top, ink_x + 1, bottom), fill=INK)
        pieces.append((piece, ox + left))
    return pieces

def shade_shell(image: Image.Image, layout: dict[str, Any], ox: int, oy: int) -> Image.Image:
    s = SUPERSAMPLE
    x0, y0, x1, y1 = layout["body"]
    rail_w = float(layout["rail_w"])
    lx1 = layout["left_rail"][2]
    rx0 = layout["right_rail"][0]
    body_h = y1 - y0
    columns = []
    for index in range(image.width):
        x = ox + (index + 0.5) / s
        edge = min(x - x0, x1 - x) / (rail_w * 0.42)
        value = 255.0 - 34.0 * (1.0 - min(1.0, max(0.0, edge))) ** 2
        if x < lx1 or x > rx0:
            seam = min(abs(x - lx1), abs(x - rx0)) / (rail_w * 0.16)
            value -= 14.0 * (1.0 - min(1.0, seam)) ** 2
        columns.append(int(max(0.0, value)))
    rows = []
    for index in range(image.height):
        y = oy + (index + 0.5) / s
        edge = min(y - y0, y1 - y) / (body_h * 0.18)
        rows.append(int(255.0 - 24.0 * (1.0 - min(1.0, max(0.0, edge))) ** 2))
    column_image = Image.new("L", (image.width, 1))
    column_image.putdata(columns)
    row_image = Image.new("L", (1, image.height))
    row_image.putdata(rows)
    shade = ImageChops.multiply(
        column_image.resize(image.size, Image.NEAREST),
        row_image.resize(image.size, Image.NEAREST),
    )
    shaded = ImageChops.multiply(image.convert("RGB"), Image.merge("RGB", (shade, shade, shade)))
    shaded.putalpha(image.getchannel("A"))
    return shaded

def rhombus(pt, cx: float, cy: float, half_w: float, half_h: float) -> list[tuple[float, float]]:
    return [pt(cx, cy - half_h), pt(cx + half_w, cy), pt(cx, cy + half_h), pt(cx - half_w, cy)]

def grown_rhombus(pt, cx: float, cy: float, half_w: float, half_h: float, grow: float) -> list[tuple[float, float]]:
    edge = math.hypot(half_w, half_h)
    return rhombus(pt, cx, cy, half_w + grow * edge / half_h, half_h + grow * edge / half_w)

def draw_bevelled_rhombus(draw: ImageDraw.ImageDraw, outer: list, inner: list, facet_colours: tuple, table_colour: tuple) -> None:
    for index, colour in enumerate(facet_colours):
        draw.polygon([outer[index - 1], outer[index], inner[index], inner[index - 1]], fill=colour)
    draw.polygon(inner, fill=table_colour)

def draw_gold_diamond(draw: ImageDraw.ImageDraw, pt, cx: float, cy: float, half_w: float, half_h: float) -> None:
    draw.polygon(grown_rhombus(pt, cx, cy, half_w, half_h, 2.0), fill=DIAMOND_INK)
    outer = rhombus(pt, cx, cy, half_w, half_h)
    inner = rhombus(pt, cx, cy, half_w * 0.52, half_h * 0.52)
    draw_bevelled_rhombus(draw, outer, inner, DIAMOND_FACETS, DIAMOND_TABLE)
    draw.line([inner[3], inner[0]], fill=DIAMOND_GLINT, width=2 * SUPERSAMPLE)
    for outer_point, inner_point in zip(outer, inner):
        draw.line([outer_point, inner_point], fill=DIAMOND_INK, width=SUPERSAMPLE)

def draw_diamond_seat(draw: ImageDraw.ImageDraw, pt, cx: float, cy: float, half_w: float, half_h: float) -> None:
    draw.polygon(grown_rhombus(pt, cx, cy, half_w, half_h, 1.5), fill=SEAT_EDGE)
    outer = rhombus(pt, cx, cy, half_w, half_h)
    inner = rhombus(pt, cx, cy, half_w * 0.52, half_h * 0.52)
    draw_bevelled_rhombus(draw, outer, inner, SEAT_FACETS, SEAT_TABLE)
    draw.line([inner[3], inner[0], inner[1]], fill=SEAT_EDGE, width=SUPERSAMPLE)
