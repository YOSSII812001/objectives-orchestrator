# objectives-orchestrator

**「知りたいテーマ」を 1 枚のメモに書いて置いておくだけ。あとは、あなたのパソコンの中で動くローカル LLM が、毎時間こっそり Web を調べて・選んで・要約して、あなたのノート（Obsidian）を育て続けてくれます。**

> テーマを与えると、自分で情報を集め、目的に近づく方法を考えはじめる ——
> いわば「自分の代わりに勉強し続けてくれる小さな AI」です。

---

## 🎯 これは何？（30 秒でわかる説明）

あなたはこんな経験ありませんか？

- 「このテーマ、ちゃんと勉強したいけど、毎日記事を探して読む時間がない」
- 「ブックマークだけ溜まって、知識として整理されないまま放置」

このツールは、そこを**全部自動**にします。仕組みはシンプルです:

1. あなたが **「目的ファイル」**（例: *「24時間動く自律 AI の作り方を理解したい」*）を 1 枚置く
2. **あなたのパソコンの中の LLM**（LM Studio）が、その目的に向けて検索キーワードを考える
3. Web を検索し、ヒットした記事に **「目的への関連度」を 10 点満点で採点**、合格した記事だけを拾う
4. 記事を**要約**して、Obsidian のノートに**自動で整理して保存**
5. これを **1 時間ごとに繰り返す** → ノートが勝手に分厚くなっていく

クラウドの ChatGPT / Claude API は**使いません**。全部あなたの PC の中で完結するので、**月額課金ゼロ・データも外に出ない**のがミソです（Web 検索 API だけ無料枠を使います）。

---

## 💡 こんな人におすすめ

- 特定テーマ（技術・投資・趣味・研究…何でも）を**継続的に深掘り**したい人
- ローカル LLM（Ollama / LM Studio など）を**遊ばせている**ので、何か働かせたい人
- Obsidian で**知識ベース（外部脳）**を育てている人
- 「AI に自律的に動いてもらう」仕組みを**自分の手で作って学びたい**人

---

## 🧩 仕組みをざっくり図解

```
┌──────────────────────────────────────────────────────────────┐
│  あなた: objectives/ に「目的ファイル(.md)」を 1 枚置くだけ   │
└───────────────────────────┬──────────────────────────────────┘
                            │
              ┌─────────────▼──────────────┐
              │          main.py           │  ← 1時間ごとに自動起動
              │   検索 → 採点 → 要約 → 整理  │
              └─────────────┬──────────────┘
                            │
   ┌────────────────────────┼─────────────────────────┐
   ▼                        ▼                         ▼
┌─────────┐         ┌───────────────┐          ┌──────────────┐
│  Brave  │         │   LM Studio   │          │   Obsidian   │
│ Search  │◀───────▶│  ローカルLLM   │◀────────▶│    Vault     │
│  (検索)  │         │ (例: Gemma 4) │          │ inbox / wiki │
└─────────┘         │  port 1234    │          │     raw      │
                    └───────┬───────┘          └──────────────┘
                            │
                            ▼
                  ┌───────────────────┐
                  │  state/ (作業記録)  │  ← 何を調べ済みかを覚えて
                  │  重複を避ける/自己評価 │     同じ記事を二度拾わない
                  └───────────────────┘
```

**登場人物（専門用語を 1 行ずつ）:**

| 用語 | かみ砕くと | 役割 |
|------|-----------|------|
| **LM Studio** | 自分の PC で LLM を動かす無料アプリ | このツールの「頭脳」。検索語を考え、記事を採点・要約 |
| **ローカル LLM** | あなたの PC の中だけで動く AI モデル | 例: **Gemma 4 E4B**（軽量・無料） |
| **Brave Search API** | プログラムから Web 検索する仕組み | このツールの「目」。無料枠あり |
| **Obsidian** | Markdown でノートを書く無料アプリ | 集めた知識の「保存先」 |
| **Vault（ヴォルト）** | Obsidian のノート保管フォルダ | この中にノートが溜まっていく |

---

## ⚙️ サイクル 1 回の流れ

