"""Gap分析→自律探索ループ

wiki/concepts/ から stub 状態の概念を検出し、各 objective に対して
深掘りすべきテーマと検索クエリを生成して state/search-hints/ に投入する。

main.py の run_cycle 開始時に maybe_run_gap_analysis() を呼ぶ。
GAP_ANALYSIS_INTERVAL_HOURS（config.py）で実行間隔を制御。
"""
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import frontmatter

from config import (
    GAP_ANALYSIS_INTERVAL_HOURS,
    SEARCH_HINTS_DIR,
    STATE_DIR,
    WIKI_CONCEPTS_DIR,
)
from lm_client import _chat_json
from objectives import Objective, load_objectives
from query_history import recent_unique_queries

logger = logging.getLogger(__name__)

GAP_STATE_FILE = STATE_DIR / "gap_analysis_state.json"


# =============================================================================
# Phase 1: 概念スキャン（LLM不要）
# =============================================================================


def scan_concepts_by_status() -> dict[str, list[dict]]:
    """wiki/concepts/ をスキャンし、status別に分類して返す。

    返り値: {"stub": [...], "draft": [...], "complete": [...]}
    各要素: {"name", "title", "summary", "tags", "sources_count"}
    """
    result: dict[str, list[dict]] = {"stub": [], "draft": [], "complete": []}
    if not WIKI_CONCEPTS_DIR.exists():
        return result

    for md in WIKI_CONCEPTS_DIR.glob("*.md"):
        try:
            post = frontmatter.load(str(md))
            fm = post.metadata
            status = fm.get("status", "stub")
            if status not in result:
                continue
            entry = {
                "name": md.stem,
                "title": fm.get("title", md.stem),
                "summary": fm.get("summary", ""),
                "tags": fm.get("tags", []) or [],
                "sources_count": len(fm.get("sources", []) or []),
            }
            result[status].append(entry)
        except Exception as e:
            logger.warning("concept読み込み失敗: %s — %s", md.name, e)
    return result


def match_stubs_to_objective(stubs: list[dict], obj: Objective, max_n: int = 8) -> list[dict]:
    """objective に関連する stub を抽出。

    マッチング基準:
      - タグ交差 (重み3)
      - title/summary に interest キーワードが含まれる (重み1)
      - sources_count<=1 はさらに深掘り余地ありとして加点 (重み1)
    """
    obj_tags = {str(t).lower() for t in obj.tags}
    obj_keywords: set[str] = set()
    for txt in [obj.title, obj.goal] + obj.interests:
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", txt):
            obj_keywords.add(w.lower())

    scored: list[tuple[int, dict]] = []
    for s in stubs:
        score = 0
        score += len({str(t).lower() for t in s["tags"]} & obj_tags) * 3
        text = (str(s["title"]) + " " + str(s["summary"])).lower()
        for kw in obj_keywords:
            if kw in text:
                score += 1
        if s["sources_count"] <= 1:
            score += 1
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:max_n]]


# =============================================================================
# Phase 2: LLM による hints 生成
# =============================================================================


def generate_hints_for_objective(obj: Objective, related_stubs: list[dict]) -> dict | None:
    """LM Studio に依頼して hints + knowledge_gaps を生成。

    返り値: {"hints": [{"query","search_lang","target_concept","why"}],
             "knowledge_gaps": [...]} または None
    """
    if not related_stubs:
        return None

    stubs_text = "\n".join(
        f"- [{s['title']}] (slug={s['name']}, tags={s['tags']}, sources={s['sources_count']})"
        for s in related_stubs
    )
    interests_text = "\n".join(f"- {i}" for i in obj.interests) or "（なし）"
    exclusions_text = "\n".join(f"- {e}" for e in obj.exclusions) or "（なし）"

    # 直近ユニーク10クエリを avoid-list として渡し、クエリ再生産を抑制。
    # Phase 1 では「同じ記事」問題の真因は観測構造だったが、
    # Phase 2 では生成側にも avoid-list を明示することで多様性を担保する。
    avoid_queries = recent_unique_queries(limit=10, ok_only=True)
    avoid_text = "\n".join(f"- {q}" for q in avoid_queries) or "（なし）"

    messages = [
        {
            "role": "system",
            "content": (
                "あなたは知識ベース成長を支援する調査プランナーです。"
                "目的とstub概念リストから、深掘り探索すべきテーマと検索クエリを提案します。"
                "出力はJSONのみ、説明文不要。"
            ),
        },
        {
            "role": "user",
            "content": f"""## 研究目的
{obj.title}

## ゴール
{obj.goal}

## 関心領域
{interests_text}

## 除外条件
{exclusions_text}

## 現在stub状態の関連概念（深掘り余地あり）
{stubs_text}

## 直近実行済みクエリ（これらの言い換え/再投稿は避けること）
{avoid_text}

## 指示
これらstub概念のうち、どれがゴール達成に重要か判断し、
深掘りのための検索クエリ3〜5件と知識ギャップ2〜4件を提案してください。

**多様性制約（厳守）**:
- 上記の直近実行済みクエリと同一概念の表層を再度検索しないこと
- 各クエリは次の3つの探索軸のいずれかに明示的に紐づけること:
  1. `implementation` — 実装詳細・コード例・具体API
  2. `operations` — 運用・監視・障害事例・本番経験談
  3. `alternatives` — 代替手段・比較記事・トレードオフ分析
- 3件以上生成する場合は少なくとも2軸をカバーすること
- 検索クエリは2025-2026年の最新情報を重視
- 日本語と英語を混ぜて幅広く（ASCII主体の英語クエリは search_lang を "en" にすること）

出力JSON形式（このスキーマ厳守）:
{{
  "hints": [
    {{"query": "検索クエリ", "search_lang": "ja|en", "target_concept": "概念slug", "axis": "implementation|operations|alternatives", "why": "なぜ重要か(1行)"}}
  ],
  "knowledge_gaps": ["不足している知識領域(短文)"]
}}""",
        },
    ]

    # temperature 0.6: 多様性を上げる。Phase 1 の 0.4 では言い換えに留まり
    # 「同じ記事」を再取得する傾向が実測されたため引き上げる。
    result = _chat_json(messages, temperature=0.6, max_tokens=2048)
    if not isinstance(result, dict):
        return None
    if "hints" not in result or not isinstance(result["hints"], list):
        return None
    # 最低限のキー補完
    cleaned_hints = []
    for h in result["hints"]:
        if not isinstance(h, dict):
            continue
        q = str(h.get("query", "")).strip()
        if not q:
            continue
        cleaned_hints.append({
            "query": q,
            "search_lang": str(h.get("search_lang", obj.language)),
            "target_concept": str(h.get("target_concept", "")),
            "axis": str(h.get("axis", "")),
            "why": str(h.get("why", "")),
        })
    if not cleaned_hints:
        return None
    return {
        "hints": cleaned_hints,
        "knowledge_gaps": [str(g) for g in result.get("knowledge_gaps", []) if g],
    }


