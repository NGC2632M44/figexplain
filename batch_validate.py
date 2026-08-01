"""训练集批量验证(不写 Zotero,只落本地结果,单篇失败不中断)。

用法: python batch_validate.py KEY1 KEY2 ...
输出: figexplain-tool/validation/<KEY>.json + <KEY>.html + 汇总 stdout
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from figexplain import config as cfg
from figexplain import zotero_local as zl
from figexplain import pdf_extract as pe
from figexplain import llm_explain as llm
from figexplain import note_writer as nw
from run import resolve_pdf

OUT_DIR = os.path.join(HERE, "validation")
os.makedirs(OUT_DIR, exist_ok=True)


def article_from_key(key: str) -> dict:
    # Zotero 容错:本地 API 不可达时退化为最小条目(标题未知),PDF 由
    # resolve_pdf 的 storage 目录列举兜底,不阻断整条流水线
    d = {}
    try:
        d = zl.get_item(key)
    except Exception:
        pass
    if d.get("itemType") == "attachment" and d.get("parentItem"):
        try:
            p = zl.get_item(d["parentItem"])
            if p.get("itemType") in ("journalArticle", "preprint", "bookSection", "thesis", "conferencePaper"):
                d = p
        except Exception:
            pass
    return {"key": d.get("key", key), "title": d.get("title", "") or "(Zotero 未连接)",
            "date": d.get("date", ""),
            "authors": ", ".join(c.get("lastName", "") for c in d.get("creators", [])[:3]),
            "doi": d.get("DOI", ""), "journal": d.get("publicationTitle", "")}


def run_one(key: str) -> dict:
    t0 = time.time()
    result = {"input_key": key, "status": "running", "steps": []}
    def say(s: str) -> None:
        result["steps"].append(s)
        print(f"  {s}", flush=True)
    try:
        settings = cfg.load_config()
        article = article_from_key(key)
        result["article"] = article
        say(f"文献:{article['title'][:60]} | {article.get('journal','')} | {article.get('date','')[:7]}")
        pdf_path = resolve_pdf(article["key"], settings.get("zotero_storage_dir") or "")
        if not pdf_path or not os.path.exists(pdf_path):
            result["status"] = "failed"
            result["error"] = f"找不到 PDF:{pdf_path}"
            return result
        say(f"PDF:{os.path.basename(pdf_path)[:70]}")

        figures = pe.extract_figures(pdf_path)
        full_text = pe.fulltext(pdf_path)
        abstract, intro = pe.abstract_and_intro(full_text)
        result["figures_count"] = len(figures)
        result["abstract_chars"] = len(abstract)
        result["intro_chars"] = len(intro)
        say(f"[1/4] 图 {len(figures)} 张 | 摘要 {len(abstract)} 字 | 引言 {len(intro)} 字 | 正文 {len(full_text)} 字")
        if not figures:
            result["status"] = "failed"
            result["error"] = "未提取到图表"
            return result

        e_base = settings.get("explain_base_url") or settings["openai_base_url"]
        e_key = settings.get("explain_api_key") or settings["openai_api_key"]
        e_model = settings.get("explain_model") or settings.get("openai_model") or "gpt-4o"
        say("[2/4] 逐图解读…")
        figure_explanations = []
        fig_results = []
        for i, fig in enumerate(figures):
            t1 = time.time()
            try:
                fe = llm.explain_figure(fig, e_base, e_key, e_model)
                fe["index"] = fig.index
                np_ = len(fe.get("panels", []))
                say(f"  Fig {fig.index}: OK ({np_} panels) {time.time()-t1:.0f}s")
                fig_results.append({"index": fig.index, "ok": True, "panels": np_,
                                    "gist": fe.get("gist_zh", "")[:80], "crop": fig.crop_points})
            except Exception as e:
                say(f"  Fig {fig.index}: FAIL {str(e)[:80]}")
                fe = {"index": fig.index, "panels": [], "gist_zh": f"(失败:{e})", "logical_role": ""}
                fig_results.append({"index": fig.index, "ok": False, "error": str(e)[:120]})
            figure_explanations.append(fe)
            result["figures_done"] = i + 1
        result["figures"] = fig_results

        s_base = settings.get("synthesize_base_url") or settings["openai_base_url"]
        s_key = settings.get("synthesize_api_key") or settings["openai_api_key"]
        s_model = settings.get("synthesize_model") or settings.get("openai_model") or "gpt-4o"
        say("[3/4] 综合分析…")
        try:
            synthesis = llm.synthesize(figure_explanations, abstract, intro, full_text, s_base, s_key, s_model)
            result["diff_count"] = len(synthesis.get("differences", []))
            result["key_point_count"] = len(synthesis.get("key_points", []))
            result["synthesis"] = {"figure_logical_structure": synthesis.get("figure_logical_structure", ""),
                                   "differences": synthesis.get("differences", []),
                                   "key_points": synthesis.get("key_points", [])}
            say(f"  差异 {result['diff_count']} 条 | 要点 {result['key_point_count']} 条")
        except Exception as e:
            result["synthesis"] = {"error": str(e)}
            say(f"  综合分析失败:{str(e)[:80]}")

        # 训练模式:只落本地 HTML,不写 Zotero
        try:
            html = nw.build_note_html(article, figures, figure_explanations,
                                      result.get("synthesis") or {})
            html_path = os.path.join(OUT_DIR, f"{article['key']}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            result["html"] = html_path
        except Exception as e:
            result["html_error"] = str(e)

        result["status"] = "done"
        result["elapsed_s"] = round(time.time() - t0)
        say(f"完成 {result['elapsed_s']}s")
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
    return result


def main() -> None:
    keys = sys.argv[1:]
    if not keys:
        print("用法: python batch_validate.py KEY1 KEY2 ...")
        sys.exit(1)
    print(f"训练集 {len(keys)} 篇: {keys}", flush=True)
    summary = []
    for k in keys:
        print(f"\n=== {k} ===", flush=True)
        r = run_one(k)
        r["input_key"] = k
        summary.append(r)
        with open(os.path.join(OUT_DIR, f"{k}.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print(f"=== {k} -> {r['status']} ===", flush=True)
    print("\n\n===== 汇总 =====", flush=True)
    for r in summary:
        a = r.get("article", {})
        print(f"{r['input_key']} | {r['status']} | {a.get('journal','')} | "
              f"图 {r.get('figures_count', 0)} | 引言 {r.get('intro_chars', 0)} | "
              f"差异 {r.get('diff_count', 0)} | 要点 {r.get('key_point_count', 0)} | "
              f"{r.get('elapsed_s', 0)}s | {r.get('error','')[:60]}", flush=True)


if __name__ == "__main__":
    main()
