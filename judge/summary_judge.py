"""Summary Judge — Phase 0 (観察モード)。

summarize_page() の出力品質を orchestrator.log と
Obsidian Vault の sources/*.md から解析する。
既存ロジックには一切触れず、ログとソースを読んでレポートを出力するだけ。

実行方法:
    py -m judge.summary_judge [--days 1]
    py -m judge.summary_judge --since 2026-04-22T00:00:00
    py -m judge.summary_judge --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

# 親ディレクトリを import path に追加（単体実行対応）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from judge.judge_protocol import JudgeVerdict  # noqa: E402


# =============================================================================
# 閾値（JudgeVerdict の severity 判定に使用）
# =============================================================================

EMPTY_SKIP_RATIO_URGENT = 0.30       # 空応答スキップ率 30% 超で urgent
EMPTY_SKIP_RATIO_SUGGEST = 0.15      # 15% 超で suggest
ZERO_CONCEPT_RATIO_SUGGEST = 0.20    # 抽出コンセプト 0件 ソース比率 20% 超で suggest
SHORT_SOURCE_RATIO_SUGGEST = 0.25    # 短縮ソース(<500 chars)比率 25% 超で suggest
FORMAT_NONCOMPLIANT_RATIO_SUGGEST = 0.10  # フォーマット非準拠 10% 超で suggest

SHORT_BODY_THRESHOLD = 500           # body 500 chars 未満は「短縮ソース」
EXPECTED_SECTIONS = [
    "## 核心の主張",
    "## 手法・アプローチ",
    "## 主要な発見・結論",
    "## 抽出コンセプト",
]


# =============================================================================
# ログパターン
# =============================================================================

# 実ログ例:
# 2026-04-22 19:27:57,316 [WARNING] inbox_writer: 要約が空または極小のためinbox書き込みをスキップ: url=<url> size=0
# 2026-04-22 16:14:41,704 [ERROR] lm_client: 要約生成失敗: <url> — <error>
# 2026-04-22 15:21:48,059 [INFO] inbox_writer: inbox書き込み完了: <filename> (<size> bytes)
# 2026-04-22 ... オーケストレーターサイクル開始

TS_PATTERN = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)"
INBOX_WRITE_RE = re.compile(
    TS_PATTERN + r".*inbox_writer:\s*inbox書き込み完了:\s*(\S+)\s*\((\d+)\s*bytes\)"
)
INBOX_SKIP_EMPTY_RE = re.compile(
    TS_PATTERN
    + r".*inbox_writer:\s*要約が空または極小のためinbox書き込みをスキップ:\s*"
    r"url=(\S+)\s*size=(\d+)"
)
SUMMARIZE_ERROR_RE = re.compile(
    TS_PATTERN + r".*lm_client:\s*要約生成失敗:\s*(\S+)\s*—\s*(.+?)\s*$"
)
CYCLE_START_RE = re.compile(TS_PATTERN + r".*オーケストレーターサイクル開始")


# =============================================================================
# データモデル
# =============================================================================


@dataclass
class LogStats:
    total_summarize_attempts: int = 0      # 成功 + 空スキップ + エラー
    success_count: int = 0                 # inbox書き込み完了（size > 0）
    zero_byte_writes: int = 0              # inbox書き込み完了だが size=0（異常）
    empty_skip_count: int = 0              # 「要約が空または極小」スキップ
    summarize_error_count: int = 0         # 400/接続エラー等
    success_sizes: list[int] = field(default_factory=list)
    empty_skip_urls: list[str] = field(default_factory=list)
    error_urls: list[tuple[str, str]] = field(default_factory=list)
    cycle_count: int = 0
    evidence_samples: list[str] = field(default_factory=list)
    period_start: datetime | None = None
    period_end: datetime | None = None

    def empty_skip_ratio(self) -> float:
        if self.total_summarize_attempts == 0:
            return 0.0
        return self.empty_skip_count / self.total_summarize_attempts

    def success_ratio(self) -> float:
        if self.total_summarize_attempts == 0:
            return 0.0
        return self.success_count / self.total_summarize_attempts

    def average_size(self) -> float:
        if not self.success_sizes:
            return 0.0
        return sum(self.success_sizes) / len(self.success_sizes)


@dataclass
class SourceStats:
    total_sources: int = 0
    complete_sources: int = 0
    short_sources: int = 0                   # body < 500 chars かつ status=complete
    zero_concept_sources: int = 0            # [[...]] リンク 0件
    noncompliant_sources: int = 0            # 必須セクション欠落
    truncated_sources: int = 0               # 末尾が「。」等で終わらない or 途中 --- 出現
    concept_counts: list[int] = field(default_factory=list)
    body_lengths: list[int] = field(default_factory=list)
    short_source_names: list[str] = field(default_factory=list)
    zero_concept_source_names: list[str] = field(default_factory=list)
    noncompliant_source_names: list[str] = field(default_factory=list)
    truncated_source_names: list[str] = field(default_factory=list)
    recent_source_names: list[str] = field(default_factory=list)

    def zero_concept_ratio(self) -> float:
        if self.complete_sources == 0:
            return 0.0
        return self.zero_concept_sources / self.complete_sources

    def short_source_ratio(self) -> float:
        if self.complete_sources == 0:
            return 0.0
        return self.short_sources / self.complete_sources

    def noncompliant_ratio(self) -> float:
        if self.complete_sources == 0:
            return 0.0
        return self.noncompliant_sources / self.complete_sources

    def average_concept_count(self) -> float:
        if not self.concept_counts:
            return 0.0
        return sum(self.concept_counts) / len(self.concept_counts)

    def median_concept_count(self) -> float:
        if not self.concept_counts:
            return 0.0
        return median(self.concept_counts)


# =============================================================================
# コアロジック — ログ解析
# =============================================================================


def _read_text_resilient(path: Path) -> str | None:
    """UTF-8 → cp932 → latin-1 の順に試す。"""
    for enc in ("utf-8", "cp932", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return None


def parse_log_period(log_path: Path, since: datetime) -> LogStats:
    """orchestrator.log を読んで since 以降の summarize メトリクスを集計。"""
    stats = LogStats()

    if not log_path.exists():
        return stats

    text = _read_text_resilient(log_path)
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

        # サイクル開始
        if CYCLE_START_RE.search(line):
            stats.cycle_count += 1

        # inbox 書き込み完了
        m = INBOX_WRITE_RE.search(line)
        if m:
            size = int(m.group(3))
            filename = m.group(2)
            stats.total_summarize_attempts += 1
            if size > 0:
                stats.success_count += 1
                stats.success_sizes.append(size)
            else:
                stats.zero_byte_writes += 1
                if len(stats.evidence_samples) < 30:
                    stats.evidence_samples.append(
                        f"[{m.group(1)}] 0 bytes 書き込み: {filename}"
                    )
            continue

        # 空応答スキップ
        m = INBOX_SKIP_EMPTY_RE.search(line)
        if m:
            url = m.group(2)
            stats.total_summarize_attempts += 1
            stats.empty_skip_count += 1
            stats.empty_skip_urls.append(url)
            if len(stats.evidence_samples) < 30:
                stats.evidence_samples.append(
                    f"[{m.group(1)}] 空応答スキップ: {url[:80]}"
                )
            continue

        # summarize エラー
        m = SUMMARIZE_ERROR_RE.search(line)
        if m:
            url = m.group(2)
            err = m.group(3)
            stats.total_summarize_attempts += 1
            stats.summarize_error_count += 1
            stats.error_urls.append((url, err))
            if len(stats.evidence_samples) < 30:
                stats.evidence_samples.append(
                    f"[{m.group(1)}] summarize エラー: {url[:60]} — {err[:60]}"
                )
            continue

    return stats


# =============================================================================
# コアロジック — ソース解析
# =============================================================================


def _split_frontmatter(text: str) -> tuple[str, str]:
    """先頭 '---' で囲まれた YAML frontmatter を分離して (frontmatter, body) を返す。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return "", text
    fm = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    return fm, body


