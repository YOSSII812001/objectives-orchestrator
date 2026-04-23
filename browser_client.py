"""Web検索+ページ取得クライアント

MVP: Brave Search API + httpx（ページ取得）
将来: agent-browser に切替予定（Chrome互換性問題解決後）
"""
import json
import logging
import time
from urllib.parse import quote_plus

import httpx

from config import BRAVE_API_KEY, PAGE_FETCH_TIMEOUT, QUERY_COOLDOWN_HOURS
from query_history import should_skip as _query_should_skip, record as _query_record

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

if not BRAVE_API_KEY:
    logger.warning(
        "BRAVE_API_KEY が未設定です。.env に BRAVE_API_KEY=<your key> を追加してください。"
    )


def search_google(query: str, lang: str = "ja") -> list[dict]:
    """Web検索を実行し、結果リストを返す。

    MVP: Brave Search API を使用（Google検索ではない）
    返り値: [{"title": "...", "url": "...", "snippet": "..."}]

    同一クエリが QUERY_COOLDOWN_HOURS 以内に実行済みの場合は
    API呼び出しをスキップして空リストを返す（API quota節約）。
    """
    # クールダウン判定
    if _query_should_skip(query, cooldown_hours=QUERY_COOLDOWN_HOURS):
        logger.info("クエリスキップ（クールダウン中）: %s", query)
        return []

    params = {
        "q": query,
        "count": 10,
        "search_lang": lang,
        "freshness": "py",  # past year
    }
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY,
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(BRAVE_SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })

        logger.info("Brave検索完了: '%s' → %d件", query, len(results))
        _query_record(query, len(results), status="ok")

        # レート制限: 1秒1リクエスト（無料枠）
        time.sleep(1.2)
        return results

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        if status_code == 429:
            logger.warning("Brave Search レート制限。60秒待機。")
            _query_record(query, 0, status="429")
            time.sleep(60)
            return []
        if status_code == 422:
            # 日本語クエリ等で繰り返し発生 → 24時間クールダウン
            logger.error("Brave Search APIエラー: %s", e)
            _query_record(query, 0, status="422")
            return []
        logger.error("Brave Search APIエラー: %s", e)
        _query_record(query, 0, status="error")
        return []
    except httpx.HTTPError as e:
        logger.error("Brave Search 接続エラー: %s", e)
        _query_record(query, 0, status="error")
        return []


def fetch_page_text(url: str) -> str | None:
    """URLのページ全文テキストを取得。

    httpx + 簡易テキスト抽出。JS描画は非対応（将来agent-browserで対応）。
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        with httpx.Client(timeout=PAGE_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            logger.warning("非HTMLコンテンツ (%s): %s", content_type, url)
            return None

        html = resp.text

        # 簡易テキスト抽出（BeautifulSoup使用）
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # script/style除去
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()

            # メインコンテンツ候補
            for selector in ["article", "main", '[role="main"]', ".post-content", ".entry-content"]:
                el = soup.select_one(selector)
                if el and len(el.get_text(strip=True)) > 200:
                    text = el.get_text(separator="\n", strip=True)
                    logger.info("ページ取得完了: %s (%d文字)", url, len(text))
                    return text

            # フォールバック: body全体
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 100:
                logger.info("ページ取得完了 (body全体): %s (%d文字)", url, len(text))
                return text

            logger.warning("ページ内容が短すぎます (%d文字): %s", len(text), url)
            return None

        except ImportError:
            # BS4なしのフォールバック
            import re
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 100:
                return text
            return None

    except httpx.HTTPError as e:
        logger.warning("ページ取得失敗: %s — %s", url, e)
        return None


def fetch_page_html(url: str) -> str | None:
    """URLのページHTMLを取得（raw/保存用）。"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        with httpx.Client(timeout=PAGE_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
        return resp.text
    except httpx.HTTPError as e:
        logger.warning("HTML取得失敗: %s — %s", url, e)
        return None
