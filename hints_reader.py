"""search-hints JSON読み込み（Claudeが生成、Pythonが消費）"""
import json
import logging
from pathlib import Path

from config import SEARCH_HINTS_DIR

logger = logging.getLogger(__name__)


def load_hints(objective_id: str) -> dict | None:
    """指定目的のsearch-hintsを読み込む。"""
    hints_file = SEARCH_HINTS_DIR / f"{objective_id}.json"
    if not hints_file.exists():
        return None

    try:
        data = json.loads(hints_file.read_text(encoding="utf-8"))
        logger.info(
            "search-hints読み込み: %s (クエリ%d件, ギャップ%d件)",
            objective_id,
            len(data.get("hints", [])),
            len(data.get("knowledge_gaps", [])),
        )
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("search-hints読み込み失敗: %s — %s", objective_id, e)
        return None


def get_hint_queries(objective_id: str) -> list[dict]:
    """search-hintsからクエリリストを取得。"""
    data = load_hints(objective_id)
    if data is None:
        return []
    return data.get("hints", [])


def get_knowledge_gaps(objective_id: str) -> list[str]:
    """search-hintsから知識ギャップリストを取得。"""
    data = load_hints(objective_id)
    if data is None:
        return []
    return data.get("knowledge_gaps", [])
