# TODO

このファイルは作業メモ用の任意ファイルです。`tasks/handoff_*.md` と `tasks/lessons.md` は `.gitignore` により除外されます。

## 2026-04-23 自己成長ループ改修（Codex相談経由で確定）

根本問題: cooldown→全クエリスキップ→0件→飽和カウンタ誤増加→回復不能停止。
「毎日同じ記事」の表層要因（temperature/クエリ固定）より先に**観測構造**を直す。

### Phase 1: 回復ロジック修正（最優先 / 今セッションで実装）

- [ ] `browser_client.py`: `search_google()` の返り値を `SearchBatch(results, status)` に昇格
  - `status ∈ {"ok","cooldown","422","429","error"}` で呼び出し側に状態を伝搬
- [ ] `state_manager.py`: `UrlStageTracker.finish_cycle(new_results, zero_reason=None)` にパラメータ追加
  - `zero_reason ∈ {"cooldown_only","all_422","search_failed"}` のとき `consecutive_zero_results` を増やさない
- [ ] `main.py`: hints選抜を「cooldown除外後に先頭N本」に変更
  - `query_history.should_skip` で hints 全体をフィルターしてから `[:MAX_QUERIES_PER_OBJECTIVE]`
  - 全hintsクールダウン時は `run_gap_analysis(force=True)` 実行後に再選抜
  - queries が空なら `zero_reason="cooldown_only"` で finish_cycle
- [ ] `main.py`: `search_google` 呼び出しを SearchBatch 対応に変更、batch status を記録
  - 全クエリが `status != "ok"` だった場合 `zero_reason="search_failed"` で finish_cycle

### Phase 1 検証
- [ ] `py main.py --gap-only --force` で Gap 分析を強制実行し、新 hints が生成されるか確認
- [ ] `py main.py` で通常サイクルを手動1回実行し、ログに `zero_reason` が出るか、`consecutive_zero_results` が誤増加しないか確認

### Phase 2 (2026-04-24 実装完了)
- [x] **A1** LM Studio 未応答時の早期スキップ（120s→30s、ログ1行集約）
- [x] **B1** gap_analyzer.py: temperature 0.4 → 0.6、avoid-list（直近ユニーク10）、探索軸3分類（implementation/operations/alternatives）
- [x] **B2** browser_client.py: `_auto_correct_lang` で英語クエリに ja→en 自動補正（422削減）
- [x] **C1** main.py: サイクル内ドメイン多様性ペナルティ（同ドメイン 2件以上採用済で -2点）
- [x] **D1** kpi_tracker.py 新設: 5指標を `state/kpi/<objective_id>.jsonl` 追記
      (effective_query_rate / useful_source_rate / novel_domain_rate / stagnation_hours / concept_delta_rate)
- [x] **E1** objective_drafter.py 新設: KPI NG判定時のみ `state/objective-drafts/` に改訂案。直接書き換えなし

### Phase 2 検証
- [x] 全モジュール import OK
- [x] `recent_unique_queries(limit=5, ok_only=True)` が正しい順序で返却
- [x] `_auto_correct_lang` が日英混在を正しく判定
- [x] `kpi_tracker.record_cycle` → `update_concept_delta` が連携動作
- [x] LM Studio停止下で `py main.py` が30s待機→早期スキップで完了（50s、以前は125s）
- [ ] LM Studio 稼働下での End-to-Endテスト（ユーザーが LM Studio 起動後の 20:00 Task Scheduler 実行で検証）

### Phase 3 以降（後続検討）
- [ ] 10:00 の PermissionError 原因調査（state_manager.save の排他）
- [ ] URL重複判定にタイトル類似度
- [ ] sources_created を重複スキップ除外版に修正
- [ ] active objective 2-3個化（E1で複数対応したが、新 objective の追加は未）
- [ ] RSS/HN/ArXiv ソース追加（diversity penalty の後で優先度再判定）
- [ ] drafter の LLM 呼び出しで n_ctx 4096 の制約と相性確認（長文 objective.md でプロンプト溢れ懸念）

## 公開済みロードマップ（既存）

- [ ] Phase 0.4: QueryJudge（クエリ生成品質の観察）
- [ ] Phase 0.5: 日次統合レポート強化
- [ ] Phase 1: 提案モード（`state/judge_proposals/` に書き出し、人間承認フロー）
- [ ] Phase 2: 自律モード（MetaJudge + A/B テスト）
- [ ] token ベース切り詰め（chars ベースの `SUMMARIZE_PAGE_MAX_CHARS` を置換）
- [ ] 他ローカル LLM 対応検証（Qwen3, Llama3 等）
