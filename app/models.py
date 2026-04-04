from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .encryption import EncryptedText


class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    LAWYER = "LAWYER"
    CLIENT = "CLIENT"


class CaseStage(str, enum.Enum):
    NEW_REQUEST = "NEW_REQUEST"
    DOC_ANALYSIS = "DOC_ANALYSIS"
    DOC_PREPARATION = "DOC_PREPARATION"
    COURT = "COURT"
    COMPLETED = "COMPLETED"


class TaskStatus(str, enum.Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class DocumentSource(str, enum.Enum):
    UPLOAD = "UPLOAD"
    TEMPLATE = "TEMPLATE"


class MessageVisibility(str, enum.Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"


case_lawyers = Base.metadata.tables.get("case_lawyers")
if case_lawyers is None:
    from sqlalchemy import Table, Column

    case_lawyers = Table(
        "case_lawyers",
        Base.metadata,
        Column("case_id", ForeignKey("legal_cases.id"), primary_key=True),
        Column("user_id", ForeignKey("users.id"), primary_key=True),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    first_name: Mapped[str] = mapped_column(String(80), default="")
    last_name: Mapped[str] = mapped_column(String(80), default="")
    middle_name: Mapped[str] = mapped_column(String(80), default="")
    email: Mapped[str] = mapped_column(EncryptedText, default="")
    phone: Mapped[str] = mapped_column(EncryptedText, default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.LAWYER)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    specialization: Mapped[str] = mapped_column(String(120), default="")
    current_load: Mapped[int] = mapped_column(Integer, default=0)
    similar_cases_experience: Mapped[int] = mapped_column(Integer, default=0)
    avg_task_days: Mapped[float] = mapped_column(Float, default=7.0)
    deadline_success_rate: Mapped[float] = mapped_column(Float, default=85.0)

    client_profile: Mapped["Client | None"] = relationship(foreign_keys=[client_id], uselist=False)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    client_type: Mapped[str] = mapped_column(String(24), default="ORGANIZATION")
    email: Mapped[str] = mapped_column(EncryptedText, default="")
    phone: Mapped[str] = mapped_column(EncryptedText, default="")
    address: Mapped[str] = mapped_column(EncryptedText, default="")
    notes: Mapped[str] = mapped_column(EncryptedText, default="")

    cases: Mapped[list[LegalCase]] = relationship(back_populates="client")


class LegalCase(Base):
    __tablename__ = "legal_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    stage: Mapped[CaseStage] = mapped_column(Enum(CaseStage), default=CaseStage.NEW_REQUEST)
    priority: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    opened_at: Mapped[date] = mapped_column(Date)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    responsible_lawyer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    client: Mapped[Client] = relationship(back_populates="cases")
    responsible_lawyer: Mapped[User | None] = relationship(foreign_keys=[responsible_lawyer_id])
    lawyers: Mapped[list[User]] = relationship(secondary=case_lawyers)
    tasks: Mapped[list[CaseTask]] = relationship(back_populates="legal_case")
    documents: Mapped[list["CaseDocument"]] = relationship(back_populates="legal_case")
    messages: Mapped[list["CaseMessage"]] = relationship(back_populates="legal_case")


class CaseTask(Base):
    __tablename__ = "case_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    legal_case_id: Mapped[int] = mapped_column(ForeignKey("legal_cases.id"))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.TODO)
    priority: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    legal_case: Mapped[LegalCase] = relationship(back_populates="tasks")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])


class DocumentTemplate(Base):
    __tablename__ = "document_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(120), default="Общее")
    body: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CaseDocument(Base):
    __tablename__ = "case_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    legal_case_id: Mapped[int] = mapped_column(ForeignKey("legal_cases.id"))
    title: Mapped[str] = mapped_column(String(255))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    legal_case: Mapped[LegalCase] = relationship(back_populates="documents")
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document", order_by="DocumentVersion.version_number")


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("case_documents.id"))
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[DocumentSource] = mapped_column(Enum(DocumentSource), default=DocumentSource.UPLOAD)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document: Mapped[CaseDocument] = relationship(back_populates="versions")
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])


class CaseMessage(Base):
    __tablename__ = "case_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    legal_case_id: Mapped[int] = mapped_column(ForeignKey("legal_cases.id"))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[MessageVisibility] = mapped_column(Enum(MessageVisibility), default=MessageVisibility.PUBLIC)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    legal_case: Mapped[LegalCase] = relationship(back_populates="messages")
    author: Mapped[User | None] = relationship(foreign_keys=[author_id])


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    event_type: Mapped[str] = mapped_column(String(20), default="DEADLINE")
    legal_case_id: Mapped[int | None] = mapped_column(ForeignKey("legal_cases.id"), nullable=True)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="ISSUED")
    legal_case_id: Mapped[int] = mapped_column(ForeignKey("legal_cases.id"))


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(255))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
