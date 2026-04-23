"""raw/articles/ への生データ永続化"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from config import RAW_ARTICLES_DIR

logger = logging.getLogger(__name__)


def _sanitize_filename(url: str) -> str:
    """URLからkebab-case のファイル名を生成。"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    path_parts = [p for p in parsed.path.strip("/").split("/") if p][:3]
    slug = "-".join([domain] + path_parts)
    slug = re.sub(r"[^a-z0-9\-]", "", slug.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80] or "unknown"


def save_raw_html(url: str, html: str, objective_id: str) -> Path | None:
    """HTMLとメタデータをraw/articles/に保存。"""
    RAW_ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

    slug = _sanitize_filename(url)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{slug}-{timestamp}"

    html_path = RAW_ARTICLES_DIR / f"{filename}.html"
    meta_path = RAW_ARTICLES_DIR / f"{filename}.meta.json"

    try:
        # HTML保存
        html_path.write_text(html, encoding="utf-8")

        # メタデータ保存
        meta = {
            "url": url,
            "fetched_at": datetime.now().isoformat(),
            "objective_id": objective_id,
            "html_file": html_path.name,
            "html_size_bytes": len(html.encode("utf-8")),
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        logger.info("raw保存: %s (%d bytes)", html_path.name, meta["html_size_bytes"])
        return html_path

    except OSError as e:
        logger.error("raw保存失敗: %s — %s", url, e)
        return None
