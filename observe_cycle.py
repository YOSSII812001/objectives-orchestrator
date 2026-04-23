"""observe_cycle — オーケストレーターサイクル観察レポートジェネレータ。

Task Scheduler で起動された orchestrator サイクルの結果を、
`logs/orchestrator.log` から解析して Markdown レポートを出力する。

実行方法:
    py observe_cycle.py                                 # 直近1時間
    py observe_cycle.py --since 2026-04-22T19:00:00     # 指定時刻以降

Phase 0: 既存ロジックには一切触れない。読み取り専用解析のみ。

参考: judge/classification_judge.py（ログ解析パターン）
"""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
from collections import Counter

# Windows cp932 対策: stdout/stderr を UTF-8 で再構成（絵文字対応）
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "logs" / "orchestrator.log"
REPORT_DIR = ROOT / "state" / "observation_reports"

# =============================================================================
# ログパターン（実ログから抽出）
# =============================================================================

TS_PATTERN = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+"

# サイクル境界
CYCLE_START_RE = re.compile(TS_PATTERN + r".*main:\s*=+\s*オーケストレーターサイクル開始")
CYCLE_END_RE = re.compile(TS_PATTERN + r".*main:\s*=+\s*サイクル終了\s*\(新規(\d+)件\)")
CYCLE_SKIP_RE = re.compile(TS_PATTERN + r".*main:\s*前回サイクル実行中。スキップします")
LMS_UNREACHABLE_RE = re.compile(TS_PATTERN + r".*main:\s*LM Studioサーバーに接続できません")

# Ingest 境界
INGEST_START_RE = re.compile(TS_PATTERN + r".*main:\s*=+\s*ローカルIngest開始")
INGEST_END_RE = re.compile(TS_PATTERN + r".*main:\s*=+\s*ローカルIngest完了:\s*(\{.+\})")
INGEST_SUMMARY_RE = re.compile(
    TS_PATTERN
    + r".*local_ingest:\s*Ingest完了:\s*inbox=(\d+),\s*sources=(\d+),"
    + r"\s*concepts新規=(\d+),\s*昇格=(\d+),\s*追記=(\d+),\s*エラー=(\d+)"
)

# Gap分析
GAP_START_RE = re.compile(TS_PATTERN + r".*gap_analyzer:\s*Gap分析開始:\s*stub=(\d+),\s*draft=(\d+),\s*complete=(\d+)")
GAP_SKIP_RE = re.compile(TS_PATTERN + r".*gap_analyzer:\s*Gap分析:\s*前回実行から")
GAP_RESULT_RE = re.compile(TS_PATTERN + r".*main:\s*Gap分析結果:\s*(\{.+\})")

# Brave検索
BRAVE_OK_RE = re.compile(TS_PATTERN + r".*browser_client:\s*Brave検索完了:\s*'(.+?)'\s*→\s*(\d+)件")
BRAVE_422_RE = re.compile(TS_PATTERN + r".*Brave Search APIエラー:.*422 Unprocessable Entity")
BRAVE_429_RE = re.compile(TS_PATTERN + r".*HTTP/1\.1 429 Too Many Requests.*search\.brave\.com")
BRAVE_SKIP_RE = re.compile(TS_PATTERN + r".*browser_client:\s*クエリスキップ（クールダウン中）:\s*(.+)$")

# inbox 書き込み / 空応答
INBOX_OK_RE = re.compile(TS_PATTERN + r".*inbox_writer:\s*inbox書き込み完了:\s*(.+?)\s*\((\d+)\s*bytes\)")
INBOX_EMPTY_RE = re.compile(TS_PATTERN + r".*inbox_writer:\s*要約が空または極小のためinbox書き込みをスキップ")
SUMMARIZE_FAIL_RE = re.compile(TS_PATTERN + r".*lm_client:\s*要約生成失敗:\s*(.+?)\s*—")
FETCH_FAIL_RE = re.compile(TS_PATTERN + r".*main:\s*ページ取得失敗、スキップ:")
SCORE_PASS_RE = re.compile(TS_PATTERN + r".*main:\s*スコア合格\s*\((\d+)/10\)")

