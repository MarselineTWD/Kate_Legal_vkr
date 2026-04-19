from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import html
import io
import mimetypes
from pathlib import Path
import secrets
import re
import zipfile
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .database import SessionLocal, get_db
from .models import (
    AuditLog,
    CalendarEvent,
    CaseComment,
    CaseStage,
    CaseTask,
    CaseDocument,
    Client,
    ClientChatMessage,
    Invoice,
    LegalCase,
    Notification,
    Role,
    TaskStatus,
    User,
)
from .security import hash_password, verify_password
from .seed import create_schema, seed_data
from .topsis import topsis_rank


app = FastAPI(title=settings.app_name, debug=settings.debug)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 8)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/site2static", StaticFiles(directory="app/site2"), name="site2static")
templates = Jinja2Templates(directory="app/templates")
SITE2_DIR = Path("app/site2")
UPLOADS_DIR = Path("app/uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


@app.middleware("http")
async def disable_cache_in_debug(request: Request, call_next):
    response = await call_next(request)
    if settings.debug and request.method in {"GET", "HEAD"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


STAGE_LABELS = {
    CaseStage.NEW_REQUEST: "Заявка принята",
    CaseStage.DOC_ANALYSIS: "Анализ документов",
    CaseStage.DOC_PREPARATION: "Подготовка позиции",
    CaseStage.COURT: "Процессуальные действия",
    CaseStage.COMPLETED: "Завершение",
}

STATUS_LABELS = {
    TaskStatus.TODO: "К выполнению",
    TaskStatus.IN_PROGRESS: "В работе",
    TaskStatus.DONE: "Сделано",
}

EVENT_TYPE_LABELS = {
    "COURT": "Судебное заседание",
    "MEETING": "Встреча",
    "CLIENT": "Коммуникация с клиентом",
    "DOCUMENT": "Документы",
    "DEADLINE": "Крайний срок",
    "CUSTOM": "Событие",
}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё\-\s]{2,60}$")
PHONE_RE = re.compile(r"^\+?[0-9\s()\-]{10,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


@app.on_event("startup")
def startup_event():
    create_schema()
    seed_data()
    _migrate_legacy_documents_to_db()


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_auth(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(request: Request, db: Session) -> User:
    user = require_auth(request, db)
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


def require_staff(request: Request, db: Session) -> User:
    user = require_auth(request, db)
    if user.role == Role.CLIENT:
        raise HTTPException(status_code=403, detail="Раздел доступен только сотрудникам")
    return user


def log_action(db: Session, user: User | None, action: str, details: str = "") -> None:
    db.add(AuditLog(user_id=user.id if user else None, action=action, details=details))


def find_user_by_email(db: Session, email: str) -> User | None:
    normalized = email.strip().lower()
    if not normalized:
        return None
    users = db.scalars(select(User)).all()
    for user in users:
        if (user.email or "").strip().lower() == normalized:
            return user
    return None


def find_client_by_email(db: Session, email: str) -> Client | None:
    normalized = email.strip().lower()
    if not normalized:
        return None
    clients = db.scalars(select(Client)).all()
    for client in clients:
        if (client.email or "").strip().lower() == normalized:
            return client
    return None


def find_client_for_user(db: Session, user: User) -> Client | None:
    client = db.scalar(select(Client).where(Client.user_id == user.id))
    if client:
        return client
    return find_client_by_email(db, user.email or "")


def require_client_account(request: Request, db: Session) -> tuple[User, Client]:
    user = require_auth(request, db)
    if user.role != Role.CLIENT:
        raise HTTPException(status_code=403, detail="Доступ только для клиентов")
    client = find_client_for_user(db, user)
    if not client:
        raise HTTPException(status_code=404, detail="Карточка клиента не найдена")
    return user, client


def generate_unique_username(db: Session, email: str) -> str:
    local_part = email.split("@", 1)[0].lower()
    base = re.sub(r"[^a-z0-9_]", "_", local_part)
    base = re.sub(r"_+", "_", base).strip("_")
    if len(base) < 3:
        base = "user"
    base = base[:24]
    candidate = base
    suffix = 1
    while db.scalar(select(User).where(User.username == candidate)):
        suffix += 1
        candidate = f"{base}_{suffix}"
    return candidate


def parse_iso_date(value: str) -> date | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date()
    except ValueError:
        return None


def next_case_number(db: Session) -> str:
    year = datetime.now().year
    count = db.scalar(select(func.count(LegalCase.id))) or 0
    return f"CASE-{year}-{count + 1:03d}"


def infer_case_category(title: str, description: str, fallback: str) -> str:
    text = f"{title} {description}".lower()
    rules = [
        ("труд", "Трудовое право"),
        ("договор", "Договорная работа"),
        ("корпоратив", "Корпоративное право"),
        ("суд", "Процессуальное сопровождение"),
        ("задолж", "Взыскание задолженности"),
        ("претенз", "Претензионная работа"),
    ]
    for keyword, label in rules:
        if keyword in text:
            return label
    return fallback or "Общая категория"


def build_case_workspace(db: Session, legal_case: LegalCase) -> dict:
    tasks = db.scalars(
        select(CaseTask).where(CaseTask.legal_case_id == legal_case.id).order_by(CaseTask.due_date, CaseTask.id)
    ).all()
    comments = db.scalars(
        select(CaseComment).where(CaseComment.legal_case_id == legal_case.id).order_by(CaseComment.created_at, CaseComment.id)
    ).all()
    users_map = {item.id: item for item in db.scalars(select(User)).all()}
    today = date.today()

    overdue_tasks = [task for task in tasks if task.due_date < today and task.status != TaskStatus.DONE]
    active_tasks = [task for task in tasks if task.status != TaskStatus.DONE]
    deadline_days = None
    if legal_case.deadline:
        deadline_days = (legal_case.deadline - today).days

    risk_items = []
    risk_score = 0

    if legal_case.stage != CaseStage.COMPLETED:
        if legal_case.responsible_lawyer_id is None:
            risk_items.append(("high", "Не назначен ответственный юрист"))
            risk_score += 3
        if legal_case.deadline is None:
            risk_items.append(("medium", "У дела нет установленного дедлайна"))
            risk_score += 2
        elif deadline_days is not None and deadline_days < 0:
            risk_items.append(("high", "Дедлайн уже просрочен"))
            risk_score += 4
        elif deadline_days is not None and deadline_days <= 3:
            risk_items.append(("high", "До дедлайна осталось 1-3 дня"))
            risk_score += 3
        elif deadline_days is not None and deadline_days <= 7:
            risk_items.append(("medium", "До дедлайна осталось меньше недели"))
            risk_score += 2

    if not legal_case.description or len(legal_case.description.strip()) < 80:
        risk_items.append(("medium", "Описание дела слишком краткое для уверенной передачи в работу"))
        risk_score += 1

    if not tasks:
        risk_items.append(("medium", "По делу еще не создано ни одной задачи"))
        risk_score += 2
    elif overdue_tasks:
        severity = "high" if len(overdue_tasks) >= 2 else "medium"
        risk_items.append((severity, f"Есть просроченные задачи: {len(overdue_tasks)}"))
        risk_score += 3 if severity == "high" else 2

    if legal_case.responsible_lawyer and legal_case.responsible_lawyer.current_load >= 6:
        risk_items.append(("medium", "Ответственный юрист уже сильно загружен"))
        risk_score += 1

    if risk_score >= 6:
        risk_level = "high"
        risk_label = "Высокий риск"
    elif risk_score >= 3:
        risk_level = "medium"
        risk_label = "Средний риск"
    else:
        risk_level = "low"
        risk_label = "Низкий риск"

    if not risk_items:
        risk_items.append(("low", "Критичных рисков по делу сейчас не обнаружено"))

    stage_steps = {
        CaseStage.NEW_REQUEST: [
            "Проверить комплект входящих документов и уточнить запрос клиента",
            "Назначить ответственного юриста и создать первичные задачи",
        ],
        CaseStage.DOC_ANALYSIS: [
            "Проанализировать доказательства и выделить пробелы в материалах",
            "Подготовить короткое резюме позиции для команды",
        ],
        CaseStage.DOC_PREPARATION: [
            "Сформировать проект процессуального документа и согласовать пакет приложений",
            "Проверить сроки направления документов второй стороне",
        ],
        CaseStage.COURT: [
            "Актуализировать позицию перед заседанием и сверить календарь процессуальных сроков",
            "Подготовить тезисы выступления и комплект судебной папки",
        ],
        CaseStage.COMPLETED: [
            "Закрыть оставшиеся задачи и собрать итоговые документы в карточке дела",
            "Подготовить итоговый комментарий для клиента и архива",
        ],
    }
    next_steps = list(stage_steps.get(legal_case.stage, []))
    if deadline_days is not None and 0 <= deadline_days <= 7:
        next_steps.insert(0, "Поставить дело в приоритет и перепроверить ближайшие дедлайны")
    if not active_tasks and legal_case.stage != CaseStage.COMPLETED:
        next_steps.append("Добавить рабочие задачи, чтобы команда видела следующий шаг")

    category_text = infer_case_category(legal_case.title, legal_case.description or "", legal_case.category)
    doc_map = {
        "Трудовое право": ["Трудовой договор", "Приказ/уведомление", "Расчет выплат"],
        "Договорная работа": ["Договор", "Переписка сторон", "Акт/накладная"],
        "Корпоративное право": ["Устав", "Протокол собрания", "Корпоративные решения"],
        "Процессуальное сопровождение": ["Иск/отзыв", "Доказательства", "Доверенность"],
        "Судебное производство": ["Иск/отзыв", "Доказательства", "Доверенность"],
        "Взыскание задолженности": ["Претензия", "Расчет задолженности", "Подтверждающие платежи"],
        "Претензионная работа": ["Претензия", "Подтверждение отправки", "Расчет требований"],
    }
    recommended_docs = doc_map.get(category_text, ["Описание ситуации", "Подтверждающие документы", "Контактные данные клиента"])

    ai_summary = (
        f"Дело находится на стадии «{STAGE_LABELS[legal_case.stage]}». "
        f"Основной фокус сейчас: {next_steps[0].lower() if next_steps else 'поддерживать движение по задачам'}."
    )

    comments_payload = []
    for comment in comments:
        author = users_map.get(comment.user_id)
        comments_payload.append(
            {
                "id": comment.id,
                "author": (author.full_name or author.username) if author else "Система",
                "message": comment.message,
                "is_internal": comment.is_internal,
                "created_at": comment.created_at.strftime("%d.%m.%Y %H:%M"),
            }
        )

    return {
        "risk": {
            "level": risk_level,
            "label": risk_label,
            "items": [{"level": level, "text": text} for level, text in risk_items],
        },
        "ai": {
            "summary": ai_summary,
            "predicted_category": category_text,
            "next_steps": next_steps[:4],
            "recommended_documents": recommended_docs[:4],
            "signals": [
                f"Активных задач: {len(active_tasks)}",
                f"Просроченных задач: {len(overdue_tasks)}",
                f"Комментариев в обсуждении: {len(comments_payload)}",
            ],
        },
        "comments": comments_payload,
    }


def build_client_chat_payload(db: Session, client: Client) -> dict:
    client_cases = db.scalars(
        select(LegalCase).where(LegalCase.client_id == client.id).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    ).all()
    case_ids = [item.id for item in client_cases]

    client_tasks: list[CaseTask] = []
    client_invoices: list[Invoice] = []
    if case_ids:
        client_tasks = db.scalars(
            select(CaseTask).where(CaseTask.legal_case_id.in_(case_ids)).order_by(CaseTask.due_date, CaseTask.id)
        ).all()
        client_invoices = db.scalars(
            select(Invoice).where(Invoice.legal_case_id.in_(case_ids)).order_by(Invoice.due_date.desc(), Invoice.id.desc())
        ).all()

    today = date.today()
    active_cases = [item for item in client_cases if item.stage != CaseStage.COMPLETED]
    active_tasks = [item for item in client_tasks if item.status != TaskStatus.DONE]
    overdue_tasks = [item for item in client_tasks if item.status != TaskStatus.DONE and item.due_date < today]
    unpaid_invoices = [item for item in client_invoices if (item.status or "").upper() != "PAID"]

    messages = db.scalars(
        select(ClientChatMessage)
        .where(ClientChatMessage.client_id == client.id)
        .order_by(ClientChatMessage.created_at, ClientChatMessage.id)
    ).all()
    users_map = {item.id: item for item in db.scalars(select(User)).all()}

    payload_messages = []
    for item in messages:
        user = users_map.get(item.user_id) if item.user_id else None
        author = client.name if item.is_from_client else ((user.full_name or user.username) if user else "Сотрудник")
        payload_messages.append(
            {
                "id": item.id,
                "message": item.message,
                "author": author,
                "is_from_client": item.is_from_client,
                "created_at": item.created_at.strftime("%d.%m.%Y %H:%M"),
            }
        )

    return {
        "client": {
            "id": client.id,
            "name": client.name,
            "email": client.email,
            "phone": client.phone,
            "address": client.address,
            "notes": client.notes,
        },
        "overview": {
            "cases_total": len(client_cases),
            "cases_active": len(active_cases),
            "tasks_active": len(active_tasks),
            "tasks_overdue": len(overdue_tasks),
            "invoices_total": len(client_invoices),
            "invoices_unpaid": len(unpaid_invoices),
            "recent_cases": [
                {
                    "case_number": item.case_number,
                    "title": item.title,
                    "stage": STAGE_LABELS[item.stage],
                    "deadline": item.deadline.strftime("%d.%m.%Y") if item.deadline else "Без дедлайна",
                }
                for item in client_cases[:5]
            ],
            "upcoming_tasks": [
                {
                    "title": item.title,
                    "case_number": item.legal_case.case_number if item.legal_case else "-",
                    "due_date": item.due_date.strftime("%d.%m.%Y"),
                    "status": STATUS_LABELS[item.status],
                }
                for item in active_tasks[:6]
            ],
            "recent_invoices": [
                {
                    "number": item.number,
                    "amount": float(item.amount),
                    "due_date": item.due_date.strftime("%d.%m.%Y"),
                    "status": item.status,
                }
                for item in client_invoices[:6]
            ],
        },
        "messages": payload_messages,
    }


def case_stage_progress(legal_case: LegalCase) -> list[dict]:
    steps = [
        (CaseStage.NEW_REQUEST, "Заявка принята"),
        (CaseStage.DOC_ANALYSIS, "Анализ документов"),
        (CaseStage.DOC_PREPARATION, "Подготовка позиции"),
        (CaseStage.COURT, "Процессуальные действия"),
        (CaseStage.COMPLETED, "Завершение"),
    ]
    current_index = next((index for index, (stage, _) in enumerate(steps) if stage == legal_case.stage), 0)
    progress = []
    for index, (stage, label) in enumerate(steps):
        progress.append(
            {
                "label": label,
                "state": "current" if index == current_index else "done" if index < current_index else "upcoming",
            }
        )
    return progress


def detect_required_documents(legal_case: LegalCase, documents: list[CaseDocument]) -> list[str]:
    category_text = infer_case_category(legal_case.title, legal_case.description or "", legal_case.category)
    doc_map = {
        "Трудовое право": ["Трудовой договор", "Приказ/уведомление", "Расчет выплат"],
        "Договорная работа": ["Договор", "Переписка сторон", "Акт/накладная"],
        "Корпоративное право": ["Устав", "Протокол собрания", "Корпоративные решения"],
        "Процессуальное сопровождение": ["Иск/отзыв", "Доказательства", "Доверенность"],
        "Судебное производство": ["Иск/отзыв", "Доказательства", "Доверенность"],
        "Взыскание задолженности": ["Претензия", "Расчет задолженности", "Подтверждающие платежи"],
        "Претензионная работа": ["Претензия", "Подтверждение отправки", "Расчет требований"],
    }
    recommendations = doc_map.get(
        category_text,
        ["Описание ситуации", "Подтверждающие документы", "Контактные данные клиента"],
    )
    uploaded_names = " ".join((item.original_filename or "").lower() for item in documents)
    missing = [item for item in recommendations if item.lower() not in uploaded_names]
    return missing[:4]


def build_case_message_payload(
    db: Session,
    client: Client,
    legal_case: LegalCase,
    assigned_lawyer: User | None = None,
) -> list[dict]:
    lawyer = assigned_lawyer
    if lawyer is None and legal_case.responsible_lawyer_id:
        lawyer = db.get(User, legal_case.responsible_lawyer_id)
    raw_messages = db.scalars(
        select(ClientChatMessage)
        .where(ClientChatMessage.client_id == client.id)
        .where(ClientChatMessage.legal_case_id == legal_case.id)
        .order_by(ClientChatMessage.created_at.asc(), ClientChatMessage.id.asc())
    ).all()
    payload = []
    for item in raw_messages:
        author = client.name if item.is_from_client else "Юрист"
        if not item.is_from_client and lawyer and item.user_id == lawyer.id:
            author = lawyer.full_name or lawyer.username
        created_local = _to_local_naive(item.created_at) or item.created_at
        payload.append(
            {
                "id": item.id,
                "author": author,
                "message": item.message,
                "created_at": created_local,
                "is_from_client": item.is_from_client,
            }
        )
    return payload


def build_case_chat_list(db: Session, client: Client, cases: list[LegalCase]) -> list[dict]:
    users_map = {item.id: item for item in db.scalars(select(User)).all()}
    payload = []
    for legal_case in cases:
        messages = db.scalars(
            select(ClientChatMessage)
            .where(ClientChatMessage.client_id == client.id)
            .where(ClientChatMessage.legal_case_id == legal_case.id)
            .order_by(ClientChatMessage.created_at.asc(), ClientChatMessage.id.asc())
        ).all()
        last_message = messages[-1] if messages else None
        last_client_message_at = None
        for item in reversed(messages):
            if item.is_from_client:
                last_client_message_at = item.created_at
                break
        unread_messages = [
            item
            for item in messages
            if not item.is_from_client and (last_client_message_at is None or item.created_at > last_client_message_at)
        ]
        lawyer = users_map.get(legal_case.responsible_lawyer_id) if legal_case.responsible_lawyer_id else None
        payload.append(
            {
                "case": legal_case,
                "assigned_lawyer": lawyer,
                "last_message": last_message.message if last_message else "",
                "last_message_at": (_to_local_naive(last_message.created_at) if last_message else None),
                "last_message_from_client": bool(last_message.is_from_client) if last_message else False,
                "unread_count": len(unread_messages),
                "has_unread": bool(unread_messages),
            }
        )
    return payload


def build_client_case_detail(db: Session, client: Client, legal_case: LegalCase) -> dict:
    today = date.today()
    assigned_lawyer = db.get(User, legal_case.responsible_lawyer_id) if legal_case.responsible_lawyer_id else None
    tasks = db.scalars(
        select(CaseTask).where(CaseTask.legal_case_id == legal_case.id).order_by(CaseTask.due_date.asc(), CaseTask.id.asc())
    ).all()
    documents = db.scalars(
        select(CaseDocument)
        .where(CaseDocument.legal_case_id == legal_case.id)
        .order_by(CaseDocument.created_at.desc(), CaseDocument.id.desc())
    ).all()
    events = db.scalars(
        select(CalendarEvent)
        .where(CalendarEvent.legal_case_id == legal_case.id)
        .order_by(CalendarEvent.starts_at.asc(), CalendarEvent.id.asc())
    ).all()
    invoices = db.scalars(
        select(Invoice).where(Invoice.legal_case_id == legal_case.id).order_by(Invoice.due_date.desc(), Invoice.id.desc())
    ).all()
    messages = build_case_message_payload(db, client, legal_case, assigned_lawyer)

    latest_staff_message_at = None
    latest_client_message_at = None
    for item in reversed(messages):
        if item["is_from_client"] and latest_client_message_at is None:
            latest_client_message_at = item["created_at"]
        if not item["is_from_client"] and latest_staff_message_at is None:
            latest_staff_message_at = item["created_at"]
        if latest_staff_message_at and latest_client_message_at:
            break
    unread_messages = [
        item
        for item in messages
        if not item["is_from_client"] and (latest_client_message_at is None or item["created_at"] > latest_client_message_at)
    ]

    open_tasks = [item for item in tasks if item.status != TaskStatus.DONE]
    overdue_tasks = [item for item in open_tasks if item.due_date < today]
    client_tasks = [
        item
        for item in open_tasks
        if any(
            keyword in f"{item.title} {item.description}".lower()
            for keyword in ["клиент", "предостав", "прилож", "загруз", "уточн", "подтверд"]
        )
    ]
    next_task = min(open_tasks, key=lambda item: item.due_date) if open_tasks else None
    next_event = min(events, key=lambda item: item.starts_at) if events else None
    next_milestone_date = None
    if legal_case.deadline:
        next_milestone_date = legal_case.deadline
    elif next_task:
        next_milestone_date = next_task.due_date
    elif next_event:
        next_milestone_date = next_event.starts_at.date()

    # Client action list is now controlled only by lawyer/admin tasks.
    required_docs: list[str] = []
    requires_client_action = bool(client_tasks)
    stage_label = STAGE_LABELS[legal_case.stage]
    risk_level = "normal"
    risk_text = "Сроки под контролем"
    if overdue_tasks:
        risk_level = "high"
        risk_text = "Есть риск пропуска срока"
    elif legal_case.deadline and (legal_case.deadline - today).days <= 3:
        risk_level = "medium"
        risk_text = "Ближайший срок наступает скоро"

    status_summary_map = {
        CaseStage.NEW_REQUEST: (
            "Ваше обращение зарегистрировано и ожидает первичной проверки.",
            "Администратор проверяет заявку и комплект документов.",
            "После проверки обращение будет передано ответственному юристу.",
        ),
        CaseStage.DOC_ANALYSIS: (
            "Юрист анализирует материалы и формирует правовую позицию.",
            "Сейчас команда изучает документы и выделяет ключевые риски.",
            "После анализа мы обозначим план действий и контрольные даты.",
        ),
        CaseStage.DOC_PREPARATION: (
            "По делу готовятся документы и правовая позиция.",
            "Юрист оформляет пакет материалов и согласовывает следующий шаг.",
            "После подготовки документов дело перейдет к следующему процессуальному этапу.",
        ),
        CaseStage.COURT: (
            "Дело находится на этапе процессуальных действий.",
            "Юрист сопровождает заседания и контрольные процессуальные даты.",
            "Следите за событиями и сроками: здесь появятся ближайшие заседания и запросы.",
        ),
        CaseStage.COMPLETED: (
            "Работа по делу завершена.",
            "Итоговые материалы и история взаимодействия доступны для просмотра.",
            "При необходимости вы можете открыть новое обращение по связанному вопросу.",
        ),
    }
    current_state, lawyer_next_step, nearest_stage = status_summary_map.get(
        legal_case.stage,
        ("Состояние дела обновляется.", "Юрист продолжает работу по делу.", "Следующий этап будет уточнен командой."),
    )

    if not legal_case.intake_approved:
        current_state = "Обращение принято системой и ожидает первичной проверки."
        lawyer_next_step = "Администратор проверит заявку и назначит ответственного юриста."
        nearest_stage = "После проверки вы увидите переход к этапу анализа документов."

    if client_tasks:
        client_expectation = client_tasks[0].title
    else:
        client_expectation = "Ничего не требуется."

    document_items = []
    users_map = {item.id: item for item in db.scalars(select(User)).all()}
    for item in documents:
        uploader = users_map.get(item.uploaded_by_user_id) if item.uploaded_by_user_id else None
        from_client = bool(item.uploaded_by_user_id == client.user_id)
        uploader_name = client.name if from_client else ((uploader.full_name or uploader.username) if uploader else "Юрист")
        file_ext = item.original_filename.rsplit(".", 1)[-1].upper() if "." in item.original_filename else "FILE"
        created_local = _to_local_naive(item.created_at) or item.created_at
        document_items.append(
            {
                "id": item.id,
                "name": item.original_filename,
                "ext": file_ext[:4],
                "created_at": created_local,
                "description": item.description,
                "version": "Основная версия" if not item.description else item.description,
                "uploader_name": uploader_name,
                "source": "client" if from_client else "lawyer",
            }
        )

    required_document_items: list[dict] = []

    case_created_at = _to_local_naive(legal_case.created_at) or datetime.combine(legal_case.opened_at, datetime.min.time())
    timeline = [
        {
            "date": case_created_at,
            "type": "Создание",
            "title": "Обращение создано",
            "description": "Дело зарегистрировано в клиентском кабинете.",
            "actor": client.name,
            "status": "Система",
        }
    ]
    for item in documents[:10]:
        from_client = bool(item.uploaded_by_user_id == client.user_id)
        item_date = _to_local_naive(item.created_at) or item.created_at
        timeline.append(
            {
                "date": item_date,
                "type": "Документ",
                "title": "Документ загружен",
                "description": item.original_filename,
                "actor": client.name if from_client else "Юридическая команда",
                "status": "Документ",
            }
        )
    for item in events[:10]:
        starts_local = _to_local_naive(item.starts_at) or item.starts_at
        timeline.append(
            {
                "date": starts_local,
                "type": "Событие",
                "title": item.title,
                "description": EVENT_TYPE_LABELS.get((item.event_type or "CUSTOM").upper(), "Событие"),
                "actor": "Юридическая команда",
                "status": "Событие",
            }
        )
    timeline.sort(key=lambda item: item["date"], reverse=True)

    changes = []
    if legal_case.intake_approved:
        changes.append(
            {
                "date": case_created_at,
                "title": "Обращение принято в работу",
                "description": "Карточка дела подтверждена и доступна в клиентском кабинете.",
                "actor": "Система",
            }
        )
    if legal_case.deadline:
        changes.append(
            {
                "date": datetime.combine(legal_case.deadline, datetime.min.time()),
                "title": "Добавлен контрольный срок",
                "description": legal_case.deadline.strftime("%d.%m.%Y"),
                "actor": assigned_lawyer.full_name if assigned_lawyer and assigned_lawyer.full_name else "Юрист",
            }
        )
    for item in document_items[:6]:
        changes.append(
            {
                "date": item["created_at"],
                "title": "Добавлен документ",
                "description": item["name"],
                "actor": item["uploader_name"],
            }
        )
    for item in messages[-6:]:
        changes.append(
            {
                "date": item["created_at"],
                "title": "Обновлена коммуникация",
                "description": item["message"][:100],
                "actor": item["author"],
            }
        )
    changes.sort(key=lambda item: item["date"], reverse=True)

    participants = [
        {
            "name": client.name,
            "role": "Клиент",
            "meta": client.email or client.phone or "Клиент кабинета",
            "initials": (client.name or "К").strip()[:1],
        }
    ]
    if assigned_lawyer:
        participants.append(
            {
                "name": assigned_lawyer.full_name or assigned_lawyer.username,
                "role": "Ответственный юрист",
                "meta": assigned_lawyer.email or assigned_lawyer.specialization or "Юридическая команда",
                "initials": (assigned_lawyer.full_name or assigned_lawyer.username or "Ю").strip()[:1],
            }
        )

    event_items = []
    for task in open_tasks[:8]:
        event_items.append(
            {
                "kind": "Задача",
                "title": task.title,
                "description": (task.description or "").strip(),
                "date": task.due_date,
                "status": STATUS_LABELS.get(task.status, task.status.value),
                "urgency": "danger" if task.due_date < today else "warning" if (task.due_date - today).days <= 3 else "normal",
            }
        )
    for event in events[:8]:
        starts_local = _to_local_naive(event.starts_at) or event.starts_at
        starts_local_date = starts_local.date()
        event_items.append(
            {
                "kind": EVENT_TYPE_LABELS.get((event.event_type or "CUSTOM").upper(), "Событие"),
                "title": event.title,
                "description": "",
                "date": starts_local_date,
                "status": "Запланировано",
                "urgency": "warning" if 0 <= (starts_local_date - today).days <= 3 else "normal",
            }
        )
    event_items.sort(key=lambda item: (item["date"], item["title"]))

    latest_documents = document_items[:3]
    latest_messages = list(reversed(messages[-3:]))
    finances = [
        {
            "number": item.number,
            "amount": float(item.amount),
            "due_date": item.due_date,
            "status": item.status,
        }
        for item in invoices
    ]

    return {
        "case": legal_case,
        "assigned_lawyer": assigned_lawyer,
        "stage_label": stage_label,
        "messages": messages,
        "chat_unread_count": len(unread_messages),
        "documents": document_items,
        "required_documents": required_document_items,
        "timeline": timeline[:16],
        "changes": changes[:8],
        "participants": participants,
        "event_items": event_items[:10],
        "latest_documents": latest_documents,
        "latest_messages": latest_messages,
        "finances": finances,
        "tasks_open": open_tasks,
        "client_tasks": client_tasks,
        "overview": {
            "current_state": current_state,
            "lawyer_next_step": lawyer_next_step,
            "client_expectation": client_expectation,
            "nearest_stage": nearest_stage,
            "risk_level": risk_level,
            "risk_text": risk_text,
            "requires_client_action": requires_client_action,
            "next_milestone_date": next_milestone_date,
        },
        "stats": {
            "documents_count": len(documents),
            "messages_count": len(messages),
            "changes_count": len(timeline),
            "client_tasks_count": len(client_tasks),
            "next_event": event_items[0] if event_items else None,
        },
        "stage_progress": case_stage_progress(legal_case),
        "today": today,
    }


@app.get("/", response_class=HTMLResponse)
def landing():
    return FileResponse(SITE2_DIR / "index.html")


@app.get("/o-nas", response_class=HTMLResponse)
def about_page():
    return RedirectResponse("/#about", status_code=307)


@app.get("/kontakty", response_class=HTMLResponse)
def contacts_page():
    return RedirectResponse("/#contacts", status_code=307)


@app.get("/Страница-1.html", response_class=HTMLResponse)
def old_home_alias():
    return FileResponse(SITE2_DIR / "index.html")


@app.get("/О-нас.html", response_class=HTMLResponse)
def old_about_alias():
    return RedirectResponse("/o-nas", status_code=307)


@app.get("/Контакты.html", response_class=HTMLResponse)
def old_contacts_alias():
    return RedirectResponse("/kontakty", status_code=307)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    registered = request.query_params.get("registered") == "1"
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "user": None,
            "show_footer": False,
            "show_chat": False,
            "active_tab": "login",
            "login_error": None,
            "register_error": None,
            "success": "Регистрация прошла успешно. Теперь войдите в систему." if registered else None,
            "login_email": "",
            "register_last_name": "",
            "register_first_name": "",
            "register_middle_name": "",
            "register_phone": "",
            "register_email": "",
        },
    )


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email = email.strip().lower()
    user = find_user_by_email(db, email)
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "user": None,
                "show_footer": False,
                "show_chat": False,
                "active_tab": "login",
                "login_error": "Неверный email или пароль",
                "register_error": None,
                "success": None,
                "login_email": email,
                "register_last_name": "",
                "register_first_name": "",
                "register_middle_name": "",
                "register_phone": "",
                "register_email": "",
            },
            status_code=400,
        )
    request.session["user_id"] = user.id
    log_action(db, user, "Вход в систему")
    db.commit()
    return RedirectResponse("/app", status_code=303)


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    last_name: str = Form(...),
    first_name: str = Form(...),
    middle_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    intake_flow: str = Form(""),
    db: Session = Depends(get_db),
):
    last_name = last_name.strip()
    first_name = first_name.strip()
    middle_name = middle_name.strip()
    phone = phone.strip()
    email = email.strip().lower()
    full_name = " ".join([last_name, first_name, middle_name]).strip()

    if not NAME_RE.fullmatch(last_name):
        error = "Фамилия заполнена некорректно."
    elif not NAME_RE.fullmatch(first_name):
        error = "Имя заполнено некорректно."
    elif middle_name and not NAME_RE.fullmatch(middle_name):
        error = "Отчество заполнено некорректно."
    elif not PHONE_RE.fullmatch(phone) or not (10 <= len(re.sub(r"\D", "", phone)) <= 15):
        error = "Телефон должен содержать от 10 до 15 цифр."
    elif not EMAIL_RE.fullmatch(email):
        error = "Введите корректный email."
    elif find_user_by_email(db, email):
        error = "Пользователь с таким email уже существует."
    elif find_client_by_email(db, email):
        error = "Клиент с таким email уже зарегистрирован."
    elif len(password) < 8:
        error = "Пароль должен содержать минимум 8 символов."
    elif not re.search(r"[A-ZА-ЯЁ]", password) or not re.search(r"[a-zа-яё]", password) or not re.search(r"\d", password):
        error = "Пароль должен содержать буквы верхнего и нижнего регистра и хотя бы одну цифру."
    elif password != password_confirm:
        error = "Пароли не совпадают."
    else:
        username = generate_unique_username(db, email)
        new_user = User(
            username=username,
            full_name=full_name,
            first_name=first_name,
            last_name=last_name,
            middle_name=middle_name,
            phone=phone,
            email=email,
            password_hash=hash_password(password),
            role=Role.CLIENT,
        )
        db.add(new_user)
        db.flush()
        new_client = Client(
            user_id=new_user.id,
            name=full_name,
            client_type="PERSON",
            email=email,
            phone=phone,
            notes="Регистрация через форму входа",
        )
        db.add(new_client)
        log_action(db, new_user, "Регистрация клиента", f"email={email}")
        db.commit()
        request.session["user_id"] = new_user.id
        if intake_flow.strip() == "1":
            return RedirectResponse("/client/intake?prefill=1", status_code=303)
        return RedirectResponse("/app", status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "user": None,
            "show_footer": False,
            "show_chat": False,
            "active_tab": "register",
            "login_error": None,
            "register_error": error,
            "success": None,
            "login_email": "",
            "register_last_name": last_name,
            "register_first_name": first_name,
            "register_middle_name": middle_name,
            "register_phone": phone,
            "register_email": email,
        },
        status_code=400,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    today = datetime.now().date()
    is_client = user.role == Role.CLIENT

    tasks: list[CaseTask] = []
    overdue: list[CaseTask] = []
    cases_count = 0
    active_cases = 0
    clients_count = 0
    client_record = None

    if is_client:
        client_record = find_client_for_user(db, user)
        client_cases = []
        recent_messages = []
        attention_items: list[str] = []
        if client_record:
            client_cases = db.scalars(
                select(LegalCase).where(LegalCase.client_id == client_record.id).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
            ).all()
        case_ids = [item.id for item in client_cases]
        if case_ids:
            tasks = db.scalars(select(CaseTask).where(CaseTask.legal_case_id.in_(case_ids))).all()
        overdue = [t for t in tasks if t.due_date < today and t.status != TaskStatus.DONE]
        cases_count = len(client_cases)
        active_cases = len([c for c in client_cases if c.stage != CaseStage.COMPLETED])
        clients_count = 1 if client_record else 0
        case_ids = [item.id for item in client_cases]
        recent_notifications = db.scalars(
            select(Notification)
            .where(Notification.recipient_id == user.id)
            .order_by(Notification.is_read.asc(), Notification.created_at.desc(), Notification.id.desc())
            .limit(8)
        ).all()
        unread_notifications = (
            db.scalar(
                select(func.count(Notification.id))
                .where(Notification.recipient_id == user.id)
                .where(Notification.is_read.is_(False))
            )
            or 0
        )

        upcoming_events = []
        for task in sorted([t for t in tasks if t.status != TaskStatus.DONE], key=lambda t: t.due_date)[:8]:
            upcoming_events.append(
                {
                    "date": task.due_date,
                    "title": task.title,
                    "kind": "TASK",
                    "status": STATUS_LABELS.get(task.status, task.status.value),
                }
            )

        if case_ids:
            calendar_events = db.scalars(
                select(CalendarEvent)
                .where(CalendarEvent.legal_case_id.in_(case_ids))
                .order_by(CalendarEvent.starts_at.asc(), CalendarEvent.id.asc())
                .limit(8)
            ).all()
            for event in calendar_events:
                upcoming_events.append(
                    {
                        "date": event.starts_at.date(),
                        "title": event.title,
                        "kind": "EVENT",
                        "status": EVENT_TYPE_LABELS.get((event.event_type or "CUSTOM").upper(), "Событие"),
                    }
                )
        upcoming_events.sort(key=lambda item: (item["date"], item["title"]))

        if case_ids:
            case_map = {item.id: item for item in client_cases}
            lawyers_map = {item.id: item for item in db.scalars(select(User)).all()}
            raw_messages = db.scalars(
                select(ClientChatMessage)
                .where(ClientChatMessage.client_id == client_record.id)
                .where(ClientChatMessage.legal_case_id.in_(case_ids))
                .order_by(ClientChatMessage.created_at.desc(), ClientChatMessage.id.desc())
                .limit(6)
            ).all()
            for item in raw_messages:
                legal_case = case_map.get(item.legal_case_id)
                author = client_record.name if item.is_from_client else "Юрист"
                if item.user_id and item.user_id in lawyers_map:
                    lawyer = lawyers_map[item.user_id]
                    author = lawyer.full_name or lawyer.username
                recent_messages.append(
                    {
                        "author": author,
                        "message": item.message,
                        "created_at": item.created_at,
                        "is_from_client": item.is_from_client,
                        "case_number": legal_case.case_number if legal_case else "",
                        "case_id": legal_case.id if legal_case else None,
                        "case_title": legal_case.title if legal_case else "",
                    }
                )

        pending_cases = [item for item in client_cases if not item.intake_approved]
        if pending_cases:
            attention_items.append(f"На проверке администратора: {len(pending_cases)}")
        if overdue:
            attention_items.append(f"Есть просроченные задачи: {len(overdue)}")
        cases_without_lawyer = [
            item for item in client_cases if item.intake_approved and item.stage != CaseStage.COMPLETED and not item.responsible_lawyer_id
        ]
        if cases_without_lawyer:
            attention_items.append(f"Ожидают назначения юриста: {len(cases_without_lawyer)}")
        imminent_cases = [
            item
            for item in client_cases
            if item.deadline and item.stage != CaseStage.COMPLETED and 0 <= (item.deadline - today).days <= 5
        ]
        if imminent_cases:
            attention_items.append(f"Близкие дедлайны в течение 5 дней: {len(imminent_cases)}")
        if not attention_items:
            attention_items.append("Критичных действий со стороны клиента сейчас не требуется")

        return templates.TemplateResponse(
            "client_dashboard.html",
            {
                "request": request,
                "user": user,
                "client": client_record,
                "cases_count": cases_count,
                "active_cases": active_cases,
                "tasks_todo": len([t for t in tasks if t.status != TaskStatus.DONE]),
                "overdue_tasks": len(overdue),
                "my_cases": client_cases[:6],
                "last_notifications": recent_notifications,
                "unread_notifications": unread_notifications,
                "upcoming_events": upcoming_events[:10],
                "recent_messages": recent_messages,
                "attention_items": attention_items,
                "stage_labels": STAGE_LABELS,
                "today": today,
            },
        )
    else:
        tasks = db.scalars(select(CaseTask)).all()
        overdue = [t for t in tasks if t.due_date < today and t.status != TaskStatus.DONE]
        cases_count = db.scalar(select(func.count(LegalCase.id))) or 0
        active_cases = db.scalar(select(func.count(LegalCase.id)).where(LegalCase.stage != CaseStage.COMPLETED)) or 0
        clients_count = db.scalar(select(func.count(Client.id))) or 0

    lawyers = []
    pending_intakes = []
    intake_accepted = request.query_params.get("intake_accepted") == "1"
    if user.role == Role.ADMIN:
        lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
        pending_cases = db.scalars(
            select(LegalCase)
            .where(LegalCase.intake_approved.is_(False))
            .order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
        ).all()
        for item in pending_cases:
            recommendations = topsis_rank(item.category or "", lawyers)[:3]
            pending_intakes.append(
                {
                    "case": item,
                    "recommendations": recommendations,
                }
            )

    data = {
        "cases_count": cases_count,
        "active_cases": active_cases,
        "clients_count": clients_count,
        "tasks_todo": len([t for t in tasks if t.status != TaskStatus.DONE]),
        "overdue_tasks": len(overdue),
        "recent_tasks": sorted(tasks, key=lambda t: t.due_date)[:8],
        "last_notifications": db.scalars(
            select(Notification)
            .where(Notification.recipient_id == user.id)
            .order_by(Notification.is_read.asc(), Notification.created_at.desc(), Notification.id.desc())
            .limit(5)
        ).all(),
        "lawyers": lawyers,
        "pending_intakes": pending_intakes,
        "intake_accepted": intake_accepted,
        "lawyer_created": request.query_params.get("lawyer_created") == "1",
        "lawyer_error": request.query_params.get("lawyer_error", "").strip(),
        "is_client_dashboard": is_client,
    }
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, **data, "user": user, "status_labels": STATUS_LABELS, "today": today},
    )


