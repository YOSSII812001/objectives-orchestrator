"""LM Studio API クライアント - 検索クエリ生成/スコアリング/要約"""
import json
import logging
import time

import httpx

from config import (
    LMS_BASE_URL,
    LMS_MODEL,
    LMS_TIMEOUT,
    MAX_RETRIES_JSON,
    SUMMARIZE_PAGE_MAX_CHARS,
    SUMMARIZE_PAGE_MAX_TOKENS,
)

logger = logging.getLogger(__name__)


def _chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 4096) -> str:
    """LM Studio chat completions APIを呼び出す。

    Gemma系モデルはsystemロール非対応のため、systemメッセージを
    最初のuserメッセージに統合する。
    """
    # systemロールをuserロールに統合（Gemma互換性対策）
    merged = []
    system_content = ""
    for msg in messages:
        if msg["role"] == "system":
            system_content += msg["content"] + "\n\n"
        else:
            if system_content and msg["role"] == "user":
                merged.append({"role": "user", "content": system_content + msg["content"]})
                system_content = ""
            else:
                merged.append(msg)
    if system_content:
        merged.insert(0, {"role": "user", "content": system_content.strip()})

    payload = {
        "messages": merged,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if LMS_MODEL:
        payload["model"] = LMS_MODEL

    with httpx.Client(timeout=LMS_TIMEOUT) as client:
        resp = client.post(f"{LMS_BASE_URL}/chat/completions", json=payload)
        if resp.status_code >= 400:
            body_preview = (resp.text or "(empty)")[:500]
            logger.error(
                "LM Studio API %d at /chat/completions — response: %s",
                resp.status_code, body_preview,
            )
            resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason", "unknown")
        usage = data.get("usage", {}) or {}
        # 空応答 + finish=length は思考トークン予算不足または n_ctx 超過の決定的サイン
        if not content.strip() and finish_reason == "length":
            logger.warning(
                "LM Studio 空応答 (finish=length): prompt=%s completion=%s total=%s / max_tokens=%s — "
                "思考予算不足または n_ctx 超過の疑い",
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                usage.get("total_tokens"), max_tokens,
            )
        else:
            logger.debug(
                "LM Studio: finish=%s prompt=%s completion=%s total=%s",
                finish_reason, usage.get("prompt_tokens"),
                usage.get("completion_tokens"), usage.get("total_tokens"),
            )
        return content


def _parse_json(text: str) -> dict | list | None:
    """LLM出力からJSONを抽出。コードブロック内のJSONも対応。"""
    import re

    # まず直接パース
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # ```json ... ``` ブロック内を抽出
    match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # [ ... ] または { ... } を正規表現で抽出
    match = re.search(r"[\[{][\s\S]*[\]}]", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _chat_json(messages: list[dict], temperature: float = 0.1, max_tokens: int = 4096) -> dict | list | None:
    """JSON出力を期待するchat呼び出し。リトライ付き。"""
    for attempt in range(MAX_RETRIES_JSON + 1):
        try:
            text = _chat(messages, temperature=temperature if attempt == 0 else 0.0, max_tokens=max_tokens)
            result = _parse_json(text)
            if result is not None:
                return result
            logger.warning("JSON解析失敗 (attempt %d/%d):\n--- LLM OUTPUT START ---\n%s\n--- LLM OUTPUT END ---", attempt + 1, MAX_RETRIES_JSON + 1, text[:1000])
        except httpx.HTTPError as e:
            logger.error("LM Studio API エラー (attempt %d): %s", attempt + 1, e)
            if attempt < MAX_RETRIES_JSON:
                time.sleep(2 ** attempt)
    return None


def generate_search_queries(
    objective_title: str,
    objective_goal: str,
    interests: list[str],
    exclusions: list[str],
    knowledge_gaps: list[str] | None = None,
    previous_queries: list[str] | None = None,
    language: str = "ja",
) -> list[dict]:
    """目的から検索クエリを生成。返り値: [{"query": "...", "lang": "ja|en"}]"""
    gaps_text = "\n".join(f"- {g}" for g in (knowledge_gaps or [])) or "（なし）"
    prev_text = "\n".join(f"- {q}" for q in (previous_queries or [])) or "（なし）"
    interests_text = "\n".join(f"- {i}" for i in interests)
    exclusions_text = "\n".join(f"- {e}" for e in exclusions) or "（なし）"

    messages = [
        {
            "role": "system",
            "content": (
                "あなたは調査アシスタントです。与えられた研究目的に基づいて、"
                "Web検索で使用する検索クエリを生成してください。"
                "出力はJSON配列のみ。説明文は不要です。"
            ),
        },
        {
            "role": "user",
            "content": f"""## 研究目的
{objective_title}

## ゴール
{objective_goal}

## 関心領域
{interests_text}

## 除外条件
{exclusions_text}

## 既知の知識ギャップ
{gaps_text}

## 過去の検索クエリ（重複を避ける）
{prev_text}

## 指示
- 3〜5個の検索クエリを生成
- 日本語と英語を混ぜて幅広く検索
- 2025-2026年の最新情報を重視
- 以下のJSON配列形式で出力:
[{{"query": "検索クエリ", "lang": "ja"}}]""",
        },
    ]

    result = _chat_json(messages, temperature=0.3, max_tokens=4096)
    if result is None:
        logger.error("検索クエリ生成失敗")
        return []
    if isinstance(result, list):
        return result
    return []


def score_relevance(
    objective_summary: str,
    result_title: str,
    result_url: str,
    result_snippet: str,
    exclusions: list[str],
) -> dict | None:
    """検索結果の関連度をスコアリング。返り値: {"score": N, "reason": "..."}"""
    exclusions_text = "\n".join(f"- {e}" for e in exclusions) or "（なし）"

    messages = [
        {
            "role": "system",
            "content": (
                "検索結果の関連性を0-10で評価してください。"
                "7以上 = inbox取込対象。JSON出力のみ。説明文は不要です。"
            ),
        },
        {
            "role": "user",
            "content": f"""## 研究目的
{objective_summary}

## 検索結果
タイトル: {result_title}
URL: {result_url}
スニペット: {result_snippet}

## 除外条件
{exclusions_text}

JSON出力: {{"score": 0-10の整数, "reason": "理由を1文で"}}""",
        },
    ]

    return _chat_json(messages, temperature=0.1, max_tokens=2048)


def summarize_page(
    url: str,
    page_text: str,
    objective_id: str,
    max_chars: int | None = None,
) -> str | None:
    """ページ全文からvault形式のMarkdown要約を生成。

    max_chars のデフォルトは config.SUMMARIZE_PAGE_MAX_CHARS。
    LM Studio の Gemma 4 E4B は実測 n_ctx=4096 で稼働しているため、
    入力 prompt + 出力 + 思考で 4096 tokens を超えないよう切り詰める。
    """
    from datetime import date

    today = date.today().isoformat()
    effective_max = max_chars if max_chars is not None else SUMMARIZE_PAGE_MAX_CHARS
    truncated = page_text[:effective_max]

    messages = [
        {
            "role": "system",
            "content": (
                "Webページの内容をナレッジベース用のMarkdown要約に変換してください。"
                "日本語で記述。以下のフォーマットに正確に従ってください。"
            ),
        },
        {
            "role": "user",
            "content": f"""## ソースURL
{url}

## ページ内容
{truncated}

## 出力フォーマット（これに正確に従うこと）
---
title: "日本語タイトル"
date_created: {today}
date_modified: {today}
summary: "1行要約"
tags: [関連タグ]
type: source
status: complete
url: "{url}"
author: "著者名または不明"
year: 2026
source_objective: "{objective_id}"
---
# 著者名 (年) — タイトル

## 核心の主張
[1-3文]

## 手法・アプローチ
[概要]

## 主要な発見・結論
[箇条書き]

## 抽出コンセプト
- [[concept-name]] — 関連性説明""",
        },
    ]

    try:
        return _chat(messages, temperature=0.1, max_tokens=SUMMARIZE_PAGE_MAX_TOKENS)
    except Exception as e:
        logger.error("要約生成失敗: %s — %s", url, e)
        return None


def check_server() -> bool:
    """LM Studio APIサーバーの稼働状態を確認。"""
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{LMS_BASE_URL}/models")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


def wait_for_server(max_wait: int = 120) -> bool:
    """サーバーが応答するまでポーリング。"""
    start = time.time()
    interval = 2
    while time.time() - start < max_wait:
        if check_server():
            logger.info("LM Studio APIサーバー稼働確認")
            return True
        logger.debug("LM Studio待機中... (%ds経過)", int(time.time() - start))
        time.sleep(interval)
        interval = min(interval * 2, 15)
    logger.error("LM Studioサーバー応答タイムアウト (%ds)", max_wait)
    return False