1. **キーワード生成** — 目的ファイルを読んで、LM Studio が 3〜5 個の検索キーワードを考える
2. **Web 検索** — Brave Search で各キーワード最大 10 件を取得
3. **関連度の採点** — LM Studio が 0〜10 点で採点し、**7 点以上だけ**採用（ノイズを弾く）
4. **本文の取得** — 合格した記事の本文を抜き出す
5. **要約 & 受信箱へ** — Markdown に要約して `inbox/` に保存
6. **整理（Ingest）** — `inbox/` → `wiki/` に移動し、概念ノートを生成・更新
7. **次の作戦** — 「まだ分かっていないこと」を分析し、次回の検索キーワードのヒントを残す

---

## 📦 用意するもの（前提条件）

| 要件 | 最低ライン | おすすめ |
|------|-----------|---------|
| OS | Windows 10/11・macOS・Linux | Windows 11 |
| GPU メモリ (VRAM) | 8GB | 16GB 以上 |
| Python | 3.11 以上 | 3.13 |
| LM Studio | 0.4 以上 | 最新版 |
| Obsidian | 任意のバージョン | （[ai-brain](#-関連プロジェクト) スキル併用が理想） |
| Brave Search API キー | 必須（無料枠 2,000 回/月） | — |

> 💬 **GPU が非力でも大丈夫？** → **Gemma 4 E4B** は軽量なので 8GB クラスの VRAM でも動きます。まずはこれで始めて、物足りなければ大きいモデルに替えればOK。

---

## 🚀 はじめかた（手取り足取り）

### ステップ 1. Python が入っているか確認

```bash
py --version      # Windows
python3 --version # macOS / Linux
```

`3.11` 以上が表示されればOK。入っていなければ [python.org](https://www.python.org/downloads/) からインストールしてください。

> 🪟 **Windows で `py` が動かない場合**: 「設定 → アプリ → アプリ実行エイリアス」で *python.exe* のエイリアスをオフにするか、インストール時に **「Add Python to PATH」にチェック**を入れ直してください。

### ステップ 2. このリポジトリを取得して依存をインストール

```bash
git clone https://github.com/YOSSII812001/objectives-orchestrator.git
cd objectives-orchestrator
py -m pip install -r requirements.txt
```

### ステップ 3. LM Studio をセットアップ（＝頭脳を用意）

1. [LM Studio](https://lmstudio.ai) をダウンロードしてインストール
2. アプリ内の検索で **モデルをダウンロード**:
   - 🥇 まずはこれ → **`google/gemma-4-e4b`**（軽量・このツールで動作確認済み）
   - 🚀 余裕があれば → **`openai/gpt-oss-20b`**（より賢く、分類精度が上がる）
3. ダウンロードしたモデルを **ロード（Load）** する
4. 左メニューの **「Developer」→「Server」**（または Settings → Server）で **API サーバーを起動**
   - **Port は `1234`**（デフォルトのままでOK）
   - 「Auto-start on launch（起動時に自動でサーバーON）」をオンにしておくと楽

**動いているか確認:**

```bash
curl http://localhost:1234/v1/models
```

モデル名を含む JSON が返ってくれば成功です。

### ステップ 4. Brave Search API キーを取得（＝目を用意）

1. [Brave Search API ダッシュボード](https://api-dashboard.search.brave.com/) にアクセスして登録
2. **Free プラン**（2,000 クエリ/月・1 秒 1 回）を選択
3. 発行された **API キーをコピー**しておく

### ステップ 5. `.env`（設定ファイル）を作る

```bash
# Windows (PowerShell)
Copy-Item .env.example .env

# macOS / Linux
cp .env.example .env
```

`.env` を開いて、最低限ここだけ埋めます:

```ini
LMS_BASE_URL=http://localhost:1234/v1
LMS_MODEL=google/gemma-4-e4b          # LM Studio にロードしたモデル ID と一致させる
BRAVE_API_KEY=ここにあなたの Brave API キーを貼る

# Obsidian Vault の場所（未指定なら ~/Documents/Obsidian Vault が使われます）
# VAULT_PATH=C:\Users\あなたの名前\Documents\Obsidian Vault
```

> 🔐 **`.env` は絶対に GitHub へ上げないでください。** このリポジトリは `.gitignore` で `.env` を除外済みなので、普通に使っていれば事故りません。

### ステップ 6. Obsidian Vault を準備

ノートの保存先フォルダ（Vault）に、以下のフォルダ構造を作ります。
ai-brain スキルがあれば `/wiki-init` で自動生成されます。手動なら下記を作るだけ:

```
<VAULT_PATH>/
├── inbox/                 ← 集めた記事がまず入る「受信箱」
├── raw/articles/          ← 取得した生 HTML のバックアップ
└── wiki/
    ├── sources/           ← 整理済みの記事ノート
    ├── concepts/          ← 概念ごとにまとめたノート
    ├── index.md           ← （空でOK・自動更新）
    └── log.md             ← （空でOK・自動追記）
```

### ステップ 7. 「目的ファイル」を書く

`objectives/` フォルダに、調べたいテーマを 1 枚書きます（書き方は [後述](#-目的ファイルの書き方)）。
まずは同梱の **`objectives/example-objective.md`** をコピーして書き換えるのが早いです。

### ステップ 8. 試運転！

```bash
py main.py
```

LM Studio が起動していれば、検索 → 採点 → 要約が走ります。

### ステップ 9. うまくいくと、こうなります 🎉

- `<VAULT_PATH>/inbox/` に、要約済みの **Markdown 記事が数件**生成される
- `<VAULT_PATH>/raw/articles/` に、元記事の **HTML バックアップ**が残る
- `logs/orchestrator.log` に **`サイクル完了: ... inbox新規◯件`** と記録される

何も出ない場合は [困ったとき](#-困ったとき初心者向け-faq) を参照してください。

---

## ⏰ 自動運転（1 時間ごとに勝手に働かせる）

ここが本領発揮。手動実行を毎回やる必要はありません。

### Windows（タスクスケジューラに登録）

```powershell
powershell -File scripts\register_task.ps1
```

これで `ObjectivesOrchestrator` というタスクが登録され、**毎時 00 分**に自動でサイクルが回ります。

> 🤫 **静かに動きます**: このタスクは **S4U（非対話）モード**で登録されるので、毎時間ターミナルの黒い窓がポップアップすることはありません。
>
> 🛌 **LM Studio が起動していない時は、何もせず即終了**します（`run_hourly.bat` が起動前に LM Studio の生存を 3 秒で確認し、いなければそのまま撤退）。つまり「**LM Studio を立てている時だけ働く**」挙動になっています。あなたのノートが勝手に変なデータで汚れる心配はありません。

**解除したいとき:**

```powershell
powershell -File scripts\unregister_task.ps1
```

### macOS / Linux（cron）

```cron
0 * * * * cd /path/to/objectives-orchestrator && /usr/bin/python3 main.py >> logs/cron.log 2>&1
```

---

## ✍️ 目的ファイルの書き方

`objectives/<好きな名前>.md` を、次のテンプレートで作ります:

```markdown
---
title: "目的を一文で表したタイトル"
date_created: 2026-05-29
status: active              # active のものだけ拾われる（止めたい時は paused に）
priority: high              # high / normal / low
language: ja                # 検索言語のデフォルト（ja / en）
max_results_per_cycle: 5    # 1サイクルで保存する記事の上限
max_cycles: 0               # 0 = 無制限
saturation_threshold: 5     # 新規0件がN回続いたら自動で一時停止
tags: [短いタグ, 複数OK]
---
# タイトル

## ゴール
（何が分かれば「達成」と言えるか。1〜3 文で）

## 関心領域
- 領域1（できるだけ具体的なキーワードで）
- 領域2
- ...（10 個くらいが目安）

## 除外条件
- 拾ってほしくないもの（古い情報・広告記事 など）

## ステータスメモ
初期状態。
```

**書き方のコツ:**

- 🎯 **関心領域は固有名詞を入れる** — 「プロセス監視ツール」より「`systemd`, `PM2`, `Supervisor`」と書く方が、的確な記事が集まります
- 📏 **関心領域は 10 項目前後** — 広すぎると浅く、狭すぎるとすぐ「もう集めるものがない」状態（飽和）になります
- 🚫 **除外条件は 4〜6 項目** — ノイズ（広告・古い情報）を弾くのに効きます

実例は同梱の `objectives/example-objective.md` と `objectives/autonomous-ai-24-7.md` を見てください。

---

## 🔧 主な設定（`config.py`）

普段は触らなくてOKですが、調整したい時のための主要パラメータ:

| 設定 | 既定値 | 意味 |
|------|--------|------|
| `LMS_MODEL` | `google/gemma-4-e4b` | 使うローカル LLM。`.env` で上書き可 |
| `RELEVANCE_THRESHOLD` | `7` | 採用する関連度スコアの下限（0〜10） |
| `MAX_RESULTS_PER_OBJECTIVE` | `5` | 1 サイクルで保存する記事数の上限 |
| `MAX_QUERIES_PER_OBJECTIVE` | `3` | 1 サイクルで使う検索キーワード数 |
| `QUERY_COOLDOWN_HOURS` | `6` | 同じキーワードの再検索を抑える時間 |
| `SUMMARIZE_PAGE_MAX_CHARS` | `8000` | 要約時に LLM へ渡す本文の文字数上限 |
| `PROGRESS_TTL_DAYS` | `30` | 「調べ済み URL」の記憶保持日数 |

---

## 🩺 困ったとき（初心者向け FAQ）

### Q. `py` コマンドが見つからない

Python が未インストール、または PATH に通っていません。[python.org](https://www.python.org/downloads/) から入れ直し、Windows なら「Add Python to PATH」にチェックを。`python` / `python3` でも試してください。

### Q. `inbox/` に何も生成されない

次の順でチェック:
1. **LM Studio が起動してサーバーが ON か** → `curl http://localhost:1234/v1/models` で確認
2. **モデルがロードされているか**（LM Studio の画面で確認）
3. **`.env` の `BRAVE_API_KEY` が正しいか**
4. `logs/orchestrator.log` の末尾にエラーが出ていないか

### Q. ログに「LM Studio 未応答」と出てスキップされる

LM Studio のサーバーが起動していません。LM Studio を立ち上げ、モデルをロードし、サーバーを ON にしてください。**これは「異常」ではなく、頭脳が居ない時に安全に何もしない正常動作**です。

### Q. LLM の応答が空（`content=""`）になる

Gemma 系モデルは内部で「思考トークン」を消費するため、コンテキストが足りないと空応答になります。

| 症状 | 原因 | 対策 |
|------|------|------|
| `finish_reason=length`, `content=""` | 思考トークン不足 / `n_ctx` 超過 | `config.py` の `SUMMARIZE_PAGE_MAX_CHARS` を下げる |
| `400 Bad Request: n_keep >= n_ctx` | プロンプトが `n_ctx` を超過 | `SUMMARIZE_PAGE_MAX_CHARS=8000` 以下に |
| 全部「その他」に分類される | 分類用 `max_tokens` 不足 | より賢いモデル（gpt-oss-20b 等）に切替 |

> 根本的には LM Studio 側でモデルの **コンテキスト長（n_ctx）を 4096 超**に再ロードすると改善します。

### Q. 同じキーワードが毎回スキップされる

`QUERY_COOLDOWN_HOURS=6` の重複防止が効いています。`state/query_history.json` の古いエントリを消すか、目的ファイルの関心領域を更新してください。

### Q. 検索 API のレート制限に当たる

Brave の無料枠は **2,000 クエリ/月・1 秒 1 回**。複数の目的を毎時回すと超えることがあります。`browser_client.py` は 1.2 秒待機 & 429 時 60 秒待機を実装済みですが、足りなければ有料プラン（$5/月）を検討してください。

---

## 📁 ディレクトリ構成

```
objectives-orchestrator/
├── main.py                  # エントリポイント（検索サイクル + 自動Ingest）
├── config.py                # 全設定（.env の参照ポイント）
├── lm_client.py             # LM Studio API クライアント
├── browser_client.py        # Brave Search + 本文取得
├── local_ingest.py          # inbox → wiki/ の構造化（クラウド不要）
├── gap_analyzer.py          # 「まだ分からないこと」を分析し次の検索ヒントを生成
├── kpi_tracker.py           # 5 指標を記録（探索効率・新規ドメイン率など）
├── objective_drafter.py     # 停滞時に「目的ファイルの改訂案」を下書き（自動適用はしない）
├── query_history.py         # 検索キーワードのクールダウン管理
├── state_manager.py         # 二重起動防止ロック + URL の進捗追跡
├── progress_tracker.py      # URL の正規化・重複判定
├── inbox_writer.py          # inbox への書き込み（空応答ガード付き）
├── observe_cycle.py         # 統合観察レポートの生成
├── reclassify_others.py     # 「その他」カテゴリの後処理バックフィル
├── judge/                   # 自己評価モジュール（上級者向け・後述）
│   ├── judge_protocol.py
│   ├── classification_judge.py
│   ├── summary_judge.py
│   └── objective_judge.py
├── objectives/              # 🖊 あなたが書く「目的ファイル」置き場
├── scripts/
│   ├── register_task.ps1    # Windows: 毎時タスク登録（無音モード）
│   ├── unregister_task.ps1  # Windows: タスク解除
│   └── run_hourly.bat       # タスクが叩くラッパー（LM Studio 生存チェック付き）
├── state/                   # ⚙ 実行時に自動生成（.gitignore 済み・個人データ）
├── logs/                    # ログ出力先
├── .env.example             # 設定ファイルのひな型
├── requirements.txt
└── README.md
```

---

## 🛠 その他の実行モード

```bash
py main.py                          # 通常サイクル（検索 → Ingest）
py main.py --ingest-only            # inbox の整理だけ実行（検索しない）
py main.py --ingest-only --dry-run  # 整理のドライラン（実際には変更しない）
py main.py --gap-only --force       # 「次に何を調べるか」の分析だけ強制実行
```

---

## 🧠 Judge システム（上級者向け・自己評価のしくみ）

このツールは、自分の仕事ぶりを**自分で採点**する仕組みを持っています。
**既存の処理には一切手を加えず**、ログ・wiki・目的ファイルを読んで採点レポートを
`state/judge_reports/` に書き出すだけの「観察モード」です。

| Judge | 何を見るか | 主な指標 |
|-------|-----------|---------|
| **ClassificationJudge** | 記事の分類処理 | JSON 空応答率、「その他」比率 |
| **SummaryJudge** | 要約の品質 | 空応答スキップ率、フォーマット準拠率 |
| **ObjectiveJudge** | 目的の達成度 | 関心領域カバレッジ、知識ギャップ解消率 |

```bash
py judge/classification_judge.py
py judge/summary_judge.py
py judge/objective_judge.py --dry-run   # レポートを書かずに確認だけ

py observe_cycle.py                      # 直近1時間の統合レポート
```

---

## 🗺 ロードマップ

| フェーズ | 内容 | 状態 |
|----------|------|------|
| Phase 0 | Judge 観察モード（読むだけ・採点レポート生成） | ✅ 実装済 |
| Phase 0.4 | QueryJudge（検索キーワード品質の観察） | 未着手 |
| Phase 0.5 | 日次の統合レポート強化 | 一部実装（`observe_cycle.py`） |
| Phase 1 | 提案モード（改善案を書き出し、人間が承認して適用） | 未着手 |
| Phase 2 | 自律モード（提案の自動査読・A/B テスト・悪化時ロールバック） | 一部実装（`objective_drafter.py`） |

---

## 🧬 設計思想 ——「自分を作るために、自分が探求する」

このツールに与える最初の目的は **「24 時間 365 日稼働する自律型 AI の構築」** です。
すると、Temporal / Self-healing / Ambient Agents といった概念が wiki に蓄積されていきます。

そこで見つかった **「LLM-as-Judge（AI が AI を採点する）」パターン**を、
このツール自身の自己評価機能（Judge 系統）に逆輸入しました。
**調べる対象の設計が、調べる側の設計に跳ね返る** —— そんなメタ再帰的な構造で育てています。

---

## 🔗 関連プロジェクト

- **ai-brain** — Obsidian Vault を raw / wiki / CLAUDE.md の 3 層で管理する Claude Code スキル。このツールはその「自動収集係」にあたります
- **Claude Code** — 本プロジェクトの設計・実装を支援した開発ツール

---

## 📜 ライセンス

MIT License — [LICENSE](LICENSE) を参照してください。

---

## 🤝 コントリビュート

Issue / PR を歓迎します。送る前に以下をご確認ください:

- **`.env` はコミットしない**でください（`.gitignore` 済み）
- **`state/` 配下の個人データ**もコミット対象外です
- 新しい Judge を追加する場合は `judge/judge_protocol.py` の `JudgeVerdict` を使ってください
