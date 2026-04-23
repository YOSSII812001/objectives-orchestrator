"""案B (MAX_CHARS=8000) 適用効果の直接検証スクリプト。

Codexが実測で確認した空応答3URLに対し、summarize_page を直接叩いて
現行の config 設定で実際に何 chars 返るかを測定する。

実行: py verify_case_b.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 診断強化ログを stdout に出す
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from browser_client import fetch_page_text
from config import SUMMARIZE_PAGE_MAX_CHARS, SUMMARIZE_PAGE_MAX_TOKENS
from lm_client import summarize_page

URLS = [
    "https://dev.to/akki907/temporal-workflow-orchestration-building-reliable-agentic-ai-systems-3bpm",
    "https://temporal.io/blog/from-ai-hype-to-durable-reality-why-agentic-flows-need-distributed-systems",
    "https://ai.islinux.com/articles/fixing-vllm-memory-leaks-kernel-tuning.html",
]


def main() -> int:
    print(f"[CONFIG] SUMMARIZE_PAGE_MAX_CHARS={SUMMARIZE_PAGE_MAX_CHARS}")
    print(f"[CONFIG] SUMMARIZE_PAGE_MAX_TOKENS={SUMMARIZE_PAGE_MAX_TOKENS}")
    print()

    results = []
    for url in URLS:
        print(f"=== {url[:80]} ===")
        try:
            page_text = fetch_page_text(url)
        except Exception as e:
            print(f"  FETCH FAILED: {e}")
            continue
        if page_text is None:
            print("  FETCH returned None")
            continue
        print(f"  page_text length: {len(page_text)} chars")

        result = summarize_page(url, page_text, "verify-case-b")
        content_len = len(result) if result else 0
        status = "SUCCESS" if content_len > 80 else "EMPTY/SHORT"
        print(f"  summarize_page → {content_len} chars [{status}]")
        if content_len > 0 and content_len <= 200:
            print(f"  preview: {result[:200]!r}")
        results.append((url, len(page_text), content_len, status))
        print()

    print("=== SUMMARY ===")
    print(f"{'URL':50s} {'page_chars':>10s} {'out_chars':>10s} {'status':>12s}")
    for url, pg, out, st in results:
        print(f"{url[:50]:50s} {pg:>10d} {out:>10d} {st:>12s}")
    success = sum(1 for _, _, _, s in results if s == "SUCCESS")
    print(f"\nSuccess: {success}/{len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
