---
name: objectives-orchestrator
description: |
  目的駆動型自律知識成長システムのセットアップ・運用ガイド。
  LM Studio（ローカルLLM）+ Brave Search APIで自律的にWeb検索→素材収集→
  Obsidian inbox投入を行い、ai-brainの外部脳を目的に向かって成長させる。
  新規PCへの導入、設定変更、トラブルシューティング、Judge運用に使用。
  トリガー: "objectives-orchestrator", "自律知識成長", "オーケストレーター導入",
  "外部脳自動化", "LM Studio連携", "objectives setup"
---

# objectives-orchestrator — セットアップ・運用ガイド

## 概要

ユーザーが「目的ファイル」を配置するだけで、ローカルLLMが自律的にWeb検索→
素材収集→Obsidian inbox投入を行い、ai-brainと連携してナレッジベースが
目的に向かって自動成長するシステム。

**リポジトリ**: https://github.com/YOSSII812001/objectives-orchestrator

## このスキルの位置付け

このスキルは git clone された `objectives-orchestrator/` プロジェクトと一緒に
配布される。Claude Code はこの SKILL.md を自動読み込みして、ユーザーからの
「objectives-orchestrator をセットアップしたい」「目的ファイルを追加したい」
等の質問に応じる。

**本体コードの位置**: リポジトリルート（例: `~/objectives-orchestrator/`）
- `main.py` — エントリポイント
- `config.py` — 設定集約（env 参照）
- `lm_client.py` — LM Studio API
- `browser_client.py` — Brave Search API
- `local_ingest.py` — inbox → wiki/ 構造化
- `judge/` — Classification / Summary / Objective 3 系統

詳細は [README.md](../../README.md) を参照。

## 前提条件

| 要件 | 最小 | 推奨 |
|------|------|------|
| OS | Windows 10/11, macOS, Linux | Windows 11 |
| VRAM | 8GB | 16GB 以上 |
| Python | 3.11+ | 3.13 |
| LM Studio | 0.4+ | 最新 |
| Obsidian | 任意バージョン | ai-brain skill 併用推奨 |
| Brave API キー | 必須 | [取得先](https://api-dashboard.search.brave.com/) |

## セットアップ手順（要約）

### Step 1: Obsidian Vault の準備

ai-brain skill が未導入なら先に `/wiki-init` を実行。vault 構造:

```
<VAULT_PATH>/
├── inbox/
├── raw/articles/
└── wiki/
    ├── sources/
    ├── concepts/
    ├── index.md
    └── log.md
```

### Step 2: LM Studio

1. https://lmstudio.ai からインストール
2. モデルをロード（推奨: `google/gemma-4-e4b` 最小 / `openai/gpt-oss-20b` 高品質）
3. Settings → Server → Port 1234 で API サーバー起動

### Step 3: Brave Search API キー

https://api-dashboard.search.brave.com/ で発行（Free: 2000 queries/月）

### Step 4: リポジトリ clone & 環境変数

```bash
git clone https://github.com/YOSSII812001/objectives-orchestrator.git
cd objectives-orchestrator
py -m pip install -r requirements.txt
cp .env.example .env
# .env を編集して BRAVE_API_KEY を埋める
```

### Step 5: 目的ファイル

`objectives/<slug>.md` に [example-objective.md](../../objectives/example-objective.md) を
ベースとして作成。

### Step 6: 実行

```bash
py main.py                          # 通常サイクル
py main.py --ingest-only            # Ingestのみ
py main.py --ingest-only --dry-run  # ドライラン
```

毎時自動実行: `scripts/register_task.ps1`（Windows Task Scheduler）

## Judge 運用

Phase 0 観察モード — 読み取り専用で既存ロジックに触れない:

```bash
py judge/classification_judge.py --since 2026-04-23T00:00:00
py judge/summary_judge.py --since 2026-04-23T00:00:00
py judge/objective_judge.py

# 統合観察
py observe_cycle.py --since 2026-04-23T09:00:00
```

レポート出力先: `state/judge_reports/`, `state/observation_reports/`

### Judge が検出する代表的症状

| 症状 | Judge | 代表的対策 |
|------|-------|-----------|
| `classify_category` 空応答率 > 30% | ClassificationJudge | `classify_category` の max_tokens を 1024 以上に |
| `summarize_page` 空応答率 > 30% | SummaryJudge | `SUMMARIZE_PAGE_MAX_CHARS` を下げて prompt_tokens を減らす（推奨 8000） |
| stub 比率 > 70% | ObjectiveJudge | ドラフト昇格を促進、wiki-compile で統合 |
| 孤立 concept 比率 > 30% | ObjectiveJudge | summarize プロンプトで `[[...]]` を強制 |
| 関心領域カバレッジ < 50% | ObjectiveJudge | search-hints を再生成、関心領域の表現を concept 名に寄せる |

## トラブルシューティング

### LM Studio 空応答（`finish_reason=length`, `content=""`）

Gemma 系は **~450 tokens の内部思考** を消費。さらに LM Studio の実コンテキスト
`n_ctx=4096` が真の上限。診断順序:

1. `logs/orchestrator.log` で `prompt_tokens` を確認（`_chat` の診断ログ）
2. 400 Bad Request 時は `n_keep: NNNN >= n_ctx: 4096` メッセージを探す
3. `SUMMARIZE_PAGE_MAX_CHARS` を 8000 まで下げる（`max_tokens` を上げても無効）
4. 根治: LM Studio 側で Gemma を n_ctx > 4096 で再ロード

詳細: `~/.claude/projects/*/memory/feedback_gemma_thinking_tokens.md`

### `index.md のパースに失敗`

BOM 混入が原因。`parse_index` は `utf-8-sig` で読むため自動修復されるが、
手動書き換え時は BOM なし（UTF-8 without BOM）で保存すること。

### クエリが全部クールダウンでスキップされる

`state/query_history.json` の古いエントリを削除、または `search-hints/*.json` に
新規クエリを追加。`QUERY_COOLDOWN_HOURS=6` が config.py のデフォルト。

## 目的ファイルの書き方（推奨事項）

- **関心領域に具体名を含める**: 「プロセス監視」ではなく「systemd, PM2, Supervisor」
- **10 項目前後**: 広すぎると浅い、狭すぎると早期飽和
- **除外条件 4-6 項目**: 古い情報、広告記事、哲学論、等
- **priority: high** の目的が search-hints 生成で優先される

サンプル: `objectives/example-objective.md`

## 重要な運用メモ

### 秘匿情報の扱い

- `.env` は **絶対に git にコミットしない**（.gitignore 済み）
- `BRAVE_API_KEY` は `.env` のみ。`browser_client.py` にハードコードしない
- コミット前に `grep -r "key\|token\|secret" *.py` で誤混入チェック

### 3 層防御の挙動

- **gap-cooldown**: `GAP_ANALYSIS_INTERVAL_HOURS=6` 以内に gap 分析を再実行しない
- **query-cooldown**: `QUERY_COOLDOWN_HOURS=6` 以内の同一クエリはスキップ
- **URL-tracking**: 処理済み URL を `state/progress/` に記録、再取得スキップ

これらは API quota 節約に有効だが、検証時に「サイクルが空振る」副作用もある。
MAX_QUERIES_PER_OBJECTIVE=3 と合わせて、未実行クエリを先頭に置くか、
query_history を手動調整して検証する。

### LM Studio n_ctx の確認

LM Studio の Model Settings で `n_ctx` を確認。4096 だと空応答が多発するため、
可能なら 8192 以上に再ロードするのが根治策。
