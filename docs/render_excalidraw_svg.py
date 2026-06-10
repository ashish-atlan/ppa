#!/usr/bin/env python3
"""Minimal Excalidraw-JSON -> SVG renderer for the ppa docs diagrams.

Supports the subset of the Excalidraw element format used by the diagram
sources in this directory: rectangle / ellipse / diamond (with optional
`label` and `roundness`), standalone `text`, and `arrow` (with `points`,
`label`, `strokeStyle: dashed`, and `startArrowhead`/`endArrowhead`).

Usage:
    python3 docs/render_excalidraw_svg.py docs/foo.excalidraw.json
    # writes docs/foo.svg

The goal is a faithful-enough static render for embedding in markdown, not a
pixel-perfect clone of the Excalidraw canvas.
"""
from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path

FONT = "Segoe UI, Helvetica, Arial, sans-serif"
PAD = 24  # viewbox padding around content


def _char_w(font_size: float) -> float:
    return font_size * 0.55


def _text_w(text: str, font_size: float) -> float:
    return max((len(line) for line in text.split("\n")), default=0) * _char_w(font_size)


def _marker_id(color: str) -> str:
    return "ah-" + color.lstrip("#")


def _collect_arrow_colors(elements: list[dict]) -> set[str]:
    colors = set()
    for el in elements:
        if el.get("type") == "arrow":
            if el.get("endArrowhead", "arrow") not in (None, "null"):
                colors.add(el.get("strokeColor", "#1e1e1e"))
            if el.get("startArrowhead") not in (None, "null", None):
                if el.get("startArrowhead"):
                    colors.add(el.get("strokeColor", "#1e1e1e"))
    return colors


def _bounds(elements: list[dict]) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for el in elements:
        t = el.get("type")
        if t in ("cameraUpdate", "delete", "restoreCheckpoint"):
            continue
        x = el.get("x", 0)
        y = el.get("y", 0)
        if t == "arrow":
            for dx, dy in el.get("points", [[0, 0]]):
                xs.append(x + dx)
                ys.append(y + dy)
        elif t == "text":
            xs.extend([x, x + _text_w(el.get("text", ""), el.get("fontSize", 16))])
            n = el.get("text", "").count("\n") + 1
            ys.extend([y, y + n * el.get("fontSize", 16) * 1.25])
        else:
            w = el.get("width", 0)
            h = el.get("height", 0)
            xs.extend([x, x + w])
            ys.extend([y, y + h])
    return min(xs), min(ys), max(xs), max(ys)


def _render_label(cx: float, cy: float, label: dict, default_color: str) -> str:
    text = label.get("text", "")
    fs = label.get("fontSize", 16)
    color = label.get("strokeColor", default_color)
    lines = text.split("\n")
    total_h = len(lines) * fs * 1.2
    start_y = cy - total_h / 2 + fs * 0.9
    out = []
    for i, line in enumerate(lines):
        out.append(
            f'<text x="{cx:.1f}" y="{start_y + i * fs * 1.2:.1f}" '
            f'font-size="{fs}" fill="{color}" text-anchor="middle" '
            f'font-family="{FONT}">{escape(line)}</text>'
        )
    return "\n  ".join(out)