# Ingest 内部
STUB_CLASSIFY_RE = re.compile(TS_PATTERN + r".*local_ingest:\s*スタブ生成:\s*([^\s→]+)\s*→\s*(.+?)\s*$")
DRAFT_PROMOTE_RE = re.compile(TS_PATTERN + r".*local_ingest:\s*ドラフト昇格:\s*(.+?)\s*$")
APPEND_CONCEPT_RE = re.compile(TS_PATTERN + r".*local_ingest:\s{3,}既存概念に追記:\s*(.+?)\s*$")

# エラー系
ERROR_LINE_RE = re.compile(TS_PATTERN + r"\s*\[ERROR\]\s*(.+)$")


# =============================================================================
# データモデル
# =============================================================================

@dataclass
class CycleStats:
    cycle_starts: list[datetime] = field(default_factory=list)
    cycle_ends: list[tuple[datetime, int]] = field(default_factory=list)  # (ts, 新規件数)
    cycle_skips: int = 0
    lms_unreachable: int = 0

    ingest_start: datetime | None = None
    ingest_end: datetime | None = None
    ingest_summary_raw: str | None = None
    ingest_inbox: int = 0
    ingest_sources: int = 0
    ingest_concepts_new: int = 0
    ingest_concepts_promoted: int = 0
    ingest_concepts_appended: int = 0
    ingest_errors: int = 0

    gap_executed: bool = False
    gap_skipped: bool = False
    gap_stub: int = 0
    gap_draft: int = 0
    gap_complete: int = 0
    gap_hints_generated: int = 0

    brave_queries: list[str] = field(default_factory=list)
    brave_ok: int = 0
    brave_422: int = 0
    brave_429: int = 0
    brave_skipped: int = 0
    brave_skipped_queries: list[str] = field(default_factory=list)

    inbox_writes: list[tuple[str, int]] = field(default_factory=list)  # (filename, bytes)
    inbox_empty_skips: int = 0
    summarize_failures: int = 0
    fetch_failures: int = 0
    score_pass_count: int = 0

    stub_classifications: list[tuple[str, str]] = field(default_factory=list)  # (concept, category)
    draft_promotions: int = 0
    concept_appends: int = 0

    errors: list[str] = field(default_factory=list)  # サマリー用の短い説明

    period_start: datetime | None = None
    period_end: datetime | None = None

    @property
    def cycle_duration_sec(self) -> float | None:
        """最初のサイクル開始から最後のサイクル終了（または Ingest 終了）までの秒数。"""
        if not self.cycle_starts:
            return None
        start = self.cycle_starts[0]
        end = None
        if self.ingest_end:
            end = self.ingest_end
        elif self.cycle_ends:
            end = self.cycle_ends[-1][0]
        if end is None:
            return None
        return (end - start).total_seconds()

    @property
    def inbox_success_count(self) -> int:
        return len(self.inbox_writes)

    @property
    def inbox_avg_size(self) -> float:
        if not self.inbox_writes:
            return 0.0
        return sum(sz for _, sz in self.inbox_writes) / len(self.inbox_writes)

    def category_counter(self) -> Counter:
        return Counter(cat for _, cat in self.stub_classifications)

    def others_count(self) -> int:
        return self.category_counter().get("その他", 0)


# =============================================================================
# コアロジック
# =============================================================================

def _read_log_text(log_path: Path) -> str | None:
    """UTF-8→CP932→Latin-1 の3段フォールバック。"""
    for enc in ("utf-8", "cp932", "latin-1"):
        try:
            return log_path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return None


