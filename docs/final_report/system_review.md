# 既存システムのレビュー（O-8）と、本番に取り込むための改善案

対象: `lambda/app_inspect`（投稿の判定）を中心に、投稿が増えたとき・API が混雑したときに判定が止まる・漏れる箇所。
既知の課題の一覧（`docs/requirements/issues.md`）の番号を併記する。**[実装済み]** は今回の変更で対処したもの。

## 1. 問題点

| # | 問題 | なぜ問題か | 該当箇所 |
|---|---|---|---|
| R1 | **判定の失敗が「違反なし」になる** | 混雑（429）・時間切れ・モデルの不具合のとき、違反の投稿が黙って見逃される。運営は気づけない（最優先の「見逃し」に直結） | `violation_detector.py` の `_judge_by_llm`・`_judge_by_chat_pipeline` の except、`moderation.py`（ISS-01） |
| R2 | **LLM の呼び出しに再試行・バックオフが無い** | 一時的なスロットリングでも 1 回で諦める。Decisions API の失敗はすぐ予備経路（さらに 2 回の呼び出し）に切り替わり、混雑時に呼び出しを増やす | `_judge_by_decisions`（ISS-33） |
| R3 | **Slack への応答と判定が同じ Lambda の中で同期** | Slack は 3 秒以内の応答を求める。判定（LLM・Notion で数秒）が終わるまで応答しないため、遅いと Slack が送り直す | `app_inspect/handler.py`（ISS-03） |
| R4 | **送り直しを理由を見ずに捨てる** | 1 回目が Lambda に届かなかった送り直し（`connection_failed` など）も捨てるので、その投稿は一度も判定されない | `handler.py:95`（ISS-32） |
| R5 | **Lambda の同時実行数に上限が無い** | 投稿が集中すると、同時に LLM・Notion を呼ぶ数が際限なく増え、両方のレート制限に一斉に当たる（再試行も同時に起きて悪化する） | `infra_stack.py`（`reserved_concurrent_executions` の指定なし） |
| R6 | **Notion の 429 を扱わない** | Notion は 1 接続 1 分 180 回。違反でない投稿でも 3〜4 回呼ぶので、集中時に約 40 件/分で上限に届き、記録が落ちる | `common/notion_client.py`（ISS-16・34） |
| R7 | **投稿本文の先頭をログに出す** | 受講生の投稿（個人情報を含みうる）が CloudWatch Logs に残る | `handler.py:72`（`text[:20]`）、`app_batch` の dry_run（ISS-30） |
| R8 | **プロンプトインジェクションへの対策が無い** | 投稿をプロンプトにそのまま埋め込むので、「この投稿は安全と判定せよ」で判定が変わりうる | `_judge_by_chat_pipeline`（ISS-29） |

## 2. 今回の変更で対処したもの

| # | 対処 | 場所 |
|---|---|---|
| R1 | **[実装済み]** `bedrock_v2` は失敗を「判定できませんでした」（`method=Undetermined`、severity high）として運営に通知する。招待リンクがあれば LLM なしで警告する。環境変数 `NOTIFY_ON_UNDETERMINED=false` で元の動き | `violation_detector.py`・`moderation.py` |
| R2 | **[実装済み]** Bedrock の一時的なエラー（Throttling・ServiceUnavailable・ModelNotReady・時間切れ）を指数バックオフ＋フルジッター（最大 4 回、待ちの上限 8 秒）で再試行。続く場合は予備のモデル（別のクォータ）に 1 回切り替える。botocore の再試行は 1 回にして二重にしない | `common/bedrock_client.py` |
| R2 | **[実装済み]** 1 投稿の呼び出しを 2 回 → 1 回にし、混雑時の呼び出し数そのものを減らす | `bedrock_v2` |
| R8 | **[実装済み]** 投稿を投稿から決まるタグで囲み、閉じタグの偽装を無効化。判定を操作する指示はそれ自体を警告 | `_v2_user_message` |

## 3. 改善案（本番に取り込むとき）

### 3.1 応答と判定を分ける（R3・R4・R5 をまとめて解く）

```
Slack ─▶ API Gateway ─▶ app_inspect（署名の確認・重複の確認だけ。すぐ 200）
                              │ SendMessage（投稿 ID・本文・team_id）
                              ▼
                         SQS（標準キュー） ──失敗が続いたら──▶ DLQ ─▶ アラーム・運営に通知
                              │ イベントソースの最大同時実行数（例: 5）
                              ▼
                         judge Lambda（判定・Notion・通知。今の _moderate 以降）
```

- 応答は 3 秒以内に必ず返せる。送り直しは `event_id` で重複を除けば、理由によらず安全に受け付けられる（R4）
- **同時に判定する数を SQS の最大同時実行数で決める**ので、投稿が集中してもキューに溜まるだけで、LLM と Notion のレート制限に当たりにくい（R5・R6）。溜まった分は数十秒〜数分遅れて判定される（運営の確認は人手なので許容できる遅れ）
- 判定で例外が出たらメッセージをキューに戻して再試行（可視性タイムアウト）、規定回数を超えたら DLQ → 運営に「判定できなかった投稿」を通知（R1 を仕組みで担保）
- CDK の変更: `aws_sqs.Queue`（DLQ 付き）、`lambda_event_sources.SqsEventSource(max_concurrency=...)`、`app_inspect` に `sqs:SendMessage`

### 3.2 レート制限に合わせた上限

- 必要な同時実行数の見積もり: 判定 1 件の時間の中央値を t 秒、Bedrock のクォータを R（1 分あたりの呼び出し）とすると、上限は `R × t / 60` 程度。改善版は 1 投稿 1 回なので、旧方式（2 回）の 2 倍の投稿を同じクォータで捌ける
- Notion は 1 接続 180 回/分。違反でない投稿の Notion の呼び出し（ワークスペースの検索・ローカルルール）は、team_id ごとに Lambda の中で数分キャッシュすれば 1 投稿 1 回以下にできる

### 3.3 その他

- R7: 本文の代わりに長さ・ハッシュだけをログに出す（`log_info(..., text_sha256=...)`）
- 判定の監視: `method` ごと（BedrockV2・Rule・Undetermined）の件数と、再試行・予備のモデルへの切り替えの回数を CloudWatch のメトリクスに出し、Undetermined が増えたらアラーム
