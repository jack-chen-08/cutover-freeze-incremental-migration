"""SQLAlchemy 2 models for the member-space schema.

Columns follow the platform-wide convention: id (UUID), org_id, site_id, created_at,
created_by, updated_at, deleted_at on every business table.  Two tables also carry a
redundant counter that the cutover recomputes from live child rows.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Base(DeclarativeBase):
    pass


class AuditMixin:
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    site_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Org(AuditMixin, Base):
    __tablename__ = "orgs"
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")


class Site(AuditMixin, Base):
    __tablename__ = "sites"
    name: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)


class Role(AuditMixin, Base):
    __tablename__ = "roles"
    name: Mapped[str] = mapped_column(Text)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class User(AuditMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    legacy_no: Mapped[int] = mapped_column(
        BigInteger, server_default=text("nextval('users_legacy_no_seq')")
    )


class UserRole(AuditMixin, Base):
    __tablename__ = "user_roles"
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id"))
    role_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("roles.id"))


class MemberCompany(AuditMixin, Base):
    __tablename__ = "member_companies"
    name: Mapped[str] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    tax_no: Mapped[str | None] = mapped_column(Text)
    employee_count: Mapped[int] = mapped_column(Integer, default=0)
    contact_count: Mapped[int] = mapped_column(Integer, default=0)
    legacy_no: Mapped[int] = mapped_column(
        BigInteger, server_default=text("nextval('member_companies_legacy_no_seq')")
    )


class Contact(AuditMixin, Base):
    __tablename__ = "contacts"
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("member_companies.id"))
    full_name: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    legacy_no: Mapped[int] = mapped_column(
        BigInteger, server_default=text("nextval('contacts_legacy_no_seq')")
    )


class Tag(AuditMixin, Base):
    __tablename__ = "tags"
    label: Mapped[str] = mapped_column(Text)


class TagLink(AuditMixin, Base):
    __tablename__ = "tag_links"
    tag_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tags.id"))
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("member_companies.id"))


class Activity(AuditMixin, Base):
    __tablename__ = "activities"
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    registration_count: Mapped[int] = mapped_column(Integer, default=0)


class Registration(AuditMixin, Base):
    __tablename__ = "registrations"
    activity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("activities.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("contacts.id"))
    status: Mapped[str] = mapped_column(String(32), default="confirmed")


class Opportunity(AuditMixin, Base):
    __tablename__ = "opportunities"
    company_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("member_companies.id"))
    title: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(BigInteger, default=0)  # amounts are integer cents
    stage: Mapped[str] = mapped_column(String(32), default="new")


class KbArticle(AuditMixin, Base):
    __tablename__ = "kb_articles"
    slug: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)


_engine_kwargs = {"pool_pre_ping": True, "future": True}
SourceEngine = create_engine(settings.source_dsn, **_engine_kwargs)
TargetEngine = create_engine(settings.target_dsn, **_engine_kwargs)
SourceSession = sessionmaker(bind=SourceEngine, expire_on_commit=False)
TargetSession = sessionmaker(bind=TargetEngine, expire_on_commit=False)

# Backwards-compatible alias used by older scripts.
Company = MemberCompany
