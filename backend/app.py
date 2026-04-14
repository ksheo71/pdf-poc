import statistics
from pathlib import Path
from typing import List
from urllib.parse import quote

import fitz
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
PDF_PATH = ROOT / "구성도.pdf"
FRONTEND_DIR = ROOT / "frontend"
SCALE = 2
COMMIT_SCALE = 16
SAMPLE_DEPTH = 4  # pixels


def _sample_bg(pix, x0, y0, x1, y1):
    """Sample background color from strips just outside the bbox (image px coords)."""
    w, h = pix.width, pix.height
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    strips = [
        (max(0, x0), max(0, y0 - SAMPLE_DEPTH), min(w, x1), max(0, y0)),          # top
        (max(0, x0), min(h, y1), min(w, x1), min(h, y1 + SAMPLE_DEPTH)),          # bottom
        (max(0, x0 - SAMPLE_DEPTH), max(0, y0), max(0, x0), min(h, y1)),          # left
        (min(w, x1), max(0, y0), min(w, x1 + SAMPLE_DEPTH), min(h, y1)),          # right
    ]
    rs, gs, bs = [], [], []
    for sx0, sy0, sx1, sy1 in strips:
        for sy in range(sy0, sy1):
            for sx in range(sx0, sx1):
                r, g, b = pix.pixel(sx, sy)[:3]
                rs.append(r); gs.append(g); bs.append(b)
    if not rs:
        return (1.0, 1.0, 1.0)
    return (
        statistics.median(rs) / 255,
        statistics.median(gs) / 255,
        statistics.median(bs) / 255,
    )


def _int_to_rgb01(ci):
    return (((ci >> 16) & 0xFF) / 255, ((ci >> 8) & 0xFF) / 255, (ci & 0xFF) / 255)


def _extract_words_by_gap(page):
    """Split text into words by horizontal gaps, so adjacent table cells don't merge."""
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                chars = span.get("chars", [])
                if not chars:
                    continue
                gap_threshold = span.get("size", 10.0) * 0.3

                buf = []

                def flush(buf=buf):
                    if not buf:
                        return
                    text = "".join(c["c"] for c in buf)
                    if text.strip():
                        xs0 = [c["bbox"][0] for c in buf]
                        ys0 = [c["bbox"][1] for c in buf]
                        xs1 = [c["bbox"][2] for c in buf]
                        ys1 = [c["bbox"][3] for c in buf]
                        origin_y = buf[0]["origin"][1]
                        out.append((min(xs0), min(ys0), max(xs1), max(ys1), text, span, origin_y))
                    buf.clear()

                prev = None
                for c in chars:
                    ch = c["c"]
                    if ch.isspace():
                        flush()
                        prev = None
                        continue
                    if prev is not None and c["bbox"][0] - prev["bbox"][2] > gap_threshold:
                        flush()
                    buf.append(c)
                    prev = c
                flush()
    return out


_NUMERIC_CHARS = set("0123456789,.-()/%")


def _is_numeric_token(text):
    t = text.strip()
    if not t:
        return False
    return all(c in _NUMERIC_CHARS or c.isspace() for c in t)


def _load_page1():
    doc = fitz.open(PDF_PATH)
    page = doc.load_page(0)
    matrix = fitz.Matrix(SCALE, SCALE)

    font_cache = {}
    for finfo in doc.get_page_fonts(0):
        xref = finfo[0]
        basefont = finfo[3]
        key = basefont.split("+")[-1]
        try:
            _, _, _, buffer = doc.extract_font(xref)
        except Exception:
            continue
        if buffer and key not in font_cache:
            font_cache[key] = buffer

    probe = page.get_pixmap(matrix=matrix, alpha=False)

    raw_blocks = []
    letter_font_counts = {}
    for x0, y0, x1, y1, text, span, origin_y in _extract_words_by_gap(page):
        fontsize = span.get("size", 10.0)
        font_name = span.get("font", "")
        text_color = _int_to_rgb01(span.get("color", 0))
        color_hex = "#%02x%02x%02x" % (
            int(text_color[0] * 255),
            int(text_color[1] * 255),
            int(text_color[2] * 255),
        )
        if not _is_numeric_token(text):
            letter_font_counts[font_name] = letter_font_counts.get(font_name, 0) + 1
        raw_blocks.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "text": text,
            "font_name": font_name,
            "font_size": fontsize,
            "color": color_hex,
            "origin_y": origin_y,
        })

    dominant_letter_font = (
        max(letter_font_counts.items(), key=lambda kv: kv[1])[0]
        if letter_font_counts
        else ""
    )

    blocks = []
    for b in raw_blocks:
        if _is_numeric_token(b["text"]) and dominant_letter_font:
            b = {**b, "font_name": dominant_letter_font}
        blocks.append(b)
        fill = _sample_bg(
            probe,
            b["x0"] * SCALE, b["y0"] * SCALE,
            b["x1"] * SCALE, b["y1"] * SCALE,
        )
        page.add_redact_annot(
            fitz.Rect(b["x0"], b["y0"], b["x1"], b["y1"]),
            fill=fill,
            cross_out=False,
        )

    page.apply_redactions()

    pix = page.get_pixmap(matrix=matrix, alpha=False)
    png_bytes = pix.tobytes("png")

    meta = {
        "image_width": pix.width,
        "image_height": pix.height,
        "pdf_width": page.rect.width,
        "pdf_height": page.rect.height,
        "scale": SCALE,
        "blocks": blocks,
    }
    doc.close()
    return png_bytes, meta, font_cache


