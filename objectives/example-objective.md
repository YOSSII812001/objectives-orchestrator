---
title: "調査したい目的の一文タイトル"
date_created: 2026-04-23
status: inactive          # 有効化する場合は active に変更
priority: normal          # high / normal / low
language: ja              # 検索のデフォルト言語
max_results_per_cycle: 5  # 1サイクルあたりの inbox 書き込み上限
max_cycles: 0             # 0 = 無制限
saturation_threshold: 5   # 飽和判定の閾値（目安）
tags: [sample, template]
---
# 調査したい目的の一文タイトル

## ゴール
（1-3 文で、何を理解できれば目的達成かを明記する）

例:
> Rust の Web アプリケーションフレームワーク（Actix / Axum / Rocket / Warp）の
> 設計思想・エコシステム・本番運用事例を体系的に把握し、新規プロジェクトでの
> 技術選定に必要な判断材料を揃える。

## 関心領域
（具体的なキーワード・ライブラリ名・技術名を含めると ObjectiveJudge の
カバレッジ評価が実態と一致しやすい。10 項目前後が目安）

- 例: Axum のミドルウェア設計と tower エコシステムの統合
- 例: Actix-Web の actor モデルと async/await の関係
- 例: Tokio ランタイムとタスクスケジューリング
- 例: Diesel / SQLx / SeaORM の比較と async 対応状況
- 例: 本番デプロイパターン（Lambda, Cloud Run, Fly.io）
- 例: トレーシング（tracing crate, OpenTelemetry 連携）
- 例: ベンチマーク比較（TechEmpower 等）
- 例: エラーハンドリング（thiserror, anyhow）
- 例: WebSocket / SSE 実装パターン
- 例: 認証・認可ライブラリ（jsonwebtoken, axum-login）

## 除外条件
（4-6 項目が目安）

- 2023 年以前の古い記事（フレームワーク仕様が大きく変わっている）
- 単純な Hello World チュートリアル
- 広告記事・アフィリエイト記事
- 哲学的議論（「なぜ Rust か」等）
- 他言語 (Go/Node.js/Python) との機能比較で Rust が脇役の記事

## ステータスメモ
初期状態。まだ検索サイクルは実行されていない。

---

## このファイルの使い方

1. このファイルを `objectives/<your-slug>.md` にコピー（ファイル名は英数字ハイフン推奨）
2. `status: active` に変更
3. `title` / `ゴール` / `関心領域` / `除外条件` をあなたのテーマに書き換える
4. `py main.py` を実行（または Task Scheduler 自動実行）
5. 結果は Obsidian vault の `wiki/sources/` と `wiki/concepts/` に蓄積される
6. Judge レポート（`state/judge_reports/`）で進捗と品質を確認

複数目的を並行させる場合は、このファイルを別 slug で複数作成してください。
