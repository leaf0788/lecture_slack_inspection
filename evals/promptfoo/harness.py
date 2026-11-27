"""評価のために、本番の判定のコード（lambda/ の ViolationDetector）をそのまま呼ぶ共通の部分。

provider.py（promptfoo のプロバイダー）から使う。本番のコードは変えずに、次のことを足す:

- 1 投稿ごとの API の呼び出し（種類・モデル・トークン数・時間・応答コード）の記録
- Decisions API の一時的なエラー（429・5xx）の再試行。評価の再現性のため、予備経路に切り替える前に同じ経路で
  再試行する。503 は OpenAI の資料で「Model temporarily overloaded」。Retry-After があればその秒数、
  無ければ RETRY_WAIT_SEC 待つ（https://developers.openai.com/api/docs/guides/error-codes ）。
  本番のコードは再試行しない（docs/requirements/issues.md の ISS-33）
- Decisions API の応答の記録（警告の確率と、条文の候補の上位 3 つ）。「違反なし」と判定すると本番のコードは
  条文を None にするので、取りこぼしの分析のために残す
- 判定の失敗の印。今のコードは判定の失敗を「違反なし」にし、理由が「LLM判定エラー」で始まる結果を返す（ISS-01）。
  評価では、これを "error" として「違反なし」と混ぜない
- プロンプトは作業ツリーの版を使う（プロンプトを書き換えて改善を評価できるように）。prompts_from_head=True のときだけ
  HEAD の版を使う（このリポジトリの作業ツリーの judge_violation.txt が壊れていることがあるため。ISS-22）
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "lambda") not in sys.path:
    sys.path.insert(0, str(REPO / "lambda"))

import requests  # noqa: E402
from openai import OpenAI  # noqa: E402

import app_inspect.services.violation_detector as vd  # noqa: E402
from common.bedrock_client import BedrockChatClient  # noqa: E402

# Bedrock の既定のモデル。東京リージョンでは APAC のクロスリージョン推論プロファイルを使う
# （環境に合わせて promptfooconfig.yml の config.model か、環境変数 BEDROCK_MODEL_ID で替える）
BEDROCK_DEFAULT_MODEL = os.environ.get("BEDROCK_MODEL_ID", "apac.amazon.nova-lite-v1:0")
BEDROCK_MODES = {"bedrock", "bedrock_v2"}
MODELS = {"decisions": "gpt-6-luna", "luna": "gpt-6-luna", "llm_score": "gpt-4o-mini",
          "bedrock": BEDROCK_DEFAULT_MODEL, "bedrock_v2": BEDROCK_DEFAULT_MODEL}
ERROR_PREFIX = "LLM判定エラー"
TRANSIENT_STATUS = {429, 500, 502, 503, 504}
RETRY_WAIT_SEC = 3.0
MAX_TRANSIENT_RETRIES = 3

# USD / 100 万トークン（docs/research/20261013_detection_model_migration.md の 6.2）
# Amazon Nova はオンデマンドの公開価格（https://aws.amazon.com/bedrock/pricing/ 。キャッシュの読み出しは 75% 引き）。
# リージョン・推論プロファイルで変わることがあるので、評価の前に料金ページで確かめて直す
PRICES = {
    ("decisions", "gpt-6-luna"): {"in": 0.10, "cached": 0.10, "out": 0.0},
    ("chat", "gpt-6-luna"): {"in": 0.10, "cached": 0.01, "out": 0.50},
    ("chat", "gpt-4o-mini"): {"in": 0.15, "cached": 0.075, "out": 0.60},
    ("chat", "amazon.nova-micro-v1:0"): {"in": 0.035, "cached": 0.00875, "out": 0.14},
    ("chat", "amazon.nova-lite-v1:0"): {"in": 0.06, "cached": 0.015, "out": 0.24},
}


def _price_key(api: str, model: str | None) -> tuple[str, str]:
    """推論プロファイルの接頭辞（apac. / us. / jp. など）を外して単価を引く"""
    m = model or ""
    if ".amazon." in m:
        m = m.split(".", 1)[1]
    return api, m

_tl = threading.local()


def _calls() -> list:
    if not hasattr(_tl, "calls"):
        _tl.calls = []
    return _tl.calls


class _Completions:
    def __init__(self, real):
        self._real = real

    def create(self, **kw):
        t0 = time.time()
        resp = self._real.create(**kw)
        u = resp.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        _calls().append({"api": "chat", "model": getattr(resp, "model", None) or kw.get("model"),
                         "in": u.prompt_tokens, "cached": cached,
                         "out": u.completion_tokens, "sec": round(time.time() - t0, 3)})
        return resp


class _Chat:
    def __init__(self, real):
        self.completions = _Completions(real.completions)


class RecordingClient:
    """openai.OpenAI（または BedrockChatClient）の代わり。ViolationDetector は api_key と chat.completions.create だけを使う"""

    def __init__(self, real):
        self._real = real
        self.api_key = real.api_key
        self.provider = getattr(real, "provider", "openai")
        self.chat = _Chat(real.chat)


_real_post = requests.post


def _retry_wait(resp) -> float:
    try:
        return max(float(resp.headers.get("Retry-After", "")), RETRY_WAIT_SEC)
    except (TypeError, ValueError):
        return RETRY_WAIT_SEC


def _decisions_summary(data: dict) -> dict:
    out = {}
    for ans in data.get("answers", []) or []:
        probs = sorted(ans.get("probabilities", []) or [], key=lambda p: p.get("probability", 0.0), reverse=True)
        if ans.get("name") == "verdict":
            out["p_warn"] = next((p.get("probability") for p in probs if p.get("value") == "warn"), None)
        elif ans.get("name") == "article":
            out["article_top3"] = [[p.get("value"), round(float(p.get("probability", 0.0)), 4)] for p in probs[:3]]
    return out


def _recording_post(url, *args, **kw):
    resp = None
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        t0 = time.time()
        resp = _real_post(url, *args, **kw)
        rec = {"api": "decisions", "model": vd.DECISIONS_MODEL, "in": 0, "cached": 0, "out": 0,
               "sec": round(time.time() - t0, 3), "status": resp.status_code}
        try:
            data = resp.json()
            u = data.get("usage", {}) or {}
            rec["in"] = u.get("input_tokens", 0) or 0
            rec["cached"] = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
            if resp.status_code == 200:
                rec.update(_decisions_summary(data))
        except Exception:
            pass
        _calls().append(rec)
        if resp.status_code not in TRANSIENT_STATUS or attempt == MAX_TRANSIENT_RETRIES:
            return resp
        wait = _retry_wait(resp)
        rec["retry_wait_sec"] = wait
        time.sleep(wait)
    return resp


_orig_load_prompt = vd._load_prompt
_head_cache: dict[str, str] = {}


def _head_prompt(filename: str, default: str = "") -> str:
    if filename not in _head_cache:
        r = subprocess.run(
            ["git", "-C", str(REPO), "show", f"HEAD:lambda/app_inspect/services/data/prompts/{filename}"],
            capture_output=True, text=True)
        _head_cache[filename] = r.stdout if r.returncode == 0 else ""
    return _head_cache[filename] or _orig_load_prompt(filename, default)


def install_patches(prompts_from_head: bool = False) -> None:
    vd.requests.post = _recording_post
    vd._load_prompt = _head_prompt if prompts_from_head else _orig_load_prompt


def load_api_key() -> str:
    """OPENAI_API_KEY（環境変数）か、リポジトリの .env の LLM_API_KEY（開発用のキー）。値は表示しない"""
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("LLM_API_KEY="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    raise RuntimeError("OPENAI_API_KEY（または .env の LLM_API_KEY）がありません")


_clients: dict[tuple, RecordingClient] = {}
_client_lock = threading.Lock()
_patched = False


def get_client(prompts_from_head: bool = False, mode: str = "decisions", bedrock_opts: dict | None = None) -> RecordingClient:
    """方式ごとにクライアントを 1 つ作って使い回す。Bedrock の認証は boto3 の標準（環境変数・プロファイル・SSO）"""
    global _patched
    opts = bedrock_opts or {}
    key = ("bedrock", tuple(sorted(opts.items()))) if mode in BEDROCK_MODES else ("openai",)
    with _client_lock:
        if not _patched:
            install_patches(prompts_from_head)
            _patched = True
        if key not in _clients:
            if mode in BEDROCK_MODES:
                real = BedrockChatClient(region=opts.get("region"), fallback_model_id=opts.get("fallback_model"),
                                         cache_system=bool(opts.get("prompt_cache", False)))
            else:
                real = OpenAI(api_key=load_api_key(), max_retries=2, timeout=60)
            _clients[key] = RecordingClient(real)
    return _clients[key]


def cost_of(calls: list[dict]) -> float:
    total = 0.0
    for c in calls:
        p = PRICES.get((c.get("api"), c.get("model"))) or PRICES.get(_price_key(c.get("api"), c.get("model")))
        if not p:
            continue
        cached = c.get("cached", 0) or 0
        total += ((c.get("in", 0) - cached) * p["in"] + cached * p["cached"] + c.get("out", 0) * p["out"]) / 1e6
    return total


def detect(text: str, mode: str = "decisions", model: str | None = None, retries: int = 2,
           prompts_from_head: bool = False, bedrock_opts: dict | None = None) -> dict:
    """本番の ViolationDetector で 1 投稿を判定し、評価用の結果を返す（判定の失敗は error に入る）"""
    model = model or MODELS[mode]
    client = get_client(prompts_from_head, mode=mode, bedrock_opts=bedrock_opts)
    last_err = ""
    calls: list = []
    for attempt in range(retries + 1):
        _tl.calls = []
        det = vd.ViolationDetector(openai_client=client, judge_model=model, detection_mode=mode)
        t0 = time.time()
        try:
            res = det.detect(text)
        except Exception as e:  # detect は通常は例外を外に出さないが、念のため
            last_err, calls = f"exception: {e}", list(_tl.calls)
            continue
        sec = round(time.time() - t0, 3)
        calls = list(_tl.calls)
        # bedrock_v2 は判定の失敗を「判定できなかった」（method=Undetermined）として通知に回す。評価では失敗として数える
        if res.reason.startswith(ERROR_PREFIX) or res.method == "Undetermined":
            last_err = res.reason[:300]
            continue
        dec = next((c for c in calls if c.get("api") == "decisions" and c.get("status") == 200), {})
        return {"error": None, "is_violation": bool(res.is_violation), "article_id": res.article_id,
                "confidence": res.confidence, "method": res.method, "latency_sec": sec,
                "attempts": attempt + 1, "p_warn": dec.get("p_warn"), "article_top3": dec.get("article_top3"),
                "signals": getattr(det, "last_signals", None),
                "calls": calls, "cost_usd": round(cost_of(calls), 7)}
    return {"error": last_err, "is_violation": None, "article_id": None, "confidence": None, "method": None,
            "latency_sec": None, "attempts": retries + 1, "p_warn": None, "article_top3": None,
            "calls": calls, "cost_usd": round(cost_of(calls), 7)}
