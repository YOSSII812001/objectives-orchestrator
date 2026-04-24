"""設定モジュール - 全パスとAPI設定を一元管理"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# === パス設定 ===
ORCHESTRATOR_DIR = Path(__file__).parent
OBJECTIVES_DIR = ORCHESTRATOR_DIR / "objectives"
STATE_DIR = ORCHESTRATOR_DIR / "state"
SEARCH_HINTS_DIR = STATE_DIR / "search-hints"
PROGRESS_DIR = STATE_DIR / "progress"
LOGS_DIR = ORCHESTRATOR_DIR / "logs"
LOCK_FILE = STATE_DIR / "cycle.lock"

# Obsidian Vault の実体パス。.env で VAULT_PATH を指定可能。
# 未指定の場合は ~/Documents/Obsidian Vault を使用（Windows/Mac/Linux 共通）。
VAULT_PATH = Path(os.getenv("VAULT_PATH", str(Path.home() / "Documents" / "Obsidian Vault")))
INBOX_DIR = VAULT_PATH / "inbox"
RAW_ARTICLES_DIR = VAULT_PATH / "raw" / "articles"

# === Wiki パス ===
WIKI_DIR = VAULT_PATH / "wiki"
WIKI_SOURCES_DIR = WIKI_DIR / "sources"
WIKI_CONCEPTS_DIR = WIKI_DIR / "concepts"
WIKI_INDEX_PATH = WIKI_DIR / "index.md"
WIKI_LOG_PATH = WIKI_DIR / "log.md"

# === LM Studio API ===
LMS_BASE_URL = os.getenv("LMS_BASE_URL", "http://localhost:1234/v1")
# Gemma 4 E4B を明示的に固定（複数モデル読み込み時の暴発を防ぐ）
LMS_MODEL = os.getenv("LMS_MODEL", "google/gemma-4-e4b")
LMS_TIMEOUT = 120  # 秒

# === Brave Search API ===
# キーは .env に設定。未設定時は browser_client 側でエラーを出す。
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")

# === LLMプロンプト安全上限（Gemma 4 E4B コンテキスト保護） ===
# ローカル書き込みは無制限。これはLLMに送る入力トークン量の上限のみ。
PROMPT_MAX_CHARS = 8000               # LLMプロンプトに埋め込む文脈の上限
PROMPT_MAX_BODY_CHARS = 5000          # 既存bodyをプロンプトに含める際の上限
# LM Studio の Gemma 4 E4B は実測 n_ctx=4096 で稼働している（2026-04-23 Codex 調査）。
# 12000 chars だと prompt だけで窓を食い切り content_len=0 になるため 8000 に縮小。
# dev.to 実測: prompt_tokens 3650→2412、content_len 0→1162 に改善確認済。
SUMMARIZE_PAGE_MAX_CHARS = 8000       # summarize_page に渡すページ文字数上限
# max_tokens を 8192 にしても total_tokens=4096 で頭打ちになるため 4096 のまま固定。
# 根治には LM Studio 側で n_ctx を 4096 超に再ロードする必要がある。
SUMMARIZE_PAGE_MAX_TOKENS = 4096      # summarize_page の出力トークン上限

# === agent-browser ===
# agent-browser 本体へのフルパス。Chrome 互換性問題のため現在は未使用。
# 有効化する場合は .env で AGENT_BROWSER_CMD を指定。
AGENT_BROWSER_CMD = os.getenv("AGENT_BROWSER_CMD", "")
SEARCH_ENGINE_URL = "https://www.google.com/search?q={query}"
PAGE_FETCH_TIMEOUT = 30  # 秒

# === パイプライン設定 ===
MAX_RESULTS_PER_OBJECTIVE = 5  # 1目的あたりの最大inbox書き込み数
MAX_QUERIES_PER_OBJECTIVE = 3  # 1目的あたりの最大検索クエリ数
RELEVANCE_THRESHOLD = 7  # 0-10スコアでこれ以上を採用
MAX_RETRIES_JSON = 2  # JSON解析失敗時のリトライ回数

# === 状態管理 ===
PROGRESS_TTL_DAYS = 30  # URL記録の保持日数
PROGRESS_MAX_URLS = 1000  # 1目的あたりのURL上限
LOCK_STALE_MINUTES = 90  # これ以上古いロックは無視

# === ログ ===
LOG_FILE = LOGS_DIR / "orchestrator.log"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 5

# === Claude CLI ===
CLAUDE_CMD = "claude"
CLAUDE_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep"
GAP_ANALYSIS_INTERVAL_HOURS = 6  # gap分析の最小間隔

# === クエリ実行履歴 ===
QUERY_COOLDOWN_HOURS = 6  # 同一クエリの再実行を抑制する時間（時間）

# === Phase 2: Objective 自己更新ドラフト ===
# `state/objective-drafts/` に改訂案を書き出す（直接適用はしない）。
# 健康度がNGと判定された時のみ LLM 呼び出しが走る。
OBJECTIVE_DRAFTS_DIR = STATE_DIR / "objective-drafts"
DRAFTER_INTERVAL_HOURS = 24                # 最低実行間隔
DRAFTER_STAGNATION_HOURS_THRESHOLD = 24.0  # 停滞と判定するKPI閾値
DRAFTER_NOVEL_DOMAIN_RATE_THRESHOLD = 0.2  # 新規ドメイン率がこれ未満で連続時NG
DRAFTER_CONSECUTIVE_NG_CYCLES = 3          # NGが何サイクル連続したらドラフト生成するか