def _hex_to_rgb01(hex_color):
    s = hex_color.lstrip("#")
    if len(s) != 6:
        return (0.0, 0.0, 0.0)
    return (int(s[0:2], 16) / 255, int(s[2:4], 16) / 255, int(s[4:6], 16) / 255)


app = FastAPI()

_png_cache, _meta_cache, _font_cache = _load_page1()


class Box(BaseModel):
    orig_x0: float
    orig_y0: float
    orig_x1: float
    orig_y1: float
    x0: float
    y0: float
    x1: float
    y1: float
    text: str = ""
    font_name: str = ""
    font_size: float = 10.0
    color: str = "#000000"
    origin_y: float | None = None


class DownloadRequest(BaseModel):
    boxes: List[Box]


@app.get("/api/page1")
def get_page1_meta():
    return _meta_cache


@app.get("/api/page1/image.png")
def get_page1_image():
    return Response(content=_png_cache, media_type="image/png")


@app.post("/api/commit")
def commit(req: DownloadRequest):
    from collections import defaultdict

    doc = fitz.open(PDF_PATH)
    page = doc.load_page(0)
    matrix = fitz.Matrix(COMMIT_SCALE, COMMIT_SCALE)

    if req.boxes:
        probe = page.get_pixmap(matrix=matrix, alpha=False)
        for b in req.boxes:
            fill = _sample_bg(
                probe,
                b.orig_x0 * COMMIT_SCALE, b.orig_y0 * COMMIT_SCALE,
                b.orig_x1 * COMMIT_SCALE, b.orig_y1 * COMMIT_SCALE,
            )
            page.add_redact_annot(
                fitz.Rect(b.orig_x0, b.orig_y0, b.orig_x1, b.orig_y1),
                fill=fill,
                cross_out=False,
            )
        page.apply_redactions()

        fonts = {}
        for basefont, buffer in _font_cache.items():
            try:
                fonts[basefont] = fitz.Font(fontbuffer=buffer)
            except Exception:
                continue
        fallback = next(iter(fonts.values()), None)

        by_color = defaultdict(list)
        for b in req.boxes:
            if b.text.strip():
                by_color[b.color].append(b)

        for color_hex, color_boxes in by_color.items():
            tw = fitz.TextWriter(page.rect)
            for b in color_boxes:
                font = fonts.get(b.font_name) or fallback
                if font is None:
                    continue
                base_origin = (
                    b.origin_y if b.origin_y is not None else (b.orig_y1 - 0.2 * b.font_size)
                )
                baseline_y = base_origin + (b.y0 - b.orig_y0)
                try:
                    tw.append(
                        fitz.Point(b.x0, baseline_y),
                        b.text,
                        font=font,
                        fontsize=b.font_size,
                    )
                except Exception:
                    if fallback is not None and font is not fallback:
                        try:
                            tw.append(
                                fitz.Point(b.x0, baseline_y),
                                b.text,
                                font=fallback,
                                fontsize=b.font_size,
                            )
                        except Exception:
                            pass
            tw.write_text(page, color=_hex_to_rgb01(color_hex))

    pdf_bytes = doc.tobytes()
    doc.close()
    filename = "구성도_수정본.pdf"
    disposition = (
        f"attachment; filename=\"{quote(filename)}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
