"""PDF figure extraction: image + caption + panel split + body Fig references.

Borrows ideas from:
  - zotero-figure: locate figure regions via PDF structure (simplified: embedded raster + caption text)
  - fig_explain: manifest schema {page, caption, refs, panels, img}
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List
import re

import fitz  # PyMuPDF

# Build regex without backslash-in-literal (keeps this file JSON-safe to author).
# Use re character classes / explicit chars only; avoid \s \b \d \w \. escapes.
_WS = r"[ \t\r\n\f]"            # whitespace
# 含 Unicode 空格(\xa0 不换行空格、 窄空格、 窄不换行空格):PNAS/Nature
# 系排版广泛使用,缺它们导致 "Fig.\xa01A" 全部漏匹配(实测 PNAS 图=0 根因)
_WS_P = r"[ \t\r\n\f\xa0  ]*"         # zero-or-more whitespace
_WS_P1 = r"[ \t\r\n\f\xa0  ]+"        # one-or-more whitespace

# caption head: "Fig. N |" (Nature/Cell style titled caption) -- preferred
CAPTION_TITLED_RE = re.compile(
    r"(?:Fig[.]?|Figure|图|Abb[.]?)" + _WS_P + r"([0-9]+)" + _WS_P + r"[|]"
)
# looser caption head (no pipe) for journals without titled captions
CAPTION_HEAD_RE = re.compile(
    r"(?:Fig[.]?|Figure|图|Abb[.]?)" + _WS_P + r"([0-9]+)",
    re.IGNORECASE,
)
# panel label: single capital letter (or A-B range) that starts a panel description.
# Must NOT be preceded by a digit/letter (rules out "Fig. 1A", "1B").
# Followed by whitespace then any non-whitespace char (description start).
PANEL_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z])(?:" + _WS_P + r"[-]" + _WS_P + r"([A-Z]))?" + _WS_P1 + r"(?=\S)"
)
# body reference: optional '(' then Fig/Figure/Figs + digits
FIG_REF_RE = re.compile(
    r"[(]?Fig(?:ure|s)?[.]?" + _WS_P + r"([0-9]+)",
    re.IGNORECASE,
)
# sentence end (for refs splitting) - keep simple
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# hyphen/dash variants
DASH = r"[-]"


def _split_sentences(text: str) -> List[str]:
    """Split text into sentences, first by newline (block) then by sentence end.

    Also joins short orphan fragments (PDF column reflow) so a Fig reference
    and its sentence are not split across two chunks.
    """
    blocks = [b for b in text.split("\n")]
    raw = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        for s in SENT_SPLIT_RE.split(b):
            s = s.strip()
            if s:
                raw.append(s)
    # merge: if a chunk does not end with sentence punct and next chunk starts
    # lowercase/digit, they were split by a newline mid-sentence (column reflow).
    merged = []
    for s in raw:
        if merged and not _ends_sentence(merged[-1]) and (s[:1].islower() or s[:1].isdigit()):
            merged[-1] = merged[-1] + " " + s
        else:
            merged.append(s)
    return merged


def _ends_sentence(s: str) -> bool:
    if not s:
        return True
    return s[-1] in ".!?:)" or s.endswith("|")


def _offset_in(full_text: str, sentence: str) -> int:
    """Return the first offset of `sentence` in full_text (or -1)."""
    i = full_text.find(sentence)
    return i


@dataclass
class Panel:
    label: str            # "A" or "C-D"
    caption_segment: str


@dataclass
class Figure:
    index: int                  # figure number 1,2,3...
    page_label: str
    page_index: int
    caption: str
    panels: List[Panel] = field(default_factory=list)
    image_b64: str = ""         # base64 image (jpeg preferred) for LLM/note
    image_mime: str = "image/jpeg"
    crop_points: list = field(default_factory=list)
    refs: List[str] = field(default_factory=list)


def _split_panels(caption: str) -> List[Panel]:
    """Split caption into panels by letter labels."""
    if not caption:
        return []
    panels = []
    matches = list(PANEL_LABEL_RE.finditer(caption))
    if not matches:
        return [Panel(label="*", caption_segment=caption.strip())]
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(caption)
        seg = caption[start:end].strip()
        label = m.group(1)
        if m.group(2):
            label = f"{m.group(1)}-{m.group(2)}"
        if seg:
            panels.append(Panel(label=label, caption_segment=seg))
    return panels


def _page_text(page) -> str:
    """Extract page text preserving whitespace (PyMuPDF)."""
    try:
        return page.get_text("text")
    except Exception:
        return ""


def _find_caption_for_index(full_text: str, fig_index: int) -> str:
    """Find the caption block for figure number fig_index.

    Prefers titled captions 'Fig N | <title>. <panels>' (Nature/Cell style).
    Falls back to any 'Fig N' head. Caption runs from head until the next
    caption head, the end of a long run, or a section boundary.
    """
    # 1) try titled caption heads
    titled = list(CAPTION_TITLED_RE.finditer(full_text))
    target_head = None
    heads = titled
    for h in titled:
        try:
            if int(h.group(1)) == fig_index:
                target_head = h
                break
        except ValueError:
            continue
    # 2) fallback to loose heads if no titled match for this index
    if target_head is None:
        loose = list(CAPTION_HEAD_RE.finditer(full_text))
        heads = loose
        for h in loose:
            try:
                if int(h.group(1)) == fig_index:
                    target_head = h
                    break
            except ValueError:
                continue
    if target_head is None:
        return ""
    start = target_head.start()
    # end = next caption head after this one (any kind)
    next_heads = [h for h in (titled + (list(CAPTION_HEAD_RE.finditer(full_text)) if target_head in titled else [])) if h.start() > start]
    if next_heads:
        end = min(h.start() for h in next_heads)
    else:
        end = min(start + 2000, len(full_text))
    seg = full_text[start:end].strip()
    if len(seg) > 1800:
        seg = seg[:1800]
    return seg


def _collect_refs(full_text: str, fig_index: int, max_refs: int = 4) -> List[str]:
    """Collect body sentences referencing 'Fig N' for this figure.

    Works on match positions in the full text (not pre-split sentences),
    because naive sentence splitting breaks 'Fig. N' at the period. For each
    'Fig N' reference we take a window around it and trim to sentence-like
    boundaries, skipping occurrences that are caption heads.
    """
    import re as _re
    fig_token = str(fig_index)
    # caption head positions to skip
    caption_starts = set()
    for h in CAPTION_TITLED_RE.finditer(full_text):
        try:
            if int(h.group(1)) == fig_index:
                caption_starts.add(h.start())
        except ValueError:
            pass

    out = []
    seen = set()
    for m in FIG_REF_RE.finditer(full_text):
        if m.group(1) != fig_token:
            continue
        pos = m.start()
        # skip if this is a caption head
        if any(abs(pos - c) < 6 for c in caption_starts):
            continue
        # expand backward to sentence start: last '. ' / '。 ' / newline, but not
        # a period belonging to an abbreviation like 'Fig.' / 'et al.'
        back = full_text.rfind("\n", 0, pos)
        back = back + 1 if back >= 0 else 0
        # further trim to last real sentence end between back and pos
        seg_before = full_text[back:pos]
        # find last '. ' not preceded by Fig/etc abbreviation
        last_end = _re.search(r"[.。]\s+(?=[A-Z(])", seg_before)
        cut = back
        if last_end:
            cut = back + last_end.end()
        # expand forward to next sentence end
        fwd = full_text.find("\n", pos)
        fwd = fwd if fwd >= 0 else len(full_text)
        seg_after = full_text[pos:fwd]
        next_end = _re.search(r"[.。](?:\s|$)", seg_after)
        end = fwd
        if next_end:
            end = pos + next_end.end()
        sentence = full_text[cut:end]
        sentence = _re_sub_ws(sentence)
        if len(sentence) < 20 or len(sentence) > 700:
            continue
        key = sentence[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sentence)
        if len(out) >= max_refs:
            break
    return out


def _re_sub_ws(s: str) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", s).strip()


def _render_figure_image(page, caption_rect=None, max_dim: int = 1024) -> tuple[bytes, list, str]:
    """Render the figure region of a page to PNG bytes + crop_points.

    Accepts an explicit `caption_rect` (a fitz.Rect) computed by the caller
    from caption/page layout (preferred — captures vector-graphic figures with
    no embedded raster image). When `caption_rect` is given we render exactly
    that clip. Otherwise we fall back to the largest embedded raster image
    bbox on the page (original behaviour), then top-70% of the page.
    """
    rect = page.rect
    bbox = None
    if caption_rect is not None:
        # honour caller-provided region; clamp to page
        bbox = fitz.Rect(
            max(rect.x0, caption_rect.x0), max(rect.y0, caption_rect.y0),
            min(rect.x1, caption_rect.x1), min(rect.y1, caption_rect.y1))
    if bbox is None:
        try:
            imgs = page.get_images(full=True)
        except Exception:
            imgs = []
        if imgs:
            try:
                all_bboxes = []
                for im in imgs:
                    for b in page.get_image_rects(im[0]):
                        all_bboxes.append(b)
            except Exception:
                all_bboxes = []
            best = None
            for b in all_bboxes:
                area = (b.x1 - b.x0) * (b.y1 - b.y0)
                if best is None or area > best[0]:
                    best = (area, b)
            if best and best[0] > (rect.width * rect.height * 0.05):
                bbox = best[1]
        if bbox is None:
            bbox = fitz.Rect(rect.x0 + 10, rect.y0 + 10, rect.x1 - 10, rect.y0 + rect.height * 0.7)
    # small margin around the bbox so we don't crop panel labels
    margin = 6
    bbox = fitz.Rect(max(rect.x0, bbox.x0 - margin), max(rect.y0, bbox.y0 - margin),
                     min(rect.x1, bbox.x1 + margin), min(rect.y1, bbox.y1 + margin))
    crop_points = [round(bbox.x0, 1), round(bbox.y0, 1), round(bbox.x1, 1), round(bbox.y1, 1)]
    w = bbox.x1 - bbox.x0
    h = bbox.y1 - bbox.y0
    scale = min(max_dim / max(w, h), 3.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=bbox, alpha=False)
    # JPEG for smaller payload (vision models accept it); fall back to PNG
    try:
        out = pix.tobytes("jpeg")
        fmt = "jpeg"
    except Exception:
        out = pix.tobytes("png")
        fmt = "png"
    return out, crop_points, fmt


def _find_caption_page_and_y(page_texts: list[str], doc, fig_index: int) -> tuple[int, float]:
    """Find the page index and caption-head y0 for figure fig_index.

    Caption head = first 'Fig N |' occurrence (Nature titled style) or 'Fig N'
    (loose). Returns (page_index, caption_y0) or (-1, 0).
    """
    cap_re = CAPTION_TITLED_RE if False else re.compile(
        r"Fig[.]?\s*" + str(fig_index) + r"\s*[|]", re.IGNORECASE)
    for pi, pt in enumerate(page_texts):
        m = cap_re.search(pt)
        if m:
            # caption y0 via blocks
            cy = None
            try:
                for b in doc[pi].get_text("blocks"):
                    if m.group(0) in b[4]:
                        cy = b[1]; break
            except Exception:
                pass
            return pi, (cy if cy is not None else 49.0)
    # fallback loose
    loose = re.compile(r"Fig[.]?\s*" + str(fig_index), re.IGNORECASE)
    for pi, pt in enumerate(page_texts):
        m = loose.search(pt)
        if m:
            cy = None
            try:
                for b in doc[pi].get_text("blocks"):
                    if m.group(0) in b[4]:
                        cy = b[1]; break
            except Exception:
                pass
            return pi, (cy if cy is not None else 49.0)
    return -1, 0.0


def _figure_region(doc, cap_page: int, cap_y0: float) -> tuple[fitz.Rect, int]:
    """Return (clip_rect, src_page_index) for a figure.

    Nature-style two-column layout: a figure occupies the bottom of the page
    that PRECEDES its caption (when the caption sits at the top of the next
    page), or the top of the SAME page above the caption.

    We render the union of ALL content blocks AND embedded raster image rects
    above the caption (same page) or the whole content area of the previous
    page. Unioning image rects too is important: some figures (e.g. Fig 4
    here) are mostly one big raster whose bbox extends lower than the panel
    text labels, so text-block-only union would clip the figure's bottom.
    """
    pr = doc[cap_page].rect
    TOP = pr.y0 + 10
    BOT = pr.y1 - 10
    if cap_y0 < 200:
        # caption at top -> figure on previous page (full content area)
        src = cap_page - 1
        if 0 <= src < len(doc):
            blocks = [b for b in doc[src].get_text("blocks")
                      if b[6] == 0 and b[4].strip()]
            rects = _image_rects_on(doc[src])
            xs, ys = [], []
            for b in blocks:
                xs += [b[0], b[2]]; ys += [b[1], b[3]]
            for r in rects:
                xs += [r.x0, r.x1]; ys += [r.y0, r.y1]
            if xs:
                x0 = max(pr.x0 + 10, min(xs))
                y0 = max(TOP, min(ys))
                x1 = min(pr.x1 - 10, max(xs))
                y1 = min(BOT, max(ys))
                return fitz.Rect(x0, y0, x1, y1), src
        return fitz.Rect(pr.x0 + 10, TOP, pr.x1 - 10, BOT), cap_page
    # caption lower on page -> figure region = content above caption
    blocks = [b for b in doc[cap_page].get_text("blocks")
              if b[6] == 0 and b[4].strip() and b[3] <= cap_y0 + 5]
    rects = [r for r in _image_rects_on(doc[cap_page]) if r.y1 <= cap_y0 + 5]
    xs, ys = [], []
    for b in blocks:
        xs += [b[0], b[2]]; ys += [b[1], b[3]]
    for r in rects:
        xs += [r.x0, r.x1]; ys += [r.y0, r.y1]
    if xs:
        x0 = max(pr.x0 + 10, min(xs))
        y0 = max(TOP, min(ys))
        x1 = min(pr.x1 - 10, max(xs))
        y1 = min(cap_y0, max(ys))  # never extend into caption
        return fitz.Rect(x0, y0, x1, y1), cap_page
    # fallback
    return fitz.Rect(pr.x0 + 10, TOP, pr.x1 - 10, cap_y0), cap_page


def _image_rects_on(page) -> list:
    """All embedded raster image rects on a page (any size)."""
    out = []
    try:
        imgs = page.get_images(full=True)
    except Exception:
        return out
    for im in imgs:
        try:
            for r in page.get_image_rects(im[0]):
                out.append(r)
        except Exception:
            pass
    return out


def extract_figures(pdf_path: str, max_figures: int = 12) -> List[Figure]:
    """Open PDF, extract up to max_figures figures with caption/panels/refs."""
    import base64
    doc = fitz.open(pdf_path)
    # concatenate all page text with page markers
    page_texts = []
    for i in range(len(doc)):
        page_texts.append(_page_text(doc[i]))
    full_text = "\n".join(page_texts)

    # discover figure indices present in captions
    head_matches = list(CAPTION_HEAD_RE.finditer(full_text))
    seen_idx = []
    for h in head_matches:
        try:
            n = int(h.group(1))
        except ValueError:
            continue
        if n not in seen_idx:
            seen_idx.append(n)
    seen_idx.sort()
    seen_idx = seen_idx[:max_figures]

    figures: List[Figure] = []
    for n in seen_idx:
        caption = _find_caption_for_index(full_text, n)
        if not caption:
            continue
        panels = _split_panels(caption)
        refs = _collect_refs(full_text, n)
        # find the page where this figure's caption head appears, then the
        # figure region (may be on the previous page)
        cap_page, cap_y0 = _find_caption_page_and_y(page_texts, doc, n)
        if cap_page < 0:
            page_index = 0
            page_label = "1"
            rect = None
        else:
            rect, page_index = _figure_region(doc, cap_page, cap_y0)
            page_label = str(doc[page_index].get_label() or (page_index + 1))
        try:
            img_bytes, crop, fmt = _render_figure_image(doc[page_index], rect)
            img_b64 = base64.b64encode(img_bytes).decode("ascii")
            mime = "image/jpeg" if fmt == "jpeg" else "image/png"
        except Exception:
            img_b64, crop, mime = "", [], "image/jpeg"
        figures.append(Figure(
            index=n,
            page_label=page_label,
            page_index=page_index,
            caption=caption,
            panels=panels,
            image_b64=img_b64,
            image_mime=mime,
            crop_points=crop,
            refs=refs,
        ))
    doc.close()
    return figures


def fulltext(pdf_path: str, max_chars: int = 60000) -> str:
    """Concatenated page text (truncated) for LLM context (intro/summary/body)."""
    doc = fitz.open(pdf_path)
    parts = []
    total = 0
    for i in range(len(doc)):
        t = _page_text(doc[i])
        parts.append(t)
        total += len(t)
        if total > max_chars:
            break
    doc.close()
    text = "\n".join(parts)
    return text[:max_chars]


def abstract_and_intro(full_text: str) -> tuple[str, str]:
    """Heuristically extract Abstract and Introduction sections.

    Many journals (e.g. Nature Communications) do not label the abstract with a
    header. Strategy:
      - Abstract: prefer an 'Abstract' header line; else take the paragraph that
        immediately follows the author/title block (first long prose paragraph).
      - Introduction: text after an 'Introduction' header line until the next
        section; else empty (LLM can locate it from full text).
    """
    import re as _re
    abstract = ""
    intro = ""
    # 1) explicit Abstract header
    m = _re.search(r"(?is)\n\s*abstract\b", full_text)
    if not m:
        m = _re.search(r"(?is)\babstract\b\s*\n", full_text)
    if m:
        rest = full_text[m.end():]
        m2 = _re.search(r"(?is)\n\s*(introduction|keywords|background)", rest)
        abstract = rest[:m2.start()] if m2 else rest[:3000]
        abstract = _re_sub_ws(abstract)[:3000]
    else:
        # 2) fallback: the abstract is the first paragraph after the title and
        # author/affiliation block. Heuristic: skip lines until a line ends with
        # a sentence-ending period AND is "prose-like" (>50 chars, low digit
        # density), then collect the paragraph (lines joined) until a blank line.
        lines = full_text.split("\n")
        abst_lines = []
        started = False
        for ln in lines:
            ln = ln.strip()
            if not ln:
                if started:
                    break
                continue
            digit_ratio = len(_re.findall(r"\d", ln)) / max(len(ln), 1)
            if not started:
                # prose line: long enough, low digits, ends with sentence punct
                # or hyphen (word continues next line)
                is_prose_start = (
                    len(ln) > 50 and digit_ratio < 0.12
                    and (ln[-1] in ".!?" or ln.endswith("-") or ln[-1].islower())
                )
                if is_prose_start:
                    started = True
                    abst_lines.append(ln)
                continue
            abst_lines.append(ln)
            if len(" ".join(abst_lines)) >= 1500:
                break
        abstract = _re_sub_ws(" ".join(abst_lines))[:3000] if abst_lines else ""

    # Introduction(标题可带章节编号,如 FASEB 的 "1 | Introduction")
    m = _re.search(r"(?is)\n\s*\d*\s*\|?\s*introduction\b", full_text)
    if m:
        rest = full_text[m.end():]
        m2 = _re.search(r"(?is)\n\s*\d*\s*\|?\s*(results|methods|materials|discussion)", rest)
        intro = rest[:m2.start()] if m2 else rest[:4000]
        intro = _re_sub_ws(intro)[:4000]
    else:
        # 无 "Introduction" 标题的期刊(如 Nature Metabolism,引言紧跟摘要):
        # 取「摘要尾部 → 第一个 Results/Methods 标题」之间的正文。
        # 在折叠空白后的全文上定位,保证与 abstract 同一坐标系。
        norm = _re_sub_ws(full_text)
        rm = _re.search(r"(?is)\b(results|methods)\b", norm)
        if rm:
            seg_start = 0
            if abstract:
                tail = _re_sub_ws(abstract)[-80:]
                p = norm.find(tail)
                seg_start = (p + len(tail)) if p >= 0 else 0
            intro = norm[seg_start:rm.start()][:4000]
    return abstract, intro
