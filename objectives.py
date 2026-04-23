"""目的ファイルの読み込みと管理"""
import logging
from pathlib import Path
from dataclasses import dataclass

import frontmatter

from config import OBJECTIVES_DIR

logger = logging.getLogger(__name__)


@dataclass
class Objective:
    id: str  # ファイル名(拡張子なし)
    title: str
    status: str
    priority: str
    language: str
    max_results_per_cycle: int
    max_cycles: int
    saturation_threshold: int
    tags: list[str]
    goal: str
    interests: list[str]
    exclusions: list[str]
    file_path: Path


def load_objectives() -> list[Objective]:
    """objectives/配下のactive目的ファイルを優先度順で読み込み。"""
    priority_order = {"high": 0, "medium": 1, "low": 2}
    objectives = []

    if not OBJECTIVES_DIR.exists():
        logger.warning("目的ファイルディレクトリが存在しません: %s", OBJECTIVES_DIR)
        return []

    for md_file in sorted(OBJECTIVES_DIR.glob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
            meta = post.metadata

            if meta.get("status", "active") != "active":
                logger.debug("スキップ (status=%s): %s", meta.get("status"), md_file.name)
                continue

            # 本文からセクションを抽出
            content = post.content
            goal = _extract_section(content, "ゴール")
            interests = _extract_list_section(content, "関心領域")
            exclusions = _extract_list_section(content, "除外条件")

            obj = Objective(
                id=md_file.stem,
                title=meta.get("title", md_file.stem),
                status=meta.get("status", "active"),
                priority=meta.get("priority", "medium"),
                language=meta.get("language", "ja"),
                max_results_per_cycle=meta.get("max_results_per_cycle", 5),
                max_cycles=meta.get("max_cycles", 0),
                saturation_threshold=meta.get("saturation_threshold", 5),
                tags=meta.get("tags", []),
                goal=goal,
                interests=interests,
                exclusions=exclusions,
                file_path=md_file,
            )
            objectives.append(obj)
            logger.info("目的ファイル読み込み: %s (priority=%s)", obj.title, obj.priority)

        except Exception as e:
            logger.error("目的ファイル解析失敗: %s — %s", md_file.name, e)

    objectives.sort(key=lambda o: priority_order.get(o.priority, 1))
    return objectives


def _extract_section(content: str, heading: str) -> str:
    """Markdownから指定見出しのセクション本文を抽出。"""
    lines = content.split("\n")
    capturing = False
    result = []
    for line in lines:
        if line.strip().startswith("## ") and heading in line:
            capturing = True
            continue
        if capturing and line.strip().startswith("## "):
            break
        if capturing:
            result.append(line)
    return "\n".join(result).strip()


def _extract_list_section(content: str, heading: str) -> list[str]:
    """Markdownから指定見出しのリスト項目を抽出。"""
    section = _extract_section(content, heading)
    items = []
    for line in section.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items
