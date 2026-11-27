# 最終課題での取り組み（PJT04 Slack 監視 App の違反検知の精度向上と多言語対応）

> AI エンジニアリング実践講座 2026 最終課題。元の README は、この節の後ろにそのまま残しています。
> 説明資料（PDF）: `docs/final_report/`（下書きと図の作り方）

## ① 取り組んだ課題と内容

| 課題 | 区分 | 取り組み |
|---|---|---|
| 課題 1 誤検知 | 必須 | 判定を「行為の分類」に作り替え、**投稿者自身が行っている・求めている行為だけ**を数える（注意・報告・可否の質問・引用は数えない）。投稿者の立場（`stance`）を先に答えさせる |
| 課題 2 言語による差 | 必須 | 判定の前に**日本語での言い換え（`gist_ja`）**を出させ、方針（日本語）と同じ言語で判断させる。外部グループの**招待リンクは言語によらずコードで検出**し、取りこぼしの安全網にする |
| 課題 3 費用 | 任意 | 2 回の呼び出し（判定 → 条文の選択）を **1 回** にし、条文はコードで決める。静的な方針は Bedrock の**プロンプトキャッシュ**に載せる。Nova Micro との比較も同じ設定で測れる |
| 課題 4 条文の一貫性 | 任意（O-2） | LLM には条文 ID ではなく**決まった行為の値（20 種）**を選ばせ、条文は**コードの表**（`V2_ACT_TO_ARTICLE`。運営の `article_guide.txt` と同じ対応）で付ける。同じ行為には必ず同じ条文 |
| 課題 5 投稿の増加 | 任意（O-8） | Bedrock の一時的なエラーを**指数バックオフ＋ジッター**で再試行し、混雑が続けば**別のモデル（別のクォータ）**に切り替える。既存システムの問題点と改善案は `docs/final_report/system_review.md` |
| O-3 プロンプトインジェクション | 任意 | 投稿を「投稿から決まるタグ」で囲んでデータとして渡し、閉じタグの偽装を無効化。判定を操作する指示があれば**それ自体を警告**（`addresses_moderator`） |
| O-4 判定できなかったとき | 任意 | LLM の失敗を黙って「違反なし」にせず、**「判定できませんでした」として high で運営に通知**（`method=Undetermined`）。招待リンクがあれば LLM なしで警告 |
| O-1 評価用データの追加 | 任意 | 提供のデータに無い種類の架空の投稿 **10 シナリオ × 6 言語 = 60 件**（`evals/datasets_extra/`） |
| O-5 評価の自動化 | 任意 | `.github/workflows/eval.yml`。プロンプト・判定の処理を変えた PR で評価が走り、**再現率などがしきい値を下回ると落ちる** |

## ② 判定方式の変更点

`DETECTION_MODE` に 2 つの方式を追加しました（既存の方式・テストはそのまま）。

| 方式 | 内容 | LLM の呼び出し |
|---|---|---|
| `bedrock`（ベースライン、M-3） | 現行の新方式の経路 B（「警告すべきか＋条文の候補 3 つ」→「条文を 1 つ選ぶ」）を、**ロジックを変えずに** Bedrock で動かす。`common/bedrock_client.py` が `chat.completions.create` と同じ形で Converse API を呼ぶ（JSON スキーマを 1 つのツールにして `toolChoice` で強制） | 警告なら 2 回、なければ 1 回 |
| `bedrock_v2`（改善版、M-4・M-5） | 1 回の呼び出しで `language → gist_ja → stance → acts → addresses_moderator → decision → reason` を構造化して返させ、**警告の判断と条文はコードで決める**（`violation_detector.py` の `_judge_by_structured`） | 1 回 |

改善版の警告の決め方（取りこぼしを避ける側に倒す）:

```
警告 = (モデルの decision が warn) または (acts が 1 つ以上) または (判定を操作する指示) または (外部グループの招待リンク)
条文 = acts のうち最も具体的な行為 → V2_ACT_TO_ARTICLE の表（ワークスペース独自のルールはそのルールの ID）
```

本番での切り替え（CDK のパラメータ）: `LlmProvider=bedrock`、`DetectionMode=bedrock_v2`、`BedrockModelId`（既定 `apac.amazon.nova-lite-v1:0`）、`BedrockFallbackModelId`（既定 `apac.amazon.nova-micro-v1:0`）。`LlmProvider=openai`（既定）なら今までどおり。

## ③ 評価の実行手順

