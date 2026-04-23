"""inbox/ へのsource-template形式MD書き出し（atomic書き込み）"""
import logging
import re
import uuid
from pathlib import Path

from config import INBOX_DIR

logger = logging.getLogger(__name__)


def _generate_inbox_filename(url: str) -> str:
    """URLからkebab-case のinboxファイル名を生成。"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    path_parts = [p for p in parsed.path.strip("/").split("/") if p][:2]
    slug = "-".join([domain] + path_parts)
    slug = re.sub(r"[^a-z0-9\-]", "", slug.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return (slug[:60] or "unknown") + ".md"


def write_to_inbox(markdown_content: str, url: str) -> Path | None:
    """Markdown要約をinbox/にatomic書き込み。

    一時ファイル(.tmp_uuid.md)に書いてからリネーム。
    空・極小コンテンツは書き込まない（summarize_page の空応答対策）。
    """
    INBOX_DIR.mkdir(parents=True, exist_ok=True)

    # 空 / 極小 / frontmatter だけの内容は保存しない（Gemma 空応答対策）
    stripped = (markdown_content or "").strip()
    if len(stripped) < 80:
        logger.warning("要約が空または極小のためinbox書き込みをスキップ: url=%s size=%d", url, len(stripped))
        return None
    # frontmatter のみで body が無い場合もスキップ
    # 例: '---\n{}\n---' のようなゴミ
    if stripped.count("---") >= 2:
        # 2個目の --- 以降を本文とみなす
        parts = stripped.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else ""
        if len(body) < 40:
            logger.warning("要約本文が空（frontmatterのみ）、スキップ: url=%s", url)
            return None

    filename = _generate_inbox_filename(url)
    target = INBOX_DIR / filename

    # 同名ファイルが既に存在する場合はスキップ
    if target.exists():
        logger.info("inbox/に同名ファイル既存、スキップ: %s", filename)
        return target

    # 一時ファイル → リネーム (atomic)
    tmp_name = f".tmp_{uuid.uuid4().hex[:8]}.md"
    tmp_path = INBOX_DIR / tmp_name

    try:
        tmp_path.write_text(markdown_content, encoding="utf-8")
        tmp_path.rename(target)
        logger.info("inbox書き込み完了: %s (%d bytes)", filename, len(markdown_content.encode("utf-8")))
        return target
    except OSError as e:
        logger.error("inbox書き込み失敗: %s — %s", filename, e)
        # クリーンアップ
        if tmp_path.exists():
            tmp_path.unlink()
        return None
