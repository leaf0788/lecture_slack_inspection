"""Amazon Bedrock (Converse API) を、OpenAI の chat.completions.create と同じ形で呼ぶための薄いアダプタ。

ViolationDetector は `client.chat.completions.create(model=, messages=, response_format=)` だけを使う。
このアダプタを渡せば、判定のロジック（経路 B）を変えずに Bedrock のモデル（Amazon Nova Micro / Lite など）で動く。

- 構造化出力: response_format の JSON スキーマを「1 つだけのツール」として渡し、toolChoice でそのツールを
  必ず呼ばせる（Converse API に response_format は無いため）。ツールの入力をそのまま JSON 文字列で返す
- system メッセージ: messages の role=system は Converse の system に移す。cache_system=True なら
  system の後ろに cachePoint を置き、静的な部分（方針・条文）をプロンプトキャッシュに載せる
- 一時的なエラー（スロットリング・5xx・時間切れ）: 指数バックオフ＋ジッターで再試行し、
  それでも駄目なら fallback_model_id（別のモデル＝別のクォータ）で 1 回やり直す（課題 5）
- 戻り値: OpenAI の応答に似せたオブジェクト（choices[0].message.content と usage）。
  評価の harness がトークン数（キャッシュを含む）をそのまま記録できる
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_REGION = "ap-northeast-1"
DEFAULT_MODEL_ID = "apac.amazon.nova-lite-v1:0"

# 再試行する Bedrock のエラーコード（https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html ）
RETRYABLE_CODES = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelNotReadyException",
    "ModelTimeoutException",
    "TooManyRequestsException",
}
THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException"}


class BedrockInvocationError(RuntimeError):
    """再試行・予備モデルでも判定の呼び出しができなかった"""

    def __init__(self, message: str, code: str = "", model_id: str = ""):
        super().__init__(message)
        self.code = code
        self.model_id = model_id


def _error_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str((resp.get("Error") or {}).get("Code") or "")
    name = type(exc).__name__
    # botocore の通信系の例外（ReadTimeoutError・EndpointConnectionError など）
    if name.endswith("TimeoutError") or name in ("EndpointConnectionError", "ConnectionClosedError"):
        return "NetworkError"
    return ""


def _is_retryable(code: str) -> bool:
    return code in RETRYABLE_CODES or code == "NetworkError"


def _extract_json(text: str) -> str:
    """ツールを呼ばずに本文で JSON を返した場合の予備（最初の {...} を取り出す）"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        json.loads(text)
        return text
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else ""


def _schema_from_response_format(response_format: Optional[dict]) -> tuple[str, dict] | None:
    if not response_format or response_format.get("type") != "json_schema":
        return None
    js = response_format.get("json_schema") or {}
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", js.get("name") or "output")[:64]
    return name, js.get("schema") or {"type": "object"}


class _Completions:
    def __init__(self, owner: "BedrockChatClient"):
        self._owner = owner

    def create(self, model: str, messages: list[dict], response_format: Optional[dict] = None, **kw):
        return self._owner.complete(model=model, messages=messages, response_format=response_format, **kw)


class _Chat:
    def __init__(self, owner: "BedrockChatClient"):
        self.completions = _Completions(owner)


