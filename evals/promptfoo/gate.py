#!/usr/bin/env python3
"""評価の結果（summarize.py --json の出力）が、しきい値（evals/thresholds.json）を下回っていないかを確かめる。CI 用。

使い方: python gate.py output/summary.json ../thresholds.json [--profile holdout]
- 取りこぼし（再現率）を最優先で見る。下回ったら終了コード 1（CI を落とす）
- しきい値は、改善版を同じデータで測った値から決めて commit する（下げるときは理由を PR に書く）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def check(summary: dict, th: dict) -> list[str]:
    fails = []
    total = summary["by_language"].get("全体", {})
    rec, prec = total.get("recall"), total.get("precision")
    if "min_recall" in th and (rec is None or rec < th["min_recall"]):
        fails.append(f"再現率 {rec} < {th['min_recall']}（取りこぼしが増えた）")
    if "max_fn" in th and total.get("fn", 0) > th["max_fn"]:
        fails.append(f"取りこぼし {total.get('fn')} 件 > {th['max_fn']} 件")
    if "min_precision" in th and (prec is None or prec < th["min_precision"]):
        fails.append(f"適合率 {prec} < {th['min_precision']}（誤検知が増えた）")
    if "max_errors" in th and total.get("errors", 0) > th["max_errors"]:
        fails.append(f"判定の失敗 {total.get('errors')} 件 > {th['max_errors']} 件")
    gap = (summary.get("language_gap") or {}).get("recall")
    if "max_language_recall_gap" in th and gap is not None and gap > th["max_language_recall_gap"]:
        fails.append(f"言語による再現率の差 {gap:.3f} > {th['max_language_recall_gap']}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("summary", type=Path)
    ap.add_argument("thresholds", type=Path)
    ap.add_argument("--profile", default="holdout")
    a = ap.parse_args()
    summary = json.loads(a.summary.read_text(encoding="utf-8"))
    th = json.loads(a.thresholds.read_text(encoding="utf-8"))[a.profile]
    fails = check(summary, th)
    total = summary["by_language"].get("全体", {})
    print(f"再現率 {total.get('recall')}・適合率 {total.get('precision')}・取りこぼし {total.get('fn')}・"
          f"誤検知 {total.get('fp')}・失敗 {total.get('errors')}（しきい値 {a.profile}: {th}）")
    for f in fails:
        print(f"NG: {f}")
    if not fails:
        print("OK: しきい値を満たしている")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
