"""LLM を使わずに取れる、判定の手がかり（言語によらない）。

外部のチャットグループの「招待リンク」は、言語・言い回しに関係なく形が決まっている。運営の方針では
リンクだけの投稿も警告の対象（judge_decision.txt「警告する」1）なので、招待リンクがあれば
LLM が「違反なし」と言っても警告に回す（取りこぼしを防ぐ安全網）。

- 対象は「グループ・サーバーへの招待」の URL の形だけ。個人のプロフィール（linkedin.com/in/...）や
  講座の公式の場所（Zoom・YouTube・Google Drive・Omnicampus）は対象外
- 表記の崩し（全角、「t . me」のような空白、[.]・(dot)）は正規化してから照合する
- 誤検知になりうるのは「この招待リンクは違反？」と本文にリンクを貼って報告する投稿。運営が目で見て
  除ける誤検知で、見逃しよりよい（運営の方針: 見逃しを最優先で避ける）
"""
from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

# 外部のチャットグループ・サーバーの招待リンクの形（ドメイン + 招待のパス）
_INVITE_PATTERNS = [
    ("whatsapp", r"chat\.whatsapp\.com/[a-z0-9]+"),
    ("telegram", r"(?:t|telegram)\.me/(?:\+|joinchat/)?[a-z0-9_]{3,}"),
    ("discord", r"(?:discord\.gg|discord(?:app)?\.com/invite)/[a-z0-9-]+"),
    ("line", r"line\.me/(?:r/)?ti/g2?/[a-z0-9_-]+"),
    ("facebook_group", r"(?:facebook|fb)\.com/groups/[a-z0-9._-]+"),
    ("slack_invite", r"join\.slack\.com/[a-z0-9/_-]+"),
    ("wechat_group", r"weixin\.qq\.com/g/[a-z0-9_-]+"),
]
_INVITE_RE = [(name, re.compile(p, re.I)) for name, p in _INVITE_PATTERNS]


def normalize(text: str) -> str:
    """全角→半角（NFKC）、ゼロ幅文字の除去、ドットの言い換え（[.] (dot) ．）と、ドット・スラッシュの周りの空白を詰める"""
    s = unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH).lower()
    s = re.sub(r"\s*(?:\[\.\]|\(\.\)|\[dot\]|\(dot\)|。)\s*", ".", s)
    s = re.sub(r"\s*([./])\s*", r"\1", s)
    return s


def find_invite_links(text: str) -> list[str]:
    """外部のチャットグループの招待リンクの種類（whatsapp・telegram など）を返す。無ければ空"""
    s = normalize(text)
    return [name for name, rx in _INVITE_RE if rx.search(s)]
