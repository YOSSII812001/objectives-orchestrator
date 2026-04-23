"""Judge 共通のデータモデルとプロトコル。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Literal, Any
import json


Severity = Literal["info", "suggest", "urgent"]


@dataclass
class JudgeVerdict:
    """Judge が出す評価結果の共通形式。

    Phase 0 では observation 専用。proposed_change は None でよい。
    """

    judge_name: str                            # "ClassificationJudge"
    target: str                                # "classify_concept" など評価対象
    severity: Severity                         # info / suggest / urgent
    finding: str                               # 1文のサマリー
    evidence: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    proposed_change: dict[str, Any] | None = None
    confidence: float = 0.0                    # 0.0-1.0
    created_at: datetime = field(default_factory=datetime.now)
    cycle_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def to_markdown(self) -> str:
        """レポート用の Markdown 表現。"""
        emoji = {"info": "ℹ️", "suggest": "💡", "urgent": "🚨"}[self.severity]
        lines = [
            f"### {emoji} {self.judge_name} — {self.severity.upper()}",
            "",
            f"**判定**: {self.finding}",
            f"**対象**: `{self.target}`",
            f"**信頼度**: {self.confidence:.0%}",
            f"**作成時刻**: {self.created_at.isoformat(timespec='seconds')}",
        ]
        if self.metrics:
            lines.append("")
            lines.append("**メトリクス**:")
            for k, v in self.metrics.items():
                lines.append(f"- `{k}`: {v}")
        if self.evidence:
            lines.append("")
            lines.append("**根拠（抜粋）**:")
            for ev in self.evidence[:10]:
                lines.append(f"- {ev}")
            if len(self.evidence) > 10:
                lines.append(f"- *（他 {len(self.evidence) - 10} 件省略）*")
        if self.proposed_change:
            lines.append("")
            lines.append("**改善提案**:")
            lines.append("```")
            lines.append(json.dumps(self.proposed_change, ensure_ascii=False, indent=2))
            lines.append("```")
        return "\n".join(lines)
