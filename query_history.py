"""クエリ実行履歴の管理 - 短期間再投擲を抑制

Brave Search APIへの無駄なリクエストを減らすため、
同一クエリを N 時間以内は再実行しないようにする。

- キー: クエリ文字列のsha256の先頭16文字
- 値: {"query": str, "last_executed_at": ISO8601, "result_count": int, "status": "ok|422|429|error"}
- 422エラーは24時間以上クールダウンを延長（日本語クエリで繰り返し発生するため）
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from config import STATE_DIR, QUERY_COOLDOWN_HOURS

logger = logging.getLogger(__name__)

HISTORY_PATH = STATE_DIR / "query_history.json"
EXTENDED_COOLDOWN_HOURS_422 = 24  # 日本語422エラーは長期クールダウン


def _query_key(query: str) -> str:
    """クエリ文字列を正規化してハッシュ化。"""
    normalized = query.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _load() -> dict:
    if not HISTORY_PATH.exists():
        return {}
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("query_history.json破損。新規作成します。")
        return {}


def _save(data: dict) -> None:
    """atomicに保存。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HISTORY_PATH)


def should_skip(query: str, cooldown_hours: int = QUERY_COOLDOWN_HOURS) -> bool:
    """指定クエリがクールダウン中かどうか判定。

    - 初回実行のクエリ: False (実行する)
    - 前回status=422: EXTENDED_COOLDOWN_HOURS_422 を基準に判定
    - それ以外: cooldown_hours を基準に判定
    """
    key = _query_key(query)
    data = _load()
    entry = data.get(key)
    if not entry:
        return False

    try:
        last = datetime.fromisoformat(entry["last_executed_at"])
    except (KeyError, ValueError):
        return False

    status = entry.get("status", "ok")
    effective_hours = EXTENDED_COOLDOWN_HOURS_422 if status == "422" else cooldown_hours
    threshold = datetime.now() - timedelta(hours=effective_hours)
    return last > threshold


def recent_unique_queries(limit: int = 10, ok_only: bool = True) -> list[str]:
    """直近のユニーククエリを新しい順に返す。

    Phase 2 で Gap 分析プロンプトの avoid-list として使う。
    ok_only=True のとき status != "ok" （422/error）は除外する。
    422 はそもそも「質の悪いクエリ形状」のサインで再提案候補ではあるため。
    """
    data = _load()
    entries = []
    for key, entry in data.items():
        if ok_only and entry.get("status", "ok") != "ok":
            continue
        if "query" not in entry or "last_executed_at" not in entry:
            continue
        entries.append(entry)
    entries.sort(key=lambda e: e.get("last_executed_at", ""), reverse=True)
    seen: set[str] = set()
    result: list[str] = []
    for e in entries:
        q = str(e["query"]).strip()
        norm = q.lower()
        if norm in seen:
            continue
        seen.add(norm)
        result.append(q)
        if len(result) >= limit:
            break
    return result


def record(query: str, result_count: int, status: str = "ok") -> None:
    """クエリ実行結果を記録。

    status: "ok" | "422" | "429" | "error"
    """
    key = _query_key(query)
    data = _load()
    data[key] = {
        "query": query,
        "last_executed_at": datetime.now().isoformat(),
        "result_count": int(result_count),
        "status": status,
    }
    _save(data)
