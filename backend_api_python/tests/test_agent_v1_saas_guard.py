"""Tests for the SaaS / multi-tenant deployment guard on token issuance.

When `QUANTDINGER_DEPLOYMENT_MODE=saas` (or `hosted` / `shared` / ...), an admin
must NOT be able to issue an Agent token that can route real-money trades:

  * Any request including the `T` scope is rejected with 403 — no silent
    downgrade, so the operator sees their request was modified.
  * `paper_only` is force-pinned to `True` regardless of payload.

Self-hosted deployments (env var unset) keep the V3.1.0 behavior unchanged.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.routes.agent_v1 import admin as admin_routes
from app.utils import agent_auth, auth as core_auth


# ──────────────────────────── helper-level coverage ────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False),
        ("", False),
        ("self", False),
        ("local", False),
        ("saas", True),
        ("SaaS", True),
        ("HOSTED", True),
        ("shared", True),
        ("multitenant", True),
        ("multi-tenant", True),
    ],
)
def test_is_saas_mode_recognizes_known_spellings(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("QUANTDINGER_DEPLOYMENT_MODE", raising=False)
    else:
        monkeypatch.setenv("QUANTDINGER_DEPLOYMENT_MODE", raw)
    assert admin_routes._is_saas_mode() is expected


# ──────────────────────────── route-level integration ──────────────────────────


@pytest.fixture
def admin_authed(monkeypatch):
    """Stub the JWT verifier so /admin/* routes accept any bearer string.

    Patches `verify_token` rather than the decorators themselves — that way the
    real `login_required` + `admin_required` code paths still run (they just
    see an admin payload), so we exercise the same chain production uses.
    """
    monkeypatch.setattr(
        core_auth,
        "verify_token",
        lambda _raw: {"sub": "tester", "user_id": 42, "role": "admin"},
    )
    yield {"user_id": 42}


@pytest.fixture
def stub_db_for_issue(monkeypatch):
    """Make `INSERT INTO qd_agent_tokens` succeed without a live Postgres."""
    class _StubCursor:
        def __init__(self):
            self._row = None

        def execute(self, _sql, _params=None):
            self._row = {"id": 1, "created_at": datetime(2026, 5, 2, 0, 0, 0)}

        def fetchone(self):
            return self._row

        def close(self):
            pass

    class _StubConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _StubCursor()

        def commit(self):
            pass

    monkeypatch.setattr(admin_routes, "get_db_connection", lambda: _StubConn())


def _post_issue(client, payload, *, base_url="http://localhost"):
    return client.post(
        "/api/agent/v1/admin/tokens",
        headers={
            "Authorization": "Bearer admin-jwt-doesnt-matter",
            "Content-Type": "application/json",
        },
        json=payload,
        base_url=base_url,
    )


def test_self_hosted_mode_allows_T_scope(
    client, admin_authed, stub_db_for_issue, monkeypatch
):
    """With no env var set, T-scope tokens issue normally (paper_only respected)."""
    monkeypatch.delenv("QUANTDINGER_DEPLOYMENT_MODE", raising=False)

    resp = _post_issue(client, {
        "name": "selfhost-trader",
        "scopes": "R,T",
        "paper_only": True,
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 0
    data = body["data"]
    assert "T" in data["scopes"]
    assert data["paper_only"] is True
    assert data["token"].startswith(agent_auth.TOKEN_PREFIX)


def test_saas_mode_rejects_T_scope_with_403(
    client, admin_authed, stub_db_for_issue, monkeypatch
):
    """Hosted deployment must surface a clear 403 on T-scope, not a silent downgrade."""
    monkeypatch.setenv("QUANTDINGER_DEPLOYMENT_MODE", "saas")

    resp = _post_issue(client, {
        "name": "saas-trader-attempt",
        "scopes": "R,T",
        "paper_only": False,
    })
    assert resp.status_code == 403
    body = resp.get_json()
    # Error envelope: {code, message, ...}
    assert body["code"] == 403
    assert "T-scope" in body["message"] or "live trading" in body["message"].lower()


def test_saas_mode_force_pins_paper_only_for_non_T_tokens(
    client, admin_authed, stub_db_for_issue, monkeypatch
):
    """Even without T, hosted mode keeps paper_only=true (defense in depth)."""
    monkeypatch.setenv("QUANTDINGER_DEPLOYMENT_MODE", "hosted")

    resp = _post_issue(client, {
        "name": "saas-research-bot",
        "scopes": "R,B",
        "paper_only": False,  # operator tries to opt out — must still be pinned
    })
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert "T" not in data["scopes"]
    assert data["paper_only"] is True
