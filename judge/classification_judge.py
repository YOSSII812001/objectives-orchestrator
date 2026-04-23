"""Classification Judge — Phase 0 (観察モード)。

classify_concept の出力品質を orchestrator.log から解析する。
既存ロジックには一切触れず、ログを読んでレポートを出力するだけ。

実行方法:
    py -m judge.classification_judge [--days 1]
    py -m judge.classification_judge --since 2026-04-22
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# 親ディレクトリを import path に追加（単体実行対応）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from judge.judge_protocol import JudgeVerdict  # noqa: E402


# =============================================================================
# 閾値（JudgeVerdict の severity 判定に使用）
# =============================================================================

EMPTY_RESPONSE_RATIO_URGENT = 0.30      # JSON空応答率 30% 超で urgent
EMPTY_RESPONSE_RATIO_SUGGEST = 0.15     # 15% 超で suggest
OTHERS_RATIO_SUGGEST = 0.40             # その他カテゴリ比率 40% 超で suggest
CONCEPT_REPEAT_FAIL_THRESHOLD = 3       # 同じ concept 名で N 回連続失敗


# =============================================================================
# ログパターン
# =============================================================================

# 実ログ例:
# 2026-04-22 15:28:33,718 [WARNING] lm_client: JSON解析失敗 (attempt 2/3):
# 2026-04-22 15:28:39,993 [INFO] local_ingest:   スタブ生成: fault-tolerance → その他
# 2026-04-22 15:29:03,932 [INFO] local_ingest:   ドラフト昇格: ai
# 2026-04-22 15:28:39,996 [INFO] local_ingest:   既存概念に追記: temporal

TS_PATTERN = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)"
JSON_FAIL_RE = re.compile(TS_PATTERN + r".*lm_client:\s*JSON解析失敗\s*\(attempt (\d)/(\d)\)")
# 実際のログパターン: "スタブ生成: <concept> → <category>"
STUB_CLASSIFY_RE = re.compile(TS_PATTERN + r".*local_ingest:\s*スタブ生成:\s*([^\s→]+)\s*→\s*(.+?)\s*$")
CLASSIFY_INVALID_RE = re.compile(TS_PATTERN + r".*LLMが未知カテゴリを返却:\s*(.+)$")
CYCLE_START_RE = re.compile(TS_PATTERN + r".*オーケストレーターサイクル開始")
DRAFT_PROMOTE_RE = re.compile(TS_PATTERN + r".*local_ingest:\s*ドラフト昇格:\s*(.+?)\s*$")
APPEND_EXISTING_RE = re.compile(TS_PATTERN + r".*local_ingest:\s*既存概念に追記:\s*(.+?)\s*$")


@dataclass
class LogStats:
    total_classify: int = 0
    empty_json_attempts: int = 0            # attempt X/3 失敗回数（3回1セット=1分類で3カウント）
    full_empty_failures: int = 0            # 3/3 失敗（分類失敗確定）
    others_category: int = 0
    valid_category: int = 0
    invalid_category_returned: int = 0
    concepts_classified: Counter = field(default_factory=Counter)   # concept → count
    concept_others: Counter = field(default_factory=Counter)        # 「その他」になった concept
    cycle_count: int = 0
    evidence_samples: list[str] = field(default_factory=list)
    period_start: datetime | None = None
    period_end: datetime | None = None

    def empty_response_ratio(self) -> float:
        if self.total_classify == 0:
            return 0.0
        return self.full_empty_failures / self.total_classify

    def others_ratio(self) -> float:
        if self.total_classify == 0:
            return 0.0
        return self.others_category / self.total_classify


# =============================================================================
# コアロジック
# =============================================================================

def parse_log_period(log_path: Path, since: datetime) -> LogStats:
    """ログを読んで since 以降の分類メトリクスを集計。"""
    stats = LogStats()

    if not log_path.exists():
        return stats

    # エンコーディング耐性: utf-8 → cp932 の順に試す
    text = None
    for enc in ("utf-8", "cp932", "latin-1"):
        try:
            text = log_path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return stats

    in_period = False

    for line in text.splitlines():
        ts_match = re.match(TS_PATTERN, line)
        if ts_match:
            try:
                ts = datetime.strptime(
                    ts_match.group(1).split(",")[0], "%Y-%m-%d %H:%M:%S"
                )
                if ts >= since:
                    in_period = True
                    if stats.period_start is None or ts < stats.period_start:
                        stats.period_start = ts
                    if stats.period_end is None or ts > stats.period_end:
                        stats.period_end = ts
                else:
                    in_period = False
                    continue
            except ValueError:
                pass

        if not in_period:
            continue

        # サイクル開始カウント
        if CYCLE_START_RE.search(line):
            stats.cycle_count += 1

        # JSON 空応答（attempt 1/3 などで出る）
        m = JSON_FAIL_RE.search(line)
        if m:
            attempt = int(m.group(2))
            total_attempts = int(m.group(3))
            stats.empty_json_attempts += 1
            if attempt == total_attempts:
                stats.full_empty_failures += 1
                if len(stats.evidence_samples) < 30:
                    stats.evidence_samples.append(
                        f"[{m.group(1)}] 3回リトライ全敗（JSON空応答）"
                    )

        # スタブ分類
        m = STUB_CLASSIFY_RE.search(line)
        if m:
            concept = m.group(2).strip()
            category = m.group(3).strip()
            stats.total_classify += 1
            stats.concepts_classified[concept] += 1
            if category == "その他":
                stats.others_category += 1
                stats.concept_others[concept] += 1
            else:
                stats.valid_category += 1

        # LLMが未知カテゴリを返した
        m = CLASSIFY_INVALID_RE.search(line)
        if m:
            stats.invalid_category_returned += 1

    return stats


def build_verdicts(stats: LogStats, cycle_ids: list[str]) -> list[JudgeVerdict]:
    """統計データから JudgeVerdict を生成。"""
    verdicts: list[JudgeVerdict] = []

    empty_ratio = stats.empty_response_ratio()
    others_ratio = stats.others_ratio()

    # --- JSON 空応答率 ---
    if empty_ratio >= EMPTY_RESPONSE_RATIO_URGENT:
        verdicts.append(JudgeVerdict(
            judge_name="ClassificationJudge",
            target="classify_concept",
            severity="urgent",
            finding=(
                f"JSON空応答率 {empty_ratio:.0%} "
                f"（全{stats.total_classify}回中{stats.full_empty_failures}回全敗）"
                f"— モデル切替またはプロンプト簡素化を強く推奨"
            ),
            evidence=stats.evidence_samples[:10],
            metrics={
                "total_classify_calls": stats.total_classify,
                "full_empty_failures": stats.full_empty_failures,
                "empty_response_ratio": round(empty_ratio, 3),
                "cycle_count": stats.cycle_count,
            },
            proposed_change={
                "rationale": (
                    "Gemma 4 E4B が長いプロンプトで空応答を返している疑い。"
                    "gpt-oss-20b へのモデル切替（環境変数 LMS_MODEL）または"
                    "プロンプト短縮を検討。"
                ),
                "options": [
                    "環境変数 LMS_MODEL=openai/gpt-oss-20b を設定",
                    "分類プロンプトの選択肢数を減らす（4→2）",
                    "temperature を 0.0 → 0.3 に上げて多様性を持たせる",
                ],
            },
            confidence=0.85,
            cycle_ids=cycle_ids,
        ))
    elif empty_ratio >= EMPTY_RESPONSE_RATIO_SUGGEST:
        verdicts.append(JudgeVerdict(
            judge_name="ClassificationJudge",
            target="classify_concept",
            severity="suggest",
            finding=f"JSON空応答率 {empty_ratio:.0%} — モニタリング推奨",
            evidence=stats.evidence_samples[:5],
            metrics={
                "total_classify_calls": stats.total_classify,
                "full_empty_failures": stats.full_empty_failures,
                "empty_response_ratio": round(empty_ratio, 3),
            },
            confidence=0.7,
            cycle_ids=cycle_ids,
        ))

    # --- 「その他」カテゴリ比率 ---
    if others_ratio >= OTHERS_RATIO_SUGGEST and stats.total_classify >= 5:
        top_others = stats.concept_others.most_common(10)
        verdicts.append(JudgeVerdict(
            judge_name="ClassificationJudge",
            target="classify_concept",
            severity="suggest",
            finding=(
                f"「その他」カテゴリ比率 {others_ratio:.0%} — "
                f"カテゴリ体系の見直しまたは分類精度改善が望ましい"
            ),
            evidence=[
                f"{c}: {n}回「その他」分類" for c, n in top_others
            ],
            metrics={
                "others_category": stats.others_category,
                "valid_category": stats.valid_category,
                "others_ratio": round(others_ratio, 3),
                "top_others_concepts": dict(top_others),
            },
            proposed_change={
                "rationale": (
                    "頻出する「その他」分類 concept がカテゴリ体系の"
                    "盲点を示している可能性。"
                ),
                "options": [
                    "既存カテゴリに新カテゴリ追加",
                    "concept グラフから自動でカテゴリ候補を抽出",
                    "分類対象を上位concept のみに絞る",
                ],
            },
            confidence=0.75,
            cycle_ids=cycle_ids,
        ))

    # --- 不正カテゴリ返却 ---
    if stats.invalid_category_returned > 0:
        verdicts.append(JudgeVerdict(
            judge_name="ClassificationJudge",
            target="classify_concept",
            severity="info",
            finding=f"LLMが未知カテゴリを{stats.invalid_category_returned}回返却（フォールバック成功）",
            metrics={"invalid_category_returned": stats.invalid_category_returned},
            confidence=0.9,
            cycle_ids=cycle_ids,
        ))

    # --- 健全な場合の info ---
    if not verdicts and stats.total_classify > 0:
        verdicts.append(JudgeVerdict(
            judge_name="ClassificationJudge",
            target="classify_concept",
            severity="info",
            finding=f"分類品質は健全（全{stats.total_classify}回、空応答率 {empty_ratio:.0%}）",
            metrics={
                "total_classify_calls": stats.total_classify,
                "empty_response_ratio": round(empty_ratio, 3),
                "others_ratio": round(others_ratio, 3),
            },
            confidence=0.9,
            cycle_ids=cycle_ids,
        ))

    return verdicts


def write_report(
    verdicts: list[JudgeVerdict],
    stats: LogStats,
    report_dir: Path,
) -> Path:
    """Markdown レポートを書き出す。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = report_dir / f"{today}-classification.md"

    lines = [
        "---",
        f"title: \"ClassificationJudge レポート - {today}\"",
        f"date: {today}",
        f"judge: ClassificationJudge",
        f"phase: 0",
        "---",
        "",
        f"# ClassificationJudge レポート — {today}",
        "",
        "## 集計期間",
        "",
        f"- 開始: `{stats.period_start.isoformat() if stats.period_start else 'N/A'}`",
        f"- 終了: `{stats.period_end.isoformat() if stats.period_end else 'N/A'}`",
        f"- 観察サイクル数: **{stats.cycle_count}**",
        "",
        "## サマリーメトリクス",
        "",
        "| 指標 | 値 |",
        "|------|-----|",
        f"| 総 classify 呼び出し数 | {stats.total_classify} |",
        f"| JSON空応答 attempt 回数 | {stats.empty_json_attempts} |",
        f"| 3回リトライ全敗回数 | {stats.full_empty_failures} |",
        f"| 空応答率 (3/3失敗 / 総呼び出し) | **{stats.empty_response_ratio():.1%}** |",
        f"| 「その他」カテゴリ数 | {stats.others_category} |",
        f"| 有効カテゴリ数 | {stats.valid_category} |",
        f"| 「その他」比率 | **{stats.others_ratio():.1%}** |",
        f"| LLM未知カテゴリ返却（フォールバック） | {stats.invalid_category_returned} |",
        "",
    ]

    if stats.concept_others:
        lines.append("## 「その他」に落ちた concept (頻度順)")
        lines.append("")
        for concept, count in stats.concept_others.most_common(20):
            lines.append(f"- `{concept}` × {count}")
        lines.append("")

    lines.append("## Judge 判定")
    lines.append("")
    for v in verdicts:
        lines.append(v.to_markdown())
        lines.append("")
        lines.append("---")
        lines.append("")

    if not verdicts:
        lines.append("*このレポート期間中に判定対象のデータがありませんでした。*")

    lines.append("")
    lines.append("## 次のステップ")
    lines.append("")
    lines.append("- このレポートは Phase 0（観察モード）の出力です。")
    lines.append("- プロンプト変更は行われていません。")
    lines.append("- 改善提案を適用したい場合は、手動で `lm_client.py` / `local_ingest.py` を編集してください。")
    lines.append("- Phase 1（提案モード）への移行は `~/.claude/plans/llm-as-judge-orchestrator.md` 参照。")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# =============================================================================
