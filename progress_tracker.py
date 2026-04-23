"""URL重複排除と正規化"""
import re
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

logger = logging.getLogger(__name__)

# 除外するクエリパラメータ
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "fbclid", "gclid", "ref", "source", "mc_cid", "mc_eid",
}


def normalize_url(url: str) -> str:
    """URLを正規化して重複排除に使う。"""
    parsed = urlparse(url)

    # スキーム統一
    scheme = "https"

    # ホスト正規化
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    # パス正規化
    path = parsed.path.rstrip("/") or "/"

    # トラッキングパラメータ除去
    params = parse_qs(parsed.query, keep_blank_values=False)
    filtered = {k: v for k, v in params.items() if k.lower() not in TRACKING_PARAMS}
    query = urlencode(filtered, doseq=True)

    return urlunparse((scheme, host, path, "", query, ""))


def is_valid_url(url: str) -> bool:
    """取得対象として有効なURLかチェック。"""
    parsed = urlparse(url)

    # httpsのみ
    if parsed.scheme not in ("http", "https"):
        return False

    # プライベートIPブロック
    host = parsed.netloc.lower()
    private_patterns = [
        r"^localhost",
        r"^127\.",
        r"^192\.168\.",
        r"^10\.",
        r"^172\.(1[6-9]|2\d|3[01])\.",
        r"^0\.0\.0\.0",
    ]
    for pattern in private_patterns:
        if re.match(pattern, host):
            logger.warning("プライベートURL除外: %s", url)
            return False

    # ファイル拡張子フィルタ
    skip_extensions = {".pdf", ".zip", ".tar", ".gz", ".exe", ".dmg", ".mp4", ".mp3", ".jpg", ".png", ".gif"}
    path_lower = parsed.path.lower()
    for ext in skip_extensions:
        if path_lower.endswith(ext):
            return False

    return True
