"""目的駆動型自律知識成長システム — メインオーケストレーター

毎時実行: 目的読み込み → 検索クエリ生成 → Web検索 → スコアリング →
ページ取得 → raw保存 → inbox書き込み
"""
import logging
import logging.handlers
import sys
from pathlib import Path

from config import (
    LOG_FILE,
    LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
    LOGS_DIR,
    MAX_QUERIES_PER_OBJECTIVE,
    RELEVANCE_THRESHOLD,
    PROGRESS_DIR,
    PROGRESS_TTL_DAYS,
    PROGRESS_MAX_URLS,
    INBOX_DIR,
)


def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    console = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(fmt)
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)


def run_cycle():
    """1サイクルのメインパイプライン。"""
    from state_manager import acquire_lock, release_lock, UrlStageTracker
    from objectives import load_objectives
    from lm_client import generate_search_queries, score_relevance, summarize_page, wait_for_server
    from browser_client import search_google, fetch_page_text, fetch_page_html
    from raw_writer import save_raw_html
    from inbox_writer import write_to_inbox
    from progress_tracker import normalize_url, is_valid_url
    from hints_reader import get_hint_queries, get_knowledge_gaps

    logger = logging.getLogger("main")

    # ロック取得
    if not acquire_lock():
        logger.info("前回サイクル実行中。スキップします。")
        return 0

    try:
        # LM Studio サーバー確認
        if not wait_for_server():
            logger.error("LM Studioサーバーに接続できません。サイクル中断。")
            return 0

        # Gap分析（GAP_ANALYSIS_INTERVAL_HOURS 経過時のみ実行）
        try:
            from gap_analyzer import run_gap_analysis
            gap_stats = run_gap_analysis()
            logger.info("Gap分析結果: %s", gap_stats)
        except Exception:
            logger.exception("Gap分析中に例外が発生（サイクルは継続）")

        # 目的ファイル読み込み
        objectives = load_objectives()
        if not objectives:
            logger.info("active目的ファイルなし。サイクル終了。")
            return 0

        total_inbox_written = 0

        for obj in objectives:
            logger.info("=== 目的処理開始: %s (priority=%s) ===", obj.title, obj.priority)

            # 状態トラッカー
            tracker = UrlStageTracker(PROGRESS_DIR / f"{obj.id}.json")

            # TTLパージ
            tracker.purge_old_urls(PROGRESS_TTL_DAYS, PROGRESS_MAX_URLS)

            # 飽和チェック
            if obj.saturation_threshold > 0 and tracker.data["consecutive_zero_results"] >= obj.saturation_threshold:
                logger.info("飽和検知 (%d回連続0件)。自動pause: %s", tracker.data["consecutive_zero_results"], obj.id)
                # TODO: ファイルのstatusをpausedに更新
                continue

            # サイクル上限チェック
            if obj.max_cycles > 0 and tracker.data["cycles_completed"] >= obj.max_cycles:
                logger.info("サイクル上限到達 (%d/%d): %s", tracker.data["cycles_completed"], obj.max_cycles, obj.id)
                continue

            # search-hintsからクエリを取得（あれば優先）
            hint_queries = get_hint_queries(obj.id)
            knowledge_gaps = get_knowledge_gaps(obj.id)

            # 過去のクエリ一覧
            prev_queries = [q["query"] for q in tracker.data.get("queries_history", [])[-20:]]

            # 検索クエリ生成
            if hint_queries:
                queries = [{"query": h["query"], "lang": h.get("search_lang", obj.language)} for h in hint_queries[:MAX_QUERIES_PER_OBJECTIVE]]
                logger.info("search-hintsから%d件のクエリを使用", len(queries))
            else:
                queries = generate_search_queries(
                    objective_title=obj.title,
                    objective_goal=obj.goal,
                    interests=obj.interests,
                    exclusions=obj.exclusions,
                    knowledge_gaps=knowledge_gaps,
                    previous_queries=prev_queries,
                    language=obj.language,
                )[:MAX_QUERIES_PER_OBJECTIVE]
                logger.info("LM Studioから%d件のクエリを生成", len(queries))

            if not queries:
                logger.warning("検索クエリ生成失敗。次の目的へ。")
                tracker.finish_cycle(0)
                continue

            cycle_new_results = 0

            for q in queries:
                query_text = q.get("query", str(q))
                query_lang = q.get("lang", obj.language)

                # Google検索
                search_results = search_google(query_text, lang=query_lang)
                accepted = 0

                for result in search_results:
                    url = result.get("url", "")
                    if not url or not is_valid_url(url):
                        continue

                    normalized = normalize_url(url)

                    # 重複チェック
                    if tracker.is_at_least(normalized, "discovered"):
                        continue

                    tracker.set_stage(normalized, "discovered")

                    # スコアリング
                    score_result = score_relevance(
                        objective_summary=f"{obj.title}: {obj.goal}",
                        result_title=result.get("title", ""),
                        result_url=url,
                        result_snippet=result.get("snippet", ""),
                        exclusions=obj.exclusions,
                    )

                    if score_result is None:
                        logger.warning("スコアリング失敗、スキップ: %s", url)
                        continue

                    score = score_result.get("score", 0)
                    tracker.set_stage(normalized, "scored", score=score)

                    if score < RELEVANCE_THRESHOLD:
                        logger.debug("スコア不足 (%d/%d): %s", score, RELEVANCE_THRESHOLD, url)
                        continue

                    logger.info("スコア合格 (%d/10): %s", score, result.get("title", ""))

                    # ページ全文取得
                    page_text = fetch_page_text(url)
                    if page_text is None:
                        logger.warning("ページ取得失敗、スキップ: %s", url)
                        continue
                    tracker.set_stage(normalized, "fetched")

                    # raw/articles/ に生データ保存
                    page_html = fetch_page_html(url)
                    if page_html:
                        save_raw_html(url, page_html, obj.id)
                    tracker.set_stage(normalized, "raw_saved")

                    # 要約生成
                    summary_md = summarize_page(url, page_text, obj.id)
                    if summary_md is None:
                        logger.warning("要約生成失敗、スキップ: %s", url)
                        continue

                    # inbox書き込み
                    inbox_path = write_to_inbox(summary_md, url)
                    if inbox_path:
                        tracker.set_stage(normalized, "inbox_written", inbox_filename=inbox_path.name)
                        accepted += 1
                        cycle_new_results += 1

                    # 目的あたりの上限チェック
                    if cycle_new_results >= obj.max_results_per_cycle:
                        logger.info("目的あたりの上限到達 (%d件)", obj.max_results_per_cycle)
                        break

                tracker.record_query(query_text, len(search_results), accepted)

                if cycle_new_results >= obj.max_results_per_cycle:
                    break

            tracker.finish_cycle(cycle_new_results)
            total_inbox_written += cycle_new_results
            logger.info("=== 目的処理完了: %s - 新規%d件 ===", obj.title, cycle_new_results)

        # サイクルサマリー
        inbox_count = len(list(INBOX_DIR.glob("*.md"))) if INBOX_DIR.exists() else 0
        logger.info(
            "サイクル完了: 目的%d件処理, inbox新規%d件, inbox合計%d件",
            len(objectives),
            total_inbox_written,
            inbox_count,
        )

        return total_inbox_written

    finally:
        release_lock()


