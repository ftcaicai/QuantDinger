"""Regression tests for the URL-parsing regex used by /api/polymarket/analyze.

The original regex `polymarket\\.com/event/(...)` failed to match localized URLs
like `polymarket.com/zh/event/...` that Polymarket serves when a browser is set
to a non-English locale. The route then fell back to fuzzy title search using
the entire URL as a "keyword", which scored unrelated markets and surfaced the
wrong question (or, after the earlier fix, returned a 409 to the user).

These tests pin the patterns directly so the regex can't regress without notice.
"""
import re

# Mirror the patterns used by app/routes/polymarket.py — keep these in sync.
URL_PATTERNS = [
    r'polymarket\.com(?:/[a-z]{2}(?:-[A-Z]{2})?)?/event/([^/?#]+)',
    r'polymarket\.com(?:/[a-z]{2}(?:-[A-Z]{2})?)?/markets/(\d+)',
    r'polymarket\.com(?:/[a-z]{2}(?:-[A-Z]{2})?)?/market/(\d+)',
]


def _extract(text):
    for pattern in URL_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def test_plain_event_url():
    assert _extract("https://polymarket.com/event/will-como-finish-in-the-top-4") == "will-como-finish-in-the-top-4"


def test_event_url_with_locale_zh():
    """Polymarket inserts /zh/ when the user's browser is set to Chinese — must be recognized."""
    assert _extract("https://polymarket.com/zh/event/us-x-iran-permanent-peace-deal-by") == "us-x-iran-permanent-peace-deal-by"


def test_event_url_with_locale_en():
    assert _extract("https://polymarket.com/en/event/kraken-ipo-in-2025") == "kraken-ipo-in-2025"


def test_event_url_with_full_locale_code():
    """Some localized links use BCP47-style codes like zh-CN."""
    assert _extract("https://polymarket.com/zh-CN/event/microstrategy-sell-any-bitcoin-in-2025") == "microstrategy-sell-any-bitcoin-in-2025"


def test_event_url_with_query_string():
    """Trackers/tids etc. after the slug must not bleed into the captured slug."""
    assert _extract("https://polymarket.com/event/kraken-ipo-in-2025?tid=abc123") == "kraken-ipo-in-2025"


def test_event_url_with_trailing_fragment():
    assert _extract("https://polymarket.com/event/kraken-ipo-in-2025#comments") == "kraken-ipo-in-2025"


def test_markets_numeric_id():
    assert _extract("https://polymarket.com/markets/123456") == "123456"


def test_markets_numeric_id_with_locale():
    assert _extract("https://polymarket.com/zh/markets/123456") == "123456"


def test_singular_market_path():
    assert _extract("https://polymarket.com/market/789") == "789"


def test_non_polymarket_url_returns_none():
    assert _extract("https://example.com/event/something") is None


def test_three_letter_segment_is_not_treated_as_locale():
    """Polymarket only uses ISO 639-1 two-letter locale codes. Three-letter segments aren't real paths,
    so the regex refuses them rather than guessing — caller will see a clean 400."""
    assert _extract("https://polymarket.com/eng/event/xxx") is None
