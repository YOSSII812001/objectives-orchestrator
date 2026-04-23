"""ローカルLLM (LM Studio) によるIngestパイプライン

inbox/*.md → wiki/sources/ 移動 + wiki/concepts/ 生成/更新 + index.md/log.md 更新
Claude不要。LM Studio APIで完結。
"""
import logging
import re
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path

import frontmatter

from config import (
    INBOX_DIR,
    WIKI_CONCEPTS_DIR,
    WIKI_DIR,
    WIKI_INDEX_PATH,
    WIKI_LOG_PATH,
    WIKI_SOURCES_DIR,
    PROGRESS_DIR,
    PROMPT_MAX_CHARS,
    PROMPT_MAX_BODY_CHARS,
)
from lm_client import _chat_json, wait_for_server
from progress_tracker import normalize_url
from state_manager import UrlStageTracker

logger = logging.getLogger(__name__)


# =============================================================================
# Phase 1: ファイル操作（LLM不要）
# =============================================================================


def scan_inbox() -> list[Path]:
    """inbox/*.md をスキャンし、更新日順にソートしたリストを返す。"""
    if not INBOX_DIR.exists():
        return []
    files = sorted(INBOX_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime)
    # 一時ファイルを除外
    files = [f for f in files if not f.name.startswith(".tmp_")]
    return files


