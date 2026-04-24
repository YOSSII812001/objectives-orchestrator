"""Objective 自己更新ドラフト方式 — Phase 2

KPI トレンドに基づき、objective.md の改訂案を
`state/objective-drafts/<objective_id>-<timestamp>.md` に生成する。

重要な設計判断:
  - objective.md を**直接書き換えない**。ユーザー承認を前提としたドラフト方式。
  - 健康度判定（KPI閾値ベース）に合致した objective のみ LLM 呼び出し。
  - 呼び出し間隔は `DRAFTER_INTERVAL_HOURS` で律速（LLM コスト抑制）。
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    DRAFTER_CONSECUTIVE_NG_CYCLES,
    DRAFTER_INTERVAL_HOURS,
    DRAFTER_NOVEL_DOMAIN_RATE_THRESHOLD,
    DRAFTER_STAGNATION_HOURS_THRESHOLD,
    OBJECTIVE_DRAFTS_DIR,
    STATE_DIR,
)
from kpi_tracker import _load_history as _load_kpi_history

logger = logging.getLogger(__name__)

DRAFTER_STATE_FILE = STATE_DIR / "objective_drafter_state.json"


def _load_state() -> dict:
    if not DRAFTER_STATE_FILE.exists():
        return {}
    try:
        return json.loads(DRAFTER_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DRAFTER_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _interval_elapsed(objective_id: str, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    state = _load_state()
    last = state.get(objective_id, {}).get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (now - last_dt) >= timedelta(hours=DRAFTER_INTERVAL_HOURS)


def analyze_health(objective_id: str) -> dict:
    """直近 KPI 履歴から健康度を判定する。

    返り値:
      {
        "ng": bool,                       — NG 判定フラグ
        "reason": str,                    — NG 理由（"stagnation"/"low_novelty"/"ok"）
        "consecutive_ng": int,            — 連続NGサイクル数
        "last_stagnation_hours": float,   — 直近レコードの停滞時間
        "recent_novel_domain_rate": float — 直近ユニークレコードの平均
      }
    """
    history = _load_kpi_history(objective_id)
    if not history:
        return {
            "ng": False,
            "reason": "no_history",
            "consecutive_ng": 0,
            "last_stagnation_hours": 0.0,
            "recent_novel_domain_rate": 0.0,
        }

    recent = history[-DRAFTER_CONSECUTIVE_NG_CYCLES:]

    # 連続停滞判定
    last_stag = float(history[-1].get("kpi", {}).get("stagnation_hours") or 0.0)
    stagnation_ng = last_stag >= DRAFTER_STAGNATION_HOURS_THRESHOLD

    # 連続 novel_domain_rate 低下判定
    rates = []
    for r in recent:
        v = r.get("kpi", {}).get("novel_domain_rate")
        if v is not None:
            rates.append(float(v))
    avg_rate = (sum(rates) / len(rates)) if rates else 0.0
    novelty_ng = (
        len(rates) >= DRAFTER_CONSECUTIVE_NG_CYCLES
        and all(r < DRAFTER_NOVEL_DOMAIN_RATE_THRESHOLD for r in rates)
    )

    consecutive_ng = 0
    for r in reversed(history):
        nr = r.get("kpi", {}).get("novel_domain_rate")
        if nr is None or float(nr) >= DRAFTER_NOVEL_DOMAIN_RATE_THRESHOLD:
            break
        consecutive_ng += 1

    if stagnation_ng:
        return {
            "ng": True,
            "reason": "stagnation",
            "consecutive_ng": consecutive_ng,
            "last_stagnation_hours": last_stag,
            "recent_novel_domain_rate": avg_rate,
        }
    if novelty_ng:
        return {
            "ng": True,
            "reason": "low_novelty",
            "consecutive_ng": consecutive_ng,
            "last_stagnation_hours": last_stag,
            "recent_novel_domain_rate": avg_rate,
        }
    return {
        "ng": False,
        "reason": "ok",
        "consecutive_ng": consecutive_ng,
        "last_stagnation_hours": last_stag,
        "recent_novel_domain_rate": avg_rate,
    }


def _build_draft_prompt(obj, health: dict, current_md: str) -> list[dict]:
    """LLM へ渡すドラフト生成プロンプト。"""
    return [
        {
            "role": "system",
            "content": (
                "あなたは知識ベースの自律成長を支援するリサーチ戦略家です。"
                "objective.md の内容と、その目的で実行されたクエリ/収集の KPI 結果に基づき、"
                "objective.md の改訂案（差分ではなく完成形）を Markdown で提案します。"
                "出力は Markdown のみ。説明文や前置きは書かないでください。"
            ),
        },
        {
            "role": "user",
            "content": f"""## 現在の objective.md