class BedrockChatClient:
    provider = "bedrock"

    def __init__(
        self,
        runtime: Any = None,
        region: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 700,
        fallback_model_id: Optional[str] = None,
        max_retries: int = 4,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        cache_system: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if runtime is None:
            import boto3
            from botocore.config import Config

            runtime = boto3.client(
                "bedrock-runtime",
                region_name=region or os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or DEFAULT_REGION,
                # 再試行はこのクラスで行う（botocore と二重にならないように 1 回だけにする）
                config=Config(retries={"total_max_attempts": 1, "mode": "standard"},
                              connect_timeout=5, read_timeout=30),
            )
        self.runtime = runtime
        self.api_key = None  # ViolationDetector の Decisions API 経路が参照するため（Bedrock では使わない）
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.fallback_model_id = fallback_model_id or None
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.cache_system = cache_system
        self._sleep = sleep
        self.chat = _Chat(self)

    # ------------------------------------------------------------------
    def _build_request(self, model: str, messages: list[dict], response_format: Optional[dict]) -> dict:
        system_blocks: list[dict] = []
        conv: list[dict] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system":
                system_blocks.append({"text": content})
            else:
                conv.append({"role": "assistant" if role == "assistant" else "user", "content": [{"text": content}]})
        if system_blocks and self.cache_system:
            system_blocks.append({"cachePoint": {"type": "default"}})

        req: dict = {
            "modelId": model,
            "messages": conv,
            "inferenceConfig": {"temperature": self.temperature, "maxTokens": self.max_tokens},
        }
        if self.temperature == 0:
            # Nova でツールを使う場合は貪欲法（topK=1）が推奨。判定の安定性のため
            req["additionalModelRequestFields"] = {"inferenceConfig": {"topK": 1}}
        if system_blocks:
            req["system"] = system_blocks
        schema = _schema_from_response_format(response_format)
        if schema:
            name, js = schema
            req["toolConfig"] = {
                "tools": [{"toolSpec": {"name": name, "description": "判定結果を返す", "inputSchema": {"json": js}}}],
                "toolChoice": {"tool": {"name": name}},
            }
        return req

    def _converse_with_retry(self, req: dict) -> dict:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.runtime.converse(**req)
            except Exception as e:  # noqa: BLE001
                code = _error_code(e)
                last_exc = e
                if not _is_retryable(code) or attempt == self.max_retries:
                    raise BedrockInvocationError(f"{code or type(e).__name__}: {e}", code=code,
                                                 model_id=req["modelId"]) from e
                # full jitter（https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/ ）
                delay = random.uniform(0, min(self.max_delay, self.base_delay * (2 ** attempt)))
                logger.warning("bedrock retry model=%s code=%s attempt=%d wait=%.2fs",
                               req["modelId"], code, attempt + 1, delay)
                self._sleep(delay)
        raise BedrockInvocationError(str(last_exc), model_id=req["modelId"])  # pragma: no cover

    def complete(self, model: str, messages: list[dict], response_format: Optional[dict] = None, **_kw):
        req = self._build_request(model, messages, response_format)
        used_model = model
        try:
            resp = self._converse_with_retry(req)
        except BedrockInvocationError as e:
            # 混雑・停止のときだけ予備のモデルへ（モデルごとにクォータが別）
            if self.fallback_model_id and self.fallback_model_id != model and e.code in THROTTLE_CODES | {"NetworkError"}:
                logger.warning("bedrock fallback %s -> %s (%s)", model, self.fallback_model_id, e.code)
                used_model = self.fallback_model_id
                resp = self._converse_with_retry({**req, "modelId": used_model})
            else:
                raise
        return self._to_openai_like(resp, used_model)

    @staticmethod
    def _to_openai_like(resp: dict, model: str):
        content_blocks = (((resp.get("output") or {}).get("message") or {}).get("content")) or []
        content = ""
        for b in content_blocks:
            if "toolUse" in b:
                content = json.dumps(b["toolUse"].get("input") or {}, ensure_ascii=False)
                break
        if not content:
            content = _extract_json("".join(b.get("text", "") for b in content_blocks if "text" in b))
        u = resp.get("usage") or {}
        cached = int(u.get("cacheReadInputTokens", 0) or 0)
        cache_write = int(u.get("cacheWriteInputTokens", 0) or 0)
        # Converse の inputTokens はキャッシュから読んだ分を含まないので、OpenAI と同じ「入力の合計」に直す
        prompt_tokens = int(u.get("inputTokens", 0) or 0) + cached + cache_write
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=int(u.get("outputTokens", 0) or 0),
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
            cache_write_tokens=cache_write,
        )
        return SimpleNamespace(
            model=model,
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=usage,
            stop_reason=resp.get("stopReason"),
            latency_ms=(resp.get("metrics") or {}).get("latencyMs"),
        )


def build_bedrock_client_from_env(**overrides) -> BedrockChatClient:
    """Lambda の環境変数から作る（BEDROCK_REGION・BEDROCK_FALLBACK_MODEL_ID・BEDROCK_PROMPT_CACHE）"""
    kw = dict(
        region=os.environ.get("BEDROCK_REGION") or None,
        fallback_model_id=os.environ.get("BEDROCK_FALLBACK_MODEL_ID") or None,
        cache_system=os.environ.get("BEDROCK_PROMPT_CACHE", "false").lower() == "true",
    )
    kw.update(overrides)
    return BedrockChatClient(**kw)


_shared: Optional[BedrockChatClient] = None


def get_shared_client() -> BedrockChatClient:
    """Lambda のコンテナの間で使い回す（呼び出しのたびに boto3 のクライアントを作らない）"""
    global _shared
    if _shared is None:
        _shared = build_bedrock_client_from_env()
    return _shared


def llm_provider() -> str:
    """環境変数 LLM_PROVIDER（openai／bedrock）。既定は openai（今の本番のまま）"""
    v = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower()
    return v if v in ("openai", "bedrock") else "openai"


def bedrock_model_id() -> str:
    return os.environ.get("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID
