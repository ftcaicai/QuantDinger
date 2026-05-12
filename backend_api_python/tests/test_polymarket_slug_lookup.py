"""Regression tests for PolymarketDataSource._fetch_market_by_slug.

These pin the behavior that fixes the bug where pasting different Polymarket
event URLs always returned the same (popular but unrelated) market: the lookup
must hit both /markets?slug=xxx and /events?slug=xxx, and must return None
(not a fallback popular market) when neither endpoint matches.
"""
from app.data_sources.polymarket import PolymarketDataSource


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


def _record_session(handler):
    """Build a fake session whose .get(url, params=..., timeout=...) delegates to handler."""
    calls = []

    class _FakeSession:
        @staticmethod
        def get(url, params=None, timeout=None):
            calls.append((url, params or {}))
            return handler(url, params or {})

    return _FakeSession(), calls


def test_slug_lookup_uses_markets_endpoint_first(monkeypatch):
    """Single-market events (slug is a market slug) must resolve via /markets?slug= without ever hitting /events."""
    src = PolymarketDataSource()

    def handler(url, params):
        if url.endswith("/markets") and params.get("slug") == "como-slug":
            return _FakeResp([{"slug": "como-slug", "question": "Q?", "id": "123"}])
        return _FakeResp([])

    fake_session, calls = _record_session(handler)
    monkeypatch.setattr(src, "session", fake_session)
    monkeypatch.setattr(
        src,
        "_parse_gamma_events",
        lambda lst: [{"market_id": "123", "question": "Q?", "slug": "como-slug"}],
    )

    result = src._fetch_market_by_slug("como-slug")

    assert result is not None
    assert result["market_id"] == "123"
    assert result["slug"] == "como-slug"
    # /events must NOT be queried when /markets already answered
    assert not any(url.endswith("/events") for url, _ in calls)


def test_slug_lookup_falls_back_to_events_for_multi_market_events(monkeypatch):
    """Multi-market events (slug is an event slug, not a market slug) must resolve via /events?slug= and pick the sub-market whose slug equals the event slug."""
    src = PolymarketDataSource()

    def handler(url, params):
        if url.endswith("/markets"):
            return _FakeResp([])  # event slug is not a market slug → empty
        if url.endswith("/events") and params.get("slug") == "ms-event":
            return _FakeResp([
                {
                    "slug": "ms-event",
                    "markets": [
                        # the "primary" market shares the event slug
                        {"slug": "ms-event", "question": "Primary?", "id": "100"},
                        {"slug": "ms-other", "question": "Other?", "id": "101"},
                    ],
                }
            ])
        return _FakeResp([])

    fake_session, calls = _record_session(handler)
    monkeypatch.setattr(src, "session", fake_session)
    # _parse_gamma_events is wrapped: it sees the chosen sub-market (with slug == event slug)
    monkeypatch.setattr(
        src,
        "_parse_gamma_events",
        lambda lst: [{"market_id": lst[0]["id"], "question": lst[0]["question"], "slug": lst[0]["slug"]}],
    )

    result = src._fetch_market_by_slug("ms-event")

    assert result is not None
    # must pick the sub-market matching the event slug, not the first arbitrary one
    assert result["market_id"] == "100"
    assert result["slug"] == "ms-event"
    # both endpoints participated
    urls = [u for u, _ in calls]
    assert any(u.endswith("/markets") for u in urls)
    assert any(u.endswith("/events") for u in urls)


def test_slug_lookup_returns_none_when_neither_endpoint_finds_it(monkeypatch):
    """No silent fallback: an unknown slug must return None so the route can 404, instead of substituting an unrelated popular market."""
    src = PolymarketDataSource()

    def handler(url, params):
        return _FakeResp([])

    fake_session, _ = _record_session(handler)
    monkeypatch.setattr(src, "session", fake_session)
    monkeypatch.setattr(src, "_parse_gamma_events", lambda lst: [])

    assert src._fetch_market_by_slug("does-not-exist-12345") is None


def test_slug_lookup_handles_empty_input():
    """Defensive: empty slug must short-circuit to None."""
    assert PolymarketDataSource()._fetch_market_by_slug("") is None
    assert PolymarketDataSource()._fetch_market_by_slug(None) is None