```markdown
{current_md}
```

## 健康度評価
- 判定: NG（理由: {health['reason']}）
- 直近 stagnation_hours: {health['last_stagnation_hours']}
- 直近 novel_domain_rate: {health['recent_novel_domain_rate']:.3f}
- 連続NGサイクル数: {health['consecutive_ng']}

## 改訂指示
以下の観点で objective.md を改訂してください:
1. `## 関心領域` の粒度を見直し、2〜3項目を新しい探索軸に置換（例: 実装詳細→運用知見→代替手段）
2. `## 除外条件` に、直近の重複/表層ドメインを減らす条件を1〜2項目追加
3. `## ステータスメモ` に今回の改訂理由を1パラグラフ記述（KPI 根拠を含める）
4. frontmatter（title, priority, tags 等）は保持し、title だけ微調整可

**制約**:
- ゴール文自体は変更しない（目的は維持）
- 既存の関心領域を全部差し替えない（1〜3項目のみ改訂）
- 出力は完成形 Markdown（frontmatter 含む）のみ
""",
        },
    ]


def generate_draft(obj, health: dict) -> Path | None:
    """LLM に改訂案を生成させ `state/objective-drafts/` に保存して Path を返す。

    LLM 呼び出しが失敗した場合は None を返す（例外は呼び出し側でロギング）。
    """
    from lm_client import _chat

    current_md = obj.file_path.read_text(encoding="utf-8")
    messages = _build_draft_prompt(obj, health, current_md)
    draft_text = _chat(messages, temperature=0.4, max_tokens=3072)

    if not draft_text or not draft_text.strip():
        logger.warning("[%s] ドラフト生成失敗: LLM 応答が空", obj.id)
        return None

    OBJECTIVE_DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = OBJECTIVE_DRAFTS_DIR / f"{obj.id}-{ts}.md"
    # ドラフトヘッダー（人間がレビューする時に何が根拠か一目でわかるよう追記）
    header = (
        f"<!-- objective-draft: generated={datetime.now().isoformat(timespec='seconds')} "
        f"reason={health['reason']} "
        f"consecutive_ng={health['consecutive_ng']} "
        f"stagnation_hours={health['last_stagnation_hours']} "
        f"novel_domain_rate={health['recent_novel_domain_rate']:.3f} -->\n"
    )
    path.write_text(header + draft_text.strip() + "\n", encoding="utf-8")
    logger.info(
        "[%s] Objective ドラフト生成: %s (reason=%s)",
        obj.id, path.name, health["reason"],
    )
    return path


def maybe_generate_drafts() -> dict:
    """全 active objective をチェックし、NG なら ドラフトを生成する。

    返り値: {"checked": N, "drafted": M, "skipped_interval": K}
    """
    from objectives import load_objectives

    stats = {"checked": 0, "drafted": 0, "skipped_interval": 0}
    state = _load_state()
    now = datetime.now()

    for obj in load_objectives():
        stats["checked"] += 1

        if not _interval_elapsed(obj.id, now):
            stats["skipped_interval"] += 1
            logger.debug("[%s] ドラフト判定スキップ（間隔未経過）", obj.id)
            continue

        health = analyze_health(obj.id)
        if not health["ng"]:
            logger.info(
                "[%s] ドラフト不要: %s (stagnation=%.1fh, novelty=%.3f)",
                obj.id, health["reason"],
                health["last_stagnation_hours"], health["recent_novel_domain_rate"],
            )
            continue

        try:
            path = generate_draft(obj, health)
        except Exception:
            logger.exception("[%s] ドラフト生成中に例外", obj.id)
            path = None

        if path:
            stats["drafted"] += 1
            state.setdefault(obj.id, {})["last_run_at"] = now.isoformat(timespec="seconds")
            state[obj.id]["last_draft_path"] = str(path)
            state[obj.id]["last_reason"] = health["reason"]

    _save_state(state)
    return stats


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

    result = maybe_generate_drafts()
    print(f"Objective drafter: {result}")
