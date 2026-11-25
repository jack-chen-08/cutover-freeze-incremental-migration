"""Member-data service: read endpoints keep serving during the cutover freeze,
write endpoints answer 503 with a maintenance notice (acceptance criterion 5)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.db import SourceSession, TargetSession, MemberCompany, Contact

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Read-only endpoints that stay available while the freeze is active.
ALWAYS_OPEN = {"/health", "/api/v1/freeze/status"}

app = FastAPI(title="Member Space — Cutover Freeze Service", version="1.0.0")


@app.middleware("http")
async def freeze_guard(request: Request, call_next):
    path = request.url.path
    if (
        settings.freeze_enabled
        and request.method in WRITE_METHODS
        and not path.startswith("/api/v1/freeze")
    ):
        return JSONResponse(
            status_code=503,
            content={"detail": "service_temporarily_frozen", "message": settings.freeze_message},
            headers={"Retry-After": "3600"},
        )
    return await call_next(request)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "freeze_enabled": settings.freeze_enabled}


@app.get("/api/v1/freeze/status")
def freeze_status() -> dict:
    return {"freeze_enabled": settings.freeze_enabled, "message": settings.freeze_message}


class CompanyIn(BaseModel):
    name: str
    org_id: uuid.UUID
    site_id: uuid.UUID
    tax_no: str | None = None
    industry: str | None = None
    employee_count: int = 0


@app.get("/api/v1/companies")
def list_companies(limit: int = 20, offset: int = 0) -> dict:
    with SourceSession() as s:
        rows = s.query(MemberCompany).order_by(MemberCompany.created_at).offset(offset).limit(limit).all()
        return {
            "items": [
                {
                    "id": str(r.id),
                    "name": r.name,
                    "industry": r.industry,
                    "employee_count": r.employee_count,
                    "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                }
                for r in rows
            ],
            "limit": limit,
            "offset": offset,
        }


@app.post("/api/v1/companies", status_code=201)
def create_company(payload: CompanyIn) -> dict:
    now = datetime.now(timezone.utc)
    row = MemberCompany(id=uuid.uuid4(), created_at=now, updated_at=now, contact_count=0, **payload.model_dump())
    with SourceSession() as s:
        s.add(row)
        s.commit()
    return {"id": str(row.id)}


@app.get("/api/v1/contacts")
def list_contacts(limit: int = 20, offset: int = 0) -> dict:
    with SourceSession() as s:
        rows = s.query(Contact).order_by(Contact.created_at).offset(offset).limit(limit).all()
        return {
            "items": [
                {
                    "id": str(r.id),
                    "company_id": str(r.company_id),
                    "full_name": r.full_name,
                    "title": r.title,
                    "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                }
                for r in rows
            ]
        }


@app.get("/api/v1/target/companies/count")
def target_company_count() -> dict:
    with TargetSession() as s:
        return {"count": s.query(MemberCompany).count()}