@app.post("/admin/lawyers/new")
def create_lawyer_by_admin(
    request: Request,
    last_name: str = Form(...),
    first_name: str = Form(...),
    middle_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(...),
    specialization: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    last_name = last_name.strip()
    first_name = first_name.strip()
    middle_name = middle_name.strip()
    phone = phone.strip()
    email = email.strip().lower()
    specialization = specialization.strip()
    full_name = " ".join([last_name, first_name, middle_name]).strip()

    if not NAME_RE.fullmatch(last_name):
        return RedirectResponse("/app?lawyer_error=invalid_last_name", status_code=303)
    if not NAME_RE.fullmatch(first_name):
        return RedirectResponse("/app?lawyer_error=invalid_first_name", status_code=303)
    if middle_name and not NAME_RE.fullmatch(middle_name):
        return RedirectResponse("/app?lawyer_error=invalid_middle_name", status_code=303)
    if phone and (not PHONE_RE.fullmatch(phone) or not (10 <= len(re.sub(r"\D", "", phone)) <= 15)):
        return RedirectResponse("/app?lawyer_error=invalid_phone", status_code=303)
    if not EMAIL_RE.fullmatch(email):
        return RedirectResponse("/app?lawyer_error=invalid_email", status_code=303)
    if find_user_by_email(db, email):
        return RedirectResponse("/app?lawyer_error=email_exists", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/app?lawyer_error=short_password", status_code=303)
    if not re.search(r"[A-ZА-ЯЁ]", password) or not re.search(r"[a-zа-яё]", password) or not re.search(r"\d", password):
        return RedirectResponse("/app?lawyer_error=weak_password", status_code=303)
    if password != password_confirm:
        return RedirectResponse("/app?lawyer_error=password_mismatch", status_code=303)

    username = generate_unique_username(db, email)
    new_user = User(
        username=username,
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
        phone=phone,
        email=email,
        password_hash=hash_password(password),
        role=Role.LAWYER,
        specialization=specialization,
    )
    db.add(new_user)
    log_action(db, admin, "Админ добавил юриста", f"{full_name} / {email}")
    db.commit()
    return RedirectResponse("/app?lawyer_created=1", status_code=303)


def _pick_lawyer_for_case(db: Session) -> User | None:
    return db.scalar(select(User).where(User.role == Role.LAWYER).order_by(User.current_load.asc(), User.id.asc()))


def _find_client_user_for_case(db: Session, legal_case: LegalCase) -> User | None:
    if not legal_case.client_id:
        return None
    client = db.get(Client, legal_case.client_id)
    if not client:
        return None
    if client.user_id:
        linked = db.get(User, client.user_id)
        if linked and linked.role == Role.CLIENT:
            return linked
    candidate = find_user_by_email(db, client.email or "")
    if candidate and candidate.role == Role.CLIENT:
        return candidate
    return None


def _store_uploaded_file(uploaded_file: UploadFile) -> tuple[str, str, str, bytes]:
    original_name = Path(uploaded_file.filename or "document.bin").name
    suffix = Path(original_name).suffix
    safe_suffix = suffix if len(suffix) <= 12 else ""
    stored_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(8)}{safe_suffix}"
    file_bytes = uploaded_file.file.read()
    mime_type = uploaded_file.content_type or "application/octet-stream"
    return original_name, stored_name, mime_type, file_bytes


def _migrate_legacy_documents_to_db() -> None:
    db = SessionLocal()
    try:
        legacy_docs = db.scalars(
            select(CaseDocument).where(CaseDocument.file_content.is_(None))
        ).all()
        changed = False
        for item in legacy_docs:
            path = UPLOADS_DIR / item.stored_filename
            if not path.exists():
                continue
            file_bytes = path.read_bytes()
            item.file_content = file_bytes
            item.file_size = len(file_bytes)
            if not item.mime_type:
                item.mime_type = "application/octet-stream"
            changed = True
        if changed:
            db.commit()
    finally:
        db.close()


def _build_content_disposition(filename: str, inline: bool = False) -> str:
    safe_name = filename or "document.bin"
    ascii_fallback = "".join(ch if ord(ch) < 128 else "_" for ch in safe_name) or "document.bin"
    encoded_name = quote(safe_name, safe="")
    mode = "inline" if inline else "attachment"
    return f"{mode}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_name}"


def _load_document_bytes(document: CaseDocument) -> tuple[bytes | None, str]:
    if document.file_content:
        mime_type = document.mime_type or mimetypes.guess_type(document.original_filename or "")[0] or "application/octet-stream"
        return document.file_content, mime_type
    path = UPLOADS_DIR / document.stored_filename
    if not path.exists():
        return None, "application/octet-stream"
    file_bytes = path.read_bytes()
    mime_type = document.mime_type or mimetypes.guess_type(document.original_filename or "")[0] or "application/octet-stream"
    return file_bytes, mime_type


def _extract_docx_text(file_bytes: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            xml_bytes = archive.read("word/document.xml")
    except Exception:
        return ""
    xml_text = xml_bytes.decode("utf-8", errors="ignore")
    xml_text = xml_text.replace("</w:p>", "\n").replace("<w:tab/>", "\t")
    plain = re.sub(r"<[^>]+>", "", xml_text)
    plain = html.unescape(plain)
    lines = [line.strip() for line in plain.splitlines()]
    return "\n".join(line for line in lines if line)


def _to_local_naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().replace(tzinfo=None)


def _safe_client_return_path(raw_path: str, fallback: str) -> str:
    candidate = (raw_path or "").strip()
    if candidate.startswith("/client/"):
        return candidate
    return fallback


@app.post("/admin/intake/{case_id}/accept")
def accept_client_intake(
    case_id: int,
    request: Request,
    responsible_lawyer_id: int = Form(...),
    priority: str = Form("MEDIUM"),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    if legal_case.intake_approved:
        return RedirectResponse("/app?intake_accepted=1", status_code=303)

    lawyer = db.get(User, responsible_lawyer_id)
    if not lawyer or lawyer.role != Role.LAWYER:
        raise HTTPException(status_code=400, detail="Выбран некорректный юрист")

    priority_value = (priority or "MEDIUM").strip().upper()
    if priority_value not in {"LOW", "MEDIUM", "HIGH"}:
        priority_value = "MEDIUM"

    legal_case.responsible_lawyer_id = lawyer.id
    legal_case.priority = priority_value
    legal_case.intake_approved = True
    if lawyer not in legal_case.lawyers:
        legal_case.lawyers.append(lawyer)

    db.add(
        CaseTask(
            legal_case_id=legal_case.id,
            title="Первичный разбор обращения клиента",
            description="Создано администратором при принятии обращения.",
            due_date=date.today() + timedelta(days=1),
            status=TaskStatus.TODO,
            priority="HIGH",
            assignee_id=lawyer.id,
        )
    )

    db.add(
        Notification(
            recipient_id=lawyer.id,
            title="Обращение принято и назначено вам",
            message=f"{legal_case.case_number}: {legal_case.title}",
        )
    )
    client_user = _find_client_user_for_case(db, legal_case)
    if client_user:
        db.add(
            Notification(
                recipient_id=client_user.id,
                title="Ваше обращение принято",
                message=f"{legal_case.case_number}: обращение принято в работу. Назначен юрист: {lawyer.full_name or lawyer.username}",
            )
        )

    log_action(db, admin, "Принято обращение клиента", f"{legal_case.case_number} -> {lawyer.full_name or lawyer.username}")
    db.commit()
    return RedirectResponse("/app?intake_accepted=1", status_code=303)


@app.get("/client/profile", response_class=HTMLResponse)
def client_profile_page(request: Request, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    # Backward-compatible fallback: if old requisites were stored as one field,
    # show them in "other details" until user edits the split requisites fields.
    if not (client.inn or client.ogrn or client.bank_details or client.passport_details or client.other_details):
        client.other_details = (client.requisites or "").strip()
    return templates.TemplateResponse(
        "client_profile.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "saved": request.query_params.get("saved") == "1",
            "error": request.query_params.get("error", "").strip(),
        },
    )


@app.post("/client/profile")
def client_profile_update(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    address: str = Form(""),
    inn: str = Form(""),
    ogrn: str = Form(""),
    bank_details: str = Form(""),
    passport_details: str = Form(""),
    other_details: str = Form(""),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)
    cleaned_email = email.strip().lower()
    if not EMAIL_RE.fullmatch(cleaned_email):
        return RedirectResponse("/client/profile?error=email", status_code=303)
    existing_user = find_user_by_email(db, cleaned_email)
    if existing_user and existing_user.id != user.id:
        return RedirectResponse("/client/profile?error=email_exists", status_code=303)

    client.name = name.strip()
    client.email = cleaned_email
    client.phone = phone.strip()
    client.address = address.strip()
    client.inn = re.sub(r"\D", "", inn)[:12]
    client.ogrn = re.sub(r"\D", "", ogrn)[:15]
    client.bank_details = bank_details.strip()
    client.passport_details = passport_details.strip()
    client.other_details = other_details.strip()
    client.requisites = "\n".join(
        part
        for part in [
            f"ИНН: {client.inn}" if client.inn else "",
            f"ОГРН: {client.ogrn}" if client.ogrn else "",
            f"Банковские реквизиты: {client.bank_details}" if client.bank_details else "",
            f"Паспортные данные: {client.passport_details}" if client.passport_details else "",
            f"Иные сведения: {client.other_details}" if client.other_details else "",
        ]
        if part
    )

    user.full_name = client.name
    user.email = cleaned_email
    user.phone = client.phone
    db.add(
        Notification(
            recipient_id=user.id,
            title="Профиль обновлен",
            message="Данные профиля клиента успешно сохранены",
        )
    )
    db.commit()
    return RedirectResponse("/client/profile?saved=1", status_code=303)


@app.get("/client/intake", response_class=HTMLResponse)
def client_intake_page(request: Request, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    return templates.TemplateResponse(
        "client_intake.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "created": request.query_params.get("created") == "1",
            "today": date.today(),
        },
    )


@app.post("/client/intake")
def client_intake_submit(
    request: Request,
    case_title: str = Form(...),
    category: str = Form(...),
    message: str = Form(""),
    documents: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)

    legal_case = LegalCase(
        case_number=next_case_number(db),
        title=case_title.strip(),
        category=category.strip() or "Общее",
        description=message.strip(),
        stage=CaseStage.NEW_REQUEST,
        priority="MEDIUM",
        intake_approved=False,
        opened_at=date.today(),
        deadline=None,
        client_id=client.id,
        responsible_lawyer_id=None,
    )
    db.add(legal_case)
    db.flush()

    for uploaded in documents:
        if not uploaded.filename:
            continue
        original_name, stored_name, mime_type, file_bytes = _store_uploaded_file(uploaded)
        db.add(
            CaseDocument(
                legal_case_id=legal_case.id,
                uploaded_by_user_id=user.id,
                original_filename=original_name,
                stored_filename=stored_name,
                mime_type=mime_type,
                file_size=len(file_bytes),
                file_content=file_bytes,
                description="Документ из формы подачи обращения",
            )
        )

    admins = db.scalars(select(User).where(User.role == Role.ADMIN)).all()
    for admin in admins:
        db.add(
            Notification(
                recipient_id=admin.id,
                title="Новое обращение клиента",
                message=f"{legal_case.case_number}: {legal_case.title}",
            )
        )
    db.add(
        Notification(
            recipient_id=user.id,
            title="Заявка отправлена",
            message=f"Обращение {legal_case.case_number} отправлено и ожидает проверки администратором",
        )
    )
    log_action(db, user, "Клиент подал обращение", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()
    return RedirectResponse("/client/intake?created=1", status_code=303)


@app.get("/client/cases", response_class=HTMLResponse)
def client_cases_page(request: Request, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    my_cases = db.scalars(
        select(LegalCase).where(LegalCase.client_id == client.id).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    ).all()
    case_ids = [item.id for item in my_cases]
    tasks = []
    documents = []
    if case_ids:
        tasks = db.scalars(select(CaseTask).where(CaseTask.legal_case_id.in_(case_ids))).all()
        documents = db.scalars(select(CaseDocument).where(CaseDocument.legal_case_id.in_(case_ids))).all()

    task_stats = {}
    for legal_case in my_cases:
        case_tasks = [item for item in tasks if item.legal_case_id == legal_case.id]
        done_tasks = [item for item in case_tasks if item.status == TaskStatus.DONE]
        overdue_tasks = [item for item in case_tasks if item.status != TaskStatus.DONE and item.due_date < date.today()]
        next_task = None
        pending_tasks = [item for item in case_tasks if item.status != TaskStatus.DONE]
        if pending_tasks:
            next_task = min(pending_tasks, key=lambda item: item.due_date)
        task_stats[legal_case.id] = {
            "total": len(case_tasks),
            "done": len(done_tasks),
            "overdue": len(overdue_tasks),
            "next_due": next_task.due_date if next_task else None,
        }

    document_counts = {item.id: 0 for item in my_cases}
    for document in documents:
        document_counts[document.legal_case_id] = document_counts.get(document.legal_case_id, 0) + 1

    chat_state = {item["case"].id: item for item in build_case_chat_list(db, client, my_cases)}

    return templates.TemplateResponse(
        "client_cases.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "cases": my_cases,
            "task_stats": task_stats,
            "document_counts": document_counts,
            "chat_state": chat_state,
            "stage_labels": STAGE_LABELS,
            "today": date.today(),
        },
    )


@app.get("/client/cases/{case_id}", response_class=HTMLResponse)
def client_case_detail_page(case_id: int, request: Request, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=404, detail="Дело не найдено")

    my_cases = db.scalars(
        select(LegalCase).where(LegalCase.client_id == client.id).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    ).all()
    chat_list = build_case_chat_list(db, client, my_cases)
    chat_index = {item["case"].id: item for item in chat_list}
    detail = build_client_case_detail(db, client, legal_case)
    return templates.TemplateResponse(
        "client_case_detail.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "legal_case": legal_case,
            "case_detail": detail,
            "chat_list": chat_list,
            "active_chat": chat_index.get(legal_case.id),
            "stage_labels": STAGE_LABELS,
            "status_labels": STATUS_LABELS,
        },
    )


@app.get("/client/documents", response_class=HTMLResponse)
def client_documents_page(request: Request, case_id: int | None = None, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    my_cases = db.scalars(
        select(LegalCase).where(LegalCase.client_id == client.id).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    ).all()
    selected_case_id = next((item.id for item in my_cases if item.id == case_id), None)
    case_ids = [item.id for item in my_cases]
    documents = []
    if case_ids:
        documents = db.scalars(
            select(CaseDocument)
            .where(CaseDocument.legal_case_id.in_(case_ids))
            .order_by(CaseDocument.created_at.desc(), CaseDocument.id.desc())
        ).all()
    case_map = {item.id: item for item in my_cases}
    return templates.TemplateResponse(
        "client_documents.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "cases": my_cases,
            "case_map": case_map,
            "documents": documents,
            "uploaded": request.query_params.get("uploaded") == "1",
            "deleted": request.query_params.get("deleted") == "1",
            "error": request.query_params.get("error", "").strip(),
            "selected_case_id": selected_case_id,
        },
    )


@app.post("/client/documents/upload")
def client_documents_upload(
    request: Request,
    legal_case_id: int = Form(...),
    description: str = Form(""),
    return_to: str = Form(""),
    documents: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)
    legal_case = db.get(LegalCase, legal_case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    default_redirect = f"/client/documents?case_id={legal_case.id}"
    redirect_base = _safe_client_return_path(return_to, default_redirect)
    valid_documents = [item for item in documents if item and item.filename]
    if not valid_documents:
        separator = "&" if "?" in redirect_base else "?"
        return RedirectResponse(f"{redirect_base}{separator}error=file", status_code=303)

    existing_names = {
        (name or "").strip().lower()
        for name in db.scalars(
            select(CaseDocument.original_filename)
            .join(LegalCase, CaseDocument.legal_case_id == LegalCase.id)
            .where(LegalCase.client_id == client.id)
        ).all()
        if (name or "").strip()
    }
    batch_names: set[str] = set()
    duplicate_names: set[str] = set()
    for document in valid_documents:
        normalized = (document.filename or "").strip().lower()
        if not normalized:
            continue
        if normalized in existing_names or normalized in batch_names:
            duplicate_names.add(document.filename or normalized)
        batch_names.add(normalized)
    if duplicate_names:
        separator = "&" if "?" in redirect_base else "?"
        return RedirectResponse(f"{redirect_base}{separator}error=duplicate", status_code=303)

    uploaded_names: list[str] = []
    for document in valid_documents:
        original_name, stored_name, mime_type, file_bytes = _store_uploaded_file(document)
        uploaded_names.append(original_name)
        db.add(
            CaseDocument(
                legal_case_id=legal_case.id,
                uploaded_by_user_id=user.id,
                original_filename=original_name,
                stored_filename=stored_name,
                mime_type=mime_type,
                file_size=len(file_bytes),
                file_content=file_bytes,
                description=description.strip(),
            )
        )

    uploaded_count = len(uploaded_names)
    first_name = uploaded_names[0]
    summary = first_name if uploaded_count == 1 else f"{first_name} и еще {uploaded_count - 1}"
    if legal_case.responsible_lawyer_id:
        db.add(
            Notification(
                recipient_id=legal_case.responsible_lawyer_id,
                title="Клиент добавил документы" if uploaded_count > 1 else "Клиент добавил документ",
                message=f"{legal_case.case_number}: {summary}",
            )
        )
    db.add(
        Notification(
            recipient_id=user.id,
            title="Документы загружены" if uploaded_count > 1 else "Документ загружен",
            message=f"К делу {legal_case.case_number} прикреплено файлов: {uploaded_count}",
        )
    )
    db.commit()
    separator = "&" if "?" in redirect_base else "?"
    return RedirectResponse(f"{redirect_base}{separator}uploaded=1", status_code=303)


@app.post("/client/documents/{document_id}/delete")
def client_document_delete(
    document_id: int,
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)
    wants_json = (
        request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
        or "application/json" in request.headers.get("accept", "").lower()
    )
    document = db.get(CaseDocument, document_id)
    if not document:
        if wants_json:
            return JSONResponse({"ok": False, "error": "document_not_found"}, status_code=404)
        return RedirectResponse("/client/documents?error=document", status_code=303)

    legal_case = db.get(LegalCase, document.legal_case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=403, detail="Нет доступа к документу")
    default_redirect = f"/client/documents?case_id={legal_case.id}"
    redirect_base = _safe_client_return_path(return_to, default_redirect)

    # Backward compatibility: if old documents still have files on disk, clean them too.
    file_path = UPLOADS_DIR / (document.stored_filename or "")
    if file_path.name and file_path.exists():
        try:
            file_path.unlink()
        except OSError:
            pass

    document_name = document.original_filename or "документ"
    db.delete(document)
    db.add(
        Notification(
            recipient_id=user.id,
            title="Документ удален",
            message=f"Файл «{document_name}» удален из дела {legal_case.case_number}",
        )
    )
    if legal_case.responsible_lawyer_id:
        db.add(
            Notification(
                recipient_id=legal_case.responsible_lawyer_id,
                title="Клиент удалил документ",
                message=f"{legal_case.case_number}: {document_name}",
            )
        )
    db.commit()
    if wants_json:
        return JSONResponse({"ok": True, "deleted_id": document_id})
    separator = "&" if "?" in redirect_base else "?"
    return RedirectResponse(f"{redirect_base}{separator}deleted=1", status_code=303)


@app.get("/client/documents/{document_id}/download")
def client_document_download(document_id: int, request: Request, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    document = db.get(CaseDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    legal_case = db.get(LegalCase, document.legal_case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=403, detail="Нет доступа к документу")
    file_bytes, mime_type = _load_document_bytes(document)
    if file_bytes is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    headers = {"Content-Disposition": _build_content_disposition(document.original_filename or "document.bin")}
    return Response(content=file_bytes, media_type=mime_type, headers=headers)


@app.get("/client/documents/{document_id}/view", response_class=HTMLResponse)
def client_document_view(document_id: int, request: Request, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    document = db.get(CaseDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    legal_case = db.get(LegalCase, document.legal_case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=403, detail="Нет доступа к документу")

    file_bytes, mime_type = _load_document_bytes(document)
    if file_bytes is None:
        raise HTTPException(status_code=404, detail="Файл не найден")

    original_name = document.original_filename or "document.bin"
    suffix = Path(original_name).suffix.lower()

    if mime_type == "application/pdf" or mime_type.startswith("image/"):
        headers = {"Content-Disposition": _build_content_disposition(original_name, inline=True)}
        return Response(content=file_bytes, media_type=mime_type, headers=headers)

    if mime_type.startswith("text/") or suffix in {".txt", ".md", ".csv", ".json", ".xml", ".log", ".ini"}:
        text_preview = file_bytes.decode("utf-8", errors="replace")
        return templates.TemplateResponse(
            "client_document_preview.html",
            {
                "request": request,
                "user": user,
                "client": client,
                "document": document,
                "preview_title": original_name,
                "preview_text": text_preview,
                "preview_supported": True,
            },
        )

    if suffix == ".docx":
        text_preview = _extract_docx_text(file_bytes)
        return templates.TemplateResponse(
            "client_document_preview.html",
            {
                "request": request,
                "user": user,
                "client": client,
                "document": document,
                "preview_title": original_name,
                "preview_text": text_preview or "Не удалось извлечь текст из файла DOCX.",
                "preview_supported": bool(text_preview),
            },
        )

    return templates.TemplateResponse(
        "client_document_preview.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "document": document,
            "preview_title": original_name,
            "preview_text": "",
            "preview_supported": False,
        },
    )


@app.get("/client/chat", response_class=HTMLResponse)
def client_chat_page(request: Request, case_id: int | None = None, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    my_cases = db.scalars(
        select(LegalCase).where(LegalCase.client_id == client.id).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    ).all()
    selected_case = None
    if my_cases:
        selected_case = next((item for item in my_cases if item.id == case_id), my_cases[0])

    chat_list = build_case_chat_list(db, client, my_cases)
    chat_index = {item["case"].id: item for item in chat_list}
    messages = []
    assigned_lawyer = None
    selected_case_pending_tasks = []
    selected_case_documents_count = 0
    if selected_case:
        assigned_lawyer = db.get(User, selected_case.responsible_lawyer_id) if selected_case.responsible_lawyer_id else None
        messages = build_case_message_payload(db, client, selected_case, assigned_lawyer)
        selected_case_pending_tasks = db.scalars(
            select(CaseTask)
            .where(CaseTask.legal_case_id == selected_case.id)
            .where(CaseTask.status != TaskStatus.DONE)
            .order_by(CaseTask.due_date.asc(), CaseTask.id.asc())
            .limit(4)
        ).all()
        selected_case_documents_count = (
            db.scalar(select(func.count(CaseDocument.id)).where(CaseDocument.legal_case_id == selected_case.id)) or 0
        )

    return templates.TemplateResponse(
        "client_chat.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "cases": chat_list,
            "selected_case": selected_case,
            "assigned_lawyer": assigned_lawyer,
            "messages": messages,
            "sent": request.query_params.get("sent") == "1",
            "error": request.query_params.get("error", "").strip(),
            "selected_case_pending_tasks": selected_case_pending_tasks,
            "selected_case_documents_count": selected_case_documents_count,
            "stage_labels": STAGE_LABELS,
            "active_chat": chat_index.get(selected_case.id) if selected_case else None,
            "today": date.today(),
        },
    )


@app.post("/client/chat")
def client_chat_send(
    request: Request,
    legal_case_id: int = Form(...),
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)
    legal_case = db.get(LegalCase, legal_case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    if not legal_case.responsible_lawyer_id:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            raise HTTPException(status_code=400, detail="Юрист еще не назначен")
        return RedirectResponse(f"/client/chat?case_id={legal_case_id}&error=lawyer", status_code=303)
    text = message.strip()
    if not text:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")
        return RedirectResponse(f"/client/chat?case_id={legal_case_id}&error=empty", status_code=303)

    new_message = ClientChatMessage(
        client_id=client.id,
        legal_case_id=legal_case.id,
        user_id=None,
        message=text,
        is_from_client=True,
    )
    db.add(new_message)
    db.commit()
    assigned_lawyer = db.get(User, legal_case.responsible_lawyer_id) if legal_case.responsible_lawyer_id else None
    messages = build_case_message_payload(db, client, legal_case, assigned_lawyer)

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(
            {
                "ok": True,
                "message": {
                    "id": new_message.id,
                    "author": client.name,
                    "message": new_message.message,
                    "created_at": new_message.created_at.strftime("%d.%m.%Y %H:%M"),
                    "is_from_client": True,
                },
                "messages": [
                    {
                        "id": item["id"],
                        "author": item["author"],
                        "message": item["message"],
                        "created_at": item["created_at"].strftime("%d.%m.%Y %H:%M"),
                        "is_from_client": item["is_from_client"],
                    }
                    for item in messages
                ],
            }
        )
    return RedirectResponse(f"/client/chat?case_id={legal_case_id}&sent=1", status_code=303)


@app.get("/client/calendar", response_class=HTMLResponse)
def client_calendar_page(request: Request, db: Session = Depends(get_db)):
    user, client = require_client_account(request, db)
    today = date.today()
    my_cases = db.scalars(
        select(LegalCase).where(LegalCase.client_id == client.id).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    ).all()
    case_ids = [item.id for item in my_cases]
    tasks = []
    events = []
    case_map = {item.id: item for item in my_cases}
    if case_ids:
        tasks = db.scalars(
            select(CaseTask).where(CaseTask.legal_case_id.in_(case_ids)).order_by(CaseTask.due_date.asc(), CaseTask.id.asc())
        ).all()
        events = db.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.legal_case_id.in_(case_ids))
            .order_by(CalendarEvent.starts_at.asc(), CalendarEvent.id.asc())
        ).all()

    timeline = []
    for task in tasks:
        timeline.append(
            {
                "date": task.due_date,
                "title": task.title,
                "kind": "Задача",
                "status": STATUS_LABELS.get(task.status, task.status.value),
                "case_number": task.legal_case.case_number if task.legal_case else "",
                "urgency": (
                    "overdue"
                    if task.status != TaskStatus.DONE and task.due_date < today
                    else "soon"
                    if task.status != TaskStatus.DONE and (task.due_date - today).days <= 3
                    else "normal"
                ),
            }
        )
    for event in events:
        timeline.append(
            {
                "date": event.starts_at.date(),
                "title": event.title,
                "kind": "Событие",
                "status": EVENT_TYPE_LABELS.get((event.event_type or "CUSTOM").upper(), "Событие"),
                "case_number": case_map[event.legal_case_id].case_number if event.legal_case_id in case_map else "",
                "urgency": "soon" if 0 <= (event.starts_at.date() - today).days <= 3 else "normal",
            }
        )
    timeline.sort(key=lambda item: (item["date"], item["title"]))
    return templates.TemplateResponse(
        "client_calendar.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "timeline": timeline,
            "today": today,
        },
    )


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, q: str = "", chat_client_id: int | None = None, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    stmt = select(Client)
    if q.strip():
        stmt = stmt.where(Client.name.ilike(f"%{q.strip()}%"))
    clients = db.scalars(stmt.order_by(Client.id.desc())).all()
    initial_chat_client_id = None
    if chat_client_id and db.get(Client, chat_client_id):
        initial_chat_client_id = chat_client_id
    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "clients": clients,
            "q": q,
            "initial_chat_client_id": initial_chat_client_id,
            "user": user,
            "created": request.query_params.get("created") == "1",
        },
    )


@app.post("/clients/new")
def create_client(
    request: Request,
    name: str = Form(...),
    client_type: str = Form("ORGANIZATION"),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    clean_name = name.strip()
    if len(clean_name) < 2:
        return RedirectResponse("/clients?error=invalid", status_code=303)

    db.add(
        Client(
            name=clean_name,
            client_type=(client_type or "ORGANIZATION").upper(),
            email=email.strip().lower(),
            phone=phone.strip(),
            address=address.strip(),
            notes=notes.strip(),
        )
    )
    db.add(Notification(recipient_id=user.id, title="Новый клиент", message=f"Добавлен клиент: {clean_name}"))
    log_action(db, user, "Создан клиент", clean_name)
    db.commit()
    return RedirectResponse("/clients?created=1", status_code=303)


@app.get("/clients/{client_id}/chat")
def client_chat(client_id: int, request: Request, db: Session = Depends(get_db)):
    _ = require_auth(request, db)
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    return JSONResponse(build_client_chat_payload(db, client))


@app.post("/clients/{client_id}/chat")
def add_client_chat_message(
    client_id: int,
    request: Request,
    message: str = Form(...),
    is_from_client: str = Form("false"),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    text = message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")

    from_client = is_from_client.strip().lower() in {"1", "true", "yes", "on"}
    db.add(
        ClientChatMessage(
            client_id=client.id,
            user_id=None if from_client else user.id,
            message=text,
            is_from_client=from_client,
        )
    )
    log_action(db, user, "Сообщение в чате клиента", f"{client.name}: {text[:80]}")
    db.commit()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(build_client_chat_payload(db, client))
    return RedirectResponse("/clients", status_code=303)


@app.get("/cases", response_class=HTMLResponse)
def cases_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    stmt = select(LegalCase).where(LegalCase.intake_approved.is_(True)).order_by(LegalCase.id.desc())
    if user.role == Role.LAWYER:
        stmt = stmt.where(LegalCase.responsible_lawyer_id == user.id)
    cases = db.scalars(stmt).all()
    clients = db.scalars(select(Client).order_by(Client.name)).all()
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name)).all()
    return templates.TemplateResponse(
        "cases.html",
        {
            "request": request,
            "cases": cases,
            "stage_labels": STAGE_LABELS,
            "stages": list(CaseStage),
            "clients": clients,
            "lawyers": lawyers,
            "generated_case_number": next_case_number(db),
            "user": user,
            "created": request.query_params.get("created") == "1",
        },
    )


@app.post("/cases/new")
def create_case(
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    client_id: int = Form(...),
    case_number: str = Form(""),
    stage: str = Form(CaseStage.NEW_REQUEST.value),
    priority: str = Form("MEDIUM"),
    deadline: str = Form(""),
    responsible_lawyer_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    client = db.get(Client, client_id)
    if not client:
        return RedirectResponse("/cases?error=client", status_code=303)

    stage_value = CaseStage.NEW_REQUEST
    try:
        stage_value = CaseStage(stage)
    except ValueError:
        pass

    deadline_value = parse_iso_date(deadline)
    opened_at = date.today()
    number = case_number.strip() or next_case_number(db)

    legal_case = LegalCase(
        case_number=number,
        title=title.strip(),
        category=category.strip() or "Общее",
        description=description.strip(),
        stage=stage_value,
        priority=priority.strip().upper() or "MEDIUM",
        opened_at=opened_at,
        deadline=deadline_value,
        client_id=client.id,
        responsible_lawyer_id=responsible_lawyer_id,
    )
    db.add(legal_case)
    db.flush()

    if responsible_lawyer_id:
        lawyer = db.get(User, responsible_lawyer_id)
        if lawyer:
            legal_case.lawyers.append(lawyer)

    db.add(
        Notification(
            recipient_id=user.id,
            title="Создано новое дело",
            message=f"{legal_case.case_number}: {legal_case.title}",
        )
    )
    log_action(db, user, "Создано дело", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()
    return RedirectResponse("/cases?created=1", status_code=303)


@app.post("/cases/{case_id}/stage")
def update_case_stage(
    case_id: int,
    request: Request,
    stage: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")

    try:
        legal_case.stage = CaseStage(stage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректная стадия") from exc

    db.add(Notification(recipient_id=user.id, title="Стадия дела изменена", message=legal_case.case_number))
    log_action(db, user, "Обновлена стадия дела", f"{legal_case.case_number} -> {legal_case.stage.value}")
    db.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"ok": True, "label": STAGE_LABELS[legal_case.stage]})
    return RedirectResponse("/cases", status_code=303)


@app.post("/cases/{case_id}/edit")
def update_case_details(
    case_id: int,
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    deadline: str = Form(""),
    priority: str = Form("MEDIUM"),
    responsible_lawyer_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")

    lawyer_id_value = None
    raw_lawyer_id = (responsible_lawyer_id or "").strip()
    if raw_lawyer_id:
        try:
            lawyer_id_value = int(raw_lawyer_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Некорректный юрист") from exc

    deadline_value = parse_iso_date(deadline) if deadline else None
    legal_case.title = title.strip()
    legal_case.category = category.strip()
    legal_case.description = description.strip()
    legal_case.deadline = deadline_value
    legal_case.priority = priority.strip().upper() or "MEDIUM"
    legal_case.responsible_lawyer_id = lawyer_id_value

    if lawyer_id_value:
        lawyer = db.get(User, lawyer_id_value)
        if lawyer and lawyer not in legal_case.lawyers:
            legal_case.lawyers.append(lawyer)

    db.add(
        Notification(
            recipient_id=user.id,
            title="Карточка дела обновлена",
            message=f"{legal_case.case_number}: {legal_case.title}",
        )
    )
    log_action(db, user, "Обновлена карточка дела", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        assignee = legal_case.responsible_lawyer
        return JSONResponse(
            {
                "ok": True,
                "case": {
                    "id": legal_case.id,
                    "title": legal_case.title,
                    "category": legal_case.category,
                    "description": legal_case.description,
                    "priority": legal_case.priority,
                    "priority_label": {"LOW": "Низкий", "MEDIUM": "Средний", "HIGH": "Высокий"}.get(
                        legal_case.priority,
                        legal_case.priority,
                    ),
                    "deadline": legal_case.deadline.strftime("%d.%m.%Y") if legal_case.deadline else "Без дедлайна",
                    "deadline_input": legal_case.deadline.isoformat() if legal_case.deadline else "",
                    "responsible_lawyer_id": legal_case.responsible_lawyer_id or "",
                    "responsible_lawyer_name": (
                        (assignee.full_name or assignee.username) if assignee else "Не назначен"
                    ),
                },
            }
        )
    return RedirectResponse("/kanban", status_code=303)


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    tasks = db.scalars(select(CaseTask).order_by(CaseTask.due_date, CaseTask.id.desc())).all()
    cases = db.scalars(select(LegalCase).order_by(LegalCase.case_number)).all()
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name)).all()
    return templates.TemplateResponse(
        "tasks.html",
        {
            "request": request,
            "tasks": tasks,
            "cases": cases,
            "lawyers": lawyers,
            "status_labels": STATUS_LABELS,
            "statuses": list(TaskStatus),
            "user": user,
            "created": request.query_params.get("created") == "1",
        },
    )


@app.post("/tasks/new")
def create_task(
    request: Request,
    legal_case_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    due_date: str = Form(...),
    status: str = Form(TaskStatus.TODO.value),
    priority: str = Form("MEDIUM"),
    assignee_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    task_date = parse_iso_date(due_date)
    if not task_date:
        return RedirectResponse("/tasks?error=date", status_code=303)

    legal_case = db.get(LegalCase, legal_case_id)
    if not legal_case:
        return RedirectResponse("/tasks?error=case", status_code=303)

    status_value = TaskStatus.TODO
    try:
        status_value = TaskStatus(status)
    except ValueError:
        pass

    new_task = CaseTask(
        legal_case_id=legal_case.id,
        title=title.strip(),
        description=description.strip(),
        due_date=task_date,
        status=status_value,
        priority=priority.strip().upper() or "MEDIUM",
        assignee_id=assignee_id,
    )
    db.add(new_task)
    db.add(
        Notification(
            recipient_id=user.id,
            title="Новая задача",
            message=f"{new_task.title} ({legal_case.case_number})",
        )
    )
    log_action(db, user, "Создана задача", f"{new_task.title} / {legal_case.case_number}")
    db.commit()
    return RedirectResponse("/tasks?created=1", status_code=303)


@app.post("/tasks/{task_id}/status")
def update_task_status(
    task_id: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    task = db.get(CaseTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    try:
        task.status = TaskStatus(status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный статус") from exc

    db.add(
        Notification(
            recipient_id=user.id,
            title="Статус задачи обновлен",
            message=f"{task.title}: {STATUS_LABELS[task.status]}",
        )
    )
    log_action(db, user, "Обновлен статус задачи", f"{task.title} -> {task.status.value}")
    db.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"ok": True, "label": STATUS_LABELS[task.status]})
    return RedirectResponse("/tasks", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, lawyer_id: int | None = None, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
    selected_lawyer = None

    if user.role == Role.LAWYER:
        selected_lawyer = user
    elif lawyers:
        selected_lawyer = next((item for item in lawyers if item.id == lawyer_id), lawyers[0])

    stmt = select(CaseTask).order_by(CaseTask.due_date, CaseTask.id)
    if selected_lawyer:
        stmt = stmt.where(CaseTask.assignee_id == selected_lawyer.id)
    tasks = db.scalars(stmt).all()
    legal_cases = db.scalars(select(LegalCase).order_by(LegalCase.case_number)).all()
    case_map = {item.id: item for item in legal_cases}
    calendar_events = db.scalars(select(CalendarEvent).order_by(CalendarEvent.starts_at, CalendarEvent.id)).all()

    events = []
    for task in tasks:
        events.append(
            {
                "title": task.title,
                "date": task.due_date.isoformat(),
                "status": STATUS_LABELS[task.status],
                "is_done": task.status == TaskStatus.DONE,
                "kind": "TASK",
                "event_type": "DEADLINE",
                "case_number": task.legal_case.case_number if task.legal_case else "",
            }
        )
    for event in calendar_events:
        event_type = (event.event_type or "CUSTOM").upper()
        related_case = case_map.get(event.legal_case_id) if event.legal_case_id else None
        events.append(
            {
                "title": event.title,
                "date": event.starts_at.date().isoformat(),
                "status": EVENT_TYPE_LABELS.get(event_type, event_type),
                "is_done": False,
                "kind": "EVENT",
                "event_type": event_type,
                "case_number": related_case.case_number if related_case else "",
            }
        )
    events.sort(key=lambda item: (item["date"], item["title"]))

    return templates.TemplateResponse(
        "calendar.html",
        {
            "request": request,
            "events": events,
            "cases": legal_cases,
            "lawyers": lawyers,
            "selected_lawyer": selected_lawyer,
            "user": user,
            "created_event": request.query_params.get("created_event") == "1",
        },
    )


@app.post("/calendar/events/new")
def create_calendar_event(
    request: Request,
    title: str = Form(...),
    event_date: str = Form(...),
    event_type: str = Form("CUSTOM"),
    legal_case_id: str = Form(""),
    lawyer_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    clean_title = title.strip()
    event_day = parse_iso_date(event_date)
    if not clean_title or not event_day:
        redirect = "/calendar?error=event"
        if lawyer_id.strip().isdigit():
            redirect = f"{redirect}&lawyer_id={lawyer_id.strip()}"
        return RedirectResponse(redirect, status_code=303)

    case_id_value = None
    raw_case_id = legal_case_id.strip()
    if raw_case_id:
        if not raw_case_id.isdigit():
            redirect = "/calendar?error=case"
            if lawyer_id.strip().isdigit():
                redirect = f"{redirect}&lawyer_id={lawyer_id.strip()}"
            return RedirectResponse(redirect, status_code=303)
        case_id_value = int(raw_case_id)
        if not db.get(LegalCase, case_id_value):
            redirect = "/calendar?error=case"
            if lawyer_id.strip().isdigit():
                redirect = f"{redirect}&lawyer_id={lawyer_id.strip()}"
            return RedirectResponse(redirect, status_code=303)

    event_type_value = (event_type or "CUSTOM").strip().upper()
    if event_type_value not in EVENT_TYPE_LABELS:
        event_type_value = "CUSTOM"

    db.add(
        CalendarEvent(
            title=clean_title,
            starts_at=datetime.combine(event_day, datetime.min.time()),
            event_type=event_type_value,
            legal_case_id=case_id_value,
        )
    )
    db.add(
        Notification(
            recipient_id=user.id,
            title="Новое событие календаря",
            message=f"{clean_title} ({event_day.strftime('%d.%m.%Y')})",
        )
    )
    log_action(db, user, "Создано событие календаря", clean_title)
    db.commit()

    redirect = "/calendar?created_event=1"
    if lawyer_id.strip().isdigit():
        redirect = f"{redirect}&lawyer_id={lawyer_id.strip()}"
    return RedirectResponse(redirect, status_code=303)


@app.get("/cases/{case_id}/workspace")
def case_workspace(case_id: int, request: Request, db: Session = Depends(get_db)):
    _ = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    return JSONResponse(build_case_workspace(db, legal_case))


@app.post("/cases/{case_id}/comments")
def add_case_comment(
    case_id: int,
    request: Request,
    message: str = Form(...),
    is_internal: str = Form("true"),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")

    text = message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Комментарий не может быть пустым")

    comment = CaseComment(
        legal_case_id=legal_case.id,
        user_id=user.id,
        message=text,
        is_internal=is_internal.strip().lower() != "false",
    )
    db.add(comment)
    db.add(
        Notification(
            recipient_id=user.id,
            title="Новый комментарий по делу",
            message=f"{legal_case.case_number}: {legal_case.title}",
        )
    )
    log_action(db, user, "Добавлен комментарий по делу", f"{legal_case.case_number}: {text[:80]}")
    db.commit()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(build_case_workspace(db, legal_case))
    return RedirectResponse("/kanban", status_code=303)


@app.get("/kanban", response_class=HTMLResponse)
def kanban_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    today = datetime.now().date()
    stmt = select(LegalCase).where(LegalCase.intake_approved.is_(True)).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    if user.role == Role.LAWYER:
        stmt = stmt.where(LegalCase.responsible_lawyer_id == user.id)
    cases = db.scalars(stmt).all()
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
    grouped = defaultdict(list)
    for legal_case in cases:
        grouped[legal_case.stage].append(legal_case)
    return templates.TemplateResponse(
        "kanban.html",
        {
            "request": request,
            "grouped": grouped,
            "stages": list(CaseStage),
            "stage_labels": STAGE_LABELS,
            "lawyers": lawyers,
            "today": today,
            "user": user,
        },
    )


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    notifications = db.scalars(
        select(Notification)
        .where(Notification.recipient_id == user.id)
        .order_by(Notification.is_read.asc(), Notification.created_at.desc(), Notification.id.desc())
        .limit(100)
    ).all()
    unread = len([item for item in notifications if not item.is_read])
    return templates.TemplateResponse(
        "notifications.html",
        {"request": request, "notifications": notifications, "unread": unread, "user": user, "today": date.today()},
    )


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")
    if notification.recipient_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа к уведомлению")
    notification.is_read = True
    db.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        unread = (
            db.scalar(
                select(func.count(Notification.id))
                .where(Notification.recipient_id == user.id)
                .where(Notification.is_read.is_(False))
            )
            or 0
        )
        return JSONResponse({"ok": True, "unread": unread})
    return RedirectResponse("/notifications", status_code=303)


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    invoices = db.scalars(select(Invoice).order_by(Invoice.due_date.desc(), Invoice.id.desc())).all()
    cases = db.scalars(select(LegalCase).order_by(LegalCase.case_number)).all()
    return templates.TemplateResponse(
        "invoices.html",
        {
            "request": request,
            "invoices": invoices,
            "cases": cases,
            "user": user,
            "created": request.query_params.get("created") == "1",
        },
    )


@app.post("/invoices/new")
def create_invoice(
    request: Request,
    number: str = Form(...),
    amount: float = Form(...),
    due_date: str = Form(...),
    legal_case_id: int = Form(...),
    status: str = Form("ISSUED"),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    due = parse_iso_date(due_date)
    if not due:
        return RedirectResponse("/invoices?error=date", status_code=303)

    legal_case = db.get(LegalCase, legal_case_id)
    if not legal_case:
        return RedirectResponse("/invoices?error=case", status_code=303)

    db.add(
        Invoice(
            number=number.strip(),
            amount=amount,
            due_date=due,
            status=status.strip().upper() or "ISSUED",
            legal_case_id=legal_case.id,
        )
    )
    db.add(Notification(recipient_id=user.id, title="Выставлен счет", message=f"Счет {number.strip()}"))
    log_action(db, user, "Создан счет", number.strip())
    db.commit()
    return RedirectResponse("/invoices?created=1", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    entries = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(200)).all()
    users = {item.id: item for item in db.scalars(select(User)).all()}
    return templates.TemplateResponse(
        "audit.html",
        {"request": request, "entries": entries, "users_map": users, "user": user},
    )


@app.get("/portal/intake", response_class=HTMLResponse)
def intake_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    return templates.TemplateResponse(
        "intake.html",
        {
            "request": request,
            "user": user,
            "show_chat": False,
            "show_footer": not bool(user),
            "success": request.query_params.get("success") == "1",
        },
    )


@app.post("/portal/intake")
def intake_submit(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    case_title: str = Form(...),
    category: str = Form(...),
    message: str = Form(""),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    client = Client(
        name=full_name.strip(),
        client_type="PERSON",
        email=email.strip().lower(),
        phone=phone.strip(),
        notes="Заявка через публичный портал",
    )
    db.add(client)
    db.flush()

    legal_case = LegalCase(
        case_number=next_case_number(db),
        title=case_title.strip(),
        category=category.strip() or "Общее",
        description=message.strip(),
        stage=CaseStage.NEW_REQUEST,
        priority="MEDIUM",
        opened_at=date.today(),
        deadline=None,
        client_id=client.id,
        responsible_lawyer_id=None,
    )
    db.add(legal_case)
    db.flush()

    db.add(
        CaseTask(
            legal_case_id=legal_case.id,
            title="Первичный разбор обращения",
            description="Автоматически создано из публичной заявки.",
            due_date=date.today() + timedelta(days=1),
            status=TaskStatus.TODO,
            priority="HIGH",
        )
    )

    recipients = db.scalars(select(User).where(User.role.in_([Role.ADMIN, Role.LAWYER]))).all()
    for recipient in recipients:
        db.add(
            Notification(
                recipient_id=recipient.id,
                title="Новая входящая заявка",
                message=f"{legal_case.case_number}: {legal_case.title}",
            )
        )

    log_action(db, user, "Публичная заявка", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()
    return RedirectResponse("/portal/intake?success=1", status_code=303)


@app.get("/cases/{case_id}/topsis", response_class=HTMLResponse)
def topsis_page(case_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER)).all()
    ranking = topsis_rank(legal_case.category, lawyers)
    return templates.TemplateResponse(
        "topsis.html",
        {"request": request, "legal_case": legal_case, "ranking": ranking, "user": user},
    )
