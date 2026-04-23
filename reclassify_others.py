"""index.md の「その他」カテゴリ内 concepts を再分類するワンオフスクリプト。

Gemma 4 E4B の max_tokens=64 問題で100%失敗していた分類を、
修正後の max_tokens=1024 で再実行して正しいカテゴリへ移す。

使い方:
    py reclassify_others.py          # 実行
    py reclassify_others.py --dry-run # 結果表示のみ
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import frontmatter
from config import WIKI_CONCEPTS_DIR, WIKI_INDEX_PATH
from local_ingest import classify_category


# index.md のカテゴリヘッダ（上から定義順）
CATEGORY_ORDER = [
    "概念",
    "Anthropic・Claude API",
    "エンティティ",
    "ソース",
    "AI運用・自律システム",
    "統合分析",
    "出力",
    "その他",
]


def parse_index(index_text: str) -> dict[str, list[str]]:
    """index.md を section 別の行リストに分解。"""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in index_text.splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            current = m.group(1).strip()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def extract_concept_name(line: str) -> str | None:
    """'- [[concept-name]] — …' から concept-name を抽出。"""
    m = re.match(r"^- \[\[([^\]]+)\]\]", line)
    if m:
        return m.group(1).strip()
    return None


def load_concept_meta(concept_name: str) -> tuple[str, list[str]] | None:
    """concepts/<name>.md から summary と tags を取り出す。"""
    path = WIKI_CONCEPTS_DIR / f"{concept_name}.md"
    if not path.exists():
        return None
    try:
        post = frontmatter.load(str(path))
        summary = post.metadata.get("summary", "") or ""
        tags = post.metadata.get("tags", []) or []
        if isinstance(tags, str):
            tags = [tags]
        return str(summary), [str(t) for t in tags]
    except Exception as e:
        print(f"  WARN: concept読み込み失敗 {concept_name}: {e}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    index_text = WIKI_INDEX_PATH.read_text(encoding="utf-8-sig")
    sections = parse_index(index_text)

    if "その他" not in sections:
        print("「その他」セクションが見つかりません")
        return 0

    # 既存カテゴリ一覧（「その他」は除外して分類先候補とする）
    categories = [c for c in sections.keys() if c != "その他"]

    others_lines = sections["その他"]
    concept_lines: list[tuple[str, str]] = []  # (concept_name, original_line)
    non_concept_lines: list[str] = []

    for line in others_lines:
        name = extract_concept_name(line)
        if name:
            concept_lines.append((name, line))
        else:
            non_concept_lines.append(line)

    print(f"その他セクションから {len(concept_lines)} concepts を抽出")
    print(f"既存カテゴリ候補: {categories}")
    print()

    # 各 concept を再分類
    reclassifications: dict[str, list[str]] = {cat: [] for cat in categories}
    stayed_others: list[str] = []

    for name, line in concept_lines:
        meta = load_concept_meta(name)
        if not meta:
            print(f"  SKIP (file not found): {name}")
            stayed_others.append(line)
            continue
        summary, tags = meta
        new_cat = classify_category(name, summary, tags, categories)

        # classify_category はフォールバックで「その他」を返しうる
        if new_cat in reclassifications:
            reclassifications[new_cat].append(line)
            print(f"  {name:28s} → {new_cat}")
        else:
            stayed_others.append(line)
            print(f"  {name:28s} → その他（据え置き）")

    print()

    # 移動サマリー
    moved = sum(len(v) for v in reclassifications.values())
    print(f"移動予定: {moved} 件 / 据え置き: {len(stayed_others)} 件")
    for cat, lines in reclassifications.items():
        if lines:
            print(f"  [{cat}] +{len(lines)}")

    if args.dry_run:
        print()
        print("[DRY-RUN] ファイル書き換えはスキップしました")
        return 0

    # index.md 再構築
    new_index_lines: list[str] = []
    # フロントマター（先頭 ... ---）を保持
    fm_match = re.match(r"(---\n.*?\n---\n)", index_text, re.DOTALL)
    if fm_match:
        new_index_lines.append(fm_match.group(1).rstrip("\n"))
        new_index_lines.append("")

    # タイトル行
    title_match = re.search(r"^(# .+)$", index_text, re.MULTILINE)
    if title_match:
        new_index_lines.append(title_match.group(1))
        new_index_lines.append("")

    # カテゴリ順序で出力（「その他」はループ内で扱わない、末尾で専用処理）
    for cat in CATEGORY_ORDER:
        if cat == "その他":
            continue
        if cat not in sections:
            continue
        new_index_lines.append(f"## {cat}")
        existing = [l for l in sections.get(cat, []) if l.strip() or l == ""]
        # 末尾の空行を削除
        while existing and existing[-1].strip() == "":
            existing.pop()
        new_index_lines.extend(existing)
        # 再分類で追加された行を末尾に追加
        for line in reclassifications.get(cat, []):
            new_index_lines.append(line)
        new_index_lines.append("")

    # 「その他」は stayed_others（= 分類できなかった残り）だけを書く
    # 元の sections["その他"] は参照しない（既に reclassifications で移動済み or stayed_others に記録）
    new_index_lines.append("## その他")
    while stayed_others and stayed_others[-1].strip() == "":
        stayed_others.pop()
    if stayed_others:
        new_index_lines.extend(stayed_others)
    new_index_lines.append("")

    # atomic write
    backup = WIKI_INDEX_PATH.with_suffix(".md.bak")
    backup.write_text(index_text, encoding="utf-8")
    print(f"バックアップ作成: {backup}")

    WIKI_INDEX_PATH.write_text("\n".join(new_index_lines), encoding="utf-8")
    print(f"index.md 更新完了: {WIKI_INDEX_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
