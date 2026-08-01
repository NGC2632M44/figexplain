"""figexplain entry point.

Usage:
  python run.py            # pick the current Zotero collection's article by index
  python run.py <itemKey>  # process a specific item by Zotero key
"""
from __future__ import annotations
import io
import os
import sys
import glob

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from figexplain import config as cfg
from figexplain import zotero_local as zl
from figexplain import pdf_extract as pe
from figexplain import llm_explain as llm
from figexplain import note_writer as nw


def pick_article() -> dict:
    """Ask user to pick an article from the current Zotero collection."""
    if not zl.ping():
        print("✗ Zotero 未运行或本地 HTTP 服务未开启 (127.0.0.1:23119)。")
        print("  请先启动 Zotero。")
        sys.exit(1)
    coll = zl.get_selected_collection()
    coll_id = coll.get("id")
    print(f"当前选中分类：{coll.get('name')} (id={coll_id})")
    articles = zl.list_articles(coll_id)
    if not articles:
        print("✗ 该分类下没有 journalArticle 条目。")
        sys.exit(1)
    print("\n分类下的文献：")
    for i, a in enumerate(articles):
        print(f"  [{i}] {a['title'][:70]}  ({a['date']})  key={a['key']}")
    while True:
        s = input(f"\n输入序号选择文献 (0-{len(articles)-1})，或直接输入 item key: ").strip()
        if s.isdigit() and 0 <= int(s) < len(articles):
            return articles[int(s)]
        # treat as item key
        if s:
            try:
                d = zl.get_item(s)
                return {
                    "key": s,
                    "title": d.get("title", ""),
                    "date": d.get("date", ""),
                    "authors": ", ".join(c.get("lastName", "") for c in d.get("creators", [])[:3]),
                    "doi": d.get("DOI", ""),
                    "itemType": d.get("itemType", ""),
                }
            except Exception:
                pass
        print("输入无效，请重试。")


def resolve_pdf(article_key: str, storage_dir: str) -> str:
    """Find the PDF attachment path for an article item key.

    Robust to common misconfigurations of zotero_storage_dir:
      - storage_dir points at .../storage            (correct)
      - storage_dir points at .../storage/<KEY>      (one level too deep)
    Always verifies the file exists on disk before returning; falls back to a
    directory listing of the attachment-key folder when the recorded filename
    is missing or has encoding issues.
    """
    att_key = None
    filename = ""

    # article_key may be the parent (regular item) or an attachment itself
    try:
        att = zl.find_pdf_attachment(article_key)
        if att:
            att_key = att["key"]
            filename = att.get("filename", "")
    except Exception:
        att = None
    if not att_key:
        # maybe the key itself is an attachment
        try:
            d = zl.get_item(article_key)
            if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
                att_key = d["key"]
                filename = d.get("filename", "")
        except Exception:
            pass

    # candidate bases: storage_dir as given, plus its parent (covers the
    # "storage_dir already ends with <KEY>" misconfiguration).
    storage_dir = (storage_dir or "").rstrip("\\/").replace("\\", "/")
    bases = [storage_dir]
    parent = "/".join(storage_dir.split("/")[:-1])
    if parent and parent not in bases:
        bases.append(parent)

    candidates = []
    # 1) exact filename under <base>/<att_key>/
    if att_key and filename:
        for b in bases:
            candidates.append(f"{b}/{att_key}/{filename}")
    # 2) any *.pdf under <base>/<att_key>/  (filename unknown / encoding issues)
    if att_key:
        for b in bases:
            cand_dir = f"{b}/{att_key}"
            if os.path.isdir(cand_dir):
                for f in os.listdir(cand_dir):
                    if f.lower().endswith(".pdf"):
                        candidates.append(f"{cand_dir}/{f}")
    # 3) last resort: any *.pdf under <base>/<article_key>/  (key is the folder)
    for b in bases:
        cand_dir = f"{b}/{article_key}"
        if os.path.isdir(cand_dir):
            for f in os.listdir(cand_dir):
                if f.lower().endswith(".pdf"):
                    candidates.append(f"{cand_dir}/{f}")

    for c in candidates:
        if c and os.path.isfile(c):
            return c.replace("\\", "/")
    # nothing found; return the first candidate for the error message
    return candidates[0].replace("\\", "/") if candidates else ""