def extract_wikilinks(body: str) -> list[str]:
    """Markdown本文から [[concept-name]] を抽出。重複除去+ソート。

    sourceファイルへのリンク（author-year-形式）は除外し、conceptリンクのみ返す。
    """
    links = re.findall(r"\[\[([^\]]+)\]\]", body)
    normalized = set()
    for link in links:
        slug = link.strip().lower()
        # ASCII kebab-caseに正規化
        slug = re.sub(r"[^a-z0-9-]", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        if not slug:
            continue
        # sourceファイルへの参照を除外（author-year-title パターン）
        if re.match(r"^[a-z]+-\d{4}-", slug):
            continue
        if slug:
            normalized.add(slug)
    return sorted(normalized)


def _generate_source_filename(fm: dict) -> str:
    """frontmatterから source ファイル名を生成: {author}-{year}-{title}.md

    既存sourcesに合わせてASCII kebab-caseのみ。日本語は除去。
    著者名が非ASCIIの場合はURLのドメイン/パスから代替名を抽出。
    """
    author = fm.get("author", "unknown")
    year = fm.get("year", date.today().year)
    title = fm.get("title", "untitled")

    def to_slug(text: str) -> str:
        s = str(text).lower().strip()
        s = re.sub(r"[^a-z0-9-]", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
        return s[:40]

    author_slug = to_slug(author)

    # 著者名が空（日本語のみ）の場合、URLからフォールバック
    if not author_slug:
        url = fm.get("url", "")
        if url:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            # ドメインから抽出 (x.com → "x", github.com → "github")
            domain = parsed.netloc.replace("www.", "").split(".")[0]
            # パスの最初の部分も含める (x.com/ai_hakase_ → "x-ai-hakase")
            path_parts = [p for p in parsed.path.strip("/").split("/") if p][:1]
            author_slug = to_slug("-".join([domain] + path_parts)) or "unknown"
        else:
            author_slug = "unknown"

    title_slug = to_slug(title) or "untitled"

    return f"{author_slug}-{year}-{title_slug}.md"


def parse_inbox_file(path: Path) -> dict:
    """inbox/*.md をパースし構造化dictを返す。

    Returns: {
        "path": Path,
        "frontmatter": dict,
        "body": str,
        "wikilinks": list[str],
        "source_filename": str,
    }
    """
    post = frontmatter.load(str(path))
    fm = dict(post.metadata)
    body = post.content

    return {
        "path": path,
        "frontmatter": fm,
        "body": body,
        "wikilinks": extract_wikilinks(body),
        "source_filename": _generate_source_filename(fm),
    }


def move_to_sources(parsed: dict) -> Path:
    """inbox ファイルを wiki/sources/ に移動（atomic）。

    同名ファイルが既存の場合はbody末尾にappend。
    """
    WIKI_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    target = WIKI_SOURCES_DIR / parsed["source_filename"]

    if target.exists():
        # 既存ファイルにはappendしない — 重複として扱う
        logger.info("sources/に同名ファイル既存: %s — スキップ（移動のみ）", target.name)
    else:
        # 一時ファイル → リネーム
        tmp_name = f".tmp_{uuid.uuid4().hex[:8]}.md"
        tmp_path = WIKI_SOURCES_DIR / tmp_name
        content = _rebuild_source_md(parsed["frontmatter"], parsed["body"])
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.rename(target)
        logger.info("sources/に移動: %s", target.name)

    return target


def _rebuild_source_md(fm: dict, body: str) -> str:
    """frontmatter + body からMarkdown文字列を再構築。"""
    post = frontmatter.Post(body, **fm)
    return frontmatter.dumps(post)


# =============================================================================
# Phase 2: スタブ生成（軽量LLM）
# =============================================================================

# タグ→カテゴリのルールベースマッピング（LLMフォールバック用）
CATEGORY_TAG_MAP = {
    "AI動画・クリエイティブ": {"video", "creative", "animation", "remotion", "image", "3d"},
    "AI音声": {"tts", "stt", "voice", "audio", "speech", "asr"},
    "LLM・マルチエージェント": {"llm", "agent", "multi-agent", "mcp", "prompt", "rag"},
    "コーディングエージェント": {"coding-agent", "openhands", "claude-code", "copilot"},
    "金融・予測": {"finance", "dexter", "prediction", "timesfm", "forecast"},
    "データ分析・可視化": {"visualization", "graph", "analytics", "looker", "dashboard"},
    "自動化・ツール": {"automation", "workflow", "n8n", "supabase", "tool"},
    "Durable Execution・ワークフロー基盤": {
        "durable-execution", "temporal", "workflow-engine", "inngest",
    },
    "AIインフラ": {"infrastructure", "foundry", "vram", "embedding", "gpu"},
    "ナレッジ管理・ツール": {"obsidian", "knowledge", "note", "vault", "wiki"},
    "テスト": {"test", "regression", "ci", "visual-test", "e2e"},
}


def _classify_by_tags(tags: list[str]) -> str:
    """タグベースのフォールバックカテゴリ分類。"""
    tag_set = {t.lower() for t in tags}
    best_match = "その他"
    best_score = 0
    for category, keywords in CATEGORY_TAG_MAP.items():
        score = len(tag_set & keywords)
        if score > best_score:
            best_score = score
            best_match = category
    return best_match


def classify_category(
    concept_name: str, summary: str, tags: list[str], existing_categories: list[str],
) -> str:
    """コンセプトをindex.mdのカテゴリに分類。LLM(番号選択) → タグベース → "その他" の3段フォールバック。

    番号選択方式: Gemma 4 E4B のような小型モデルは長い日本語カテゴリ名を
    そのまま再現させると空応答になりやすいので、番号を返させる方式に変更。
    """
    # 「その他」を番号リストに含めて最後に置く
    options = list(existing_categories)
    if "その他" not in options:
        options.append("その他")
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(options))
    max_n = len(options)

    messages = [
        {
            "role": "system",
            "content": (
                "カテゴリ番号を1つ選びJSONで返してください。"
                "出力はJSONのみ、説明文は一切不要。"
            ),
        },
        {
            "role": "user",
            "content": f"""## 対象
名前: {concept_name}
要約: {summary[:300]}
タグ: {', '.join(tags[:10])}

## 番号付きカテゴリ
{numbered}

## 出力形式
{{"n": 番号}}

最も適切な番号を1〜{max_n}から選んでください。""",
        },
    ]

    # Gemma 4 E4B は内部思考トークンを ~450tok 消費してから実出力を開始するため
    # max_tokens=64 等の小さい値を指定すると finish_reason=length で content='' になる。
    # 1024 を最小安全値として確保する（実使用: 約 460-500tok、思考+JSON出力）。
    result = _chat_json(messages, temperature=0.2, max_tokens=1024)
    if result and isinstance(result, dict) and "n" in result:
        try:
            idx = int(result["n"]) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except (ValueError, TypeError):
            pass
        logger.warning("LLM分類: 不正な番号 %s → タグベースにフォールバック", result.get("n"))

    # フォールバック: タグベース
    tag_result = _classify_by_tags(tags)
    if tag_result != "その他" and tag_result in existing_categories:
        return tag_result
    return "その他"


def generate_concept_stub(
    concept_name: str, source_context: str, source_ref: str,
) -> str:
    """概念スタブをLLM生成（JSON→テンプレート）。フォールバック付き。"""
    messages = [
        {
            "role": "system",
            "content": (
                "あなたはナレッジベースの概念ページ作成アシスタントです。"
                "与えられたコンセプト名と文脈から、簡潔な定義を生成してください。"
                "日本語で記述。出力はJSON形式のみ。説明文は不要です。"
            ),
        },
        {
            "role": "user",
            "content": f"""## コンセプト名
{concept_name}

## 文脈（ソースでの言及部分）
{source_context[:PROMPT_MAX_CHARS]}

## 指示
以下のJSON形式で出力:
{{"title_ja": "日本語タイトル", "definition": "1-2文の定義文", "summary": "30-50語の要約", "tags": ["tag1", "tag2"], "related_concepts": ["related-1", "related-2"]}}""",
        },
    ]

    result = _chat_json(messages, temperature=0.1, max_tokens=1024)

    if result and isinstance(result, dict) and "definition" in result:
        return _build_stub_md(concept_name, result, source_ref)

    # フォールバック: 最小スタブ
    logger.warning("LLMスタブ生成失敗。最小スタブを作成: %s", concept_name)
    return _build_minimal_stub(concept_name, source_ref)


def _build_stub_md(concept_name: str, llm_result: dict, source_ref: str) -> str:
    """LLM JSON結果からスタブMarkdownを組み立て。"""
    today = date.today().isoformat()
    title_ja = llm_result.get("title_ja", concept_name.replace("-", " ").title())
    definition = llm_result.get("definition", "")
    summary = llm_result.get("summary", definition[:80])
    tags = llm_result.get("tags", [])
    related = llm_result.get("related_concepts", [])

    tags_str = ", ".join(tags)
    related_lines = "\n".join(f"- [[{r}]]" for r in related) if related else ""

    return f"""---
title: "{title_ja}"
date_created: {today}
date_modified: {today}
summary: "{summary}"
tags: [{tags_str}]
type: concept
status: stub
sources: ["{source_ref}"]
confidence: emerging
---
# {title_ja}

{definition}

## 関連概念
{related_lines}
"""


def _build_minimal_stub(concept_name: str, source_ref: str) -> str:
    """LLM不要の最小スタブ。"""
    today = date.today().isoformat()
    title = concept_name.replace("-", " ").title()
    return f"""---
title: "{title}"
date_created: {today}
date_modified: {today}
summary: "（自動生成 - 要レビュー）"
tags: []
type: concept
status: stub
sources: ["{source_ref}"]
confidence: emerging
---
# {title}

（定義未生成 - LLM処理待ち）

## 関連概念
"""


# =============================================================================
# Phase 3: ドラフト昇格 + 既存概念append
# =============================================================================


def promote_to_draft(
    concept_path: Path, new_source_context: str, new_source_ref: str,
) -> bool:
    """既存stub(1source)を新sourceの追加でdraftに昇格。"""
    post = frontmatter.load(str(concept_path))
    fm = dict(post.metadata)
    old_body = post.content

    # 既存ソースの情報
    existing_sources = fm.get("sources", [])

    messages = [
        {
            "role": "system",
            "content": (
                "あなたはナレッジベースの概念記事作成アシスタントです。"
                "2つ以上のソースから情報を統合して構造化記事を生成してください。"
                "日本語で記述。出力はJSON形式のみ。説明文は不要です。"
            ),
        },
        {
            "role": "user",
            "content": f"""## コンセプト名
{fm.get('title', concept_path.stem)}

## 既存の定義（スタブ）
{old_body[:PROMPT_MAX_BODY_CHARS]}

## 新ソースからの情報
{new_source_context[:PROMPT_MAX_CHARS]}

## 指示
以下のJSON形式で出力:
{{"overview": "概要（200-500語）", "details": "詳細（200-500語）", "summary": "30-50語の更新要約", "tags": ["tag1", "tag2"], "related_concepts": ["related-1", "related-2"]}}""",
        },
    ]

    result = _chat_json(messages, temperature=0.2, max_tokens=4096)

    today = date.today().isoformat()
    sources_list = list(existing_sources) + [new_source_ref]

    if result and isinstance(result, dict) and "overview" in result:
        # ドラフト昇格成功
        title_ja = fm.get("title", concept_path.stem.replace("-", " ").title())
        overview = result.get("overview", "")
        details = result.get("details", "")
        summary = result.get("summary", fm.get("summary", ""))
        tags = result.get("tags", fm.get("tags", []))
        related = result.get("related_concepts", [])

        tags_str = ", ".join(tags)
        sources_str = ", ".join(f'"{s}"' for s in sources_list)
        related_lines = "\n".join(f"- [[{r}]]" for r in related) if related else ""

        new_content = f"""---
title: "{title_ja}"
date_created: {fm.get('date_created', today)}
date_modified: {today}
summary: "{summary}"
tags: [{tags_str}]
type: concept
status: draft
sources: [{sources_str}]
confidence: {fm.get('confidence', 'emerging')}
---
# {title_ja}

## 概要
{overview}

## 詳細
{details}

## 関連概念
{related_lines}
"""
        _atomic_write(concept_path, new_content)
        logger.info("ドラフト昇格成功: %s", concept_path.name)
        return True

    # フォールバック: sources追加のみ
    logger.warning("LLMドラフト昇格失敗。sourcesのみ追加: %s", concept_path.name)
    _append_source_only(concept_path, new_source_ref)
    return False


def append_to_existing(concept_path: Path, source_context: str, source_ref: str):
    """既存draft/completeに新ソース情報を追記。LLM不要。"""
    post = frontmatter.load(str(concept_path))
    fm = dict(post.metadata)
    body = post.content

    # sources配列に追加
    sources = fm.get("sources", [])
    if source_ref not in sources:
        sources.append(source_ref)
    fm["sources"] = sources
    fm["date_modified"] = date.today().isoformat()

    # body末尾に追記（ローカルファイルへの書き込みなので制限なし）
    source_label = source_ref.strip("[]\"'")
    append_text = f"\n\n## {source_label} からの追加情報\n{source_context}\n"
    new_body = body.rstrip() + append_text

    new_post = frontmatter.Post(new_body, **fm)
    _atomic_write(concept_path, frontmatter.dumps(new_post))
    logger.info("既存概念に追記: %s ← %s", concept_path.name, source_ref)


def _append_source_only(concept_path: Path, new_source_ref: str):
    """sourcesリストとdate_modifiedだけ更新。body変更なし。"""
    post = frontmatter.load(str(concept_path))
    fm = dict(post.metadata)
    sources = fm.get("sources", [])
    if new_source_ref not in sources:
        sources.append(new_source_ref)
    fm["sources"] = sources
    fm["date_modified"] = date.today().isoformat()
    new_post = frontmatter.Post(post.content, **fm)
    _atomic_write(concept_path, frontmatter.dumps(new_post))


# =============================================================================
# index.md / log.md 更新
# =============================================================================


def parse_index() -> tuple[str, dict[str, list[str]], list[str]]:
    """wiki/index.md をパースし、(header, categories_dict, raw_lines) を返す。

    categories_dict: {"カテゴリ名": ["- [[entry]] — desc", ...]}
    """
    if not WIKI_INDEX_PATH.exists():
        return "", {}, []

    lines = WIKI_INDEX_PATH.read_text(encoding="utf-8-sig").splitlines()
    categories: dict[str, list[str]] = {}
    current_category = None
    header_lines = []
    in_frontmatter = False
    past_frontmatter = False

    for line in lines:
        # frontmatter処理
        if line.strip() == "---":
            if not in_frontmatter and not past_frontmatter:
                in_frontmatter = True
                header_lines.append(line)
                continue
            elif in_frontmatter:
                in_frontmatter = False
                past_frontmatter = True
                header_lines.append(line)
                continue

        if in_frontmatter:
            header_lines.append(line)
            continue

        # カテゴリ見出し (## ...)
        if line.startswith("## "):
            current_category = line[3:].strip()
            if current_category not in categories:
                categories[current_category] = []
        elif current_category and line.strip().startswith("- "):
            categories[current_category].append(line)

    header = "\n".join(header_lines)
    return header, categories, lines


def update_index(
    new_sources: list[dict], new_concepts: list[dict], existing_categories: list[str],
):
    """wiki/index.md に新エントリを一括追加。

    new_sources: [{"name": str, "summary": str}]
    new_concepts: [{"name": str, "summary": str, "tags": list, "category": str}]
    """
    header, categories, _ = parse_index()
    if not categories:
        logger.warning("index.mdのパースに失敗。更新をスキップ。")
        return

    # ソース追加
    if "ソース" not in categories:
        categories["ソース"] = []
    for src in new_sources:
        entry = f"- [[{src['name']}]] — {src['summary'][:60]}"
        if entry not in categories["ソース"]:
            categories["ソース"].append(entry)

    # コンセプト追加
    for concept in new_concepts:
        cat = concept.get("category", "その他")
        if cat not in categories:
            cat = "その他"
            if cat not in categories:
                categories[cat] = []
        entry = f"- [[{concept['name']}]] — {concept['summary'][:60]}"
        if not any(f"[[{concept['name']}]]" in existing for existing in categories[cat]):
            categories[cat].append(entry)

    # index.md再構築
    lines = [header, "", "# ナレッジベース目次", ""]
    for cat_name, entries in categories.items():
        lines.append(f"## {cat_name}")
        lines.extend(entries)
        lines.append("")

    _atomic_write(WIKI_INDEX_PATH, "\n".join(lines))
    logger.info("index.md更新: sources+%d, concepts+%d", len(new_sources), len(new_concepts))


def append_log(sources_count: int, concepts_created: int, concepts_updated: int):
    """wiki/log.md にingest操作記録を追加。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (
        f"| {now} | ingest-local | "
        f"sources: {sources_count}, concepts新規: {concepts_created}, concepts更新: {concepts_updated} | "
        f"local_ingest.py |"
    )

    if WIKI_LOG_PATH.exists():
        content = WIKI_LOG_PATH.read_text(encoding="utf-8")
        # 最後の行の前にエントリ追加（テーブル末尾に追記）
        content = content.rstrip() + "\n" + entry + "\n"
    else:
        content = f"""---
title: "Operation Log"
date_created: {date.today().isoformat()}
date_modified: {date.today().isoformat()}
type: log
status: complete
---
# Operation Log

| 日時 | 操作 | 詳細 | 実行者 |
|------|------|------|--------|
{entry}
"""
    _atomic_write(WIKI_LOG_PATH, content)


# =============================================================================
# ユーティリティ
# =============================================================================


def _atomic_write(path: Path, content: str):
    """一時ファイル→リネームでatomic書き込み。"""
    tmp_path = path.with_suffix(f".tmp_{uuid.uuid4().hex[:8]}")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _load_existing_concepts() -> dict[str, dict]:
    """wiki/concepts/*.md をスキャンし、{name: {path, status, sources}} を返す。"""
    concepts = {}
    if not WIKI_CONCEPTS_DIR.exists():
        return concepts

    for p in WIKI_CONCEPTS_DIR.glob("*.md"):
        try:
            post = frontmatter.load(str(p))
            fm = dict(post.metadata)
            concepts[p.stem] = {
                "path": p,
                "status": fm.get("status", "stub"),
                "sources": fm.get("sources", []),
                "summary": fm.get("summary", ""),
                "tags": fm.get("tags", []),
            }
        except Exception as e:
            logger.warning("concepts/パース失敗: %s — %s", p.name, e)
    return concepts


def _extract_concept_context(body: str, concept_name: str) -> str:
    """body内のコンセプト言及周辺テキストを抽出。
    ローカルMarkdownへの書き込み用なので制限は設けず、言及箇所すべてを収集する。
    該当なしの場合はソース本文全体を返してコンテキストを失わない。
    """
    lines = body.splitlines()
    context_lines: list[str] = []
    seen_ranges: set[tuple[int, int]] = set()

    for i, line in enumerate(lines):
        if concept_name in line.lower() or f"[[{concept_name}]]" in line:
            start = max(0, i - 5)
            end = min(len(lines), i + 6)
            rng = (start, end)
            if rng in seen_ranges:
                continue
            seen_ranges.add(rng)
            if context_lines:
                context_lines.append("")
            context_lines.extend(lines[start:end])

    if not context_lines:
        # 抽出コンセプトセクションから探す
        in_section = False
        for line in lines:
            if "抽出コンセプト" in line or "## 抽出" in line:
                in_section = True
                continue
            if in_section and line.startswith("## "):
                break
            if in_section and concept_name in line.lower():
                context_lines.append(line)

    # 該当なしならソース本文全体を返す（ローカル書き込みのため制限なし）
    return "\n".join(context_lines) if context_lines else body


# =============================================================================
# メインエントリポイント
# =============================================================================


def run_ingest(dry_run: bool = False) -> dict:
    """Ingestパイプラインのメインエントリポイント。

    Returns: {
        "inbox_processed": int,
        "sources_created": int,
        "concepts_created": int,
        "concepts_promoted": int,
        "concepts_appended": int,
        "errors": list[str],
    }
    """
    stats = {
        "inbox_processed": 0,
        "sources_created": 0,
        "concepts_created": 0,
        "concepts_promoted": 0,
        "concepts_appended": 0,
        "errors": [],
    }

    # 1. inboxスキャン
    files = scan_inbox()
    if not files:
        logger.info("inbox空。Ingestスキップ。")
        return stats

    logger.info("inbox %d件のファイルを検出", len(files))

    if dry_run:
        for f in files:
            logger.info("[DRY-RUN] 処理対象: %s", f.name)
            try:
                parsed = parse_inbox_file(f)
                logger.info(
                    "[DRY-RUN]   → sources/%s, wikilinks: %s",
                    parsed["source_filename"],
                    parsed["wikilinks"],
                )
            except Exception as e:
                logger.error("[DRY-RUN]   → パース失敗: %s", e)
        return stats

    # 2. LM Studioサーバー確認
    if not wait_for_server():
        logger.error("LM Studioサーバー未稼働。ファイル操作のみ実行。")
        llm_available = False
    else:
        llm_available = True

    # 3. 既存概念をスキャン
    existing_concepts = _load_existing_concepts()
    logger.info("既存concepts: %d件", len(existing_concepts))

    # 4. index.mdパース
    _, categories, _ = parse_index()
    existing_categories = list(categories.keys())

    # バッファ
    new_sources_for_index: list[dict] = []
    new_concepts_for_index: list[dict] = []

    # 5. 各ファイル処理
    for file_path in files:
        try:
            parsed = parse_inbox_file(file_path)
            logger.info("処理中: %s → %s", file_path.name, parsed["source_filename"])

            # sources/に移動
            source_path = move_to_sources(parsed)
            stats["sources_created"] += 1

            source_ref = f'[[{source_path.stem}]]'
            fm = parsed["frontmatter"]

            # index用のソースエントリ
            new_sources_for_index.append({
                "name": source_path.stem,
                "summary": fm.get("summary", ""),
            })

            # 各wikilinkを処理
            for concept_name in parsed["wikilinks"]:
                context = _extract_concept_context(parsed["body"], concept_name)

                if concept_name not in existing_concepts:
                    # 新規スタブ生成
                    if llm_available:
                        stub_md = generate_concept_stub(concept_name, context, source_ref)
                    else:
                        stub_md = _build_minimal_stub(concept_name, source_ref)

                    concept_path = WIKI_CONCEPTS_DIR / f"{concept_name}.md"
                    WIKI_CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
                    _atomic_write(concept_path, stub_md)

                    # 既存概念dictを更新
                    post = frontmatter.loads(stub_md)
                    existing_concepts[concept_name] = {
                        "path": concept_path,
                        "status": "stub",
                        "sources": [source_ref],
                        "summary": dict(post.metadata).get("summary", ""),
                        "tags": dict(post.metadata).get("tags", []),
                    }

                    # index用
                    cat = "その他"
                    if llm_available:
                        cat = classify_category(
                            concept_name,
                            existing_concepts[concept_name]["summary"],
                            existing_concepts[concept_name]["tags"],
                            existing_categories,
                        )
                    else:
                        cat = _classify_by_tags(existing_concepts[concept_name]["tags"])
                        if cat not in existing_categories:
                            cat = "その他"

                    new_concepts_for_index.append({
                        "name": concept_name,
                        "summary": existing_concepts[concept_name]["summary"],
                        "tags": existing_concepts[concept_name]["tags"],
                        "category": cat,
                    })

                    stats["concepts_created"] += 1
                    logger.info("  スタブ生成: %s → %s", concept_name, cat)

                elif existing_concepts[concept_name]["status"] == "stub" and \
                        len(existing_concepts[concept_name]["sources"]) == 1:
                    # stub → draft 昇格
                    concept_path = existing_concepts[concept_name]["path"]
                    if llm_available:
                        promoted = promote_to_draft(concept_path, context, source_ref)
                        if promoted:
                            stats["concepts_promoted"] += 1
                            logger.info("  ドラフト昇格: %s", concept_name)
                        else:
                            stats["concepts_appended"] += 1
                    else:
                        _append_source_only(concept_path, source_ref)
                        stats["concepts_appended"] += 1
                else:
                    # 既存draft/complete → append
                    concept_path = existing_concepts[concept_name]["path"]
                    append_to_existing(concept_path, context, source_ref)
                    stats["concepts_appended"] += 1
                    logger.info("  既存概念に追記: %s", concept_name)

            # state_manager更新
            url = fm.get("url", "")
            objective_id = fm.get("source_objective", "")
            if url and objective_id:
                try:
                    tracker = UrlStageTracker(PROGRESS_DIR / f"{objective_id}.json")
                    normalized = normalize_url(url)
                    tracker.set_stage(normalized, "ingested")
                except Exception as e:
                    logger.warning("state_manager更新失敗: %s", e)

            # inboxファイル削除
            file_path.unlink()
            stats["inbox_processed"] += 1
            logger.info("inbox削除: %s", file_path.name)

        except Exception as e:
            stats["errors"].append(f"{file_path.name}: {e}")
            logger.exception("Ingestエラー (続行): %s", file_path.name)
            continue

    # 6. index.md一括更新
    if new_sources_for_index or new_concepts_for_index:
        try:
            update_index(new_sources_for_index, new_concepts_for_index, existing_categories)
        except Exception as e:
            logger.error("index.md更新失敗: %s", e)
            stats["errors"].append(f"index.md: {e}")

    # 7. log.md一括記録
    try:
        append_log(stats["sources_created"], stats["concepts_created"], stats["concepts_promoted"] + stats["concepts_appended"])
    except Exception as e:
        logger.error("log.md更新失敗: %s", e)
        stats["errors"].append(f"log.md: {e}")

    logger.info(
        "Ingest完了: inbox=%d, sources=%d, concepts新規=%d, 昇格=%d, 追記=%d, エラー=%d",
        stats["inbox_processed"],
        stats["sources_created"],
        stats["concepts_created"],
        stats["concepts_promoted"],
        stats["concepts_appended"],
        len(stats["errors"]),
    )

    return stats
