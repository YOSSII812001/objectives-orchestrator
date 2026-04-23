"""objectives-orchestrator の自己観察・自己改善モジュール。

temporal.md で発見した llm-as-judge パターンを orchestrator 自身に適用する。

Phase 0: 観察モードのみ（既存ロジック不変）
Phase 1: 提案モード（承認機構経由）
Phase 2: 自律モード（MetaJudge + A/B）

設計書: ~/.claude/plans/llm-as-judge-orchestrator.md
"""
from .judge_protocol import JudgeVerdict

__all__ = [
    "JudgeVerdict",
    "SummaryLogStats",
    "SummarySourceStats",
    "parse_summary_log_period",
    "scan_summary_sources",
    "build_summary_verdicts",
]


def __getattr__(name: str):  # PEP 562 lazy import
    """summary_judge 関連シンボルは遅延 import。

    ``py -m judge.summary_judge`` 実行時の RuntimeWarning を回避するため、
    パッケージ初期化時点では summary_judge を import しない。
    """
    if name in {
        "SummaryLogStats",
        "SummarySourceStats",
        "parse_summary_log_period",
        "scan_summary_sources",
        "build_summary_verdicts",
    }:
        from . import summary_judge as _sj

        mapping = {
            "SummaryLogStats": _sj.LogStats,
            "SummarySourceStats": _sj.SourceStats,
            "parse_summary_log_period": _sj.parse_log_period,
            "scan_summary_sources": _sj.scan_sources,
            "build_summary_verdicts": _sj.build_verdicts,
        }
        return mapping[name]
    raise AttributeError(f"module 'judge' has no attribute {name!r}")
