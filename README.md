# objectives-orchestrator

**目的駆動型の自律知識成長システム** — ユーザーが「目的ファイル」を 1 枚置くだけで、
ローカル LLM（LM Studio）が Web 検索 → スコアリング → 要約 → ナレッジベース構造化を
自動で繰り返し、Obsidian vault（ai-brain）を目的に向かって成長させます。

> *テーマを与えると、自発的に情報を収集し、目的を達成するための方法を考えつく。*

---

## 特徴

- **人間介入ゼロ運用** — `objectives/*.md` を置くだけで、検索からナレッジベース構造化まで全自動
- **ローカル LLM 完結** — LM Studio（Gemma 4 E4B / gpt-oss-20b 等）で Claude API 不要
- **3 層防御** — gap-cooldown / query-cooldown / URL-tracking で重複探索を抑制
- **Judge 観察モード** — Classification / Summary / Objective の 3 系統が品質を定量評価
- **メタ再帰** — Ambient Agents パターン（Judge が自己修正シグナルを出す設計）を自身に適用
- **Windows / macOS / Linux 対応** — 個人パスは環境変数化済み

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────┐
│  ユーザー: objectives/*.md を配置                              │
└───────────────────────┬─────────────────────────────────────────┘
                        │
         ┌──────────────▼───────────────┐
         │         main.py              │
         │  run_cycle + run_auto_ingest │
         └──────────────┬───────────────┘
                        │
   ┌────────────────────┼───────────────────────┐
   ▼                    ▼                       ▼
┌────────┐        ┌────────────┐         ┌─────────────┐
│ Brave  │        │ LM Studio  │         │  Obsidian   │
│ Search │◀──────▶│ (Gemma等)  │◀───────▶│   Vault     │
│  API   │        │ port 1234  │         │  wiki/      │
└────────┘        └────────────┘         │  inbox/     │
                        │                │  raw/       │
                        │                └─────────────┘
                        ▼
               ┌─────────────────┐
               │  state/         │
               │  judge_reports/ │◀── Judge 3系統が観察
               │  search-hints/  │    （Classification/Summary/Objective）
               │  progress/      │
               └─────────────────┘
```

### サイクルの流れ

1. **クエリ生成** — 目的ファイルから LM Studio が 3-5 本の検索クエリを生成
2. **Web 検索** — Brave Search API で各クエリ最大 10 件を取得
3. **関連性スコアリング** — LM Studio が 0-10 で採点、7 以上を採用
4. **ページ本文取得** — httpx でサブセットを抽出（`article` → `main` → ...）
5. **要約 & inbox 投入** — vault 形式 Markdown を inbox に書き出し
6. **ローカル Ingest** — inbox → wiki/sources/ 移動、wiki/concepts/ 生成・更新、index.md 更新
7. **Gap 分析 & 次クエリ** — gap_analyzer が「何がまだ分からないか」を判定、次回用 search-hints を生成

---

## 前提条件

| 要件 | 最小 | 推奨 |
|------|------|------|
| OS | Windows 10/11, macOS, Linux | Windows 11 |
| VRAM | 8GB | 16GB 以上 |
| Python | 3.11+ | 3.13 |
| LM Studio | 0.4+ | 最新 |
| Obsidian | 任意バージョン | ai-brain skill 併用推奨 |
| Brave Search API キー | 必須（[発行はこちら](https://api-dashboard.search.brave.com/)） | Free tier: 2000 queries/month |

---

## クイックスタート

### 1. クローン & 依存インストール

```bash
git clone https://github.com/YOSSII812001/objectives-orchestrator.git
cd objectives-orchestrator
py -m pip install -r requirements.txt
```

### 2. `.env` の作成

```bash
# Windows (PowerShell)
Copy-Item .env.example .env

# macOS / Linux
cp .env.example .env
```

`.env` を開いて以下を埋めます:

```ini
LMS_BASE_URL=http://localhost:1234/v1
LMS_MODEL=google/gemma-4-e4b
BRAVE_API_KEY=<あなたの Brave API キー>
# VAULT_PATH=C:\path\to\your\Obsidian Vault   # 未指定時は ~/Documents/Obsidian Vault
```

### 3. Obsidian Vault の準備

vault 直下に以下の構造が必要です（ai-brain skill があれば `/wiki-init` で自動作成）:

```
<VAULT_PATH>/
├── inbox/
├── raw/articles/
└── wiki/
    ├── sources/
    ├── concepts/
    ├── index.md       (空でOK、自動更新される)
    └── log.md         (空でOK、自動追記される)
```

### 4. LM Studio のセットアップ

1. [LM Studio](https://lmstudio.ai) をインストール
2. モデルをダウンロード（推奨: **Gemma 4 E4B** — 軽量で動作確認済み / **openai/gpt-oss-20b** — より高精度）
3. API サーバーを有効化（Settings → Server → Port 1234）

### 5. 動作確認

```bash
py -c "from config import VAULT_PATH, BRAVE_API_KEY; print(VAULT_PATH, bool(BRAVE_API_KEY))"
```

### 6. 初回サイクル

```bash
py main.py
```

---

## 目的ファイルの書き方

`objectives/<your-slug>.md` に以下のテンプレートで作成:

```markdown
---
title: "目的を一文で表したタイトル"
date_created: 2026-04-23
status: active              # active のみ orchestrator が拾う
priority: high              # high / normal / low
language: ja                # 検索言語のデフォルト
max_results_per_cycle: 5    # 1サイクルの inbox 書き込み上限
max_cycles: 0               # 0 = 無制限
saturation_threshold: 5
tags: [短いタグ, 複数OK]
---
# タイトル

## ゴール
（何を理解できれば完了か。1-3 文）

## 関心領域
- 領域1（具体的なキーワードを含める）
- 領域2
- ... 10 個程度が目安

## 除外条件
- 対象外の資料（古い情報、広告記事、等）

## ステータスメモ
初期状態。
```

**書き方のコツ**:
- **関心領域は具体名を含める**。「プロセス監視ツール」より「systemd, PM2, Supervisor」と書くと ObjectiveJudge のカバレッジ評価が実態と一致する
- **10 項目前後**が目安。広すぎると浅い、狭すぎると早期飽和
- **除外条件は 4-6 項目**が目安

サンプルは `objectives/example-objective.md` を参照してください。

---

## Judge システム（Phase 0 観察モード）

orchestrator 自身が自己評価する仕組み。**既存ロジックには一切触れず**、ログ・wiki・目的ファイルを
読んで Markdown レポートを `state/judge_reports/` に書き出すだけ。

| Judge | 観察対象 | 主要メトリクス |
|-------|----------|---------------|
| **ClassificationJudge** | `local_ingest.classify_category()` | JSON 空応答率、`その他` カテゴリ比率 |
| **SummaryJudge** | `summarize_page()` + sources/*.md | 空応答スキップ率、フォーマット準拠率、コンセプト抽出数 |
| **ObjectiveJudge** | `objectives/*.md` vs `wiki/concepts/` | 関心領域カバレッジ、target_concept 到達率、knowledge_gap 解消率、stub 比率、孤立 concept 比率 |

### 実行方法

```bash
# 全 Judge を今日のログに対して実行
py judge/classification_judge.py
py judge/summary_judge.py
py judge/objective_judge.py

# 特定時刻以降のみ解析
py judge/summary_judge.py --since 2026-04-23T09:00:00

# ドライラン（レポート書き出しなし）
py judge/objective_judge.py --dry-run
```

### 統合観察レポート

```bash
py observe_cycle.py                             # 直近1時間
py observe_cycle.py --since 2026-04-23T00:00:00 # 日次集計
```

出力先: `state/observation_reports/`

---

## ディレクトリ構成

```
objectives-orchestrator/
├── main.py                  # エントリポイント (run_cycle + run_auto_ingest)
├── config.py                # 全設定（env 参照ポイント）
├── lm_client.py             # LM Studio API クライアント
├── browser_client.py        # Brave Search + httpx ページ取得
├── local_ingest.py          # inbox → wiki/ 構造化（Claude不要）
├── gap_analyzer.py          # knowledge_gap 自動生成
├── state_manager.py         # cycle.lock + URL 段階追跡
├── query_history.py         # クエリクールダウン管理
├── progress_tracker.py      # URL 正規化
├── inbox_writer.py          # inbox 書き込み（空応答ガード付き）
├── observe_cycle.py         # 統合観察レポート生成
├── judge/
│   ├── judge_protocol.py          # JudgeVerdict 共通データモデル
│   ├── classification_judge.py    # 分類品質 Judge
│   ├── summary_judge.py           # 要約品質 Judge
│   └── objective_judge.py         # 目的達成度 Judge
├── objectives/              # 目的ファイル（ユーザー入力）
├── state/                   # ランタイム状態（.gitignore で除外）
│   ├── cycle.lock
│   ├── query_history.json
│   ├── gap_analysis_state.json
│   ├── progress/            # URL 段階追跡
│   ├── judge_reports/       # Judge 出力
│   ├── observation_reports/ # 統合観察
│   └── search-hints/        # 次サイクル用クエリ
├── scripts/
│   ├── register_task.ps1    # Windows Task Scheduler 登録
│   ├── unregister_task.ps1
│   └── run_hourly.bat
├── .claude/skills/          # Claude Code スキル（同梱）
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 運用

### 手動実行

```bash
py main.py                          # 通常サイクル（検索→Ingest）
py main.py --ingest-only            # Ingest のみ
py main.py --ingest-only --dry-run  # ドライラン（変更なし）
```

### Windows Task Scheduler で毎時実行

```powershell
powershell -File scripts\register_task.ps1
```

登録された `ObjectivesOrchestrator` タスクが毎時 00 分に `py main.py` を実行します。
解除は:

```powershell
powershell -File scripts\unregister_task.ps1
```

### macOS / Linux の cron 例

```cron
0 * * * * cd /path/to/objectives-orchestrator && /usr/bin/python3 main.py >> logs/cron.log 2>&1
```

---

## トラブルシューティング

### Q. LM Studio から空応答（`content=""`）が返る

**A.** Gemma 系モデルは内部思考トークンを消費するため `max_tokens` が不足すると空応答になります。さらに、LM Studio の **実コンテキスト `n_ctx=4096`** が真の上限です。

| 症状 | 原因 | 対策 |
|------|------|------|
| `finish_reason=length`, `content=""` | 思考トークン予算不足 or n_ctx 超過 | `config.py` の `SUMMARIZE_PAGE_MAX_CHARS` を下げる |
| 400 Bad Request: `n_keep >= n_ctx` | prompt が n_ctx 超過 | `SUMMARIZE_PAGE_MAX_CHARS=8000` 以下に |
| すべての分類が「その他」 | `classify_category` の max_tokens=64 が不足 | 1024 以上に底上げ |

`lm_client._chat()` は `usage.completion_tokens` と `finish_reason` をログ出力します。
`logs/orchestrator.log` の警告を確認してください。

### Q. `index.md のパースに失敗` と出る

**A.** `index.md` 先頭に BOM (`﻿`) が混入している可能性。`parse_index` は `utf-8-sig` で読むため修復は自動ですが、手動書き換え時に BOM を付けないよう注意。

### Q. 同じクエリが毎サイクルスキップされる

**A.** `QUERY_COOLDOWN_HOURS=6` の 3 層防御が効いています。`state/query_history.json` を確認して古いエントリを削除するか、search-hints に新しいクエリを追加してください。

### Q. 新規 concept が「その他」に落ちる

**A.** Gemma 4 E4B の分類精度に限界があります。`reclassify_others.py` で後処理バックフィル、または `.env` で `LMS_MODEL=openai/gpt-oss-20b` に切替を検討。

---

## ロードマップ

| フェーズ | 内容 | 状態 |
|----------|------|------|
| Phase 0 | Judge 観察モード（読み取り専用、レポート生成のみ） | ✅ 実装済（Classification / Summary / Objective） |
| Phase 0.4 | QueryJudge 追加（クエリ生成品質の観察） | 未着手 |
| Phase 0.5 | 日次統合レポート（全 Judge を集約） | 一部実装（`observe_cycle.py`） |
| Phase 1 | 提案モード（Judge が改善案を `state/judge_proposals/` に書き出し、人間が承認ファイルで適用） | 未着手 |
| Phase 2 | 自律モード（MetaJudge が提案を査読、A/B テストで効果検証、悪化検知で自動 rollback） | 未着手 |

Phase 1 以降の詳細は [`docs/llm-as-judge-design.md`](docs/llm-as-judge-design.md) を参照。

---

## 設計思想

> **このシステム自身が、探求するテーマで自身を構築する。**

目的「24 時間 365 日稼働する自律型 AI の構築」を orchestrator に与えると、Temporal / Ambient Agents / Self-healing などの概念が wiki に蓄積されます。そこで発見された **llm-as-judge パターン** を orchestrator 自身に適用したのが Judge 系統です。情報収集対象の設計が、収集者の設計に跳ね返るメタ再帰的構造です。

---

## 関連プロジェクト

- **ai-brain skill** — Obsidian vault を raw/wiki/CLAUDE.md の 3 層構造で管理する Claude Code スキル。このプロジェクトは ai-brain が前提
- **Claude Code** — Claude が本プロジェクトの設計・実装を支援したツール

---

## License

MIT License — [LICENSE](LICENSE) を参照

---

## Contributing

Issue / PR 歓迎します。コントリビュート前に以下を確認してください:

- `.env` はコミットしないでください（`.gitignore` 済み）
- `state/` 配下の個人データもコミット対象外です
- 新しい Judge を追加する場合は `judge/judge_protocol.py` の `JudgeVerdict` を使用してください
