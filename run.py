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
    """Find the PDF attachment path for an article item key."""
    # article_key may be the parent (regular item) or an attachment itself
    att = None
    # try as parent -> find PDF child attachment
    try:
        att = zl.find_pdf_attachment(article_key)
    except Exception:
        att = None
    if att:
        return zl.resolve_pdf_path(att["key"], att.get("filename", ""), storage_dir)
    # maybe the key itself is an attachment
    try:
        d = zl.get_item(article_key)
        if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
            return zl.resolve_pdf_path(d["key"], d.get("filename", ""), storage_dir)
    except Exception:
        pass
    # last resort: search storage dir for a folder named article_key
    cand = os.path.join(storage_dir, article_key)
    pdfs = glob.glob(os.path.join(cand, "*.pdf"))
    if pdfs:
        return pdfs[0].replace("\\", "/")
    return ""


def main():
    cfg.ensure_deps()
    settings = cfg.load_config()
    if not settings.get("openai_api_key") or not settings.get("openai_base_url"):
        settings = cfg.interactive_config()
    else:
        # user chose "input each run" — re-ask to allow overrides, keep saved as defaults
        settings = cfg.interactive_config()

    # 1. pick article
    if len(sys.argv) > 1:
        key = sys.argv[1].strip()
        try:
            d = zl.get_item(key)
            article = {
                "key": key,
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

    # 4. per-figure LLM explanation (vision)
    base_url = settings["openai_base_url"]
    api_key = settings["openai_api_key"]
    model = settings.get("openai_model") or "gpt-4o"
    print(f"\n[2/4] 调用多模态 LLM 解读每张图（模型 {model}）…")
    figure_explanations = []
    for i, fig in enumerate(figures):
        print(f"  → Figure {fig.index} ({i+1}/{len(figures)})…", end=" ", flush=True)
        try:
            fe = llm.explain_figure(fig, base_url, api_key, model)
            fe["index"] = fig.index
            n_panels = len(fe.get("panels", []))
            print(f"OK ({n_panels} panels)")
        except Exception as e:
            print(f"失败: {e}")
            fe = {"index": fig.index, "panels": [], "gist_zh": f"(解读失败: {e})",
                  "logical_role": ""}
        figure_explanations.append(fe)

    # 5. synthesis (structure vs abstract/intro, locate in text, key points)
    print(f"\n[3/4] 综合分析：逻辑结构 vs 摘要/引言，定位正文差异，总结要点…")
    try:
        synthesis = llm.synthesize(figure_explanations, abstract, intro,
                                   full_text, base_url, api_key, model)
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
