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


def _hints_newer_than_last_cycle(objective_id: str, last_cycle_at: str | None) -> bool:
    """search-hints が last_cycle_at より後に生成されていれば True。

    飽和検知で止まった objective でも、新 hints が来ていれば 1 サイクル試行させる
    ための自動回復トリガー。
    """
    import json as _json
    from config import SEARCH_HINTS_DIR
    hints_file = SEARCH_HINTS_DIR / f"{objective_id}.json"
    if not hints_file.exists():
        return False
    try:
        data = _json.loads(hints_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    generated_at = data.get("generated_at")
    if not generated_at:
        return False
    if not last_cycle_at:
        return True
    return generated_at > last_cycle_at


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
    from query_history import should_skip as query_on_cooldown

    logger = logging.getLogger("main")

    # ロック取得
    if not acquire_lock():
        logger.info("前回サイクル実行中。スキップします。")
        return 0

    try:
        # LM Studio サーバー確認（早期スキップ: 30秒で見切る）
        # 完全停止時に120秒空回りするログノイズを抑止。起動中（LM Studio GUI 起動直後）
        # も 30 秒あればモデルロードが間に合うケースが多い。
        if not wait_for_server(max_wait=30):
            logger.info("LM Studio 未応答 → サイクル早期スキップ (lm_studio_down, 30s待機後)")
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

            # 飽和チェック（ただし fresh hints が到着していれば自動回復）
            if obj.saturation_threshold > 0 and tracker.data["consecutive_zero_results"] >= obj.saturation_threshold:
                if _hints_newer_than_last_cycle(obj.id, tracker.data.get("last_cycle_at")):
                    logger.info(
                        "飽和検知 (%d回連続0件) だが fresh hints 到着済み → 飽和カウンタをリセットして再試行: %s",
                        tracker.data["consecutive_zero_results"], obj.id,
                    )
                    tracker.data["consecutive_zero_results"] = 0
                    tracker.save()
                else:
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

            pre_zero_reason: str | None = None

            # 検索クエリ生成
            if hint_queries:
                # cooldown中のhintsを除外してから上限で切り詰め（先頭3本がクールダウンだと
                # 4本目以降の新鮮候補に到達できない旧挙動を修正）
                fresh_hints = [h for h in hint_queries if not query_on_cooldown(h["query"])]
                queries = [
                    {"query": h["query"], "lang": h.get("search_lang", obj.language)}
                    for h in fresh_hints[:MAX_QUERIES_PER_OBJECTIVE]
                ]

                # 全hintsクールダウン中 → Gap分析を強制再実行して新 hints を取得
                if not queries:
                    logger.info("全hintsクールダウン中 → Gap分析を強制再実行")
                    try:
                        from gap_analyzer import run_gap_analysis
                        run_gap_analysis(force=True)
                        hint_queries = get_hint_queries(obj.id)
                        fresh_hints = [h for h in hint_queries if not query_on_cooldown(h["query"])]
                        queries = [
                            {"query": h["query"], "lang": h.get("search_lang", obj.language)}
                            for h in fresh_hints[:MAX_QUERIES_PER_OBJECTIVE]
                        ]
                    except Exception:
                        logger.exception("Gap分析強制再実行で例外（サイクルは継続）")

                if queries:
                    logger.info(
                        "search-hintsから%d件のクエリを使用（cooldown除外後 / 元hints %d件）",
                        len(queries), len(hint_queries),
                    )
                else:
                    pre_zero_reason = "cooldown_only"
                    logger.info("Gap分析再実行後も全クエリがクールダウン中 → cooldown_only")
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
                if pre_zero_reason is None:
                    logger.warning("検索クエリ生成失敗。次の目的へ。")
                tracker.finish_cycle(0, zero_reason=pre_zero_reason)
                continue

            cycle_new_results = 0
            batch_statuses: list[str] = []
            # C1: ドメイン多様性ペナルティ用カウンタ（サイクル内で同ドメイン採用数を追跡）
            accepted_domains: dict[str, int] = {}
            DOMAIN_PENALTY_THRESHOLD = 2   # この数以上採用済みなら
            DOMAIN_PENALTY_POINTS = 2      # スコアを -2
            from urllib.parse import urlparse as _urlparse
            # D1: KPI 用の discovered カウンタ（サイクル開始時の url 記録件数をスナップショット）
            pre_url_count = len(tracker.data.get("urls", {}))

            for q in queries:
                query_text = q.get("query", str(q))
                query_lang = q.get("lang", obj.language)

                # Web検索（SearchBatch: results + status）
                batch = search_google(query_text, lang=query_lang)
                batch_statuses.append(batch.status)
                search_results = batch.results
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

                    # C1: 同サイクル内で同ドメインが既に2件以上採用されていたらペナルティ
                    domain = (_urlparse(url).netloc or "").lower()
                    domain_count = accepted_domains.get(domain, 0)
                    if domain and domain_count >= DOMAIN_PENALTY_THRESHOLD:
                        original_score = score
                        score = max(0, score - DOMAIN_PENALTY_POINTS)
                        logger.info(
                            "ドメイン多様性ペナルティ: %s (%d件採用済) %d→%d",
                            domain, domain_count, original_score, score,
                        )

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
                        # C1: ドメインカウンタ更新（採用成功時のみ）
                        if domain:
                            accepted_domains[domain] = accepted_domains.get(domain, 0) + 1

                    # 目的あたりの上限チェック
                    if cycle_new_results >= obj.max_results_per_cycle:
                        logger.info("目的あたりの上限到達 (%d件)", obj.max_results_per_cycle)
                        break

                tracker.record_query(query_text, len(search_results), accepted)

                if cycle_new_results >= obj.max_results_per_cycle:
                    break

            # "新規0件" の原因を分類して飽和カウンタに計上するかを判定
            final_zero_reason: str | None = None
            if cycle_new_results == 0 and batch_statuses:
                if all(s == "cooldown" for s in batch_statuses):
                    final_zero_reason = "cooldown_only"
                elif all(s == "422" for s in batch_statuses):
                    final_zero_reason = "all_422"
                elif not any(s == "ok" for s in batch_statuses):
                    # cooldown/422/429/error が混在し、成功0 → 観測不能
                    final_zero_reason = "search_failed"

            tracker.finish_cycle(cycle_new_results, zero_reason=final_zero_reason)
            total_inbox_written += cycle_new_results

            # D1: KPI 記録（Phase 2）
            try:
                from kpi_tracker import record_cycle as _kpi_record
                post_url_count = len(tracker.data.get("urls", {}))
                discovered_this_cycle = max(0, post_url_count - pre_url_count)
                _kpi_record(
                    obj.id,
                    batch_statuses=batch_statuses,
                    queries_total=len(queries),
                    accepted=cycle_new_results,
                    discovered=discovered_this_cycle,
                    accepted_domains=accepted_domains,
                    zero_reason=final_zero_reason,
                )
            except Exception:
                logger.exception("KPI 記録中に例外（サイクルは継続）")

            logger.info(
                "=== 目的処理完了: %s - 新規%d件 (statuses=%s, zero_reason=%s) ===",
                obj.title, cycle_new_results, batch_statuses, final_zero_reason,
            )

        # E1: Objective 自己更新ドラフト方式（Phase 2）
        # 各objectiveのKPIトレンドに基づいて、NG判定時のみドラフトを生成する。
        # 実際の objective.md は書き換えない — state/objective-drafts/ に保存するだけ。
        try:
            from objective_drafter import maybe_generate_drafts
            drafter_stats = maybe_generate_drafts()
            if drafter_stats.get("drafted", 0) > 0:
                logger.info("Objectiveドラフト: %s", drafter_stats)
            else:
                logger.debug("Objectiveドラフト: %s", drafter_stats)
        except Exception:
            logger.exception("Objectiveドラフト生成中に例外（サイクルは継続）")

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

        # D1: Ingest 後に concept_delta_rate を KPI へ反映（Phase 2）
        # 複数 active objective 時は直近 accepted>0 の全 objective に同じ delta を割り当てる
        # （厳密な per-objective 分離は Phase E 以降で改善）。
        if stats and not dry_run:
            try:
                from kpi_tracker import update_concept_delta
                from objectives import load_objectives
                delta = int(stats.get("concepts_created", 0))
                for obj in load_objectives():
                    update_concept_delta(obj.id, delta)
            except Exception:
                logger.exception("concept_delta 反映中に例外（Ingest自体は完了済）")

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