```bash
# 準備（evals/README.md の「準備」と同じ。OpenAI のキーは不要）
cd evals && npm install && cd ..
python -m venv evals/.venv && evals/.venv/bin/pip install -r lambda/requirements.txt pyyaml matplotlib
export AWS_PROFILE=<講座の AWS 環境のプロファイル>   # Bedrock を呼べる認証情報

# テストケース（提供のデータ 354 件＋追加のデータ 60 件）
cd evals/promptfoo
../.venv/bin/python load_dataset.py <配布のデータ>/eval_posts.jsonl ../datasets_extra/extra_posts.jsonl
export PROMPTFOO_PYTHON=$PWD/../.venv/bin/python PROMPTFOO_DISABLE_TELEMETRY=1

# ベースラインと改善版を同じデータで（安定性も見るなら --repeat 3）
npx promptfoo eval -c promptfooconfig.bedrock.yml --no-cache -o output/bedrock.json

# 方式・分割ごとの集計（dev＝調整に使った投稿、test＝使っていない投稿、holdout＝追加のデータ）
for p in bedrock-baseline bedrock-v2; do for s in dev test holdout all; do
  ../.venv/bin/python summarize.py output/bedrock.json --provider $p --split $s --json output/${p}_${s}.json > output/${p}_${s}.md
done; done

# 説明資料の表と図
../.venv/bin/python report.py --openai-reference --label ベースライン output/bedrock-baseline_all.json \
  --label 改善版 output/bedrock-v2_all.json --out output/report
```

まとめて実行するスクリプト（事前の確認 → 評価 → 方式×分割の集計・test の JH 除外の集計 → 表と図）:

```bash
cd evals/promptfoo
./run_bedrock_eval.sh <配布のデータ>/eval_posts.jsonl --repeat 3     # 結果は output/run_<日時>/
```

テスト: `pytest tests/`（Bedrock はモックで、AWS なしで通る。CI と同じ）。リポジトリの直下で引数なしの `pytest` を実行すると、
元から古くなっている `infra/tests/`（aws-cdk が必要で、上流の ef070f5 の時点で 3 件失敗する）も集めてしまうので、`tests/` を指定する。
AWS なしで評価の仕組みだけ確かめるときは `BEDROCK_STUB=1`（`BEDROCK_STUB=1 ./run_bedrock_eval.sh ...` も可）。

## ④ 追加した評価用データ

`evals/datasets_extra/extra_posts.jsonl`（作り方は `build_extra.py`）。架空の投稿 10 シナリオ × 6 言語（ja・en・zh・ar・fr・sw）= 60 件。警告すべき 30・警告しない 30。

- 警告すべき: 外部企業の有償インタビューの募集（11-xiii）、物品の売買（11-x）、判定 AI への指示＋SNS のフォロワー集め（11-xiii）、締切前の解答の要求（course-8-iv）、アカウントの共有の告知（reg-3）
- 警告しない（紛らわしい）: 研究室の公式イベントの案内、受けた勧誘の報告、ブログに書いてよいかの質問、「ブロック」「スクレイピング」を含む技術の質問、スライドの箇所を示すだけのヒント
- 正解の付け方: 運営の判断方針（`judge_decision.txt`）と条文の選び方（`article_guide.txt`）に照らして付け、根拠を `note` に書いた
- 使い方: 判定方式の調整には**一度も使わない**（`holdout`）。提供のデータは**シナリオ単位**で dev/test に分け（同じシナリオの対訳が両方に入らない）、方針の調整は dev の間違いだけを見て行う

## ⑤ 特に評価してほしいポイント

1. **条文を LLM に選ばせず、行為の分類からコードで付ける設計**（課題 4 と課題 3 を同時に解く。2 回目の呼び出しが不要）
2. **取りこぼしを最優先にした判断の合成**（モデルの判断・行為・判定の操作・招待リンクの「または」）と、**失敗を「判定できなかった」として通知**する扱い
3. **過学習を避ける評価の設計**: シナリオ単位の dev/test 分割、調整に使わない追加データ（holdout）、言語による差の指標、CI でのしきい値
4. 既存の評価の仕組み（`harness.py`・`summarize.py`）を壊さず、Bedrock と改善版を**同じデータ・同じ指標**で並べられるようにしたこと

## 結果（測定値）

> 講座の AWS 環境で ③ を実行して得た値を記入する。数値は `output/report/compare.md` からそのまま転記する

| 方式 | 取りこぼし | 誤検知 | 再現率 | 適合率 | 言語による再現率の差 | 1 投稿あたりの費用 |
|---|---|---|---|---|---|---|
| OpenAI 版（発注書の値） | 9 | 48 | 0.941 | 0.750 | — | 0.000475 ドル |
| Bedrock ベースライン | （測定後に記入） | | | | | |
| 改善版（bedrock_v2） | （測定後に記入） | | | | | |

コードだけで確かめた値: 招待リンクの検出（`signals.py`）は、提供のデータ 354 件で **20 件を検出し、すべて警告すべき投稿（誤検知 0）**。追加のデータ 60 件では検出 0（誤検知 0）。

## 使ったライブラリ・サービス

| 名前 | 用途 | ライセンス・規約 |
|---|---|---|
| Amazon Bedrock（Amazon Nova Micro / Lite） | 判定の LLM | AWS のサービス規約 |
| boto3 / botocore | Bedrock の呼び出し | Apache-2.0 |
| promptfoo | 評価 | MIT |
| matplotlib | 説明資料の図 | Matplotlib License（PSF 系） |
| PyYAML | テストケースの変換 | MIT |
| pytest / pytest-mock | テスト | MIT |

