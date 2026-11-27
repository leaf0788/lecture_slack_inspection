#!/usr/bin/env python3
"""promptfoo の結果（eval -o の JSON）を、発注書 PJT04 の指標で言語別に集計する。

使い方（evals/promptfoo/ で）:
    python summarize.py output/results.json [output/results_2.json ...] [--csv output/summary.csv]

- 複数の結果のファイル、または --repeat N で流した結果を渡すと、同じ投稿の判定がそろった割合（安定性）も出す。
  率は 1 回目の判定で出し、2 回目以降は安定性と回ごとの再現率・適合率にだけ使う
- 出力は集計値と投稿の id だけ（投稿の本文は出さない）。指標の定義は evals/README.md の「何を計測するか」
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

MAIN_LANGS = ["ja", "en", "zh", "ar", "fr", "sw"]
TAGS = ["category", "degree", "intent", "noise", "length"]


def lang_group(lang: str) -> str:
    return lang if lang in MAIN_LANGS else "mixed"


def provider_label(it: dict) -> str:
    p = it.get("provider") or {}
    return (p.get("label") or p.get("id") or "") if isinstance(p, dict) else str(p)


def load_runs(paths: list[Path], provider: str | None = None) -> list[dict[str, dict]]:
    """結果を「回」ごとの {id: 1 件} に分ける（同じファイルの中の繰り返しも別の回にする）。
    1 つの結果に複数のプロバイダー（方式）があるときは provider（label）で 1 つに絞る"""
    by_run: dict[tuple[int, int], dict[str, dict]] = defaultdict(dict)
    for fi, p in enumerate(paths):
        items = json.loads(p.read_text(encoding="utf-8"))["results"]["results"]
        labels = sorted({provider_label(it) for it in items})
        if provider:
            items = [it for it in items if provider_label(it) == provider]
            if not items:
                raise SystemExit(f"{p}: プロバイダー {provider!r} の結果が無い（ある: {labels}）")
        elif len(labels) > 1:
            raise SystemExit(f"{p}: 複数の方式の結果がある {labels}。--provider で 1 つを選ぶ")
        seen: dict[str, int] = defaultdict(int)
        for it in items:
            v = it["vars"]
            k = seen[v["id"]]  # 同じファイルの中の k 回目の出現を、そのファイルの k 回目の回とする
            seen[v["id"]] += 1
            resp = it.get("response") or {}
            out = resp.get("output")
            if not isinstance(out, dict):
                out = {"error": resp.get("error") or it.get("error") or "no output"}
            by_run[(fi, k)][v["id"]] = {"vars": v, "out": out}
    return [by_run[k] for k in sorted(by_run)]


def ratio(a: int, b: int) -> str:
    return f"{a / b:.3f}" if b else "—"


def confusion(run: dict[str, dict]) -> dict[str, dict]:
    stats: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for rec in run.values():
        v, o = rec["vars"], rec["out"]
        for key in (lang_group(v["language"]), "全体"):
            s = stats[key]
            s["n"] += 1
            if o.get("error"):
                s["err"] += 1
                s["err_pos"] += int(bool(v["expected_is_violation"]))
                continue
            exp, got = bool(v["expected_is_violation"]), bool(o.get("is_violation"))
            s["tp" if exp and got else "fn" if exp else "fp" if got else "tn"] += 1
            if exp and got:
                s["art_n"] += 1
                s["art_ok"] += int(o.get("article_id") == v.get("expected_article"))
    return stats


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("--csv", type=Path, help="言語別の表を CSV にも書く")
    ap.add_argument("--split", choices=["dev", "test", "holdout", "all"], default="all",
                    help="dev（調整に使った投稿）・test（調整に使っていない投稿）・holdout（追加のデータ）だけを集計する")
    ap.add_argument("--source", help="データの出所で絞る（provided＝発注者のデータ、extra_posts など）")
    ap.add_argument("--provider", help="結果に複数の方式があるとき、集計する方式（promptfooconfig の label）")
    ap.add_argument("--json", type=Path, help="全体と言語別の指標を JSON にも書く（CI のしきい値の確認・図の作成に使う）")
    a = ap.parse_args()
    runs = load_runs(a.results, a.provider)
    if a.split != "all" or a.source:
        def keep(rec):
            v = rec["vars"]
            return ((a.split == "all" or v.get("split") == a.split)
                    and (not a.source or v.get("source", "provided") == a.source))
        runs = [{k: r for k, r in run.items() if keep(r)} for run in runs]
    first = runs[0]
    st = confusion(first)

    print(f"- 結果: {', '.join(str(p) for p in a.results)}（{len(runs)} 回分。1 回目 {len(first)} 件。"
          f"分割 {a.split}{'・出所 ' + a.source if a.source else ''}）")
    print()
    header = ["言語", "件数", "取りこぼし", "誤検知", "再現率", "適合率", "F1", "通知", "判定の失敗", "再現率（失敗を取りこぼしに数えた場合）", "条文の一致"]
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    rows = []
    for key in MAIN_LANGS + ["mixed", "全体"]:
        s = st.get(key)
        if not s:
            continue
        tp, fn, fp = s["tp"], s["fn"], s["fp"]
        rec = tp / (tp + fn) if tp + fn else 0.0
        prec = tp / (tp + fp) if tp + fp else 0.0
        f1 = f"{2 * rec * prec / (rec + prec):.3f}" if rec + prec else "—"
        row = [key, s["n"], fn, fp, ratio(tp, tp + fn), ratio(tp, tp + fp), f1, tp + fp, s["err"],
               ratio(tp, tp + fn + s["err_pos"]), f"{s['art_ok']}/{s['art_n']}"]
        rows.append(row)
        print("| " + " | ".join(str(x) for x in row) + " |")
    # 言語による差（課題 2）: 主な 6 言語の再現率・適合率の最大と最小の差
    def _r(s, kind):
        tp, fn, fp = s["tp"], s["fn"], s["fp"]
        d = tp + fn if kind == "rec" else tp + fp
        return tp / d if d else None
    gaps = {}
    for kind, label in (("rec", "再現率"), ("prec", "適合率")):
        vals = {k: _r(st[k], kind) for k in MAIN_LANGS if k in st and _r(st[k], kind) is not None}
        if vals:
            hi, lo = max(vals, key=vals.get), min(vals, key=vals.get)
            gaps[kind] = vals[hi] - vals[lo]
            print(f"\n- 言語による{label}の差: 最大 {hi} {vals[hi]:.3f} − 最小 {lo} {vals[lo]:.3f} = {gaps[kind]:.3f}", end="")
    print()
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        out = {"provider": a.provider, "split": a.split, "source": a.source, "runs": len(runs), "by_language": {},
               "language_gap": {"recall": gaps.get("rec"), "precision": gaps.get("prec")}}
        for key, s in st.items():
            tp, fn, fp = s["tp"], s["fn"], s["fp"]
            out["by_language"][key] = {"n": s["n"], "tp": tp, "fn": fn, "fp": fp, "tn": s["tn"], "errors": s["err"],
                                       "recall": tp / (tp + fn) if tp + fn else None,
                                       "precision": tp / (tp + fp) if tp + fp else None,
                                       "article_ok": s["art_ok"], "article_n": s["art_n"]}
        ok_ = [rec["out"] for rec in first.values() if not rec["out"].get("error")]
        lat_ = [o["latency_sec"] for o in ok_ if o.get("latency_sec") is not None]
        out["latency_sec"] = {"p50": statistics.median(lat_) if lat_ else None, "p95": pct(lat_, 0.95) if lat_ else None}
        out["cost_per_post_usd"] = (sum(o["cost_usd"] for o in ok_) / len(ok_)) if ok_ else None
        a.json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.csv:
        a.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(a.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    # 間違えた投稿（id だけ）。取りこぼしの分析のため、警告の確率と条文の候補の上位も出す
    print()
    print("**間違えた投稿（1 回目）**")
    print()
    print("| id | 正解 | 判定 | 方法 | P(warn) | 正解の条文 | 判定の条文 | 条文の候補（上位 3） |")
    print("|---|---|---|---|---|---|---|---|")
    for pid, rec in sorted(first.items()):
        v, o = rec["vars"], rec["out"]
        wrong = o.get("error") or bool(o.get("is_violation")) != bool(v["expected_is_violation"])
        art_wrong = (not o.get("error") and v["expected_is_violation"] and o.get("is_violation")
                     and o.get("article_id") != v.get("expected_article"))
        if wrong or art_wrong:
            top = ", ".join(f"{x[0]}({x[1]:.2f})" for x in (o.get("article_top3") or []))
            pw = f"{o['p_warn']:.3f}" if isinstance(o.get("p_warn"), (int, float)) else "—"
            got = "失敗" if o.get("error") else ("違反" if o.get("is_violation") else "違反なし")
            print(f"| {pid} | {'違反' if v['expected_is_violation'] else '違反なし'} | {got} | {o.get('method') or '—'} | {pw} | {v.get('expected_article') or '—'} | {o.get('article_id') or '—'} | {top or '—'} |")

    # 言語の間の一致
    by_sc: dict[str, dict[str, bool]] = defaultdict(dict)
    for rec in first.values():
        v, o = rec["vars"], rec["out"]
        if v["language"] in MAIN_LANGS and not o.get("error"):
            by_sc[v["scenario_id"]][v["language"]] = bool(o.get("is_violation"))
    full = [sc for sc, d in by_sc.items() if len(d) == len(MAIN_LANGS)]
    split = sorted(sc for sc in full if len(set(by_sc[sc].values())) > 1)
    print()
    print(f"- 言語の間の一致: 6 言語がそろったシナリオ {len(full)} 件のうち、判定が全言語で同じ {len(full) - len(split)} 件（{ratio(len(full) - len(split), len(full))}）"
          + (f"。分かれたシナリオ: {', '.join(split)}" if split else ""))

    # 条文の一貫性（課題 4）: 6 言語とも警告したシナリオで、付いた条文が全言語で同じ割合
    arts: dict[str, dict[str, str | None]] = defaultdict(dict)
    for rec in first.values():
        v, o = rec["vars"], rec["out"]
        if v["language"] in MAIN_LANGS and not o.get("error") and o.get("is_violation"):
            arts[v["scenario_id"]][v["language"]] = o.get("article_id")
    full_a = [sc for sc, d in arts.items() if len(d) == len(MAIN_LANGS)]
    varied = sorted(sc for sc in full_a if len(set(arts[sc].values())) > 1)
    print(f"- 条文の言語の間の一致: 6 言語とも警告したシナリオ {len(full_a)} 件のうち、条文が全言語で同じ {len(full_a) - len(varied)} 件"
          f"（{ratio(len(full_a) - len(varied), len(full_a))}）" + (f"。分かれたシナリオ: {', '.join(varied)}" if varied else ""))

    # 安定性（2 回以上あるとき）
    if len(runs) > 1:
        ids = set.intersection(*(set(r) for r in runs))
        same = sum(1 for i in ids if len({(r[i]['out'].get('error') is not None, r[i]['out'].get('is_violation')) for r in runs}) == 1)
        print(f"- 判定の安定性: {len(runs)} 回とも判定が同じ {same}/{len(ids)} 件（{ratio(same, len(ids))}）")
        for k, r in enumerate(runs, 1):
            s = confusion(r)["全体"]
            print(f"  - {k} 回目: 取りこぼし {s['fn']}・誤検知 {s['fp']}・判定の失敗 {s['err']}・再現率 {ratio(s['tp'], s['tp'] + s['fn'])}・適合率 {ratio(s['tp'], s['tp'] + s['fp'])}")

    # 予備経路・API の応答コード
    methods: dict[str, int] = defaultdict(int)
    statuses: dict[int, int] = defaultdict(int)
    for rec in first.values():
        methods[rec["out"].get("method") or "失敗"] += 1
        for c in rec["out"].get("calls") or []:
            if c.get("api") == "decisions":
                statuses[c.get("status")] += 1
    print(f"- 判定の方法: {dict(methods)}。Decisions API の応答コード（再試行を含む）: {dict(statuses)}")

    # 時間と費用
    ok = [rec["out"] for rec in first.values() if not rec["out"].get("error")]
    lat = [o["latency_sec"] for o in ok if o.get("latency_sec") is not None]
    if lat:
        print(f"- 判定時間（秒）: 中央値 {statistics.median(lat):.2f}、95 パーセンタイル {pct(lat, 0.95):.2f}、最大 {max(lat):.2f}")
    warn_cost = [o["cost_usd"] for o in ok if o.get("is_violation")]
    safe_cost = [o["cost_usd"] for o in ok if not o.get("is_violation")]
    if ok:
        avg = sum(o["cost_usd"] for o in ok) / len(ok)
        line = f"- 費用: このデータセットでの 1 投稿あたり平均 {avg:.6f} ドル"
        if warn_cost and safe_cost:
            cw, cs = statistics.mean(warn_cost), statistics.mean(safe_cost)
            conv = "、".join(f"違反の割合 {int(p * 100)}% なら 1 投稿 {cs * (1 - p) + cw * p:.6f} ドル・月 3 万投稿で約 {(cs * (1 - p) + cw * p) * 30000:.1f} ドル" for p in (0.01, 0.05))
            line += f"（警告した投稿 {cw:.6f}・警告しない投稿 {cs:.6f}）。本番の割合に直すと: {conv}"
        print(line)

    # 区分ごとの内訳
    print()
    print("**区分ごとの取りこぼし・誤検知（1 回目、全言語）**")
    for tag in TAGS:
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])  # 件数, 取りこぼし, 誤検知, 失敗
        for rec in first.values():
            v, o = rec["vars"], rec["out"]
            a_ = agg[v.get(tag) or "—"]
            a_[0] += 1
            if o.get("error"):
                a_[3] += 1
            elif v["expected_is_violation"] and not o.get("is_violation"):
                a_[1] += 1
            elif not v["expected_is_violation"] and o.get("is_violation"):
                a_[2] += 1
        bad = {k: x for k, x in agg.items() if x[1] or x[2] or x[3]}
        if bad:
            print(f"- {tag}: " + "、".join(f"{k}（{x[0]} 件中 取りこぼし {x[1]}・誤検知 {x[2]}" + (f"・失敗 {x[3]}" if x[3] else "") + "）" for k, x in sorted(bad.items())))
        else:
            print(f"- {tag}: 間違いなし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
