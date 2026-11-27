#!/usr/bin/env bash
# Bedrock でのベースライン・改善版の評価を、事前の確認から集計・図までまとめて実行する。
#
# 使い方（evals/promptfoo/ で）:
#   ./run_bedrock_eval.sh <配布の eval_posts.jsonl> [--repeat N] [--providers 'regex'] [--out DIR]
#
#   例: ./run_bedrock_eval.sh ~/data/eval_posts.jsonl --repeat 3
#       ./run_bedrock_eval.sh ~/data/eval_posts.jsonl --providers '^bedrock-baseline$'   # 1 方式だけ
#
# 環境変数:
#   PYTHON          判定のコードの依存が入った Python（既定: ../.venv/bin/python、無ければ python）
#   AWS_PROFILE 等  Bedrock を呼べる認証情報（講座の AWS 環境）。BEDROCK_STUB=1 なら不要
#   BEDROCK_MODEL_ID 事前の確認で呼ぶモデル（既定: apac.amazon.nova-lite-v1:0）
#
# 出力（DIR、既定 output/run_<日時>）: results.json（promptfoo）、<方式>_<分割>.md / .json（summarize.py）、
#   test の JH 除外の集計、report/（比較表と図）。投稿の本文を含むので commit しない（output/ は .gitignore 済み）
set -euo pipefail

cd "$(dirname "$0")"
DATASET="${1:?配布の eval_posts.jsonl（または .csv）のパスを指定してください}"
shift
REPEAT=1
PROVIDERS='.*'
OUT="output/run_$(date +%Y%m%d_%H%M%S)"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repeat) REPEAT="$2"; shift 2 ;;
    --providers) PROVIDERS="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "不明な引数: $1" >&2; exit 2 ;;
  esac
done

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x ../.venv/bin/python ]]; then PY=../.venv/bin/python; elif [[ -x ../../.venv/bin/python ]]; then PY=../../.venv/bin/python; else PY=python; fi
fi
export PROMPTFOO_PYTHON="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
export PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1
mkdir -p "$OUT"
echo "== Python: $("$PY" -V)  Node: $(node -v)  出力: $OUT"

# 1. 事前の確認（AWS の認証と、Bedrock のモデルを 1 回だけ呼べるか）。失敗したら評価を始めない
if [[ "${BEDROCK_STUB:-}" == "1" ]]; then
  echo "== BEDROCK_STUB=1: AWS を呼ばない（評価の仕組みの確認だけ。精度の値には意味がない）"
else
  echo "== 事前の確認: AWS の認証と Bedrock の呼び出し"
  "$PY" - <<'EOF'
import os, sys, boto3
sys.path.insert(0, "../../lambda")
from common.bedrock_client import BedrockChatClient
ident = boto3.client("sts").get_caller_identity()
print(f"   AWS アカウント: {ident['Account']}（{ident['Arn'].split('/')[-1]}）")
model = os.environ.get("BEDROCK_MODEL_ID", "apac.amazon.nova-lite-v1:0")
rf = {"type": "json_schema", "json_schema": {"name": "ping", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}}
r = BedrockChatClient(max_retries=1).chat.completions.create(model=model, messages=[{"role": "user", "content": "ok を true で返して"}], response_format=rf)
print(f"   Bedrock {model}: 応答 {r.choices[0].message.content}（入力 {r.usage.prompt_tokens}・出力 {r.usage.completion_tokens} トークン）")
EOF
fi

# 2. テストケース（提供のデータ＋追加のデータ）
"$PY" load_dataset.py "$DATASET" ../datasets_extra/extra_posts.jsonl --output datasets/testcases.yaml

# 3. 評価（不合格の投稿があると promptfoo の終了コードは 0 でないので、ここでは止めない）
set +e
npx promptfoo eval -c promptfooconfig.bedrock.yml --filter-providers "$PROVIDERS" --repeat "$REPEAT" \
  --no-cache -o "$OUT/results.json" 2>&1 | tee "$OUT/promptfoo.log" | tail -5
set -e
[[ -s "$OUT/results.json" ]] || { echo "結果のファイルが無い（promptfoo.log を確認）" >&2; exit 1; }

# 4. 集計（方式 × 分割。test は JH を除いた値も）
LABELS=$("$PY" -c "import json,sys; r=json.load(open(sys.argv[1]))['results']['results']; print(' '.join(sorted({(x.get('provider') or {}).get('label','') for x in r})))" "$OUT/results.json")
for p in $LABELS; do
  for s in dev test holdout all; do
    "$PY" summarize.py "$OUT/results.json" --provider "$p" --split "$s" --json "$OUT/${p}_${s}.json" > "$OUT/${p}_${s}.md"
  done
  "$PY" summarize.py "$OUT/results.json" --provider "$p" --split test --exclude-prefix JH \
    --json "$OUT/${p}_test_noJH.json" > "$OUT/${p}_test_noJH.md"
  echo "== $p（全体）"; grep -E "^\| 全体" "$OUT/${p}_all.md" || true
done

# 5. 説明資料の表と図（全体・test・holdout）
for s in all test holdout; do
  args=()
  for p in $LABELS; do args+=(--label "$p" "$OUT/${p}_${s}.json"); done
  "$PY" report.py --openai-reference "${args[@]}" --out "$OUT/report_$s" > /dev/null
done
echo "== 完了: $OUT（比較表は report_*/compare.md、図は report_*/*.png）"