def fallback_hints(related_stubs: list[dict], obj: Objective, max_n: int = 3) -> dict:
    """LLM失敗時の機械的フォールバック。"""
    hints = []
    for s in related_stubs[:max_n]:
        hints.append({
            "query": f"{s['title']} 詳細 仕組み 2026",
            "search_lang": obj.language,
            "target_concept": s["name"],
            "why": "stub深掘り(自動生成)",
        })
    gaps = [str(s["title"]) for s in related_stubs[:3]]
    return {"hints": hints, "knowledge_gaps": gaps}


# =============================================================================
# Phase 3: 永続化と間隔制御
# =============================================================================


def save_hints(objective_id: str, hints_data: dict) -> Path:
    """search-hints/{objective_id}.json に書き込み。"""
    SEARCH_HINTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SEARCH_HINTS_DIR / f"{objective_id}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hints": hints_data.get("hints", []),
        "knowledge_gaps": hints_data.get("knowledge_gaps", []),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_state() -> dict:
    if not GAP_STATE_FILE.exists():
        return {}
    try:
        return json.loads(GAP_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    GAP_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def should_run(now: datetime | None = None) -> bool:
    """GAP_ANALYSIS_INTERVAL_HOURS 経過していれば True。"""
    if now is None:
        now = datetime.now()
    state = _load_state()
    last = state.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (now - last_dt) >= timedelta(hours=GAP_ANALYSIS_INTERVAL_HOURS)


# =============================================================================
# エントリポイント
# =============================================================================


def run_gap_analysis(force: bool = False) -> dict:
    """全 objective に対して gap 分析を実行。

    返り値: {"objectives": int, "hints_generated": int, "skipped_interval": bool}
    """
    if not force and not should_run():
        logger.info(
            "Gap分析: 前回実行から %d 時間未満のためスキップ",
            GAP_ANALYSIS_INTERVAL_HOURS,
        )
        return {"objectives": 0, "hints_generated": 0, "skipped_interval": True}

    concepts = scan_concepts_by_status()
    stubs = concepts["stub"]
    logger.info(
        "Gap分析開始: stub=%d, draft=%d, complete=%d",
        len(stubs), len(concepts["draft"]), len(concepts["complete"]),
    )

    if not stubs:
        logger.info("stub概念が0件のためGap分析スキップ")
        _save_state({"last_run_at": datetime.now().isoformat(timespec="seconds")})
        return {"objectives": 0, "hints_generated": 0, "skipped_interval": False}

    objectives = load_objectives()
    total_hints = 0
    processed_objs = 0

    for obj in objectives:
        related = match_stubs_to_objective(stubs, obj)
        if not related:
            logger.info("[%s] 関連stubなし", obj.id)
            continue
        logger.info("[%s] 関連stub %d件で hints 生成", obj.id, len(related))

        hints = generate_hints_for_objective(obj, related)
        if hints is None:
            logger.warning("[%s] LLM hints生成失敗 → フォールバック使用", obj.id)
            hints = fallback_hints(related, obj)

        path = save_hints(obj.id, hints)
        total_hints += len(hints.get("hints", []))
        processed_objs += 1
        logger.info(
            "[%s] hints保存: %s (queries=%d, gaps=%d)",
            obj.id, path.name,
            len(hints.get("hints", [])),
            len(hints.get("knowledge_gaps", [])),
        )

    _save_state({"last_run_at": datetime.now().isoformat(timespec="seconds")})
    return {
        "objectives": processed_objs,
        "hints_generated": total_hints,
        "skipped_interval": False,
    }


if __name__ == "__main__":
    import logging.handlers
    import sys

    from config import LOG_BACKUP_COUNT, LOG_FILE, LOG_MAX_BYTES, LOGS_DIR

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

    force = "--force" in sys.argv
    stats = run_gap_analysis(force=force)
    print(f"Gap analysis stats: {stats}")
