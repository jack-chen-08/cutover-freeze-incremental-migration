"""Acceptance criterion 5 — during the freeze window:
  * write endpoints answer 503 with a maintenance notice;
  * read endpoints keep answering 200.

The test flips the runtime flag on the already-imported settings object, so the
same process can prove both the frozen and the unfrozen behaviour.
"""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

import app.main as service
from app.config import settings

client = TestClient(service.app)


def _settings(freeze: bool, message: str | None = None):
    return replace(
        settings,
        freeze_enabled=freeze,
        freeze_message=message or settings.freeze_message,
    )


def test_health_reports_freeze_state(monkeypatch):
    monkeypatch.setattr(service, "settings", _settings(True))
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["freeze_enabled"] is True


def test_write_endpoints_return_503_with_notice(monkeypatch):
    notice = "Cutover freeze in progress: writes resume after the window closes."
    monkeypatch.setattr(service, "settings", _settings(True, notice))

    r = client.post(
        "/api/v1/companies",
        json={
            "name": "Fictional Freeze Test Ltd",
            "org_id": "11111111-1111-1111-1111-111111111111",
            "site_id": "22222222-2222-2222-2222-222222222222",
        },
    )
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "3600"
    body = r.json()
    assert body["detail"] == "service_temporarily_frozen"
    assert body["message"] == notice


def test_every_write_method_is_blocked(monkeypatch):
    monkeypatch.setattr(service, "settings", _settings(True))
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = client.request(method, "/api/v1/companies")
        assert r.status_code == 503, (method, r.status_code)


def test_read_endpoints_keep_serving_during_freeze(monkeypatch):
    monkeypatch.setattr(service, "settings", _settings(True))
    for path in ("/api/v1/companies", "/api/v1/contacts", "/api/v1/freeze/status"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)


def _first_org_and_site() -> tuple[str, str]:
    """Foreign keys must point at rows that really exist in the drill database."""
    from app.db import SourceSession, Org, Site

    with SourceSession() as s:
        org = s.query(Org).first()
        site = s.query(Site).first()
        if org is None or site is None:
            import pytest
            pytest.skip("drill database has no org/site rows yet — run scripts/prepare_drill.py")
        return str(org.id), str(site.id)


def test_writes_work_again_after_freeze_is_lifted(monkeypatch):
    monkeypatch.setattr(service, "settings", _settings(False))
    org_id, site_id = _first_org_and_site()
    r = client.post(
        "/api/v1/companies",
        json={
            "name": "Fictional Post Freeze Ltd",
            "org_id": org_id,
            "site_id": site_id,
        },
    )
    assert r.status_code == 201, r.text
    assert "id" in r.json()
