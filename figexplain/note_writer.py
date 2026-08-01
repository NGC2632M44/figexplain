"""Assemble the explanation result into Zotero note HTML and write it back."""
from __future__ import annotations
import html
from typing import Any

from .zotero_local import create_note
from .pdf_extract import Figure


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _fig_block(fig: Figure, explanation: dict) -> str:
    """Render one figure block: image + panel annotations + gist."""
    parts = []
    parts.append(f'<div class="figexplain-fig" data-fig="{fig.index}">')
    parts.append(f'<h3>Figure {fig.index} <span class="figexplain-meta">'
                 f'(p.{fig.page_label})</span></h3>')
    # image (base64 inline, so the note is self-contained)
    if fig.image_b64:
        parts.append(f'<img src="data:{fig.image_mime};base64,{fig.image_b64}" '
                     f'alt="Figure {fig.index}" style="max-width:100%;'
                     f'border:1px solid #ccc;border-radius:6px;" />')
    # gist
    gist = explanation.get("gist_zh") or ""
    role = explanation.get("logical_role") or ""
    if gist or role:
        parts.append('<div class="figexplain-gist">')
        if gist:
            parts.append(f'<strong>本图核心结论：</strong>{_esc(gist)}')
        if role:
            parts.append(f' <span class="figexplain-role">[{_esc(role)}]</span>')
        parts.append('</div>')
    # panels
    panels = explanation.get("panels") or []
    if panels:
        parts.append('<table class="figexplain-panels" '
                     'style="width:100%;border-collapse:collapse;margin-top:6px;">')
        parts.append('<tr><th style="text-align:left;width:32px;">Panel</th>'
                     '<th style="text-align:left;">Caption (en)</th>'
                     '<th style="text-align:left;">解读 (zh)</th></tr>')
        for p in panels:
            label = p.get("label", "")
            cap = p.get("caption_en", "")
            ann = p.get("annotation_zh", "")
            parts.append(
                f'<tr><td style="vertical-align:top;font-weight:bold;">{_esc(label)}</td>'
                f'<td style="vertical-align:top;color:#666;font-size:12px;">{_esc(cap)}</td>'
                f'<td style="vertical-align:top;">{_esc(ann)}</td></tr>'
            )
        parts.append('</table>')
    # refs
    if fig.refs:
        parts.append('<details class="figexplain-refs" '
                     'style="margin-top:4px;font-size:12px;color:#666;">'
                     '<summary>正文引用句（{n}）</summary><ul>'.format(n=len(fig.refs)))
        for r in fig.refs:
            parts.append(f'<li>{_esc(r)}</li>')
        parts.append('</ul></details>')
    parts.append('</div>')
    return "".join(parts)


def _structure_block(synthesis: dict) -> str:
    parts = []
    parts.append('<div class="figexplain-structure">')
    parts.append('<h3>🎯 图表逻辑结构</h3>')
    fs = synthesis.get("figure_logical_structure") or ""
    parts.append(f'<pre style="white-space:pre-wrap;font-size:13px;">{_esc(fs)}</pre>')
    parts.append('</div>')
    return "".join(parts)


def _diff_block(synthesis: dict) -> str:
    diffs = synthesis.get("differences") or []
    if not diffs:
        return ""
    parts = []
    parts.append('<div class="figexplain-diff">')
    parts.append('<h3>🔍 与摘要/引言的差异与遗漏</h3>')
    parts.append('<table style="width:100%;border-collapse:collapse;font-size:12px;">')
    parts.append('<tr><th style="text-align:left;">类型</th>'
                 '<th style="text-align:left;">图</th>'
                 '<th style="text-align:left;">差异点</th>'
                 '<th style="text-align:left;">正文证据</th>'
                 '<th style="text-align:left;">定位</th></tr>')
    for d in diffs:
        typ = d.get("type", "")
        fig = d.get("figure", "")
        point = d.get("point_zh", "")
        ev = d.get("evidence_quote_en", "")
        evz = d.get("evidence_zh", "")
        loc = d.get("location_hint", "")
        ev_cell = f'<div style="color:#888;">{_esc(ev)}</div>' \
                  + (f'<div>{_esc(evz)}</div>' if evz else '')
        parts.append(
            f'<tr><td style="vertical-align:top;">{_esc(typ)}</td>'
            f'<td style="vertical-align:top;">{_esc(fig)}</td>'
            f'<td style="vertical-align:top;">{_esc(point)}</td>'
            f'<td style="vertical-align:top;">{ev_cell}</td>'
            f'<td style="vertical-align:top;color:#888;">{_esc(loc)}</td></tr>'
        )
    parts.append('</table></div>')
    return "".join(parts)