# エントリポイント
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classification Judge (Phase 0 観察モード)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="直近 N 日分のログを解析（--since 指定時は無視）",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO形式 (YYYY-MM-DD) で解析開始日時を指定",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="orchestrator.log のパス（省略時はプロジェクト既定）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="レポートを書き出さず、標準出力に表示",
    )
    args = parser.parse_args()

    # ログファイルのパス解決
    if args.log_file:
        log_path = Path(args.log_file)
    else:
        log_path = ROOT / "logs" / "orchestrator.log"

    # 集計期間
    if args.since:
        since = datetime.fromisoformat(args.since)
    else:
        since = datetime.now() - timedelta(days=args.days)

    # 集計
    stats = parse_log_period(log_path, since)

    # Verdict 生成
    cycle_ids = [
        f"cycle-{stats.period_start.isoformat()}" if stats.period_start else "cycle-unknown"
    ]
    verdicts = build_verdicts(stats, cycle_ids)

    # レポート出力
    report_dir = ROOT / "state" / "judge_reports"
    if args.dry_run:
        today = datetime.now().strftime("%Y-%m-%d")
        print(f"# [DRY-RUN] {today}-classification report preview\n")
        for v in verdicts:
            print(v.to_markdown())
            print("\n---\n")
    else:
        path = write_report(verdicts, stats, report_dir)
        print(f"[ClassificationJudge] レポート生成: {path}")
        print(f"  - 判定数: {len(verdicts)}")
        print(f"  - 総classify: {stats.total_classify}")
        print(f"  - 空応答率: {stats.empty_response_ratio():.1%}")
        print(f"  - その他率: {stats.others_ratio():.1%}")

    # urgent があれば exit code 2 で返す（CI 連携など想定）
    if any(v.severity == "urgent" for v in verdicts):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