def _parse_ts(line: str) -> datetime | None:
    m = re.match(TS_PATTERN, line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_log(log_path: Path, since: datetime) -> CycleStats:
    """since 以降のログを解析して統計を返す。"""
    stats = CycleStats()

    if not log_path.exists():
        return stats

    text = _read_log_text(log_path)
    if text is None:
        return stats

    in_period = False

    for line in text.splitlines():
        ts = _parse_ts(line)
        if ts is not None:
            in_period = ts >= since
            if in_period:
                if stats.period_start is None or ts < stats.period_start:
                    stats.period_start = ts
                if stats.period_end is None or ts > stats.period_end:
                    stats.period_end = ts

        if not in_period:
            continue

        # --- サイクル境界 ---
        m = CYCLE_START_RE.search(line)
        if m:
            stats.cycle_starts.append(ts)  # type: ignore[arg-type]
            continue

        m = CYCLE_END_RE.search(line)
        if m:
            stats.cycle_ends.append((ts, int(m.group(2))))  # type: ignore[arg-type]
            continue

        if CYCLE_SKIP_RE.search(line):
            stats.cycle_skips += 1
            continue

        if LMS_UNREACHABLE_RE.search(line):
            stats.lms_unreachable += 1
            stats.errors.append("LM Studioサーバー接続不可")
            continue

        # --- Ingest 境界 ---
        if INGEST_START_RE.search(line):
            stats.ingest_start = ts
            continue

        m = INGEST_END_RE.search(line)
        if m:
            stats.ingest_end = ts
            stats.ingest_summary_raw = m.group(2)
            continue

        m = INGEST_SUMMARY_RE.search(line)
        if m:
            stats.ingest_inbox = int(m.group(2))
            stats.ingest_sources = int(m.group(3))
            stats.ingest_concepts_new = int(m.group(4))
            stats.ingest_concepts_promoted = int(m.group(5))
            stats.ingest_concepts_appended = int(m.group(6))
            stats.ingest_errors = int(m.group(7))
            continue

        # --- Gap ---
        m = GAP_START_RE.search(line)
        if m:
            stats.gap_executed = True
            stats.gap_stub = int(m.group(2))
            stats.gap_draft = int(m.group(3))
            stats.gap_complete = int(m.group(4))
            continue

        if GAP_SKIP_RE.search(line):
            stats.gap_skipped = True
            continue

        m = GAP_RESULT_RE.search(line)
        if m:
            # 文字列パースの代わりに軽量な正規表現
            hm = re.search(r"'hints_generated':\s*(\d+)", m.group(2))
            if hm:
                stats.gap_hints_generated = int(hm.group(1))
            continue

        # --- Brave ---
        m = BRAVE_OK_RE.search(line)
        if m:
            stats.brave_ok += 1
            stats.brave_queries.append(m.group(2))
            continue

        if BRAVE_422_RE.search(line):
            stats.brave_422 += 1
            continue

        if BRAVE_429_RE.search(line):
            stats.brave_429 += 1
            continue

        m = BRAVE_SKIP_RE.search(line)
        if m:
            stats.brave_skipped += 1
            stats.brave_skipped_queries.append(m.group(2).strip())
            continue

        # --- Inbox / 要約 ---
        m = INBOX_OK_RE.search(line)
        if m:
            fname = m.group(2).strip()
            size = int(m.group(3))
            stats.inbox_writes.append((fname, size))
            continue

        if INBOX_EMPTY_RE.search(line):
            stats.inbox_empty_skips += 1
            continue

        if SUMMARIZE_FAIL_RE.search(line):
            stats.summarize_failures += 1
            continue

        if FETCH_FAIL_RE.search(line):
            stats.fetch_failures += 1
            continue

        if SCORE_PASS_RE.search(line):
            stats.score_pass_count += 1
            continue

        # --- Ingest 内部 ---
        m = STUB_CLASSIFY_RE.search(line)
        if m:
            concept = m.group(2).strip()
            category = m.group(3).strip()
            stats.stub_classifications.append((concept, category))
            continue

        if DRAFT_PROMOTE_RE.search(line):
            stats.draft_promotions += 1
            continue

        if APPEND_CONCEPT_RE.search(line):
            stats.concept_appends += 1
            continue

        # --- エラー ---
        m = ERROR_LINE_RE.search(line)
        if m:
            msg = m.group(2).strip()
            # 冗長なものは省く
            if len(msg) > 200:
                msg = msg[:200] + "…"
            stats.errors.append(msg)

    return stats


# =============================================================================
# 総合判定
# =============================================================================

def determine_verdict(stats: CycleStats) -> tuple[str, list[str]]:
    """(emoji + ラベル, 理由リスト) を返す。

    判定基準:
        🔴: ERRORが1件以上、または 空応答/空要約が5件以上
        🟡: 警告あり、または 2-4件の空応答
        🟢: それ以外
    """
    reasons: list[str] = []

    empty_like = stats.inbox_empty_skips + stats.summarize_failures

    has_errors = len(stats.errors) > 0 or stats.lms_unreachable > 0 or stats.ingest_errors > 0

    if has_errors:
        if stats.lms_unreachable > 0:
            reasons.append(f"LM Studio接続不可 {stats.lms_unreachable}回")
        if stats.ingest_errors > 0:
            reasons.append(f"Ingestエラー {stats.ingest_errors}件")
        if stats.errors:
            reasons.append(f"ERRORログ {len(stats.errors)}件")

    if empty_like >= 5:
        reasons.append(f"空応答/空要約 {empty_like}件（≥5）")
        return ("🔴 異常", reasons)
    if has_errors:
        return ("🔴 異常", reasons)

    if 2 <= empty_like <= 4:
        reasons.append(f"空応答/空要約 {empty_like}件（2-4）")
        return ("🟡 注意", reasons)

    if stats.fetch_failures >= 3:
        reasons.append(f"ページ取得失敗 {stats.fetch_failures}件")
        return ("🟡 注意", reasons)

    if stats.brave_422 > 0 or stats.brave_429 > 0:
        reasons.append(f"Brave 422/429: {stats.brave_422}/{stats.brave_429}")
        # 422/429 だけなら軽度扱い
        return ("🟡 注意", reasons)

    # 健全
    if not reasons:
        reasons.append("エラー・空応答ともに許容範囲内")
    return ("🟢 OK", reasons)


# =============================================================================
# レポート生成
# =============================================================================

def _format_dt(dt: datetime | None) -> str:
    return dt.isoformat(timespec="seconds") if dt else "N/A"


def build_markdown(stats: CycleStats, since: datetime, judge_summary: str) -> str:
    verdict, reasons = determine_verdict(stats)
    duration = stats.cycle_duration_sec

    category_counter = stats.category_counter()
    others_count = stats.others_count()

    lines: list[str] = []
    lines.append("---")
    lines.append(f"generated_at: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"since: {since.isoformat(timespec='seconds')}")
    lines.append(f"verdict: \"{verdict}\"")
    lines.append("---")
    lines.append("")
    lines.append(f"# 観察レポート — {verdict}")
    lines.append("")
    lines.append(f"**解析期間**: `{since.isoformat(timespec='seconds')}` 以降")
    lines.append(f"**ログ範囲**: `{_format_dt(stats.period_start)}` 〜 `{_format_dt(stats.period_end)}`")
    lines.append("")
    lines.append("## 総合判定")
    lines.append("")
    lines.append(f"### {verdict}")
    lines.append("")
    for r in reasons:
        lines.append(f"- {r}")
    lines.append("")

    # --- サイクル ---
    lines.append("## サイクル")
    lines.append("")
    lines.append("| 指標 | 値 |")
    lines.append("|------|-----|")
    lines.append(f"| サイクル開始数 | {len(stats.cycle_starts)} |")
    lines.append(f"| サイクル終了数 | {len(stats.cycle_ends)} |")
    lines.append(f"| 実行中スキップ数 | {stats.cycle_skips} |")
    lines.append(f"| LM Studio接続不可 | {stats.lms_unreachable} |")
    if stats.cycle_starts:
        lines.append(f"| 最初の開始 | `{_format_dt(stats.cycle_starts[0])}` |")
    if stats.cycle_ends:
        last_end, last_new = stats.cycle_ends[-1]
        lines.append(f"| 最後の終了 | `{_format_dt(last_end)}` (新規{last_new}件) |")
    if duration is not None:
        lines.append(f"| 所要時間(秒) | {duration:.1f} |")
    lines.append("")

    # --- Gap ---
    lines.append("## Gap分析")
    lines.append("")
    if stats.gap_executed:
        lines.append(f"- 実行: ✅ stub={stats.gap_stub}, draft={stats.gap_draft}, complete={stats.gap_complete}")
        lines.append(f"- hints_generated: {stats.gap_hints_generated}")
    elif stats.gap_skipped:
        lines.append("- 実行: ⏭ スキップ（前回から6時間未満）")
    else:
        lines.append("- 実行: ❓ 実行ログなし")
    lines.append("")

    # --- Brave検索 ---
    lines.append("## Brave検索")
    lines.append("")
    lines.append("| 指標 | 値 |")
    lines.append("|------|-----|")
    lines.append(f"| 検索成功 (200 OK) | {stats.brave_ok} |")
    lines.append(f"| 422 Unprocessable | {stats.brave_422} |")
    lines.append(f"| 429 Too Many Requests | {stats.brave_429} |")
    lines.append(f"| クールダウンスキップ | {stats.brave_skipped} |")
    total_queries = stats.brave_ok + stats.brave_422 + stats.brave_429 + stats.brave_skipped
    lines.append(f"| クエリ総数 | {total_queries} |")
    lines.append("")
    if stats.brave_queries:
        lines.append("**実行クエリ（最大10件）**:")
        lines.append("")
        for q in stats.brave_queries[:10]:
            lines.append(f"- `{q}`")
        lines.append("")
    if stats.brave_skipped_queries:
        lines.append("**クールダウンスキップ（最大5件）**:")
        lines.append("")
        for q in stats.brave_skipped_queries[:5]:
            lines.append(f"- `{q}`")
        lines.append("")

    # --- 要約/Inbox ---
    lines.append("## 要約 / Inbox")
    lines.append("")
    lines.append("| 指標 | 値 |")
    lines.append("|------|-----|")
    lines.append(f"| inbox書き込み成功 | {stats.inbox_success_count} |")
    lines.append(f"| 空応答スキップ | {stats.inbox_empty_skips} |")
    lines.append(f"| 要約生成失敗 | {stats.summarize_failures} |")
    lines.append(f"| ページ取得失敗 | {stats.fetch_failures} |")
    lines.append(f"| スコア合格件数 | {stats.score_pass_count} |")
    if stats.inbox_writes:
        lines.append(f"| 平均サイズ (bytes) | {stats.inbox_avg_size:.0f} |")
    lines.append("")
    if stats.inbox_writes:
        lines.append("**Inbox書き込み詳細（最大10件）**:")
        lines.append("")
        for fname, size in stats.inbox_writes[:10]:
            lines.append(f"- `{fname}` ({size} bytes)")
        lines.append("")

    # --- Ingest ---
    lines.append("## Ingest結果")
    lines.append("")
    if stats.ingest_start or stats.ingest_end:
        lines.append("| 指標 | 値 |")
        lines.append("|------|-----|")
        lines.append(f"| 開始 | `{_format_dt(stats.ingest_start)}` |")
        lines.append(f"| 終了 | `{_format_dt(stats.ingest_end)}` |")
        lines.append(f"| inbox処理 | {stats.ingest_inbox} |")
        lines.append(f"| sources作成 | {stats.ingest_sources} |")
        lines.append(f"| concepts新規 | **{stats.ingest_concepts_new}** |")
        lines.append(f"| concepts昇格 | {stats.ingest_concepts_promoted} |")
        lines.append(f"| concepts追記 | {stats.ingest_concepts_appended} |")
        lines.append(f"| errors | {stats.ingest_errors} |")
        lines.append("")
    else:
        lines.append("- Ingest実行ログなし")
        lines.append("")

    # --- classify 分類内訳 ---
    lines.append("## classify 分類内訳")
    lines.append("")
    if category_counter:
        lines.append("| カテゴリ | 件数 |")
        lines.append("|----------|------|")
        for cat, cnt in category_counter.most_common():
            highlight = " ⚠" if cat == "その他" else ""
            lines.append(f"| {cat} | {cnt}{highlight} |")
        lines.append(f"| **合計** | **{sum(category_counter.values())}** |")
        lines.append("")
        lines.append(f"- 「その他」件数: **{others_count}**")
        lines.append("")
    else:
        lines.append("- 分類ログなし")
        lines.append("")

    # --- エラー ---
    if stats.errors:
        lines.append("## エラーログ（抜粋）")
        lines.append("")
        for err in stats.errors[:15]:
            lines.append(f"- {err}")
        if len(stats.errors) > 15:
            lines.append(f"- *…他 {len(stats.errors) - 15} 件省略*")
        lines.append("")

    # --- Judge 連携 ---
    lines.append("## ClassificationJudge 連携")
    lines.append("")
    lines.append("```")
    lines.append(judge_summary.strip() if judge_summary else "（Judge 実行結果なし）")
    lines.append("```")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Generated by observe_cycle.py (Phase 0 観察モード)*")

    return "\n".join(lines)


# =============================================================================
# Judge 呼び出し
# =============================================================================

def run_classification_judge(since: datetime) -> str:
    """既存 classification_judge.py を同ウィンドウで呼び出し、stdout をキャプチャ。"""
    cmd = [
        sys.executable,
        "-m",
        "judge.classification_judge",
        "--since",
        since.strftime("%Y-%m-%d"),
    ]
    # Windows cp932 対策: 子プロセスの stdout を UTF-8 で書き出させる
    import os
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
        out = result.stdout or ""
        err = result.stderr or ""
        combined = out
        if err.strip():
            combined += "\n[stderr]\n" + err
        if result.returncode != 0:
            combined += f"\n[returncode={result.returncode}]"
        return combined
    except Exception as e:  # noqa: BLE001
        return f"[Judge呼び出し失敗] {type(e).__name__}: {e}"


# =============================================================================
# エントリポイント
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="オーケストレーターサイクル観察レポート生成"
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO形式 (YYYY-MM-DDTHH:MM:SS) 以降を解析。省略時は直近1時間。",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="orchestrator.log パス（省略時はプロジェクト既定）",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="レポート出力先ディレクトリ（省略時は state/observation_reports）",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="ClassificationJudge を呼び出さない",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="ファイル書き出しを行わず、標準出力に表示",
    )
    args = parser.parse_args()

    log_path = Path(args.log_file) if args.log_file else LOG_PATH
    report_dir = Path(args.output_dir) if args.output_dir else REPORT_DIR

    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"[ERROR] --since の形式が不正: {args.since}", file=sys.stderr)
            return 1
    else:
        since = datetime.now() - timedelta(hours=1)

    stats = parse_log(log_path, since)

    if args.no_judge:
        judge_output = "(--no-judge 指定につき未実行)"
    else:
        judge_output = run_classification_judge(since)

    markdown = build_markdown(stats, since, judge_output)
    verdict, _ = determine_verdict(stats)

    if args.stdout:
        print(markdown)
        return 0

    report_dir.mkdir(parents=True, exist_ok=True)
    fname = datetime.now().strftime("%Y-%m-%d-%H%M") + ".md"
    report_path = report_dir / fname
    report_path.write_text(markdown, encoding="utf-8")

    print(f"[observe_cycle] レポート生成: {report_path}")
    print(f"  - 総合判定: {verdict}")
    print(f"  - サイクル開始数: {len(stats.cycle_starts)}")
    print(f"  - inbox書き込み成功: {stats.inbox_success_count}")
    print(f"  - 空応答スキップ: {stats.inbox_empty_skips}")
    print(f"  - 要約失敗: {stats.summarize_failures}")
    print(f"  - concepts新規/昇格/追記: "
          f"{stats.ingest_concepts_new}/{stats.ingest_concepts_promoted}/{stats.ingest_concepts_appended}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