def _keypoints_block(synthesis: dict) -> str:
    kps = synthesis.get("key_points") or []
    if not kps:
        return ""
    parts = []
    parts.append('<div class="figexplain-keypoints">')
    parts.append('<h3>⭐ 关键要点总结</h3><ul>')
    for k in kps:
        parts.append(f'<li>{_esc(k)}</li>')
    parts.append('</ul></div>')
    return "".join(parts)


_STYLE = """
<style>
.figexplain-note{font-family: -apple-system, 'Segoe UI', sans-serif; color:#222; line-height:1.55;}
.figexplain-note h2{border-bottom:2px solid #2c5f9e;padding-bottom:4px;}
.figexplain-note h3{color:#2c5f9e;margin-top:18px;}
.figexplain-fig{margin:14px 0;padding:10px;border:1px solid #e0e0e0;border-radius:8px;background:#fafcff;}
.figexplain-gist{margin:6px 0;padding:6px 8px;background:#eef4fb;border-left:3px solid #2c5f9e;border-radius:3px;}
.figexplain-role{color:#b03030;font-size:12px;}
.figexplain-panels th{background:#2c5f9e;color:#fff;padding:4px 6px;font-size:12px;}
.figexplain-panels td{padding:4px 6px;border:1px solid #eee;}
.figexplain-diff th{background:#8a6d3b;color:#fff;padding:4px 6px;}
.figexplain-diff td{padding:4px 6px;border:1px solid #eee;vertical-align:top;}
.figexplain-keypoints ul{line-height:1.7;}
</style>
"""


def build_note_html(article: dict, figures: list[Figure],
                    figure_explanations: list[dict],
                    synthesis: dict) -> str:
    """Assemble the full Zotero note HTML."""
    title = article.get("title", "")
    authors = article.get("authors", "")
    date = article.get("date", "")
    doi = article.get("doi", "")
    key = article.get("key", "")

    parts = [f'{_STYLE}<div class="figexplain-note">']
    parts.append(f'<h2>📖 图表解读：{_esc(title)}</h2>')
    parts.append('<div class="figexplain-meta" style="color:#666;font-size:12px;">')
    if authors:
        parts.append(f'<div>作者：{_esc(authors)}</div>')
    if date:
        parts.append(f'<div>日期：{_esc(date)}</div>')
    if doi:
        parts.append(f'<div>DOI：<a href="https://doi.org/{html.escape(doi)}">{_esc(doi)}</a></div>')
    parts.append(f'<div>Zotero item key：{_esc(key)}</div>')
    parts.append('</div>')

    # per-figure blocks
    fe_by_idx = {fe.get("index"): fe for fe in figure_explanations}
    parts.append('<h3>🖼 各图解读</h3>')
    for fig in figures:
        fe = fe_by_idx.get(fig.index, {})
        parts.append(_fig_block(fig, fe))

    # synthesis
    parts.append(_structure_block(synthesis))
    parts.append(_diff_block(synthesis))
    parts.append(_keypoints_block(synthesis))

    parts.append(f'<div style="margin-top:18px;color:#999;font-size:11px;">'
                 f'由 figexplain 工具自动生成</div>')
    parts.append('</div>')
    return "".join(parts)


def write_note(article: dict, figures: list[Figure],
               figure_explanations: list[dict], synthesis: dict) -> bool:
    """Build the note HTML and write it back to Zotero.

    Tries to attach the note as a child of the article item; if the local
    Zotero API can't do that, falls back to a top-level note and prepends a
    prominent hint telling the user to drag it under the article.
    """
    note_html = build_note_html(article, figures, figure_explanations, synthesis)
    title = article.get("title", "未命名")
    parent_key = article.get("key", "")
    tags = [f"fig-explain-{parent_key}", "fig-explain"]
    result = create_note(note_html, tags=tags, parent_item_key=parent_key or None)
    if not result.get("parented"):
        hint = (
            '<div style="margin:8px 0;padding:8px 10px;background:#fff8e1;'
            'border-left:4px solid #f0ad4e;border-radius:3px;font-size:13px;">'
            '⚠️ 此笔记为顶层条目（Zotero 本地 API 无法自动挂到父文献下）。'
            '请在 Zotero 中将其<strong>拖动到</strong>对应文献'
            f'「{html.escape(title)}」下，使其成为该文献的子笔记。'
            '</div>'
        )
        create_note(hint + note_html, tags=tags, parent_item_key=None)
    return True