def run_auto_ingest(dry_run: bool = False):
    """inbox にファイルがあればローカルIngestを実行。"""
    from local_ingest import run_ingest

    logger = logging.getLogger("main")
    logger.info("========== ローカルIngest開始 ==========")
    try:
        stats = run_ingest(dry_run=dry_run)
        logger.info("========== ローカルIngest完了: %s ==========", stats)
        return stats
    except Exception:
        logger.exception("ローカルIngest中に未処理例外が発生")
        return None


def main():
    setup_logging()
    logger = logging.getLogger("main")

    import sys
    args = sys.argv[1:]

    # --ingest-only モード
    if "--ingest-only" in args:
        dry_run = "--dry-run" in args
        run_auto_ingest(dry_run=dry_run)
        return

    # --gap-only モード（Gap分析だけ単独実行）
    if "--gap-only" in args:
        from gap_analyzer import run_gap_analysis
        force = "--force" in args
        stats = run_gap_analysis(force=force)
        logger.info("Gap分析: %s", stats)
        return

    # 通常サイクル
    logger.info("========== オーケストレーターサイクル開始 ==========")
    try:
        new_files = run_cycle()
        logger.info("========== サイクル終了 (新規%d件) ==========", new_files)

        # inbox にファイルがあれば自動Ingest
        if new_files > 0:
            run_auto_ingest()

    except Exception:
        logger.exception("サイクル中に未処理例外が発生")
        raise


if __name__ == "__main__":
    main()
