"""Objective Judge — Phase 0 (観察モード)。

objectives/*.md と wiki/concepts/ を突き合わせ、目的達成度を評価する。
既存ロジックには一切触れず、ファイルを読んでレポートを出力するだけ。

実行方法:
    py -m judge.objective_judge
    py -m judge.objective_judge --objective autonomous-ai-24-7
    py -m judge.objective_judge --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from judge.judge_protocol import JudgeVerdict  # noqa: E402


# =============================================================================
# 閾値
# =============================================================================

INTEREST_COVERAGE_URGENT = 0.50     # 関心領域カバレッジ 50% 未満で urgent
INTEREST_COVERAGE_SUGGEST = 0.80    # 80% 未満で suggest
TARGET_CONCEPT_REACH_URGENT = 0.50  # target_concept 到達率 50% 未満で urgent
STUB_RATIO_SUGGEST = 0.70           # stub 比率 70% 超で suggest
ORPHAN_RATIO_SUGGEST = 0.30         # 孤立 concept 比率 30% 超で suggest
KNOWLEDGE_GAP_RESOLVED_URGENT = 0.30  # gap 解消率 30% 未満で urgent


# =============================================================================
# データモデル
# =============================================================================


@dataclass
class ObjectiveSpec:
    slug: str
    title: str
    status: str
    priority: str
    interests: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class HintsSpec:
    slug: str
    queries: list[dict] = field(default_factory=list)
    knowledge_gaps: list[str] = field(default_factory=list)

    @property
    def target_concepts(self) -> list[str]:
        return [q.get("target_concept", "") for q in self.queries if q.get("target_concept")]


@dataclass
class WikiStats:
    total_concepts: int = 0
    stub_count: int = 0
    draft_count: int = 0
    complete_count: int = 0
    unknown_status_count: int = 0
    concept_names: set[str] = field(default_factory=set)
    concept_tags: dict[str, list[str]] = field(default_factory=dict)
    concept_status: dict[str, str] = field(default_factory=dict)
    concept_bodies: dict[str, str] = field(default_factory=dict)
    orphan_concepts: list[str] = field(default_factory=list)

    def stub_ratio(self) -> float:
        denom = self.stub_count + self.draft_count + self.complete_count
        return self.stub_count / denom if denom else 0.0

    def orphan_ratio(self) -> float:
        return len(self.orphan_concepts) / self.total_concepts if self.total_concepts else 0.0


@dataclass
class CoverageStats:
    interests_covered: list[tuple[str, str]] = field(default_factory=list)  # (interest, matched_concept)
    interests_uncovered: list[str] = field(default_factory=list)
    targets_reached: list[str] = field(default_factory=list)
    targets_missing: list[str] = field(default_factory=list)
    gaps_resolved: list[tuple[str, str]] = field(default_factory=list)  # (gap_excerpt, matched_concept)
    gaps_open: list[str] = field(default_factory=list)

    @property
    def interest_coverage(self) -> float:
        total = len(self.interests_covered) + len(self.interests_uncovered)
        return len(self.interests_covered) / total if total else 0.0

    @property
    def target_reach(self) -> float:
        total = len(self.targets_reached) + len(self.targets_missing)
        return len(self.targets_reached) / total if total else 0.0

    @property
    def gap_resolution(self) -> float:
        total = len(self.gaps_resolved) + len(self.gaps_open)
        return len(self.gaps_resolved) / total if total else 0.0


# =============================================================================
# 解析関数
# =============================================================================


def _read_text_resilient(path: Path) -> str | None:
    for enc in ("utf-8-sig", "utf-8", "cp932", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return None


def parse_objective(path: Path) -> ObjectiveSpec | None:
    """objectives/*.md を解析。"""
    text = _read_text_resilient(path)
    if text is None:
        return None

    fm_match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not fm_match:
        return None
    fm_raw, body = fm_match.group(1), fm_match.group(2)

    def _fm(key: str, default: str = "") -> str:
        m = re.search(rf"^\s*{key}\s*:\s*(.+?)\s*$", fm_raw, re.MULTILINE)
        return m.group(1).strip().strip('"').strip("'") if m else default

    tags_raw = _fm("tags", "")
    tags = [t.strip() for t in re.findall(r"[\w\-]+", tags_raw)] if tags_raw else []

    # 関心領域 section
    interests = _extract_section_items(body, "関心領域")
    excludes = _extract_section_items(body, "除外条件")

    return ObjectiveSpec(
        slug=path.stem,
        title=_fm("title"),
        status=_fm("status", "unknown"),
        priority=_fm("priority", "normal"),
        interests=interests,
        excludes=excludes,
        tags=tags,
    )


def _extract_section_items(body: str, section_name: str) -> list[str]:
    """## <section_name> の直下の箇条書き行を抽出。"""
    pattern = rf"^##\s*{re.escape(section_name)}\s*$"
    lines = body.splitlines()
    items = []
    in_section = False
    for line in lines:
        if re.match(pattern, line):
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            m = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if m:
                items.append(m.group(1).strip())
    return items


def parse_hints(path: Path, slug: str) -> HintsSpec:
    """state/search-hints/<slug>.json を解析。"""
    hints = HintsSpec(slug=slug)
    if not path.exists():
        return hints
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        hints.queries = data.get("hints", []) or []
        hints.knowledge_gaps = data.get("knowledge_gaps", []) or []
    except (json.JSONDecodeError, OSError):
        pass
    return hints


def scan_wiki(concepts_dir: Path) -> WikiStats:
    """wiki/concepts/ を全スキャン。"""
    stats = WikiStats()
    if not concepts_dir.exists():
        return stats

    for md_path in concepts_dir.glob("*.md"):
        stats.total_concepts += 1
        name = md_path.stem
        stats.concept_names.add(name)

        text = _read_text_resilient(md_path)
        if text is None:
            continue

        fm_match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if fm_match:
            fm_raw, body = fm_match.group(1), fm_match.group(2)
        else:
            fm_raw, body = "", text

        stats.concept_bodies[name] = body

        status_m = re.search(r"^\s*status\s*:\s*(.+?)\s*$", fm_raw, re.MULTILINE)
        status = status_m.group(1).strip().strip('"').strip("'") if status_m else "unknown"
        stats.concept_status[name] = status
        if status == "stub":
            stats.stub_count += 1
        elif status == "draft":
            stats.draft_count += 1
        elif status == "complete":
            stats.complete_count += 1
        else:
            stats.unknown_status_count += 1

        tags_m = re.search(r"^\s*tags\s*:\s*(.+?)\s*$", fm_raw, re.MULTILINE)
        tags = re.findall(r"[\w\-]+", tags_m.group(1)) if tags_m else []
        stats.concept_tags[name] = tags

    # 孤立 concept 判定: 他 concept から [[...]] でリンクされていない
    link_targets: set[str] = set()
    for body in stats.concept_bodies.values():
        for m in re.finditer(r"\[\[([^\]]+)\]\]", body):
            tgt = m.group(1).strip()
            # section リンク（ #... ）や alias（ | ）を除去
            tgt = tgt.split("#")[0].split("|")[0].strip()
            if tgt:
                link_targets.add(tgt)
    for name in stats.concept_names:
        if name not in link_targets:
            stats.orphan_concepts.append(name)

    return stats


# =============================================================================
# カバレッジ計算
# =============================================================================


_STOPWORDS = {
    "の", "と", "を", "に", "で", "から", "への", "こと", "ため", "など",
    "and", "or", "the", "a", "an", "of", "to", "in", "for", "on", "at",
    "is", "are", "by", "with", "as", "be", "from",
}


def _tokenize(text: str) -> list[str]:
    """関心領域行から英数字/カタカナ/漢字の意味語を抽出。"""
    tokens = re.findall(r"[A-Za-z][\w\-]{2,}|[ァ-ヴ][ァ-ヴー]{2,}|[一-龠]{2,}", text)
    return [t.lower() for t in tokens if t.lower() not in _STOPWORDS]


def _matches_concept(keyword: str, wiki: WikiStats) -> str | None:
    """キーワードが concept 名・tags・body のいずれかにマッチする concept を返す。"""
    kw = keyword.lower()
    for name in wiki.concept_names:
        if kw in name.lower():
            return name
    for name, tags in wiki.concept_tags.items():
        if any(kw in t.lower() for t in tags):
            return name
    for name, body in wiki.concept_bodies.items():
        if kw in body.lower():
            return name
    return None


def compute_coverage(
    objective: ObjectiveSpec, hints: HintsSpec, wiki: WikiStats,
) -> CoverageStats:
    """関心領域・target_concept・knowledge_gap を wiki と突合。"""
    cov = CoverageStats()

    for interest in objective.interests:
        tokens = _tokenize(interest)
        matched = None
        for tok in tokens:
            m = _matches_concept(tok, wiki)
            if m:
                matched = m
                break
        if matched:
            cov.interests_covered.append((interest[:80], matched))
        else:
            cov.interests_uncovered.append(interest[:120])

    for tgt in hints.target_concepts:
        if tgt in wiki.concept_names or _matches_concept(tgt, wiki):
            cov.targets_reached.append(tgt)
        else:
            cov.targets_missing.append(tgt)

    for gap in hints.knowledge_gaps:
        tokens = _tokenize(gap)
        matched = None
        for tok in tokens:
            m = _matches_concept(tok, wiki)
            if m:
                matched = m
                break
        if matched:
            cov.gaps_resolved.append((gap[:80], matched))
        else:
            cov.gaps_open.append(gap[:120])

    return cov


# =============================================================================
# Verdict 生成
# =============================================================================


def build_verdicts(
    objective: ObjectiveSpec, hints: HintsSpec, wiki: WikiStats, cov: CoverageStats,
) -> list[JudgeVerdict]:
    verdicts: list[JudgeVerdict] = []

    ic = cov.interest_coverage
    tr = cov.target_reach
    gr = cov.gap_resolution

    # 関心領域カバレッジ
    if objective.interests:
        if ic < INTEREST_COVERAGE_URGENT:
            verdicts.append(JudgeVerdict(
                judge_name="ObjectiveJudge",
                target=f"objectives/{objective.slug}.md (関心領域)",
                severity="urgent",
                finding=(
                    f"関心領域カバレッジ {ic:.0%} "
                    f"({len(cov.interests_covered)}/{len(cov.interests_covered)+len(cov.interests_uncovered)})"
                    f" — 目的の主要領域が wiki に未反映"
                ),
                evidence=[f"未カバー: {i}" for i in cov.interests_uncovered[:8]],
                metrics={
                    "interest_coverage": round(ic, 3),
                    "interests_total": len(objective.interests),
                    "interests_covered": len(cov.interests_covered),
                    "interests_uncovered": len(cov.interests_uncovered),
                },
                proposed_change={
                    "rationale": "未カバー領域を search-hints の優先クエリに反映することで、orchestrator が不足領域を集中探索できる。",
                    "options": [
                        "未カバー関心領域を search-hints に追加",
                        "関心領域の表現を concept 名に寄せる（例: ReAct, Plan-and-Execute を明示）",
                        "target_concept を未カバー領域向けに再設計",
                    ],
                },
                confidence=0.8,
            ))
        elif ic < INTEREST_COVERAGE_SUGGEST:
            verdicts.append(JudgeVerdict(
                judge_name="ObjectiveJudge",
                target=f"objectives/{objective.slug}.md (関心領域)",
                severity="suggest",
                finding=f"関心領域カバレッジ {ic:.0%} — 追加探索で埋められる余地あり",
                evidence=[f"未カバー: {i}" for i in cov.interests_uncovered[:5]],
                metrics={
                    "interest_coverage": round(ic, 3),
                    "interests_uncovered": len(cov.interests_uncovered),
                },
                confidence=0.7,
            ))

    # target_concept 到達率
    if hints.target_concepts:
        if tr < TARGET_CONCEPT_REACH_URGENT:
            verdicts.append(JudgeVerdict(
                judge_name="ObjectiveJudge",
                target="search-hints.target_concept",
                severity="urgent",
                finding=(
                    f"target_concept 到達率 {tr:.0%} "
                    f"({len(cov.targets_reached)}/{len(cov.targets_reached)+len(cov.targets_missing)})"
                    f" — hints の誘導が空振りしている"
                ),
                evidence=[f"未到達: {t}" for t in cov.targets_missing[:8]],
                metrics={
                    "target_reach": round(tr, 3),
                    "targets_reached": len(cov.targets_reached),
                    "targets_missing": len(cov.targets_missing),
                },
                confidence=0.75,
            ))

    # knowledge_gap 解消率
    if hints.knowledge_gaps:
        if gr < KNOWLEDGE_GAP_RESOLVED_URGENT:
            verdicts.append(JudgeVerdict(
                judge_name="ObjectiveJudge",
                target="search-hints.knowledge_gaps",
                severity="urgent",
                finding=(
                    f"knowledge_gap 解消率 {gr:.0%} "
                    f"({len(cov.gaps_resolved)}/{len(cov.gaps_resolved)+len(cov.gaps_open)})"
                    f" — 未解決ギャップが支配的"
                ),
                evidence=[f"未解決: {g[:100]}" for g in cov.gaps_open[:5]],
                metrics={
                    "gap_resolution": round(gr, 3),
                    "gaps_resolved": len(cov.gaps_resolved),
                    "gaps_open": len(cov.gaps_open),
                },
                proposed_change={
                    "rationale": "gaps が埋まらない場合、search-hints の再生成か検索戦略の転換が必要。",
                    "options": [
                        "gap 本文の名詞句から新クエリを生成して hints に追加",
                        "gap_analyzer.py の再実行で gap を最新化",
                        "ObjectiveJudge の語彙拡張（synonym 考慮）",
                    ],
                },
                confidence=0.75,
            ))

    # stub 比率
    stub_r = wiki.stub_ratio()
    if stub_r >= STUB_RATIO_SUGGEST:
        verdicts.append(JudgeVerdict(
            judge_name="ObjectiveJudge",
            target="wiki/concepts/ (status 分布)",
            severity="suggest",
            finding=(
                f"stub 比率 {stub_r:.0%} "
                f"(stub={wiki.stub_count}, draft={wiki.draft_count}, complete={wiki.complete_count})"
                f" — 深掘りが不足している"
            ),
            metrics={
                "stub_ratio": round(stub_r, 3),
                "stub_count": wiki.stub_count,
                "draft_count": wiki.draft_count,
                "complete_count": wiki.complete_count,
            },
            proposed_change={
                "rationale": "stub ばかりの wiki は広く浅いままで、目的達成の知識として活用しづらい。",
                "options": [
                    "stub concept に対する追加 summarize 投入",
                    "関連 source が 2 件以上ある stub を優先的に draft 昇格",
                ],
            },
            confidence=0.7,
        ))

    # 孤立 concept 比率
    orphan_r = wiki.orphan_ratio()
    if orphan_r >= ORPHAN_RATIO_SUGGEST:
        verdicts.append(JudgeVerdict(
            judge_name="ObjectiveJudge",
            target="wiki/concepts/ (孤立)",
            severity="suggest",
            finding=(
                f"孤立 concept 比率 {orphan_r:.0%} "
                f"({len(wiki.orphan_concepts)}/{wiki.total_concepts})"
                f" — グラフ構造が疎"
            ),
            evidence=[f"孤立: {n}" for n in wiki.orphan_concepts[:10]],
            metrics={
                "orphan_ratio": round(orphan_r, 3),
                "orphan_count": len(wiki.orphan_concepts),
                "total_concepts": wiki.total_concepts,
            },
            proposed_change={
                "rationale": "孤立 concept は「作ったが引用されない」状態。wiki-compile による再構造化か、summarize プロンプトに `[[...]]` 最低件数の指示が必要。",
                "options": [
                    "孤立 concept を統合対象としてリストアップ",
                    "summarize プロンプトで関連 concept への backlink を強制",
                ],
            },
            confidence=0.65,
        ))

    # 健全な場合
    if not verdicts:
        verdicts.append(JudgeVerdict(
            judge_name="ObjectiveJudge",
            target=f"objectives/{objective.slug}.md",
            severity="info",
            finding=(
                f"目的達成度は健全: 関心領域 {ic:.0%} / target {tr:.0%} / gap {gr:.0%} / "
                f"stub {stub_r:.0%} / orphan {orphan_r:.0%}"
            ),
            metrics={
                "interest_coverage": round(ic, 3),
                "target_reach": round(tr, 3),
                "gap_resolution": round(gr, 3),
                "stub_ratio": round(stub_r, 3),
                "orphan_ratio": round(orphan_r, 3),
            },
            confidence=0.85,
        ))

    return verdicts


# =============================================================================
# レポート出力
# =============================================================================


def write_report(
    objective: ObjectiveSpec, hints: HintsSpec, wiki: WikiStats,
    cov: CoverageStats, verdicts: list[JudgeVerdict], report_dir: Path,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = report_dir / f"{today}-objective-{objective.slug}.md"

    ic = cov.interest_coverage
    tr = cov.target_reach
    gr = cov.gap_resolution

    lines = [
        "---",
        f'title: "ObjectiveJudge レポート - {objective.slug} - {today}"',
        f"date: {today}",
        "judge: ObjectiveJudge",
        "phase: 0",
        f"objective: {objective.slug}",
        "---",
        "",
        f"# ObjectiveJudge レポート — {objective.title} — {today}",
        "",
        "## 目的概要",
        "",
        f"- slug: `{objective.slug}`",
        f"- status: `{objective.status}`",
        f"- priority: `{objective.priority}`",
        f"- 関心領域数: {len(objective.interests)}",
        f"- 除外条件数: {len(objective.excludes)}",
        f"- tags: {', '.join(objective.tags) if objective.tags else '—'}",
        "",
        "## カバレッジサマリー",
        "",
        "| 指標 | 値 |",
        "|------|-----|",
        f"| 関心領域カバレッジ | **{ic:.1%}** ({len(cov.interests_covered)}/{len(cov.interests_covered)+len(cov.interests_uncovered)}) |",
        f"| target_concept 到達率 | **{tr:.1%}** ({len(cov.targets_reached)}/{len(cov.targets_reached)+len(cov.targets_missing)}) |",
        f"| knowledge_gap 解消率 | **{gr:.1%}** ({len(cov.gaps_resolved)}/{len(cov.gaps_resolved)+len(cov.gaps_open)}) |",
        f"| 総 concept 数 | {wiki.total_concepts} |",
        f"| stub / draft / complete | {wiki.stub_count} / {wiki.draft_count} / {wiki.complete_count} |",
        f"| stub 比率 | **{wiki.stub_ratio():.1%}** |",
        f"| 孤立 concept 比率 | **{wiki.orphan_ratio():.1%}** ({len(wiki.orphan_concepts)}件) |",
        "",
    ]

    if cov.interests_uncovered:
        lines += ["## 未カバー関心領域", ""]
        for i in cov.interests_uncovered:
            lines.append(f"- {i}")
        lines.append("")

    if cov.interests_covered:
        lines += ["## カバー済み関心領域（マッチ concept）", ""]
        for interest, concept in cov.interests_covered:
            lines.append(f"- {interest} → `[[{concept}]]`")
        lines.append("")

    if cov.targets_missing:
        lines += ["## 未到達 target_concept", ""]
        for t in cov.targets_missing:
            lines.append(f"- `{t}`")
        lines.append("")

    if cov.gaps_open:
        lines += ["## 未解決 knowledge_gap", ""]
        for g in cov.gaps_open:
            lines.append(f"- {g}")
        lines.append("")

    if wiki.orphan_concepts:
        lines += ["## 孤立 concept (先頭20件)", ""]
        for n in wiki.orphan_concepts[:20]:
            lines.append(f"- `{n}` ({wiki.concept_status.get(n, 'unknown')})")
        lines.append("")

    lines += ["## Judge 判定", ""]
    for v in verdicts:
        lines.append(v.to_markdown())
        lines += ["", "---", ""]
    if not verdicts:
        lines.append("*判定対象データなし*")

    lines += [
        "",
        "## 次のステップ",
        "",
        "- このレポートは Phase 0（観察モード）の出力です。",
        "- 目的ファイルや search-hints の自動変更は行われていません。",
        "- 改善提案を適用する場合は `objectives/` または `state/search-hints/` を手動編集してください。",
        "- Phase 1（提案モード）への移行は `~/.claude/plans/llm-as-judge-orchestrator.md` 参照。",
    ]

    try:
        report_path.write_text("\n".join(lines), encoding="utf-8")
    except UnicodeEncodeError:
        report_path.write_text("\n".join(lines), encoding="cp932", errors="replace")
    return report_path


# =============================================================================
# エントリポイント
# =============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="Objective Judge (Phase 0 観察モード)")
    parser.add_argument("--objective", type=str, default=None,
                        help="特定の目的 slug のみ解析（省略時は全 active 目的）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    objectives_dir = ROOT / "objectives"
    hints_dir = ROOT / "state" / "search-hints"
    vault = Path.home() / "Documents" / "Obsidian Vault"
    concepts_dir = vault / "wiki" / "concepts"
    report_dir = ROOT / "state" / "judge_reports"

    # 対象目的を列挙
    if args.objective:
        obj_paths = [objectives_dir / f"{args.objective}.md"]
    else:
        obj_paths = list(objectives_dir.glob("*.md"))

    wiki = scan_wiki(concepts_dir)  # 共通スキャン（目的ごとに再スキャンしない）

    any_urgent = False
    for obj_path in obj_paths:
        if not obj_path.exists():
            print(f"[ObjectiveJudge] SKIP: {obj_path} 不在")
            continue
        objective = parse_objective(obj_path)
        if objective is None:
            print(f"[ObjectiveJudge] SKIP: {obj_path} パース失敗")
            continue
        if objective.status != "active" and not args.objective:
            continue

        hints_path = hints_dir / f"{objective.slug}.json"
        hints = parse_hints(hints_path, objective.slug)
        cov = compute_coverage(objective, hints, wiki)
        verdicts = build_verdicts(objective, hints, wiki, cov)

        if args.dry_run:
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass
            print(f"# [DRY-RUN] {objective.slug}")
            print(f"interests: {cov.interest_coverage:.1%}, target: {cov.target_reach:.1%}, gaps: {cov.gap_resolution:.1%}")
            for v in verdicts:
                print(v.to_markdown())
                print("\n---\n")
        else:
            path = write_report(objective, hints, wiki, cov, verdicts, report_dir)
            print(f"[ObjectiveJudge] {objective.slug}: {path}")
            print(f"  - interest={cov.interest_coverage:.1%} target={cov.target_reach:.1%} gap={cov.gap_resolution:.1%}")
            print(f"  - stub={wiki.stub_ratio():.1%} orphan={wiki.orphan_ratio():.1%} verdicts={len(verdicts)}")

        if any(v.severity == "urgent" for v in verdicts):
            any_urgent = True

    return 2 if any_urgent else 0


if __name__ == "__main__":
    sys.exit(main())