def render(elements: list[dict]) -> str:
    minx, miny, maxx, maxy = _bounds(elements)
    vb_x = minx - PAD
    vb_y = miny - PAD
    vb_w = (maxx - minx) + 2 * PAD
    vb_h = (maxy - miny) + 2 * PAD

    arrow_colors = _collect_arrow_colors(elements)
    defs = ['<defs>']
    for c in sorted(arrow_colors):
        mid = _marker_id(c)
        defs.append(
            f'<marker id="{mid}" markerWidth="9" markerHeight="9" refX="7" refY="3" '
            f'orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="{c}"/></marker>'
        )
    defs.append('</defs>')

    body = []
    for el in elements:
        t = el.get("type")
        if t in ("cameraUpdate", "delete", "restoreCheckpoint"):
            continue
        x = el.get("x", 0)
        y = el.get("y", 0)
        stroke = el.get("strokeColor", "#1e1e1e")
        sw = el.get("strokeWidth", 2)
        fill = el.get("backgroundColor", "transparent")
        opacity = el.get("opacity", 100) / 100.0
        op_attr = f' opacity="{opacity:.2f}"' if opacity < 1 else ""

        if t in ("rectangle", "ellipse", "diamond"):
            w = el.get("width", 0)
            h = el.get("height", 0)
            cx, cy = x + w / 2, y + h / 2
            if t == "rectangle":
                rx = 12 if el.get("roundness") else 0
                body.append(
                    f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                    f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{op_attr}/>'
                )
            elif t == "ellipse":
                body.append(
                    f'<ellipse cx="{cx}" cy="{cy}" rx="{w/2}" ry="{h/2}" '
                    f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{op_attr}/>'
                )
            else:  # diamond
                pts = f"{cx},{y} {x+w},{cy} {cx},{y+h} {x},{cy}"
                body.append(
                    f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" '
                    f'stroke-width="{sw}"{op_attr}/>'
                )
            if el.get("label"):
                body.append(_render_label(cx, cy, el["label"], "#1e1e1e"))

        elif t == "text":
            fs = el.get("fontSize", 16)
            color = el.get("strokeColor", "#1e1e1e")
            lines = el.get("text", "").split("\n")
            for i, line in enumerate(lines):
                body.append(
                    f'<text x="{x}" y="{y + fs + i * fs * 1.25:.1f}" font-size="{fs}" '
                    f'fill="{color}" font-family="{FONT}">{escape(line)}</text>'
                )

        elif t == "arrow":
            pts = el.get("points", [[0, 0]])
            abs_pts = [(x + dx, y + dy) for dx, dy in pts]
            d = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in abs_pts)
            dash = ' stroke-dasharray="7,5"' if el.get("strokeStyle") == "dashed" else ""
            end_marker = ""
            if el.get("endArrowhead", "arrow") not in (None, "null"):
                end_marker = f' marker-end="url(#{_marker_id(stroke)})"'
            start_marker = ""
            if el.get("startArrowhead"):
                start_marker = f' marker-start="url(#{_marker_id(stroke)})"'
            body.append(
                f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{sw}"'
                f'{dash}{op_attr}{end_marker}{start_marker}/>'
            )
            if el.get("label"):
                mid = abs_pts[len(abs_pts) // 2] if len(abs_pts) > 2 else (
                    (abs_pts[0][0] + abs_pts[-1][0]) / 2,
                    (abs_pts[0][1] + abs_pts[-1][1]) / 2,
                )
                lfs = el["label"].get("fontSize", 14)
                lw = _text_w(el["label"]["text"], lfs)
                body.append(
                    f'<rect x="{mid[0]-lw/2-4:.1f}" y="{mid[1]-lfs*0.75:.1f}" '
                    f'width="{lw+8:.1f}" height="{lfs*1.4:.1f}" fill="white" opacity="0.85"/>'
                )
                body.append(
                    f'<text x="{mid[0]:.1f}" y="{mid[1]+lfs*0.35:.1f}" font-size="{lfs}" '
                    f'fill="{el["label"].get("strokeColor", stroke)}" text-anchor="middle" '
                    f'font-family="{FONT}">{escape(el["label"]["text"])}</text>'
                )

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{vb_x:.0f} {vb_y:.0f} {vb_w:.0f} {vb_h:.0f}" font-family="{FONT}">',
        "  " + "\n  ".join(defs),
        f'  <rect x="{vb_x:.0f}" y="{vb_y:.0f}" width="{vb_w:.0f}" height="{vb_h:.0f}" fill="white"/>',
        "  " + "\n  ".join(body),
        "</svg>",
    ]
    return "\n".join(svg)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: render_excalidraw_svg.py <file.excalidraw.json>", file=sys.stderr)
        return 2
    src = Path(argv[1])
    elements = json.loads(src.read_text())
    svg = render(elements)
    out = src.with_suffix("").with_suffix(".svg")
    if out.name.endswith(".excalidraw"):
        out = out.with_name(out.name[: -len(".excalidraw")] + ".svg")
    out = Path(str(src).replace(".excalidraw.json", ".svg"))
    out.write_text(svg)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
