"""KPI 計測モジュール — Phase 2 観測指標

`state/kpi/<objective_id>.jsonl` にサイクルごとの指標を追記する。
5指標の定義:
  1. effective_query_rate — クエリ実行成功率 (status=="ok" / 全クエリ)
  2. useful_source_rate   — ソース採用率 (accepted / discovered)
  3. novel_domain_rate    — 新規ドメイン率 (初出 / ユニーク採用ドメイン)
  4. stagnation_hours     — 前回 accepted>0 サイクルからの経過時間
  5. concept_delta_rate   — concept 生成率（Ingest 完了後に別経路で更新する設計）

可視化・ドラフト生成（Phase E1）はこの JSONL を読み込む前提。
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from config import STATE_DIR

logger = logging.getLogger(__name__)

KPI_DIR = STATE_DIR / "kpi"


def _kpi_path(objective_id: str) -> Path:
    return KPI_DIR / f"{objective_id}.jsonl"


def _load_history(objective_id: str) -> list[dict]:
    path = _kpi_path(objective_id)
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return records


def _known_domains_before(history: list[dict]) -> set[str]:
    known: set[str] = set()
    for r in history:
        for d in r.get("domains_used", []):
            known.add(str(d))
    return known


def _last_productive_ts(history: list[dict]) -> str | None:
    for r in reversed(history):
        if r.get("accepted", 0) > 0:
            return r.get("ts")
    return None


def record_cycle(
    objective_id: str,
    *,
    batch_statuses: list[str],
    queries_total: int,
    accepted: int,
    discovered: int,
    accepted_domains: dict[str, int],
    zero_reason: str | None = None,
) -> dict:
    """1サイクル分のKPIを記録し、算出した指標を返す。

    concept_delta_rate は Ingest 完了タイミングで埋められる設計のため、
    ここでは None を入れておく（後続モジュールが in-place 更新）。
    """
    KPI_DIR.mkdir(parents=True, exist_ok=True)
    history = _load_history(objective_id)

    statuses = list(batch_statuses or [])
    queries_ok = sum(1 for s in statuses if s == "ok")
    queries_cooldown = sum(1 for s in statuses if s == "cooldown")
    queries_422 = sum(1 for s in statuses if s == "422")
    queries_error = sum(1 for s in statuses if s in ("error", "429"))

    effective_query_rate = (queries_ok / len(statuses)) if statuses else 0.0
    useful_source_rate = (accepted / discovered) if discovered > 0 else 0.0

    known = _known_domains_before(history)
    cycle_domains = {d for d in accepted_domains.keys() if d}
    novel_domains = cycle_domains - known
    unique_accepted = len(cycle_domains)
    novel_domain_rate = (len(novel_domains) / unique_accepted) if unique_accepted > 0 else 0.0

    stagnation_hours = 0.0
    if accepted == 0:
        last_ts = _last_productive_ts(history)
        if last_ts:
            try:
                delta = datetime.now() - datetime.fromisoformat(last_ts)
                stagnation_hours = round(delta.total_seconds() / 3600.0, 2)
            except ValueError:
                stagnation_hours = 0.0

    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "objective_id": objective_id,
        "queries_total": int(queries_total),
        "queries_ok": queries_ok,
        "queries_cooldown": queries_cooldown,
        "queries_422": queries_422,
        "queries_error": queries_error,
        "discovered": int(discovered),
        "accepted": int(accepted),
        "domains_used": sorted(cycle_domains),
        "novel_domains": sorted(novel_domains),
        "zero_reason": zero_reason,
        "kpi": {
            "effective_query_rate": round(effective_query_rate, 3),
            "useful_source_rate": round(useful_source_rate, 3),
            "novel_domain_rate": round(novel_domain_rate, 3),
            "stagnation_hours": stagnation_hours,
            "concept_delta_rate": None,
        },
    }

    try:
        with _kpi_path(objective_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("KPI記録 [%s] %s", objective_id, record["kpi"])
    except OSError as e:
        logger.warning("KPI書き込み失敗 [%s]: %s", objective_id, e)

    return record


def update_concept_delta(objective_id: str, concept_delta: int) -> None:
    """直近レコードの concept_delta_rate を後追いで埋める。

    Ingest パイプライン完了後に main.run_auto_ingest から呼ぶ想定。
    直近レコードの `accepted` を基に `concept_delta / accepted` を計算する。
    accepted==0 の場合は 0.0 を記録する（0除算回避）。
    """
    path = _kpi_path(objective_id)
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError:
        return
    accepted = int(last.get("accepted", 0))
    rate = (concept_delta / accepted) if accepted > 0 else 0.0
    last.setdefault("kpi", {})["concept_delta_rate"] = round(rate, 3)
    last["concept_delta"] = int(concept_delta)
    lines[-1] = json.dumps(last, ensure_ascii=False)
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("concept_delta_rate 反映 [%s]: delta=%d rate=%.3f", objective_id, concept_delta, rate)
    except OSError as e:
        logger.warning("concept_delta 反映失敗 [%s]: %s", objective_id, e)