（既存の依存は `requirements.txt`・`lambda/requirements.txt`・`evals/package.json`）

---

## ディレクトリ構成

```
lecture_slack_inspection/
├── .github/               # GitHub Actions ワークフロー
├── contracts/             # API仕様・スキーマ・テストデータ
│   ├── specs/             # Markdown仕様書
│   ├── schemas/           # JSONスキーマ
│   └── fixtures/          # テスト用JSONフィクスチャ
├── evals/                 # プロンプト評価（promptfoo）
│   └── promptfoo/         # 評価設定・テストケース
├── infra/                 # AWS CDK インフラ定義
│   ├── stacks/            # CDKスタック
│   └── tests/             # インフラテスト
├── lambda/                # Lambda アプリケーション
│   ├── app_alert/         # 警告通知（承認ボタン）
│   ├── app_batch/         # バッチ処理（定期スキャン）
│   ├── app_inspect/       # 投稿検査（Events API）
│   ├── app_oauth/         # OAuth認証
│   ├── app_remind/        # リマインダー通知
│   ├── common/            # 共通モジュール
│   └── Dockerfile         # Lambdaコンテナ定義
├── prompt_engineering/    # プロンプトエンジニアリング
├── tests/                 # テストコード
│   ├── conftest.py        # pytest設定
│   ├── unit/              # ユニットテスト
│   └── integration/       # 統合テスト
├── requirements.txt       # 共通依存
└── README.md              # 本ファイル
```

---

## Lambda アプリケーション一覧

| Lambda | 役割 | トリガー |
|--------|------|----------|
| `app_inspect` | 投稿検査・違反判定 | Slack Events API |
| `app_alert` | 警告通知（承認ボタン） | Slack Interactive |
| `app_remind` | リマインダー送信 | スケジュール |
| `app_batch` | 一括スキャン処理 | スケジュール |
| `app_oauth` | ワークスペース認証 | Slack OAuth |

---

## 詳細ディレクトリ

### `contracts/`
API仕様・スキーマ・テストデータを管理。

| ディレクトリ | 説明 |
|-------------|------|
| `specs/` | Markdown仕様書（Block Kit、Lambda入力） |
| `schemas/` | JSONスキーマ（データ検証） |
| `fixtures/` | テスト用JSONデータ |

### `evals/`
プロンプト評価・改善。

- `promptfoo/`: 評価設定とテストケース
- 評価対象: `lambda/app_inspect/services/data/prompts/`

### `infra/`
AWSインフラ（CDK）。

- `stacks/`: API Gateway、Lambda、IAM定義
- `tests/`: CDKユニットテスト

### `lambda/`
アプリケーションコード（Bot本体）。

#### `app_inspect/` - 投稿検査
| ファイル | 説明 |
|----------|------|
| `handler.py` | Lambdaエントリーポイント |
| `services/inspection_flow.py` | 検出フロー制御 |
| `services/violation_detector.py` | 違反検出 |
| `services/moderation.py` | モデレーション処理 |
| `services/violation_transition.py` | 状態遷移 |
| `components/slack_builder.py` | Slackメッセージ構築 |
| `components/slack_event_parser.py` | イベント解析 |

#### `app_alert/` - 警告通知
| ファイル | 説明 |
|----------|------|
| `handler.py` | Lambdaエントリーポイント |
| `services/actions.py` | ボタンアクション処理 |

#### `app_remind/` - リマインダー
| ファイル | 説明 |
|----------|------|
| `handler.py` | Lambdaエントリーポイント |
| `services/reminder.py` | リマインダーロジック |

#### `app_batch/` - バッチ処理
| ファイル | 説明 |
|----------|------|
| `handler.py` | Lambdaエントリーポイント |
| `services/scanner.py` | スキャンロジック |

#### `app_oauth/` - OAuth認証
| ファイル | 説明 |
|----------|------|
| `handler.py` | Lambdaエントリーポイント |

#### `common/` - 共通モジュール
| ファイル | 説明 |
|----------|------|
| `notion_client.py` | Notion APIクライアント |
| `slack_utils.py` | Slackユーティリティ |
| `template_manager.py` | テンプレート管理 |
| `secret_manager.py` | シークレット管理 |
| `observability.py` | ログ・メトリクス |
| `health.py` | ヘルスチェック |

### `tests/`
テストコード。

| ディレクトリ | 説明 |
|-------------|------|
| `conftest.py` | pytest設定・フィクスチャー |
| `unit/` | ユニットテスト |
| `integration/` | 連携テスト・契約検証 |

---

### プロンプト更新
- 本番: `lambda/app_inspect/services/data/prompts/`
- 評価: `evals/promptfoo/`

### テスト追加
- ユニット: `tests/unit/test_<app>.py`
- 統合: `tests/integration/`
- 契約: `tests/integration/test_a_to_b_flow_contract.py`

### ログ確認
- `lambda/common/observability.py`で構造化ログ
- CloudWatch Logsで参照