def _extract_status(frontmatter: str) -> str | None:
    for line in frontmatter.splitlines():
        m = re.match(r"\s*status\s*:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def _extract_date_modified(frontmatter: str) -> datetime | None:
    for line in frontmatter.splitlines():
        m = re.match(r"\s*date_modified\s*:\s*(\S+)", line)
        if m:
            try:
                return datetime.fromisoformat(m.group(1).strip().strip('"').strip("'"))
            except ValueError:
                continue
    return None


def _check_truncation(body: str) -> bool:
    """末尾切れの疑いを判定。True なら疑わしい。"""
    stripped = body.strip()
    if not stripped:
        return True

    # body 中に '---' 区切りが現れるのは frontmatter 重複の疑い
    body_inner = "\n".join(stripped.splitlines()[1:])  # 先頭見出しを除いた残り
    if re.search(r"^\s*---\s*$", body_inner, flags=re.MULTILINE):
        return True

    # 末尾が文末記号で終わっているか（日本語「。」/ 英語 . ! ? / リスト終端 / コードブロック）
    last_line = stripped.splitlines()[-1].rstrip()
    if not last_line:
        return True
    terminal_ok = (
        last_line.endswith("。")
        or last_line.endswith("．")
        or last_line.endswith(".")
        or last_line.endswith("!")
        or last_line.endswith("?")
        or last_line.endswith("！")
        or last_line.endswith("？")
        or last_line.endswith(")")
        or last_line.endswith("）")
        or last_line.endswith("```")
        or last_line.startswith("- ")   # 箇条書き末尾は許容
        or last_line.startswith("* ")
    )
    return not terminal_ok


def scan_sources(
    sources_dir: Path,
    since: datetime | None = None,
) -> SourceStats:
    """Obsidian Vault の sources/*.md を解析。"""
    stats = SourceStats()

    if not sources_dir.exists():
        return stats

    for md_path in sources_dir.glob("*.md"):
        stats.total_sources += 1
        text = _read_text_resilient(md_path)
        if text is None:
            continue

        fm, body = _split_frontmatter(text)
        status = _extract_status(fm)

        # since が指定されたら date_modified でフィルタ
        if since is not None:
            dm = _extract_date_modified(fm)
            if dm is None or dm < since:
                continue
            stats.recent_source_names.append(md_path.name)

        # status=complete のソースのみ詳細判定
        if status != "complete":
            continue
        stats.complete_sources += 1

        body_stripped = body.strip()
        body_len = len(body_stripped)
        stats.body_lengths.append(body_len)

        # 短縮ソース
        if body_len < SHORT_BODY_THRESHOLD:
            stats.short_sources += 1
            if len(stats.short_source_names) < 20:
                stats.short_source_names.append(
                    f"{md_path.name} ({body_len} chars)"
                )

        # 抽出コンセプト ([[...]] リンク) カウント
        concept_matches = re.findall(r"\[\[([^\]]+)\]\]", body)
        concept_count = len(concept_matches)
        stats.concept_counts.append(concept_count)
        if concept_count == 0:
            stats.zero_concept_sources += 1
            if len(stats.zero_concept_source_names) < 20:
                stats.zero_concept_source_names.append(md_path.name)

        # フォーマット準拠（必須セクションが揃っているか）
        missing = [s for s in EXPECTED_SECTIONS if s not in body]
        if missing:
            stats.noncompliant_sources += 1
            if len(stats.noncompliant_source_names) < 20:
                stats.noncompliant_source_names.append(
                    f"{md_path.name} (欠落: {', '.join(m.replace('## ', '') for m in missing)})"
                )

        # 末尾切れ
        if _check_truncation(body):
            stats.truncated_sources += 1
            if len(stats.truncated_source_names) < 20:
                stats.truncated_source_names.append(md_path.name)

    return stats


# =============================================================================
# Verdict 生成
# =============================================================================


def build_verdicts(
    log_stats: LogStats,
    src_stats: SourceStats,
    cycle_ids: list[str],
) -> list[JudgeVerdict]:
    verdicts: list[JudgeVerdict] = []

    empty_ratio = log_stats.empty_skip_ratio()
    zero_concept_ratio = src_stats.zero_concept_ratio()
    short_ratio = src_stats.short_source_ratio()
    noncompliant_ratio = src_stats.noncompliant_ratio()

    # --- 空応答スキップ率（ログベース） ---
    if log_stats.total_summarize_attempts > 0:
        if empty_ratio >= EMPTY_SKIP_RATIO_URGENT:
            verdicts.append(JudgeVerdict(
                judge_name="SummaryJudge",
                target="summarize_page",
                severity="urgent",
                finding=(
                    f"空応答スキップ率 {empty_ratio:.0%} "
                    f"（全{log_stats.total_summarize_attempts}回中{log_stats.empty_skip_count}回）"
                    f"— Gemma 思考トークン予算不足の強い疑い。緊急対応を推奨"
                ),
                evidence=log_stats.evidence_samples[:10],
                metrics={
                    "total_summarize_attempts": log_stats.total_summarize_attempts,
                    "success_count": log_stats.success_count,
                    "empty_skip_count": log_stats.empty_skip_count,
                    "summarize_error_count": log_stats.summarize_error_count,
                    "empty_skip_ratio": round(empty_ratio, 3),
                    "success_ratio": round(log_stats.success_ratio(), 3),
                    "average_success_size_bytes": round(log_stats.average_size(), 1),
                    "cycle_count": log_stats.cycle_count,
                },
                proposed_change={
                    "rationale": (
                        "summarize_page の max_tokens が入力サイズに対して不足している疑い。"
                        "Gemma 4 E4B の思考トークンが入力サイズに比例して膨張し、"
                        "content 生成予算を食い潰している可能性が高い。"
                    ),
                    "options": [
                        "SUMMARIZE_PAGE_MAX_TOKENS を 4096 → 8192 に増やす",
                        "SUMMARIZE_PAGE_MAX_CHARS を 12000 → 8000 に減らす"
                        "（入力を抑えて思考予算を確保）",
                        "特定の大サイズページを事前に縮約してから summarize に渡す",
                        "環境変数 LMS_MODEL=openai/gpt-oss-20b へ切替（思考トークン問題回避）",
                    ],
                },
                confidence=0.85,
                cycle_ids=cycle_ids,
            ))
        elif empty_ratio >= EMPTY_SKIP_RATIO_SUGGEST:
            verdicts.append(JudgeVerdict(
                judge_name="SummaryJudge",
                target="summarize_page",
                severity="suggest",
                finding=f"空応答スキップ率 {empty_ratio:.0%} — モニタリング推奨",
                evidence=log_stats.evidence_samples[:5],
                metrics={
                    "total_summarize_attempts": log_stats.total_summarize_attempts,
                    "empty_skip_count": log_stats.empty_skip_count,
                    "empty_skip_ratio": round(empty_ratio, 3),
                },
                proposed_change={
                    "rationale": (
                        "空応答スキップが増加傾向。思考トークン予算の逼迫が疑われる。"
                    ),
                    "options": [
                        "SUMMARIZE_PAGE_MAX_TOKENS を段階的に増やす",
                        "入力サイズが大きいページの前処理（縮約）を検討",
                    ],
                },
                confidence=0.7,
                cycle_ids=cycle_ids,
            ))

    # --- summarize エラー（接続・API 問題） ---
    if log_stats.summarize_error_count > 0:
        verdicts.append(JudgeVerdict(
            judge_name="SummaryJudge",
            target="lm_client.summarize_page",
            severity="suggest" if log_stats.summarize_error_count < 3 else "urgent",
            finding=(
                f"summarize エラー {log_stats.summarize_error_count} 件 "
                f"— LM Studio への接続または API 応答に問題あり"
            ),
            evidence=[
                f"{url[:80]} — {err[:80]}"
                for url, err in log_stats.error_urls[:10]
            ],
            metrics={
                "summarize_error_count": log_stats.summarize_error_count,
            },
            confidence=0.8,
            cycle_ids=cycle_ids,
        ))

    # --- 抽出コンセプト 0件 ソース比率（ソースベース） ---
    if (
        src_stats.complete_sources > 0
        and zero_concept_ratio >= ZERO_CONCEPT_RATIO_SUGGEST
    ):
        verdicts.append(JudgeVerdict(
            judge_name="SummaryJudge",
            target="sources/*.md (抽出コンセプト)",
            severity="suggest",
            finding=(
                f"抽出コンセプト 0件 ソース比率 {zero_concept_ratio:.0%} "
                f"（全{src_stats.complete_sources}件中{src_stats.zero_concept_sources}件）"
                f"— 要約の情報密度が低下している疑い"
            ),
            evidence=src_stats.zero_concept_source_names[:10],
            metrics={
                "complete_sources": src_stats.complete_sources,
                "zero_concept_sources": src_stats.zero_concept_sources,
                "zero_concept_ratio": round(zero_concept_ratio, 3),
                "average_concept_count": round(src_stats.average_concept_count(), 2),
                "median_concept_count": round(src_stats.median_concept_count(), 2),
            },
            proposed_change={
                "rationale": (
                    "抽出コンセプトが 0 件のソースが多いと、グラフ成長が停滞する。"
                    "要約の短縮またはプロンプト指示の劣化が疑われる。"
                ),
                "options": [
                    "summarize プロンプトで [[concept]] リンク最低3件を明示要求",
                    "max_tokens を増やして要約の完結を確保",
                    "0件ソースを再要約キューに投入",
                ],
            },
            confidence=0.75,
            cycle_ids=cycle_ids,
        ))

    # --- 短縮ソース比率（ソースベース） ---
    if (
        src_stats.complete_sources > 0
        and short_ratio >= SHORT_SOURCE_RATIO_SUGGEST
    ):
        verdicts.append(JudgeVerdict(
            judge_name="SummaryJudge",
            target="sources/*.md (本文サイズ)",
            severity="suggest",
            finding=(
                f"短縮ソース(<{SHORT_BODY_THRESHOLD} chars)比率 {short_ratio:.0%} "
                f"（全{src_stats.complete_sources}件中{src_stats.short_sources}件）"
                f"— 思考トークン逼迫による短縮要約の疑い"
            ),
            evidence=src_stats.short_source_names[:10],
            metrics={
                "complete_sources": src_stats.complete_sources,
                "short_sources": src_stats.short_sources,
                "short_source_ratio": round(short_ratio, 3),
                "short_threshold_chars": SHORT_BODY_THRESHOLD,
            },
            proposed_change={
                "rationale": (
                    "short_source の多くは思考トークン膨張で content が早期終了した疑い。"
                    "Gemma 系モデルの既知の問題と一致する。"
                ),
                "options": [
                    "SUMMARIZE_PAGE_MAX_TOKENS を 4096 → 8192 に増やす",
                    "該当ソースを再 summarize キューに投入",
                ],
            },
            confidence=0.7,
            cycle_ids=cycle_ids,
        ))

    # --- フォーマット非準拠 ---
    if (
        src_stats.complete_sources > 0
        and noncompliant_ratio >= FORMAT_NONCOMPLIANT_RATIO_SUGGEST
    ):
        verdicts.append(JudgeVerdict(
            judge_name="SummaryJudge",
            target="sources/*.md (必須セクション)",
            severity="suggest",
            finding=(
                f"フォーマット非準拠ソース比率 {noncompliant_ratio:.0%} "
                f"（全{src_stats.complete_sources}件中{src_stats.noncompliant_sources}件）"
                f"— 必須セクション欠落"
            ),
            evidence=src_stats.noncompliant_source_names[:10],
            metrics={
                "complete_sources": src_stats.complete_sources,
                "noncompliant_sources": src_stats.noncompliant_sources,
                "noncompliant_ratio": round(noncompliant_ratio, 3),
                "expected_sections": EXPECTED_SECTIONS,
            },
            proposed_change={
                "rationale": (
                    "必須セクションが揃っていないソースは情報密度が低く、"
                    "wiki-ingest の concept 抽出に失敗しやすい。"
                ),
                "options": [
                    "summarize プロンプトで必須セクション構造を再明示",
                    "要約完了後に構造バリデーションを通す",
                ],
            },
            confidence=0.7,
            cycle_ids=cycle_ids,
        ))

    # --- 末尾切れ（情報） ---
    if src_stats.truncated_sources > 0:
        verdicts.append(JudgeVerdict(
            judge_name="SummaryJudge",
            target="sources/*.md (末尾切れ)",
            severity="info",
            finding=(
                f"末尾切れの疑いがあるソース {src_stats.truncated_sources} 件"
            ),
            evidence=src_stats.truncated_source_names[:10],
            metrics={
                "truncated_sources": src_stats.truncated_sources,
            },
            confidence=0.6,
            cycle_ids=cycle_ids,
        ))

    # --- ゼロバイト書き込み（異常） ---
    if log_stats.zero_byte_writes > 0:
        verdicts.append(JudgeVerdict(
            judge_name="SummaryJudge",
            target="inbox_writer",
            severity="suggest",
            finding=(
                f"0 bytes の inbox 書き込み {log_stats.zero_byte_writes} 件 "
                f"— ガードを通り抜けた空ファイルの可能性"
            ),
            metrics={"zero_byte_writes": log_stats.zero_byte_writes},
            confidence=0.6,
            cycle_ids=cycle_ids,
        ))

    # --- 健全な場合の info ---
    if not verdicts and (
        log_stats.total_summarize_attempts > 0 or src_stats.complete_sources > 0
    ):
        verdicts.append(JudgeVerdict(
            judge_name="SummaryJudge",
            target="summarize_page",
            severity="info",
            finding=(
                f"要約品質は健全（試行{log_stats.total_summarize_attempts}回、"
                f"空応答率 {empty_ratio:.0%}、短縮率 {short_ratio:.0%}）"
            ),
            metrics={
                "total_summarize_attempts": log_stats.total_summarize_attempts,
                "empty_skip_ratio": round(empty_ratio, 3),
                "short_source_ratio": round(short_ratio, 3),
                "zero_concept_ratio": round(zero_concept_ratio, 3),
                "average_concept_count": round(src_stats.average_concept_count(), 2),
            },
            confidence=0.85,
            cycle_ids=cycle_ids,
        ))

    return verdicts


# =============================================================================
# レポート出力
# =============================================================================


def write_report(
    verdicts: list[JudgeVerdict],
    log_stats: LogStats,
    src_stats: SourceStats,
    report_dir: Path,
    since: datetime | None,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = report_dir / f"{today}-summary.md"

    lines = [
        "---",
        f"title: \"SummaryJudge レポート - {today}\"",
        f"date: {today}",
        f"judge: SummaryJudge",
        f"phase: 0",
        "---",
        "",
        f"# SummaryJudge レポート — {today}",
        "",
        "## 集計期間",
        "",
        f"- 開始: `{log_stats.period_start.isoformat() if log_stats.period_start else 'N/A'}`",
        f"- 終了: `{log_stats.period_end.isoformat() if log_stats.period_end else 'N/A'}`",
        f"- since フィルタ: `{since.isoformat() if since else 'N/A'}`",
        f"- 観察サイクル数: **{log_stats.cycle_count}**",
        "",
        "## ログ由来メトリクス（summarize_page）",
        "",
        "| 指標 | 値 |",
        "|------|-----|",
        f"| 総 summarize 試行数 | {log_stats.total_summarize_attempts} |",
        f"| 成功件数 (inbox書込 size>0) | {log_stats.success_count} |",
        f"| 空応答スキップ件数 | {log_stats.empty_skip_count} |",
        f"| summarize エラー件数 | {log_stats.summarize_error_count} |",
        f"| 0 bytes 書き込み | {log_stats.zero_byte_writes} |",
        f"| 空応答スキップ率 | **{log_stats.empty_skip_ratio():.1%}** |",
        f"| 成功率 | **{log_stats.success_ratio():.1%}** |",
        f"| 成功ソースの平均サイズ | {log_stats.average_size():.0f} bytes |",
        "",
        "## ソース品質メトリクス（sources/*.md）",
        "",
        "| 指標 | 値 |",
        "|------|-----|",
        f"| 全ソース数 | {src_stats.total_sources} |",
        f"| status=complete ソース数 | {src_stats.complete_sources} |",
        f"| 短縮ソース(<{SHORT_BODY_THRESHOLD} chars) | {src_stats.short_sources} |",
        f"| 抽出コンセプト 0件 ソース | {src_stats.zero_concept_sources} |",
        f"| フォーマット非準拠ソース | {src_stats.noncompliant_sources} |",
        f"| 末尾切れ疑いソース | {src_stats.truncated_sources} |",
        f"| 短縮ソース比率 | **{src_stats.short_source_ratio():.1%}** |",
        f"| 抽出コンセプト 0件比率 | **{src_stats.zero_concept_ratio():.1%}** |",
        f"| フォーマット非準拠比率 | **{src_stats.noncompliant_ratio():.1%}** |",
        f"| 抽出コンセプト平均数 | {src_stats.average_concept_count():.2f} |",
        f"| 抽出コンセプト中央値 | {src_stats.median_concept_count():.2f} |",
        "",
    ]

    if src_stats.short_source_names:
        lines.append("## 短縮ソース (先頭20件)")
        lines.append("")
        for name in src_stats.short_source_names:
            lines.append(f"- `{name}`")
        lines.append("")

    if src_stats.zero_concept_source_names:
        lines.append("## 抽出コンセプト 0件 ソース (先頭20件)")
        lines.append("")
        for name in src_stats.zero_concept_source_names:
            lines.append(f"- `{name}`")
        lines.append("")

    if src_stats.noncompliant_source_names:
        lines.append("## フォーマット非準拠ソース (先頭20件)")
        lines.append("")
        for name in src_stats.noncompliant_source_names:
            lines.append(f"- `{name}`")
        lines.append("")

    if log_stats.empty_skip_urls:
        lines.append("## 空応答スキップ URL (先頭20件)")
        lines.append("")
        for url in log_stats.empty_skip_urls[:20]:
            lines.append(f"- {url}")
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
    lines.append("- プロンプト変更・パラメータ変更は行われていません。")
    lines.append(
        "- 改善提案を適用したい場合は、手動で `lm_client.py` / `.env` を編集してください。"
    )
    lines.append("- Phase 1（提案モード）への移行は `~/.claude/plans/llm-as-judge-orchestrator.md` 参照。")

    # UTF-8 で書き出し。cp932 フォールバックは write 失敗時のみ。
    try:
        report_path.write_text("\n".join(lines), encoding="utf-8")
    except UnicodeEncodeError:
        report_path.write_text(
            "\n".join(lines), encoding="cp932", errors="replace"
        )
    return report_path


# =============================================================================
# エントリポイント
# =============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summary Judge (Phase 0 観察モード)"
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
        help="ISO形式 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS) で解析開始日時を指定",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="orchestrator.log のパス（省略時はプロジェクト既定）",
    )
    parser.add_argument(
        "--sources-dir",
        type=str,
        default=None,
        help="Obsidian Vault の sources ディレクトリ（省略時は既定）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="レポートを書き出さず、標準出力に表示",
    )
    args = parser.parse_args()

    # ログファイル
    if args.log_file:
        log_path = Path(args.log_file)
    else:
        log_path = ROOT / "logs" / "orchestrator.log"

    # ソースディレクトリ
    if args.sources_dir:
        sources_dir = Path(args.sources_dir)
    else:
        sources_dir = Path.home() / "Documents" / "Obsidian Vault" / "wiki" / "sources"

    # 集計期間
    if args.since:
        since = datetime.fromisoformat(args.since)
    else:
        since = datetime.now() - timedelta(days=args.days)

    # 集計
    log_stats = parse_log_period(log_path, since)
    src_stats = scan_sources(sources_dir, since=since)

    cycle_ids = [
        f"cycle-{log_stats.period_start.isoformat()}"
        if log_stats.period_start
        else "cycle-unknown"
    ]
    verdicts = build_verdicts(log_stats, src_stats, cycle_ids)

    # レポート出力
    report_dir = ROOT / "state" / "judge_reports"
    if args.dry_run:
        today = datetime.now().strftime("%Y-%m-%d")
        # Windows コンソール (cp932) で絵文字が化けないよう UTF-8 に強制。
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        print(f"# [DRY-RUN] {today}-summary report preview\n")
        for v in verdicts:
            try:
                print(v.to_markdown())
            except UnicodeEncodeError:
                # 最終フォールバック: 絵文字を ASCII 代替へ置換
                md = v.to_markdown()
                for emoji, alt in [
                    ("\U0001f6a8", "[URGENT]"),
                    ("\U0001f4a1", "[SUGGEST]"),
                    ("ℹ️", "[INFO]"),
                ]:
                    md = md.replace(emoji, alt)
                print(md.encode("cp932", errors="replace").decode("cp932"))
            print("\n---\n")
    else:
        path = write_report(verdicts, log_stats, src_stats, report_dir, since)
        print(f"[SummaryJudge] レポート生成: {path}")
        print(f"  - 判定数: {len(verdicts)}")
        print(f"  - 総summarize試行: {log_stats.total_summarize_attempts}")
        print(f"  - 空応答スキップ率: {log_stats.empty_skip_ratio():.1%}")
        print(f"  - 抽出コンセプト平均: {src_stats.average_concept_count():.2f}")
        print(f"  - 短縮ソース比率: {src_stats.short_source_ratio():.1%}")

    # urgent があれば exit code 2
    if any(v.severity == "urgent" for v in verdicts):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