def run_pdf_mode(pdf_path: str, settings: dict) -> None:
    """直接解读一个 PDF(拖放/文件夹模式):不查 Zotero,结果 HTML 自动打开。

    输出:figexplain-out/<文件名>.html;完成后 os.startfile 打开浏览器。
    """
    import hashlib
    import time
    import subprocess
    from figexplain import note_writer as nw

    pdf_path = (pdf_path or "").strip().strip('"')
    if not pdf_path or not os.path.isfile(pdf_path):
        print(f"✗ 文件不存在: {pdf_path}")
        sys.exit(1)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    key = hashlib.md5(pdf_path.encode("utf-8")).hexdigest()[:8].upper()
    article = {"key": key, "title": base, "date": "", "authors": "", "doi": ""}
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figexplain-out")
    os.makedirs(out_dir, exist_ok=True)
    out_html = os.path.join(out_dir, f"{base}.html")

    print(f"PDF: {pdf_path}")
    print("[1/4] 提取图表与文本…")
    figures = pe.extract_figures(pdf_path)
    if not figures:
        print("✗ 未提取到任何图表(可能是扫描版或无 Fig 标题)。")
        sys.exit(1)
    full_text = pe.fulltext(pdf_path)
    abstract, intro = pe.abstract_and_intro(full_text)
    print(f"  图 {len(figures)} 张 | 摘要 {len(abstract)} 字 | 引言 {len(intro)} 字")

    e_base = settings.get("explain_base_url") or settings["openai_base_url"]
    e_key = settings.get("explain_api_key") or settings["openai_api_key"]
    e_model = settings.get("explain_model") or settings.get("openai_model") or "gpt-4o"
    print(f"[2/4] 逐图解读(模型 {e_model})…")
    figure_explanations = []
    for i, fig in enumerate(figures):
        t0 = time.time()
        try:
            fe = llm.explain_figure(fig, e_base, e_key, e_model)
            fe["index"] = fig.index
            print(f"  Fig {fig.index}: OK ({len(fe.get('panels', []))} panels) {time.time()-t0:.0f}s")
        except Exception as e:
            print(f"  Fig {fig.index}: 失败 {str(e)[:80]}")
            fe = {"index": fig.index, "panels": [], "gist_zh": f"(失败: {e})", "logical_role": ""}
        figure_explanations.append(fe)

    s_base = settings.get("synthesize_base_url") or settings["openai_base_url"]
    s_key = settings.get("synthesize_api_key") or settings["openai_api_key"]
    s_model = settings.get("synthesize_model") or settings.get("openai_model") or "gpt-4o"
    print(f"[3/4] 综合分析(模型 {s_model})…")
    try:
        synthesis = llm.synthesize(figure_explanations, abstract, intro, full_text, s_base, s_key, s_model)
        print(f"  差异 {len(synthesis.get('differences', []))} 条 | 要点 {len(synthesis.get('key_points', []))} 条")
    except Exception as e:
        synthesis = {"figure_logical_structure": "", "differences": [], "key_points": [], "_raw": str(e)}
        print(f"  综合分析失败: {str(e)[:80]}")

    html = nw.build_note_html(article, figures, figure_explanations, synthesis)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[4/4] 结果: {out_html}")
    try:
        os.startfile(out_html)  # 自动打开浏览器
    except Exception:
        pass
    print("完成 ✅")


