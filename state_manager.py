"""サイクル状態管理 - ロック・冪等性保証"""
import json
import os
import time
import logging
from pathlib import Path
from datetime import datetime

from config import LOCK_FILE, LOCK_STALE_MINUTES

logger = logging.getLogger(__name__)


def acquire_lock() -> bool:
    """排他ロックを取得。既にロック中ならFalseを返す。"""
    if LOCK_FILE.exists():
        try:
            data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            lock_time = datetime.fromisoformat(data["timestamp"])
            elapsed = (datetime.now() - lock_time).total_seconds() / 60
            if elapsed < LOCK_STALE_MINUTES:
                logger.warning(
                    "ロック中 (PID=%s, %d分前に取得)", data["pid"], int(elapsed)
                )
                return False
            logger.warning("stale lock検出 (%d分経過)。上書きします。", int(elapsed))
        except (json.JSONDecodeError, KeyError):
            logger.warning("壊れたロックファイル。上書きします。")

    lock_data = {"pid": os.getpid(), "timestamp": datetime.now().isoformat()}
    LOCK_FILE.write_text(json.dumps(lock_data), encoding="utf-8")
    logger.info("ロック取得 (PID=%d)", os.getpid())
    return True


def release_lock():
    """ロックを解放。"""
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()
        logger.info("ロック解放")


class UrlStageTracker:
    """各URLの処理段階を追跡し、冪等性を保証する。

    段階遷移: discovered → scored → fetched → raw_saved → inbox_written → ingested
    """

    STAGES = [
        "discovered",
        "scored",
        "fetched",
        "raw_saved",
        "inbox_written",
        "ingested",
    ]

    def __init__(self, progress_path: Path):
        self.path = progress_path
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("progress.json破損。新規作成します。")
        return {
            "objective_id": self.path.stem,
            "cycles_completed": 0,
            "consecutive_zero_results": 0,
            "last_cycle_at": None,
            "urls": {},
            "queries_history": [],
        }

    def save(self):
        """Write-Ahead方式: 一時ファイル→リネームでatomic書き込み"""
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(self.path)

    def get_stage(self, url: str) -> str | None:
        """URLの現在の処理段階を返す。未知URLはNone。"""
        entry = self.data["urls"].get(url)
        return entry["stage"] if entry else None

    def is_at_least(self, url: str, target_stage: str) -> bool:
        """URLが指定段階以上に達しているか。"""
        current = self.get_stage(url)
        if current is None:
            return False
        return self.STAGES.index(current) >= self.STAGES.index(target_stage)

    def set_stage(self, url: str, stage: str, **extra):
        """URLの処理段階を更新し、即座にファイル保存。"""
        if url not in self.data["urls"]:
            self.data["urls"][url] = {
                "first_seen": datetime.now().isoformat(),
                "stage": stage,
            }
        else:
            self.data["urls"][url]["stage"] = stage
        self.data["urls"][url].update(extra)
        self.save()

    def record_query(self, query: str, results_count: int, accepted_count: int):
        self.data["queries_history"].append(
            {
                "query": query,
                "executed_at": datetime.now().isoformat(),
                "results_count": results_count,
                "accepted_count": accepted_count,
            }
        )
        self.save()

    # 飽和カウンタに計上しない "観測不能" な 0件理由
    NON_SATURATING_ZERO_REASONS = {"cooldown_only", "all_422", "search_failed"}

    def finish_cycle(self, new_results: int, zero_reason: str | None = None):
        """サイクル完了を記録。

        zero_reason が NON_SATURATING_ZERO_REASONS のいずれかの場合、
        consecutive_zero_results は増やさない（検索が実行できていないため
        飽和と区別できない — "新規0件" と "観測不能" を混同しないための措置）。
        """
        self.data["cycles_completed"] += 1
        self.data["last_cycle_at"] = datetime.now().isoformat()
        if new_results > 0:
            self.data["consecutive_zero_results"] = 0
        elif zero_reason in self.NON_SATURATING_ZERO_REASONS:
            logger.info(
                "zero_reason=%s のため飽和カウンタを据え置き (現在 %d回)",
                zero_reason, self.data["consecutive_zero_results"],
            )
        else:
            self.data["consecutive_zero_results"] += 1
        if zero_reason:
            self.data["last_zero_reason"] = zero_reason
        self.save()

    def purge_old_urls(self, ttl_days: int, max_urls: int):
        """TTL超過・上限超過のURL記録を削除。"""
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=ttl_days)
        urls = self.data["urls"]
        # TTLパージ
        expired = [
            u
            for u, v in urls.items()
            if datetime.fromisoformat(v["first_seen"]) < cutoff
        ]
        for u in expired:
            del urls[u]
        # 上限パージ（古い順に削除）
        if len(urls) > max_urls:
            sorted_urls = sorted(urls.items(), key=lambda x: x[1]["first_seen"])
            for u, _ in sorted_urls[: len(urls) - max_urls]:
                del urls[u]
        if expired or len(urls) > max_urls:
            self.save()
            logger.info("URL記録パージ: TTL=%d件, 上限超過=%d件", len(expired), max(0, len(urls) - max_urls))
