#!/usr/bin/env python3
"""PJT04 の評価用データセット（eval_posts.csv または .jsonl）を、promptfoo のテストケース（YAML）に変換する。

使い方（evals/promptfoo/ で）:
    python load_dataset.py <eval_posts.csv|eval_posts.jsonl> [追加のデータセット ...] [--output datasets/testcases.yaml]
                           [--split dev|test|all] [--limit N]

- 開発用（dev）と評価用（test）への分割: シナリオ単位で決まった形で分ける（split_of）。同じシナリオの対訳が
  dev と test に分かれないので、「dev で方針を調整 → test で確かめる」ときに test の投稿の言い換えを見ていないことになる。
  分割はシナリオの種類（ID の英字の部分: W・S・JH・X など）ごとに、scenario_id のハッシュの順に並べて前半を dev にする
  （層別。警告すべき・しない投稿の割合が dev と test でそろう）。データの順番に左右されない
- 条文の ID（violating_article）が lambda/common/data/articles.json にあるかを確かめ、無ければ止める
- 列: id・scenario_id・language・text・is_violation・violating_article・category・length・intent・degree・noise・note
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ARTICLES = REPO / "lambda" / "common" / "data" / "articles.json"
REQUIRED = ["id", "scenario_id", "language", "text", "is_violation", "violating_article"]
TAGS = ["category", "length", "intent", "degree", "noise"]


SPLIT_SALT = "pjt04-split-v1"


def _h(scenario_id: str) -> str:
    return hashlib.sha256(f"{SPLIT_SALT}:{scenario_id}".encode()).hexdigest()


def assign_splits(scenario_ids) -> dict[str, str]:
    """シナリオの種類ごとに、ハッシュの順で前半（切り捨て）を dev、残りを test にする"""
    groups: dict[str, list[str]] = {}
    for sid in set(scenario_ids):
        groups.setdefault(re.match(r"[A-Za-z]*", sid).group(0) or "_", []).append(sid)
    out = {}
    for ids in groups.values():
        ids.sort(key=_h)
        for i, sid in enumerate(ids):
            out[sid] = "dev" if i < len(ids) // 2 else "test"
    return out


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    raise ValueError(f"is_violation が真偽値でない: {v!r}")


def read_rows(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def to_testcases(rows: list[dict], article_ids: set[str]) -> list[dict]:
    # 提供のデータは dev/test に分ける。追加のデータ（O-1）は調整に一度も使わない holdout にする
    splits = assign_splits(r["scenario_id"] for r in rows if r.get("source", "provided") == "provided")
    seen = set()
    cases = []
    for i, r in enumerate(rows, 1):
        missing = [c for c in REQUIRED if c not in r]
        if missing:
            raise ValueError(f"{i} 行目: 列が無い {missing}")
        if r["id"] in seen:
            raise ValueError(f"{i} 行目: id が重複している {r['id']}")
        seen.add(r["id"])
        is_v = _bool(r["is_violation"])
        art = (r.get("violating_article") or "").strip()
        if is_v and not art:
            raise ValueError(f"{r['id']}: 警告すべき投稿なのに条文が無い")
        if not is_v and art:
            raise ValueError(f"{r['id']}: 警告しない投稿なのに条文がある")
        if art and art not in article_ids:
            raise ValueError(f"{r['id']}: 条文 {art} が articles.json に無い")
        vars_ = {
            "id": r["id"], "scenario_id": r["scenario_id"], "language": r["language"], "text": r["text"],
            "expected_is_violation": is_v, "expected_article": art,
            "split": splits.get(r["scenario_id"], "holdout"),
            "source": r.get("source", "provided"),
        }
        for t in TAGS:
            vars_[t] = r.get(t, "")
        cases.append({"description": f"{r['id']} [{'違反' if is_v else '非違反'}] {r.get('category', '')}", "vars": vars_})
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path, nargs="+", help="データセット（複数可。2 つ目以降は追加の評価用データなど）")
    ap.add_argument("--output", type=Path, default=HERE / "datasets" / "testcases.yaml")
    ap.add_argument("--split", choices=["dev", "test", "holdout", "all"], default="all")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    article_ids = {x["id"] for x in json.loads(ARTICLES.read_text(encoding="utf-8"))["articles"]}
    rows = []
    for i, path in enumerate(a.dataset):
        for r in read_rows(path):
            r.setdefault("source", "provided" if i == 0 else path.stem)
            rows.append(r)
    if a.limit:
        rows = rows[: a.limit]
    cases = to_testcases(rows, article_ids)
    if a.split != "all":
        cases = [c for c in cases if c["vars"]["split"] == a.split]
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(yaml.safe_dump(cases, allow_unicode=True, sort_keys=False), encoding="utf-8")
    n_v = sum(1 for c in cases if c["vars"]["expected_is_violation"])
    n_split = {k: sum(1 for c in cases if c["vars"]["split"] == k) for k in ("dev", "test", "holdout")}
    print(f"{len(cases)} 件（警告すべき {n_v}・警告しない {len(cases) - n_v}。"
          f"dev {n_split['dev']}・test {n_split['test']}・holdout {n_split['holdout']}）を {a.output} に書いた")
    return 0


if __name__ == "__main__":
    sys.exit(main())