def main():
    cfg.ensure_deps()
    settings = cfg.load_config()
    # 0. --pdf <path>:直接解读一个 PDF 文件(拖放/文件夹模式,不依赖 Zotero)
    #    必须最先处理,跳过任何交互配置
    if "--pdf" in sys.argv:
        return run_pdf_mode(sys.argv[sys.argv.index("--pdf") + 1], settings)
    # 非交互模式:--no-interactive / stdin 非 TTY / FIGEXPLAIN_NONINTERACTIVE=1
    # (skill/MCP/批量调用必需:直接使用已存配置,不阻塞等 input())
    noninteractive = ("--no-interactive" in sys.argv
                      or not sys.stdin.isatty()
                      or os.environ.get("FIGEXPLAIN_NONINTERACTIVE") == "1")
    if "--no-interactive" in sys.argv:
        sys.argv.remove("--no-interactive")
    if not noninteractive:
        settings = cfg.interactive_config()

    # 1. pick article
    if len(sys.argv) > 1:
        key = sys.argv[1].strip()
        try:
            d = zl.get_item(key)
            # key 可能是 attachment(如直接给 storage 目录名):上溯父条目,
            # 让标题/key/标签统一用父条目(文献本体)
            if d.get("itemType") == "attachment" and d.get("parentItem"):
                try:
                    p = zl.get_item(d["parentItem"])
                    if p.get("itemType") in ("journalArticle", "preprint", "bookSection", "thesis", "conferencePaper"):
                        d = p
                except Exception:
                    pass
            article = {
                "key": d.get("key", key),
                "title": d.get("title", ""),
                "date": d.get("date", ""),
                "authors": ", ".join(c.get("lastName", "") for c in d.get("creators", [])[:3]),
                "doi": d.get("DOI", ""),
            }
        except Exception:
            article = pick_article()
    else:
        article = pick_article()

    print(f"\n选定文献：{article['title']}")
    print(f"  key={article['key']}  doi={article.get('doi','')}")

    # 2. resolve PDF
    storage_dir = settings.get("zotero_storage_dir") or os.path.join(
        os.path.expanduser("~"), "Zotero", "storage")
    pdf_path = resolve_pdf(article["key"], storage_dir)
    if not pdf_path or not os.path.exists(pdf_path):
        print(f"✗ 找不到该文献的 PDF 附件。尝试过的路径：{pdf_path}")
        print("  请确认 storage 目录路径正确（可在 ~/.figexplain/config.json 里改 zotero_storage_dir）。")
        sys.exit(1)
    print(f"PDF 路径：{pdf_path}")

    # 3. extract figures + abstract/intro + full text
    print("\n[1/4] 提取图表与文本…")
    figures = pe.extract_figures(pdf_path)
    if not figures:
        print("✗ 未提取到任何图表（caption），可能是扫描版 PDF 或无 'Fig N' 标题。")
        sys.exit(1)
    print(f"  提取到 {len(figures)} 张图。")
    full_text = pe.fulltext(pdf_path)
    abstract, intro = pe.abstract_and_intro(full_text)
    print(f"  摘要 {len(abstract)} 字，引言 {len(intro)} 字，正文 {len(full_text)} 字。")

    # 4. per-figure LLM explanation (vision)——视觉配置独立(便宜模型)
    e_base = settings.get("explain_base_url") or settings["openai_base_url"]
    e_key = settings.get("explain_api_key") or settings["openai_api_key"]
    e_model = settings.get("explain_model") or settings.get("openai_model") or "gpt-4o"
    print(f"\n[2/4] 调用多模态 LLM 解读每张图（模型 {e_model}）…")
    figure_explanations = []
    for i, fig in enumerate(figures):
        print(f"  → Figure {fig.index} ({i+1}/{len(figures)})…", end=" ", flush=True)
        try:
            fe = llm.explain_figure(fig, e_base, e_key, e_model)
            fe["index"] = fig.index
            n_panels = len(fe.get("panels", []))
            print(f"OK ({n_panels} panels)")
        except Exception as e:
            print(f"失败: {e}")
            fe = {"index": fig.index, "panels": [], "gist_zh": f"(解读失败: {e})",
                  "logical_role": ""}
        figure_explanations.append(fe)

    # 5. synthesis (structure vs abstract/intro, locate in text, key points)
    # ——纯文本,用便宜配置(默认 DeepSeek flash 直连)
    s_base = settings.get("synthesize_base_url") or settings["openai_base_url"]
    s_key = settings.get("synthesize_api_key") or settings["openai_api_key"]
    s_model = settings.get("synthesize_model") or settings.get("openai_model") or "gpt-4o"
    print(f"\n[3/4] 综合分析：逻辑结构 vs 摘要/引言，定位正文差异，总结要点…(模型 {s_model})")
    try:
        synthesis = llm.synthesize(figure_explanations, abstract, intro,
                                   full_text, s_base, s_key, s_model)
        n_diff = len(synthesis.get("differences", []))
        n_kp = len(synthesis.get("key_points", []))
        print(f"  差异/遗漏 {n_diff} 条，关键要点 {n_kp} 条。")
    except Exception as e:
        print(f"  综合分析失败: {e}")
        synthesis = {"figure_logical_structure": "", "differences": [],
                     "key_points": [], "_raw": str(e)}

    # 6. write note back to Zotero
    print(f"\n[4/4] 写入 Zotero 笔记…")
    try:
        ok = nw.write_note(article, figures, figure_explanations, synthesis)
        if ok:
            print("✓ 笔记已写入 Zotero（顶层 note，在当前分类下）。")
            print(f"  标签：fig-explain-{article['key']} / fig-explain")
        else:
            print("✗ 写入失败。")
    except Exception as e:
        print(f"✗ 写入失败: {e}")

    # also dump a local copy
    try:
        local_html = nw.build_note_html(article, figures, figure_explanations, synthesis)
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           f"figexplain_{article['key']}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(local_html)
        print(f"  本地副本：{out}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
