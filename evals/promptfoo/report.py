#!/usr/bin/env python3
"""説明資料の図と表を、評価の結果（summarize.py --json の出力）から作る。

使い方（evals/promptfoo/ で）:
    python report.py --label OpenAI版 openai.json --label ベースライン base.json --label 改善版 v2.json --out output/report
    → output/report/compare.md（言語別の比較表）、recall_by_lang.png・precision_by_lang.png・errors_by_lang.png

- 図は matplotlib（pip install matplotlib）。日本語のフォントがあれば使う（Noto Sans CJK JP・IPAGothic など）
- 「OpenAI版」は発注書の 4. の値（全体の取りこぼし 9・誤検知 48・再現率 0.941・適合率 0.750）しか無いので、
  --openai-reference を付けると全体の行だけを表に足す（言語別の値は無い）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

LANGS = ["ja", "en", "zh", "ar", "fr", "sw"]
OPENAI_REFERENCE = {"fn": 9, "fp": 48, "recall": 0.941, "precision": 0.750, "cost_per_post_usd": 0.000475}


def _fmt(x, nd=3):
    return "—" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def table(runs: list[tuple[str, dict]], openai_ref: bool) -> str:
    lines = ["| 方式 | 言語 | 件数 | 取りこぼし | 誤検知 | 再現率 | 適合率 | 判定の失敗 | 条文の一致 |", "|---|---|---|---|---|---|---|---|---|"]
    if openai_ref:
        r = OPENAI_REFERENCE
        lines.append(f"| OpenAI版（発注書の値） | 全体 | 354 | {r['fn']} | {r['fp']} | {r['recall']:.3f} | {r['precision']:.3f} | — | — |")
    for label, s in runs:
        for key in LANGS + ["mixed", "全体"]:
            d = s["by_language"].get(key)
            if not d:
                continue
            lines.append(f"| {label} | {key} | {d['n']} | {d['fn']} | {d['fp']} | {_fmt(d['recall'])} | {_fmt(d['precision'])} | "
                         f"{d['errors']} | {d['article_ok']}/{d['article_n']} |")
    lines += ["", "| 方式 | 言語による再現率の差 | 言語による適合率の差 | 判定時間 中央値（秒） | 95 パーセンタイル（秒） | 1 投稿あたりの費用（ドル） |",
              "|---|---|---|---|---|---|"]
    for label, s in runs:
        g, lat = s.get("language_gap") or {}, s.get("latency_sec") or {}
        lines.append(f"| {label} | {_fmt(g.get('recall'))} | {_fmt(g.get('precision'))} | {_fmt(lat.get('p50'), 2)} | "
                     f"{_fmt(lat.get('p95'), 2)} | {_fmt(s.get('cost_per_post_usd'), 6)} |")
    return "\n".join(lines) + "\n"


def charts(runs: list[tuple[str, dict]], out: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for name in ("Noto Sans CJK JP", "IPAGothic", "IPAexGothic", "Hiragino Sans", "Yu Gothic"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    paths = []
    keys = LANGS + ["全体"]
    width = 0.8 / max(len(runs), 1)
    for metric, title, ylim in (("recall", "言語別の再現率（取りこぼしの少なさ）", (0, 1.05)),
                                ("precision", "言語別の適合率（誤検知の少なさ）", (0, 1.05)),
                                ("fn", "言語別の取りこぼしの件数", None),
                                ("fp", "言語別の誤検知の件数", None)):
        fig, ax = plt.subplots(figsize=(8, 3.9), dpi=150)
        for i, (label, s) in enumerate(runs):
            vals = [(s["by_language"].get(k) or {}).get(metric) or 0 for k in keys]
            xs = [j + (i - (len(runs) - 1) / 2) * width for j in range(len(keys))]
            bars = ax.bar(xs, vals, width, label=label)
            ax.bar_label(bars, labels=[f"{v:.2f}" if isinstance(v, float) else str(v) for v in vals], fontsize=6, padding=1)
        ax.set_xticks(range(len(keys)), keys)
        ax.set_title(title)
        if ylim:
            ax.set_ylim(*ylim)
        ax.spines[["top", "right"]].set_visible(False)
        ax.legend(fontsize=8, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=len(runs))
        fig.tight_layout()
        p = out / f"{metric}_by_lang.png"
        fig.savefig(p)
        plt.close(fig)
        paths.append(p)
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", action="append", nargs=2, metavar=("LABEL", "SUMMARY_JSON"), required=True)
    ap.add_argument("--out", type=Path, default=Path("output/report"))
    ap.add_argument("--openai-reference", action="store_true", help="発注書の OpenAI 版の値（全体）を表に足す")
    ap.add_argument("--no-charts", action="store_true")
    a = ap.parse_args()
    runs = [(label, json.loads(Path(p).read_text(encoding="utf-8"))) for label, p in a.label]
    a.out.mkdir(parents=True, exist_ok=True)
    md = table(runs, a.openai_reference)
    (a.out / "compare.md").write_text(md, encoding="utf-8")
    print(md)
    if not a.no_charts:
        for p in charts(runs, a.out):
            print(f"図: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
