from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import html
import io
import mimetypes
from pathlib import Path
import secrets
import re
import string
import zipfile
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select
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
    case_lawyers,
)
from .security import hash_password, verify_password
from .seed import create_schema, seed_data
from .topsis import get_default_topsis_settings, load_topsis_settings, save_topsis_settings, topsis_rank


app = FastAPI(title=settings.app_name, debug=settings.debug)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 8)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/site2static", StaticFiles(directory="app/site2"), name="site2static")
templates = Jinja2Templates(directory="app/templates")
SITE2_DIR = Path("app/site2")
UPLOADS_DIR = Path("app/uploads")
DATA_DIR = Path("app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    CaseStage.NEW_REQUEST: "Дело открыто",
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

INTAKE_STATUS_LABELS = {
    "PENDING_REVIEW": "На проверке",
    "NEEDS_CLARIFICATION": "Нужно уточнение",
    "CLOSED": "Закрыто администратором",
    "WITHDRAWN": "Отозвано клиентом",
    "APPROVED": "Принято в работу",
}

PREFERRED_CONTACT_METHOD_LABELS = {
    "CHAT": "Чат в кабинете",
    "EMAIL": "Электронная почта",
    "PHONE": "Телефон",
}


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
    user = require_staff(request, db)
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


def generate_secure_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(max(length, 12)))
        if (
            re.search(r"[A-Z]", password)
            and re.search(r"[a-z]", password)
            and re.search(r"\d", password)
        ):
            return password


def parse_iso_date(value: str) -> date | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_intake_status(value: str | None, intake_approved: bool = False) -> str:
    normalized = (value or "").strip().upper()
    if normalized in INTAKE_STATUS_LABELS:
        return normalized
    return "APPROVED" if intake_approved else "PENDING_REVIEW"


def case_status_label(legal_case: LegalCase) -> str:
    intake_status = normalize_intake_status(legal_case.intake_status, legal_case.intake_approved)
    if intake_status != "APPROVED":
        return INTAKE_STATUS_LABELS[intake_status]
    return STAGE_LABELS[legal_case.stage]


def case_visible_in_staff_cabinet(legal_case: LegalCase) -> bool:
    intake_status = normalize_intake_status(legal_case.intake_status, legal_case.intake_approved)
    return legal_case.intake_approved or intake_status == "PENDING_REVIEW"


def case_visible_in_client_chat(legal_case: LegalCase) -> bool:
    intake_status = normalize_intake_status(legal_case.intake_status, legal_case.intake_approved)
    return intake_status not in {"CLOSED", "WITHDRAWN"}


def case_assignment_condition(lawyer_id: int):
    return or_(
        LegalCase.responsible_lawyer_id == lawyer_id,
        LegalCase.lawyers.any(User.id == lawyer_id),
    )


def case_team_members(legal_case: LegalCase) -> list[User]:
    team: list[User] = []
    seen_ids: set[int] = set()

    if legal_case.responsible_lawyer and legal_case.responsible_lawyer.id not in seen_ids:
        team.append(legal_case.responsible_lawyer)
        seen_ids.add(legal_case.responsible_lawyer.id)

    for lawyer in legal_case.lawyers:
        if lawyer.id in seen_ids:
            continue
        team.append(lawyer)
        seen_ids.add(lawyer.id)

    return team


def case_team_member_ids(legal_case: LegalCase) -> set[int]:
    return {lawyer.id for lawyer in case_team_members(legal_case)}


def case_team_display(legal_case: LegalCase) -> str:
    names = [lawyer.full_name or lawyer.username for lawyer in case_team_members(legal_case)]
    return ", ".join(names) if names else "Не назначены"


def lawyer_assigned_to_case(legal_case: LegalCase, lawyer_id: int) -> bool:
    return any(lawyer.id == lawyer_id for lawyer in case_team_members(legal_case))


def user_can_access_case(user: User, legal_case: LegalCase) -> bool:
    if user.role != Role.LAWYER:
        return True
    return lawyer_assigned_to_case(legal_case, user.id)


def ensure_staff_case_access(user: User, legal_case: LegalCase, detail: str = "Нет доступа к делу") -> None:
    if not user_can_access_case(user, legal_case):
        raise HTTPException(status_code=403, detail=detail)


def resolve_case_team_lawyers(
    db: Session,
    raw_team_lawyer_ids: list[str] | None,
    responsible_lawyer_id: int | None,
    category: str,
    title: str,
    description: str,
    enforce_specialization: bool = True,
) -> tuple[list[User], str | None]:
    selected_ids: list[int] = []
    seen_ids: set[int] = set()

    if responsible_lawyer_id:
        selected_ids.append(responsible_lawyer_id)
        seen_ids.add(responsible_lawyer_id)

    for raw_value in raw_team_lawyer_ids or []:
        cleaned = (raw_value or "").strip()
        if not cleaned:
            continue
        if not cleaned.isdigit():
            return [], "lawyer"
        lawyer_id = int(cleaned)
        if lawyer_id in seen_ids:
            continue
        selected_ids.append(lawyer_id)
        seen_ids.add(lawyer_id)

    team_lawyers: list[User] = []
    for lawyer_id in selected_ids:
        lawyer = db.get(User, lawyer_id)
        if not lawyer or lawyer.role != Role.LAWYER:
            return [], "lawyer"
        if enforce_specialization and not lawyer_matches_case_specialization(
            lawyer,
            category,
            title,
            description or "",
        ):
            return [], "lawyer_specialization"
        team_lawyers.append(lawyer)

    return team_lawyers, None


def notify_case_team(
    db: Session,
    legal_case: LegalCase,
    title: str,
    message: str,
    exclude_user_id: int | None = None,
) -> None:
    for lawyer in case_team_members(legal_case):
        if exclude_user_id is not None and lawyer.id == exclude_user_id:
            continue
        db.add(Notification(recipient_id=lawyer.id, title=title, message=message))


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


def lawyer_matches_case_specialization(
    lawyer: User | None,
    case_category: str,
    case_title: str = "",
    case_description: str = "",
) -> bool:
    if not lawyer or lawyer.role != Role.LAWYER:
        return False

    specialization_raw = (lawyer.specialization or "").strip()
    if not specialization_raw:
        return False

    case_canonical = infer_case_category(case_title, case_description, case_category).strip().lower()
    case_category_text = (case_category or "").strip().lower()
    specialization_parts = [part.strip() for part in re.split(r"[,;/|]", specialization_raw) if part.strip()]
    if not specialization_parts:
        specialization_parts = [specialization_raw]

    for part in specialization_parts:
        part_lower = part.lower()
        part_canonical = infer_case_category(part, part, part).strip().lower()
        if case_canonical and part_canonical and case_canonical == part_canonical:
            return True
        if case_category_text and (case_category_text in part_lower or part_lower in case_category_text):
            return True
    return False


def filter_lawyers_by_specialization(
    lawyers: list[User],
    case_category: str,
    case_title: str = "",
    case_description: str = "",
) -> list[User]:
    return [
        lawyer
        for lawyer in lawyers
        if lawyer_matches_case_specialization(lawyer, case_category, case_title, case_description)
    ]


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
        next_steps.insert(0, "Поставить дело в приоритет и перепроверить ближайшие сроки выполнения")
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


def build_client_chat_payload(db: Session, client: Client, viewer: User | None = None) -> dict:
    client_cases = db.scalars(
        select(LegalCase).where(LegalCase.client_id == client.id).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    ).all()
    viewer_is_staff = bool(viewer and viewer.role in {Role.ADMIN, Role.LAWYER})
    visible_cases = [item for item in client_cases if not viewer_is_staff or case_visible_in_staff_cabinet(item)]
    if viewer and viewer.role == Role.LAWYER:
        visible_cases = [item for item in visible_cases if item.intake_approved and lawyer_assigned_to_case(item, viewer.id)]
    visible_cases = [item for item in visible_cases if case_visible_in_client_chat(item)]
    case_ids = [item.id for item in visible_cases]
    case_map = {item.id: item for item in visible_cases}

    client_tasks: list[CaseTask] = []
    client_invoices: list[Invoice] = []
    client_documents: list[CaseDocument] = []
    if case_ids:
        client_tasks = db.scalars(
            select(CaseTask).where(CaseTask.legal_case_id.in_(case_ids)).order_by(CaseTask.due_date, CaseTask.id)
        ).all()
        client_invoices = db.scalars(
            select(Invoice).where(Invoice.legal_case_id.in_(case_ids)).order_by(Invoice.due_date.desc(), Invoice.id.desc())
        ).all()
        client_documents = db.scalars(
            select(CaseDocument)
            .where(CaseDocument.legal_case_id.in_(case_ids))
            .order_by(CaseDocument.created_at.desc(), CaseDocument.id.desc())
        ).all()
    documents_by_case: dict[int, list[CaseDocument]] = defaultdict(list)
    for document in client_documents:
        documents_by_case[document.legal_case_id].append(document)

    today = date.today()
    active_cases = [item for item in visible_cases if item.stage != CaseStage.COMPLETED]
    active_tasks = [item for item in client_tasks if item.status != TaskStatus.DONE]
    overdue_tasks = [item for item in client_tasks if item.status != TaskStatus.DONE and item.due_date < today]
    unpaid_invoices = [item for item in client_invoices if (item.status or "").upper() != "PAID"]

    message_stmt = (
        select(ClientChatMessage)
        .where(ClientChatMessage.client_id == client.id)
        .order_by(ClientChatMessage.created_at, ClientChatMessage.id)
    )
    if viewer and viewer.role == Role.LAWYER:
        if case_ids:
            message_stmt = message_stmt.where(ClientChatMessage.legal_case_id.in_(case_ids))
        else:
            message_stmt = message_stmt.where(ClientChatMessage.id == -1)
    messages = db.scalars(message_stmt).all()
    users_map = {item.id: item for item in db.scalars(select(User)).all()}
    lawyers = [item for item in users_map.values() if item.role == Role.LAWYER]

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

    show_problem_details = len(visible_cases) <= 1
    latest_problem = ""
    if show_problem_details:
        for item in visible_cases:
            description = (item.description or "").strip()
            if description:
                latest_problem = description
                break
        if not latest_problem:
            latest_problem = (client.notes or "").strip()

    intake_requests = []
    for item in visible_cases:
        intake_status = normalize_intake_status(item.intake_status, item.intake_approved)
        recommendations = []
        eligible_lawyers = []
        if not item.intake_approved and intake_status == "PENDING_REVIEW":
            eligible_lawyers = filter_lawyers_by_specialization(
                lawyers,
                item.category or "",
                item.title,
                item.description or "",
            )
            recommendations = topsis_rank(item.category or item.title, eligible_lawyers)[:3]

        intake_requests.append(
            {
                "id": item.id,
                "case_number": item.case_number,
                "title": item.title,
                "category": item.category or "Не указана",
                "description": item.description or "",
                "opened_at": item.opened_at.strftime("%d.%m.%Y"),
                "stage": item.stage.value,
                "stage_label": STAGE_LABELS[item.stage],
                "priority": (item.priority or "MEDIUM").upper(),
                "priority_label": priority_label(item.priority),
                "intake_status": intake_status,
                "intake_status_label": INTAKE_STATUS_LABELS[intake_status],
                "status_label": case_status_label(item),
                "is_consultation": bool(item.is_consultation),
                "intake_approved": bool(item.intake_approved),
                "preferred_contact_method": (item.preferred_contact_method or "CHAT").upper(),
                "preferred_contact_method_label": PREFERRED_CONTACT_METHOD_LABELS.get(
                    (item.preferred_contact_method or "CHAT").upper(),
                    "Чат в кабинете",
                ),
                "allow_phone_contact": bool(item.allow_phone_contact),
                "admin_comment": (item.intake_admin_comment or "").strip(),
                "responsible_lawyer_name": (
                    (item.responsible_lawyer.full_name or item.responsible_lawyer.username)
                    if item.responsible_lawyer
                    else "Не назначен"
                ),
                "responsible_lawyer_specialization": (
                    (item.responsible_lawyer.specialization or "").strip()
                    if item.responsible_lawyer
                    else ""
                ),
                "team_lawyer_names": case_team_display(item),
                "team_lawyer_ids": [lawyer.id for lawyer in case_team_members(item)],
                "assigned_lawyers": [
                    {
                        "id": lawyer.id,
                        "full_name": lawyer.full_name or lawyer.username,
                        "specialization": (lawyer.specialization or "").strip(),
                        "is_responsible": bool(item.responsible_lawyer_id == lawyer.id),
                    }
                    for lawyer in case_team_members(item)
                ],
                "documents": [
                    {
                        "id": document.id,
                        "name": document.original_filename,
                        "description": document.description or "",
                        "created_at": (_to_local_naive(document.created_at) or document.created_at).strftime("%d.%m.%Y %H:%M"),
                    }
                    for document in documents_by_case.get(item.id, [])
                ],
                "all_lawyers": [
                    {
                        "id": lawyer.id,
                        "full_name": lawyer.full_name or lawyer.username,
                        "specialization": (lawyer.specialization or "").strip(),
                    }
                    for lawyer in lawyers
                ],
                "eligible_lawyers": [
                    {
                        "id": lawyer.id,
                        "full_name": lawyer.full_name or lawyer.username,
                        "specialization": (lawyer.specialization or "").strip(),
                    }
                    for lawyer in eligible_lawyers
                ],
                "recommendations": [
                    {
                        "id": recommendation.user.id,
                        "full_name": recommendation.user.full_name or recommendation.user.username,
                        "specialization": (recommendation.user.specialization or "").strip(),
                        "score": recommendation.score,
                        "score_label": f"{recommendation.score:.4f}".replace(".", ","),
                    }
                    for recommendation in recommendations
                ],
            }
        )

    return {
        "viewer": {
            "id": viewer.id if viewer else None,
            "role": viewer.role.value if viewer else "",
        },
        "client": {
            "id": client.id,
            "name": client.name,
            "client_type": (client.client_type or "PERSON").upper(),
            "organization_name": client.name if (client.client_type or "PERSON").upper() == "ORGANIZATION" else "",
            "email": client.email,
            "phone": client.phone,
            "address": client.address,
            "notes": client.notes if show_problem_details else "",
            "problem_summary": latest_problem,
            "show_problem_details": show_problem_details,
            "passport_details": (client.passport_details or "").strip(),
            "organization_requisites": "\n".join(
                part
                for part in [
                    f"ИНН: {(client.inn or '').strip()}" if (client.inn or "").strip() else "",
                    f"ОГРН: {(client.ogrn or '').strip()}" if (client.ogrn or "").strip() else "",
                    f"Банковские реквизиты: {(client.bank_details or '').strip()}" if (client.bank_details or "").strip() else "",
                    f"Иные сведения: {(client.other_details or '').strip()}" if (client.other_details or "").strip() else "",
                ]
                if part
            ),
        },
        "overview": {
            "cases_total": len(visible_cases),
            "cases_active": len(active_cases),
            "tasks_active": len(active_tasks),
            "tasks_overdue": len(overdue_tasks),
            "invoices_total": len(client_invoices),
            "invoices_unpaid": len(unpaid_invoices),
            "recent_cases": [
                {
                    "id": item.id,
                    "case_number": item.case_number,
                    "title": item.title,
                    "category": item.category,
                    "description": item.description or "",
                    "priority": (item.priority or "MEDIUM").upper(),
                    "priority_label": {"LOW": "Низкий", "MEDIUM": "Средний", "HIGH": "Высокий"}.get(
                        (item.priority or "MEDIUM").upper(),
                        (item.priority or "MEDIUM").upper(),
                    ),
                    "stage": STAGE_LABELS[item.stage],
                    "stage_value": item.stage.value,
                    "is_consultation": bool(item.is_consultation),
                    "intake_status": normalize_intake_status(item.intake_status, item.intake_approved),
                    "status_label": case_status_label(item),
                    "deadline": item.deadline.strftime("%d.%m.%Y") if item.deadline else "Без срока выполнения",
                    "deadline_input": item.deadline.isoformat() if item.deadline else "",
                    "responsible_lawyer_id": item.responsible_lawyer_id or "",
                    "responsible_lawyer_name": (
                        (item.responsible_lawyer.full_name or item.responsible_lawyer.username)
                        if item.responsible_lawyer
                        else "Не назначен"
                    ),
                    "responsible_lawyer_specialization": (
                        (item.responsible_lawyer.specialization or "").strip()
                        if item.responsible_lawyer
                        else ""
                    ),
                    "team_lawyer_names": case_team_display(item),
                    "team_lawyer_ids": [lawyer.id for lawyer in case_team_members(item)],
                }
                for item in visible_cases[:5]
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
            "recent_documents": [
                {
                    "name": item.original_filename,
                    "case_number": case_map[item.legal_case_id].case_number if item.legal_case_id in case_map else "-",
                    "created_at": (_to_local_naive(item.created_at) or item.created_at).strftime("%d.%m.%Y %H:%M"),
                }
                for item in client_documents[:6]
            ],
        },
        "actions": {
            "case_options": [
                {
                    "id": item.id,
                    "label": f"{item.case_number} — {item.title}" + (" (консультация)" if item.is_consultation else ""),
                }
                for item in visible_cases
                if item.intake_approved and not item.is_consultation
            ],
            "cases": [
                {
                    "id": item.id,
                    "case_number": item.case_number,
                    "title": item.title,
                    "category": item.category,
                    "description": item.description or "",
                    "priority": (item.priority or "MEDIUM").upper(),
                    "priority_label": {"LOW": "Низкий", "MEDIUM": "Средний", "HIGH": "Высокий"}.get(
                        (item.priority or "MEDIUM").upper(),
                        (item.priority or "MEDIUM").upper(),
                    ),
                    "deadline": item.deadline.strftime("%d.%m.%Y") if item.deadline else "Без срока выполнения",
                    "deadline_input": item.deadline.isoformat() if item.deadline else "",
                    "responsible_lawyer_id": item.responsible_lawyer_id or "",
                    "responsible_lawyer_name": (
                        (item.responsible_lawyer.full_name or item.responsible_lawyer.username)
                        if item.responsible_lawyer
                        else "Не назначен"
                    ),
                    "responsible_lawyer_specialization": (
                        (item.responsible_lawyer.specialization or "").strip()
                        if item.responsible_lawyer
                        else ""
                    ),
                    "team_lawyer_names": case_team_display(item),
                    "team_lawyer_ids": [lawyer.id for lawyer in case_team_members(item)],
                    "is_consultation": bool(item.is_consultation),
                }
                for item in visible_cases
            ],
            "intake_requests": intake_requests,
            "return_to": f"/clients?chat_client_id={client.id}",
        },
        "messages": payload_messages,
    }


def case_stage_progress(legal_case: LegalCase) -> list[dict]:
    steps = [
        (CaseStage.NEW_REQUEST, "Дело открыто"),
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
    users_map = {item.id: item for item in db.scalars(select(User)).all()}
    payload = []
    for item in raw_messages:
        author = client.name if item.is_from_client else "Юрист"
        if not item.is_from_client and item.user_id:
            author_user = users_map.get(item.user_id)
            if author_user:
                author = author_user.full_name or author_user.username
            elif lawyer and item.user_id == lawyer.id:
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
        if not case_visible_in_client_chat(legal_case):
            continue
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
    status_summary_map = {
        CaseStage.NEW_REQUEST: (
            "Ваше обращение зарегистрировано и ожидает первичной проверки.",
            "Администратор проверяет заявку и комплект документов.",
            "После проверки обращение будет передано ответственному юристу.",
        ),
        CaseStage.DOC_ANALYSIS: (
            "Юрист анализирует материалы и формирует правовую позицию.",
            "Сейчас команда изучает документы и выделяет ключевые обстоятельства по делу.",
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
        intake_status = normalize_intake_status(legal_case.intake_status, legal_case.intake_approved)
        if intake_status == "NEEDS_CLARIFICATION":
            current_state = "Обращение возвращено на доработку и ожидает ваших уточнений."
            lawyer_next_step = "После повторной отправки администратор снова проверит заявку."
            nearest_stage = "Исправьте данные обращения и отправьте его на повторную проверку."
        elif intake_status == "CLOSED":
            current_state = "Обращение закрыто администратором и не передано в работу."
            lawyer_next_step = "Текущее обращение завершено без назначения юриста."
            nearest_stage = "При необходимости вы можете создать новое обращение по этому вопросу."
        elif intake_status == "WITHDRAWN":
            current_state = "Вы отозвали обращение до рассмотрения администратором."
            lawyer_next_step = "Это обращение больше не участвует в распределении по юристам."
            nearest_stage = "При необходимости можно подать новое обращение с актуальными данными."
        else:
            current_state = "Обращение принято системой и ожидает первичной проверки."
            lawyer_next_step = "Администратор проверит заявку и назначит юридическую команду."
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
    for lawyer in legal_case.lawyers:
        if assigned_lawyer and lawyer.id == assigned_lawyer.id:
            continue
        participants.append(
            {
                "name": lawyer.full_name or lawyer.username,
                "role": "Юрист по делу",
                "meta": lawyer.email or lawyer.specialization or "Юридическая команда",
                "initials": (lawyer.full_name or lawyer.username or "Ю").strip()[:1],
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


def priority_label(priority: str | None) -> str:
    return {
        "LOW": "Низкий",
        "MEDIUM": "Средний",
        "HIGH": "Высокий",
    }.get((priority or "MEDIUM").upper(), (priority or "MEDIUM").upper())


def _find_client_user(db: Session, client: Client | None) -> User | None:
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


def notification_is_critical(notification: Notification) -> bool:
    text = f"{notification.title} {notification.message}".lower()
    critical_markers = [
        "проср",
        "крит",
        "срок",
        "засед",
        "удалил документ",
        "публичн",
        "нужен ответ",
        "high",
    ]
    return any(marker in text for marker in critical_markers)


def audit_log_is_case_related(entry: AuditLog) -> bool:
    text = f"{entry.action} {entry.details}".lower()
    markers = [
        "дел",
        "case-",
        "обращен",
        "документ",
        "задач",
        "комментар",
        "событи",
        "стад",
        "клиент подал",
        "клиент отозвал",
        "клиент обновил",
        "клиент дополнил",
    ]
    return any(marker in text for marker in markers)


def is_finance_related_text(text: str) -> bool:
    normalized = (text or "").lower()
    markers = ["счет", "оплат", "платеж", "invoice", "billing", "payment"]
    return any(marker in normalized for marker in markers)


def build_case_insights(db: Session, cases: list[LegalCase], lawyers: list[User] | None = None) -> dict[int, dict]:
    if not cases:
        return {}

    today = date.today()
    case_ids = [item.id for item in cases]
    task_items = db.scalars(select(CaseTask).where(CaseTask.legal_case_id.in_(case_ids))).all()
    document_items = db.scalars(
        select(CaseDocument).where(CaseDocument.legal_case_id.in_(case_ids)).order_by(CaseDocument.created_at.desc(), CaseDocument.id.desc())
    ).all()
    event_items = db.scalars(
        select(CalendarEvent).where(CalendarEvent.legal_case_id.in_(case_ids)).order_by(CalendarEvent.starts_at.asc(), CalendarEvent.id.asc())
    ).all()
    message_items = db.scalars(
        select(ClientChatMessage)
        .where(ClientChatMessage.legal_case_id.in_(case_ids))
        .order_by(ClientChatMessage.created_at.desc(), ClientChatMessage.id.desc())
    ).all()
    users_map = {item.id: item for item in db.scalars(select(User)).all()}
    lawyer_pool = lawyers or db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()

    tasks_by_case: dict[int, list[CaseTask]] = defaultdict(list)
    docs_by_case: dict[int, list[CaseDocument]] = defaultdict(list)
    events_by_case: dict[int, list[CalendarEvent]] = defaultdict(list)
    messages_by_case: dict[int, list[ClientChatMessage]] = defaultdict(list)

    for item in task_items:
        tasks_by_case[item.legal_case_id].append(item)
    for item in document_items:
        docs_by_case[item.legal_case_id].append(item)
    for item in event_items:
        if item.legal_case_id:
            events_by_case[item.legal_case_id].append(item)
    for item in message_items:
        if item.legal_case_id:
            messages_by_case[item.legal_case_id].append(item)

    payload: dict[int, dict] = {}

    for legal_case in cases:
        case_tasks = tasks_by_case.get(legal_case.id, [])
        case_docs = docs_by_case.get(legal_case.id, [])
        case_events = events_by_case.get(legal_case.id, [])
        case_messages = messages_by_case.get(legal_case.id, [])

        open_tasks = [item for item in case_tasks if item.status != TaskStatus.DONE]
        overdue_tasks = [item for item in open_tasks if item.due_date < today]
        next_task = min(open_tasks, key=lambda item: item.due_date) if open_tasks else None
        next_event = min(case_events, key=lambda item: item.starts_at) if case_events else None
        client_messages = [item for item in case_messages if item.is_from_client]
        last_client_message = max(client_messages, key=lambda item: item.created_at) if client_messages else None
        client_user = _find_client_user(db, legal_case.client) if legal_case.client else None
        client_docs = [
            item
            for item in case_docs
            if (
                (client_user and item.uploaded_by_user_id == client_user.id)
                or (item.uploaded_by_user_id is None and not legal_case.intake_approved)
            )
        ]
        last_client_document = max(client_docs, key=lambda item: item.created_at) if client_docs else None
        deadline_days = (legal_case.deadline - today).days if legal_case.deadline else None

        suggestion = None
        eligible_lawyers = filter_lawyers_by_specialization(
            lawyer_pool,
            legal_case.category or "",
            legal_case.title,
            legal_case.description or "",
        )
        ranking = topsis_rank(legal_case.category or legal_case.title, eligible_lawyers)[:3] if eligible_lawyers else []
        if ranking:
            top_item = ranking[0]
            suggestion = {
                "lawyer_name": top_item.user.full_name or top_item.user.username,
                "score": top_item.score,
                "summary": "Подходит по специализации, загрузке и скорости реакции.",
            }

        latest_client_action_dt = None
        latest_client_action_text = "Нет новых действий клиента"
        if last_client_message and (latest_client_action_dt is None or last_client_message.created_at > latest_client_action_dt):
            latest_client_action_dt = last_client_message.created_at
            latest_client_action_text = f"Сообщение клиента: {last_client_message.message[:90]}"
        if last_client_document and (latest_client_action_dt is None or last_client_document.created_at > latest_client_action_dt):
            latest_client_action_dt = last_client_document.created_at
            latest_client_action_text = f"Документ клиента: {last_client_document.original_filename}"

        recent_document = case_docs[0] if case_docs else None
        recent_document_dt = _to_local_naive(recent_document.created_at) if recent_document else None
        next_hearing = next(
            (item for item in sorted(case_events, key=lambda event: event.starts_at) if (item.event_type or "CUSTOM").upper() == "COURT"),
            None,
        )
        team_lawyers = case_team_members(legal_case)
        team_names = [lawyer.full_name or lawyer.username for lawyer in team_lawyers]
        team_ids = [str(lawyer.id) for lawyer in team_lawyers]

        payload[legal_case.id] = {
            "open_tasks": len(open_tasks),
            "overdue_tasks": len(overdue_tasks),
            "documents_count": len(case_docs),
            "messages_count": len(case_messages),
            "deadline_days": deadline_days,
            "next_task": next_task,
            "next_event": next_event,
            "next_hearing": next_hearing,
            "last_client_action_dt": _to_local_naive(latest_client_action_dt) if latest_client_action_dt else None,
            "last_client_action_text": latest_client_action_text,
            "recent_document_dt": recent_document_dt,
            "recent_document_name": recent_document.original_filename if recent_document else "",
            "suggestion": suggestion,
            "required_documents": detect_required_documents(legal_case, case_docs),
            "responsible_name": (
                legal_case.responsible_lawyer.full_name or legal_case.responsible_lawyer.username
                if legal_case.responsible_lawyer
                else "Не назначен"
            ),
            "responsible_specialization": (
                (legal_case.responsible_lawyer.specialization or "").strip()
                if legal_case.responsible_lawyer
                else ""
            ),
            "team_names": team_names,
            "team_label": ", ".join(team_names) if team_names else "Не назначены",
            "team_ids": team_ids,
            "team_filter": ",".join(team_ids),
        }

    return payload


def build_staff_case_detail(db: Session, legal_case: LegalCase) -> dict:
    today = date.today()
    client = legal_case.client
    assigned_lawyer = db.get(User, legal_case.responsible_lawyer_id) if legal_case.responsible_lawyer_id else None
    workspace = build_case_workspace(db, legal_case)

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
    comments = db.scalars(
        select(CaseComment).where(CaseComment.legal_case_id == legal_case.id).order_by(CaseComment.created_at.desc(), CaseComment.id.desc())
    ).all()
    client_user = _find_client_user(db, client) if client else None
    messages = build_case_message_payload(db, client, legal_case, assigned_lawyer) if client else []
    users_map = {item.id: item for item in db.scalars(select(User)).all()}
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
    team_lawyers = case_team_members(legal_case)
    eligible_lawyers = filter_lawyers_by_specialization(
        lawyers,
        legal_case.category or "",
        legal_case.title,
        legal_case.description or "",
    )
    eligible_lawyers_by_id = {lawyer.id: lawyer for lawyer in eligible_lawyers}
    for lawyer in team_lawyers:
        eligible_lawyers_by_id.setdefault(lawyer.id, lawyer)
    eligible_lawyers = list(eligible_lawyers_by_id.values())
    suggestions = topsis_rank(legal_case.category or legal_case.title, eligible_lawyers)[:3] if eligible_lawyers else []

    open_tasks = [item for item in tasks if item.status != TaskStatus.DONE]
    overdue_tasks = [item for item in open_tasks if item.due_date < today]
    today_tasks = [item for item in open_tasks if item.due_date == today]
    latest_documents = documents[:5]
    latest_messages = list(reversed(messages[-5:]))
    latest_client_message = next((item for item in reversed(messages) if item["is_from_client"]), None)
    latest_staff_message = next((item for item in reversed(messages) if not item["is_from_client"]), None)

    last_activity_candidates: list[tuple[datetime, str, str]] = []
    if latest_client_message:
        last_activity_candidates.append(
            (
                latest_client_message["created_at"],
                "Сообщение клиента",
                latest_client_message["message"][:110],
            )
        )
    if latest_staff_message:
        last_activity_candidates.append(
            (
                latest_staff_message["created_at"],
                "Ответ команды",
                latest_staff_message["message"][:110],
            )
        )
    if documents:
        document_dt = _to_local_naive(documents[0].created_at) or documents[0].created_at
        last_activity_candidates.append(
            (document_dt, "Новый документ", documents[0].original_filename)
        )
    if comments:
        comment_dt = _to_local_naive(comments[0].created_at) or comments[0].created_at
        last_activity_candidates.append(
            (comment_dt, "Комментарий по делу", comments[0].message[:110])
        )

    last_activity = None
    if last_activity_candidates:
        last_activity_dt, last_activity_label, last_activity_text = max(last_activity_candidates, key=lambda item: item[0])
        last_activity = {
            "date": last_activity_dt,
            "label": last_activity_label,
            "text": last_activity_text,
        }

    if latest_client_message and (not latest_staff_message or latest_staff_message["created_at"] < latest_client_message["created_at"]):
        communication_status = {
            "label": "Ожидает ответа",
            "description": "Последнее сообщение оставил клиент, стоит дать следующий шаг или подтверждение.",
        }
    elif latest_staff_message:
        communication_status = {
            "label": "Активная переписка",
            "description": "По делу уже есть рабочая коммуникация с клиентом и команда держит контакт.",
        }
    else:
        communication_status = {
            "label": "Переписка не начата",
            "description": "Чат по делу пока пуст. Первый контакт лучше зафиксировать в карточке дела.",
        }

    next_action = {
        "label": "Новых действий не запланировано",
        "description": "По делу пока нет ближайших задач или событий.",
    }
    if overdue_tasks:
        next_action = {
            "label": "Разобрать просроченные задачи",
            "description": f"Просрочено задач: {len(overdue_tasks)}. Это главный операционный приоритет по делу.",
        }
    elif open_tasks:
        next_task_item = min(open_tasks, key=lambda item: item.due_date)
        next_action = {
            "label": next_task_item.title,
            "description": f"Ближайшая задача до {next_task_item.due_date.strftime('%d.%m.%Y')}.",
        }
    elif events:
        next_event_item = min(events, key=lambda item: item.starts_at)
        next_event_dt = _to_local_naive(next_event_item.starts_at) or next_event_item.starts_at
        next_action = {
            "label": next_event_item.title,
            "description": f"Ближайшее событие: {next_event_dt.strftime('%d.%m.%Y %H:%M')}.",
        }

    document_rows = []
    for item in documents:
        uploader = users_map.get(item.uploaded_by_user_id) if item.uploaded_by_user_id else None
        is_client_source = bool(client_user and item.uploaded_by_user_id == client_user.id)
        source_label = "Публичная форма" if item.uploaded_by_user_id is None and not legal_case.intake_approved else (
            client.name if is_client_source and client else (
                (uploader.full_name or uploader.username) if uploader else "Юридическая команда"
            )
        )
        created_local = _to_local_naive(item.created_at) or item.created_at
        document_rows.append(
            {
                "id": item.id,
                "name": item.original_filename,
                "description": item.description or "Без комментария",
                "created_at": created_local,
                "source": source_label,
                "source_type": "client" if is_client_source else "staff",
                "ext": item.original_filename.rsplit(".", 1)[-1].upper()[:4] if "." in item.original_filename else "FILE",
            }
        )

    task_rows = []
    for item in tasks:
        assignee = users_map.get(item.assignee_id) if item.assignee_id else None
        task_rows.append(
            {
                "id": item.id,
                "title": item.title,
                "description": item.description,
                "due_date": item.due_date,
                "status": item.status,
                "status_label": STATUS_LABELS[item.status],
                "priority": item.priority,
                "priority_label": priority_label(item.priority),
                "assignee_name": (assignee.full_name or assignee.username) if assignee else "Не назначен",
                "is_overdue": item.status != TaskStatus.DONE and item.due_date < today,
            }
        )

    event_rows = []
    for item in events:
        starts_local = _to_local_naive(item.starts_at) or item.starts_at
        event_rows.append(
            {
                "title": item.title,
                "type": EVENT_TYPE_LABELS.get((item.event_type or "CUSTOM").upper(), "Событие"),
                "date": starts_local,
                "urgency": "warning" if 0 <= (starts_local.date() - today).days <= 3 else "normal",
            }
        )
    for task in open_tasks[:8]:
        event_rows.append(
            {
                "title": task.title,
                "type": "Срок задачи",
                "date": datetime.combine(task.due_date, datetime.min.time()),
                "urgency": "danger" if task.due_date < today else "warning" if (task.due_date - today).days <= 3 else "normal",
            }
        )
    event_rows.sort(key=lambda item: item["date"])

    client_actions = []
    for item in messages:
        if item["is_from_client"]:
            client_actions.append(
                {
                    "date": item["created_at"],
                    "title": "Сообщение клиента",
                    "description": item["message"],
                    "actor": item["author"],
                }
            )
    for item in document_rows:
        if item["source_type"] == "client":
            client_actions.append(
                {
                    "date": item["created_at"],
                    "title": "Клиент загрузил документ",
                    "description": item["name"],
                    "actor": item["source"],
                }
            )
    client_actions.sort(key=lambda item: item["date"], reverse=True)

    timeline = [
        {
            "date": _to_local_naive(legal_case.created_at) or datetime.combine(legal_case.opened_at, datetime.min.time()),
            "type": "Дело",
            "title": "Карточка дела создана",
            "description": f"{legal_case.case_number}: {legal_case.title}",
            "actor": client.name if client else "Система",
            "status": "Создание",
        }
    ]

    for item in document_rows[:10]:
        timeline.append(
            {
                "date": item["created_at"],
                "type": "Документ",
                "title": "Добавлен документ",
                "description": item["name"],
                "actor": item["source"],
                "status": item["description"],
            }
        )

    for item in comments[:10]:
        author = users_map.get(item.user_id) if item.user_id else None
        created_local = _to_local_naive(item.created_at) or item.created_at
        timeline.append(
            {
                "date": created_local,
                "type": "Комментарий",
                "title": "Комментарий по делу",
                "description": item.message[:120],
                "actor": (author.full_name or author.username) if author else "Система",
                "status": "Внутренний" if item.is_internal else "Открытый",
            }
        )

    for item in event_rows[:10]:
        timeline.append(
            {
                "date": item["date"],
                "type": item["type"],
                "title": item["title"],
                "description": item["type"],
                "actor": assigned_lawyer.full_name if assigned_lawyer and assigned_lawyer.full_name else "Юридическая команда",
                "status": "Событие",
            }
        )
    timeline.sort(key=lambda item: item["date"], reverse=True)

    participants = []
    if client:
        participants.append(
            {
                "name": client.name,
                "role": "Клиент",
                "meta": client.email or client.phone or "Карточка клиента",
                "initials": (client.name or "К").strip()[:1],
            }
        )
    if assigned_lawyer:
        participants.append(
            {
                "name": assigned_lawyer.full_name or assigned_lawyer.username,
                "role": "Ответственный юрист",
                "meta": assigned_lawyer.specialization or assigned_lawyer.email or "Юридическая команда",
                "initials": (assigned_lawyer.full_name or assigned_lawyer.username or "Ю").strip()[:1],
            }
        )
    for lawyer in legal_case.lawyers:
        if assigned_lawyer and lawyer.id == assigned_lawyer.id:
            continue
        participants.append(
            {
                "name": lawyer.full_name or lawyer.username,
                "role": "Участник команды",
                "meta": lawyer.specialization or lawyer.email or "Подключен к делу",
                "initials": (lawyer.full_name or lawyer.username or "Ю").strip()[:1],
            }
        )

    finances = [
        {
            "id": item.id,
            "number": item.number,
            "amount": float(item.amount),
            "due_date": item.due_date,
            "status": item.status,
        }
        for item in invoices
    ]

    return {
        "case": legal_case,
        "client": client,
        "assigned_lawyer": assigned_lawyer,
        "team_lawyers": team_lawyers,
        "team_lawyer_ids": {lawyer.id for lawyer in team_lawyers},
        "available_chat_lawyers": [lawyer for lawyer in lawyers if lawyer.id not in {item.id for item in team_lawyers}],
        "lawyers": lawyers,
        "eligible_lawyers": eligible_lawyers,
        "stage_label": STAGE_LABELS[legal_case.stage],
        "priority_label": priority_label(legal_case.priority),
        "assistant": {
            "summary": workspace["ai"]["summary"],
            "predicted_category": workspace["ai"]["predicted_category"],
            "next_steps": workspace["ai"]["next_steps"],
            "recommended_documents": workspace["ai"]["recommended_documents"],
            "signals": workspace["ai"]["signals"],
            "suggestions": suggestions,
        },
        "tasks": task_rows,
        "open_tasks": open_tasks,
        "overdue_tasks": overdue_tasks,
        "today_tasks": today_tasks,
        "documents": document_rows,
        "latest_documents": latest_documents,
        "messages": messages,
        "latest_messages": latest_messages,
        "events": event_rows,
        "client_actions": client_actions[:8],
        "participants": participants,
        "finances": finances,
        "comments": workspace["comments"],
        "communication_status": communication_status,
        "last_activity": last_activity,
        "next_action": next_action,
        "timeline": timeline[:20],
        "stage_progress": case_stage_progress(legal_case),
        "required_documents": detect_required_documents(legal_case, documents),
        "stats": {
            "tasks_total": len(tasks),
            "tasks_open": len(open_tasks),
            "documents_count": len(documents),
            "messages_count": len(messages),
            "events_count": len(events),
            "invoices_count": len(invoices),
        },
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
            "register_consent_personal_data": False,
            "register_consent_privacy_policy": False,
            "recovery_error": None,
            "recovery_email": "",
            "show_recovery": False,
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
                "register_consent_personal_data": False,
                "register_consent_privacy_policy": False,
                "recovery_error": None,
                "recovery_email": "",
                "show_recovery": False,
            },
            status_code=400,
        )
    request.session["user_id"] = user.id
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
    consent_personal_data: str = Form(""),
    consent_privacy_policy: str = Form(""),
    intake_flow: str = Form(""),
    db: Session = Depends(get_db),
):
    last_name = last_name.strip()
    first_name = first_name.strip()
    middle_name = middle_name.strip()
    phone = phone.strip()
    email = email.strip().lower()
    full_name = " ".join([last_name, first_name, middle_name]).strip()
    consent_personal_data_value = consent_personal_data.strip().lower() in {"1", "true", "yes", "on"}
    consent_privacy_policy_value = consent_privacy_policy.strip().lower() in {"1", "true", "yes", "on"}

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
    elif not consent_personal_data_value:
        error = "Нужно согласие на обработку персональных данных."
    elif not consent_privacy_policy_value:
        error = "Нужно согласие с политикой конфиденциальности."
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
            "register_consent_personal_data": consent_personal_data_value,
            "register_consent_privacy_policy": consent_privacy_policy_value,
            "recovery_error": None,
            "recovery_email": "",
            "show_recovery": False,
        },
        status_code=400,
    )


@app.post("/password/recovery", response_class=HTMLResponse)
def recover_password(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    user = find_user_by_email(db, email)
    if not EMAIL_RE.fullmatch(email):
        error = "Введите корректный email."
    elif not user:
        error = "Пользователь с таким email не найден."
    elif len(password) < 8:
        error = "Пароль должен содержать минимум 8 символов."
    elif not re.search(r"[A-ZА-ЯЁ]", password) or not re.search(r"[a-zа-яё]", password) or not re.search(r"\d", password):
        error = "Пароль должен содержать буквы верхнего и нижнего регистра и хотя бы одну цифру."
    elif password != password_confirm:
        error = "Пароли не совпадают."
    else:
        user.password_hash = hash_password(password)
        db.add(
            Notification(
                recipient_id=user.id,
                title="Пароль изменен",
                message="Пароль учетной записи был изменен через форму восстановления.",
            )
        )
        log_action(db, user, "Восстановление пароля", f"email={email}")
        db.commit()
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
                "success": "Пароль обновлен. Теперь войдите с новым паролем.",
                "login_email": email,
                "register_last_name": "",
                "register_first_name": "",
                "register_middle_name": "",
                "register_phone": "",
                "register_email": "",
                "register_consent_personal_data": False,
                "register_consent_privacy_policy": False,
                "recovery_error": None,
                "recovery_email": "",
                "show_recovery": False,
            },
        )

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
            "success": None,
            "login_email": "",
            "register_last_name": "",
            "register_first_name": "",
            "register_middle_name": "",
            "register_phone": "",
            "register_email": "",
            "register_consent_personal_data": False,
            "register_consent_privacy_policy": False,
            "recovery_error": error,
            "recovery_email": email,
            "show_recovery": True,
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
        if user.role == Role.LAWYER:
            unread_notifications = len([item for item in relevant_notifications if not item.is_read])

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

        pending_cases = [
            item
            for item in client_cases
            if not item.intake_approved and normalize_intake_status(item.intake_status, item.intake_approved) == "PENDING_REVIEW"
        ]
        if pending_cases:
            attention_items.append(f"На проверке администратора: {len(pending_cases)}")
        if overdue:
            attention_items.append(f"Есть просроченные задачи: {len(overdue)}")
        cases_without_lawyer = [
            item for item in client_cases if item.intake_approved and item.stage != CaseStage.COMPLETED and not case_team_members(item)
        ]
        if cases_without_lawyer:
            attention_items.append(f"Ожидают назначения юриста: {len(cases_without_lawyer)}")
        imminent_cases = [
            item
            for item in client_cases
            if item.deadline and item.stage != CaseStage.COMPLETED and 0 <= (item.deadline - today).days <= 5
        ]
        if imminent_cases:
            attention_items.append(f"Близкие сроки выполнения в течение 5 дней: {len(imminent_cases)}")
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
                "intake_status_labels": INTAKE_STATUS_LABELS,
                "today": today,
            },
        )
    else:
        lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
        case_stmt = select(LegalCase).where(LegalCase.intake_approved.is_(True)).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
        if user.role == Role.LAWYER:
            case_stmt = case_stmt.where(case_assignment_condition(user.id))
        staff_cases = db.scalars(case_stmt).all()
        case_ids = [item.id for item in staff_cases]

        all_case_tasks = db.scalars(select(CaseTask).where(CaseTask.legal_case_id.in_(case_ids))).all() if case_ids else []
        if user.role == Role.LAWYER:
            tasks = [item for item in all_case_tasks if item.assignee_id in {None, user.id}]
        else:
            tasks = db.scalars(select(CaseTask).order_by(CaseTask.due_date.asc(), CaseTask.id.asc())).all()

        overdue = [t for t in tasks if t.due_date < today and t.status != TaskStatus.DONE]
        today_tasks = [t for t in tasks if t.due_date == today and t.status != TaskStatus.DONE]
        cases_count = len(staff_cases)
        active_cases = len([item for item in staff_cases if item.stage != CaseStage.COMPLETED])
        if user.role == Role.LAWYER:
            clients_count = len({item.client_id for item in staff_cases if item.client_id})
        else:
            clients_count = db.scalar(select(func.count(Client.id))) or 0

        relevant_documents = db.scalars(
            select(CaseDocument)
            .where(CaseDocument.legal_case_id.in_(case_ids))
            .order_by(CaseDocument.created_at.desc(), CaseDocument.id.desc())
        ).all() if case_ids else []
        relevant_events = db.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.legal_case_id.in_(case_ids))
            .order_by(CalendarEvent.starts_at.asc(), CalendarEvent.id.asc())
        ).all() if case_ids else []
        relevant_messages = db.scalars(
            select(ClientChatMessage)
            .where(ClientChatMessage.legal_case_id.in_(case_ids))
            .order_by(ClientChatMessage.created_at.desc(), ClientChatMessage.id.desc())
        ).all() if case_ids else []
        relevant_comments = db.scalars(
            select(CaseComment)
            .where(CaseComment.legal_case_id.in_(case_ids))
            .order_by(CaseComment.created_at.desc(), CaseComment.id.desc())
        ).all() if case_ids else []
        relevant_notifications = db.scalars(
            select(Notification)
            .where(Notification.recipient_id == user.id)
            .order_by(Notification.is_read.asc(), Notification.created_at.desc(), Notification.id.desc())
            .limit(12)
        ).all()
        if user.role == Role.LAWYER:
            relevant_notifications = [
                item for item in relevant_notifications if not is_finance_related_text(f"{item.title} {item.message}")
            ]
        unread_notifications = (
            db.scalar(
                select(func.count(Notification.id))
                .where(Notification.recipient_id == user.id)
                .where(Notification.is_read.is_(False))
            )
            or 0
        )
        critical_notifications = len([item for item in relevant_notifications if notification_is_critical(item)])
        case_map = {item.id: item for item in staff_cases}
        case_insights = build_case_insights(db, staff_cases, lawyers)

        upcoming_events = []
        for task in sorted([item for item in tasks if item.status != TaskStatus.DONE], key=lambda item: item.due_date)[:8]:
            upcoming_events.append(
                {
                    "date": task.due_date,
                    "title": task.title,
                    "kind": "Задача",
                    "case": case_map.get(task.legal_case_id),
                    "status": STATUS_LABELS.get(task.status, task.status.value),
                    "urgency": "danger" if task.due_date < today else "warning" if (task.due_date - today).days <= 3 else "normal",
                }
            )
        for event in relevant_events[:8]:
            related_case = case_map.get(event.legal_case_id) if event.legal_case_id else None
            upcoming_events.append(
                {
                    "date": event.starts_at.date(),
                    "title": event.title,
                    "kind": EVENT_TYPE_LABELS.get((event.event_type or "CUSTOM").upper(), "Событие"),
                    "case": related_case,
                    "status": EVENT_TYPE_LABELS.get((event.event_type or "CUSTOM").upper(), "Событие"),
                    "urgency": "warning" if 0 <= (event.starts_at.date() - today).days <= 3 else "normal",
                }
            )
        upcoming_events.sort(key=lambda item: (item["date"], item["title"]))

        recent_case_changes = []
        for item in relevant_documents[:6]:
            legal_case = case_map.get(item.legal_case_id)
            if not legal_case:
                continue
            recent_case_changes.append(
                {
                    "date": _to_local_naive(item.created_at) or item.created_at,
                    "title": "Обновлен документ",
                    "description": item.original_filename,
                    "case": legal_case,
                    "icon": "icon-file",
                }
            )
        for item in relevant_comments[:6]:
            legal_case = case_map.get(item.legal_case_id)
            if not legal_case:
                continue
            recent_case_changes.append(
                {
                    "date": _to_local_naive(item.created_at) or item.created_at,
                    "title": "Комментарий по делу",
                    "description": item.message[:110],
                    "case": legal_case,
                    "icon": "icon-message",
                }
            )
        completed_case_numbers = {item.case_number: item for item in staff_cases if item.stage == CaseStage.COMPLETED}
        if completed_case_numbers:
            audit_logs = db.scalars(
                select(AuditLog)
                .where(AuditLog.action == "Завершено дело")
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(20)
            ).all()
            for item in audit_logs:
                case_number = (item.details or "").split(":", 1)[0].strip()
                legal_case = completed_case_numbers.get(case_number)
                if not legal_case:
                    continue
                recent_case_changes.append(
                    {
                        "date": _to_local_naive(item.created_at) or item.created_at,
                        "title": "Дело завершено",
                        "description": item.details or f"{legal_case.case_number}: {legal_case.title}",
                        "case": legal_case,
                        "icon": "icon-check-square",
                    }
                )
        recent_case_changes.sort(key=lambda item: item["date"], reverse=True)

        client_actions = []
        for item in relevant_messages:
            if not item.is_from_client:
                continue
            legal_case = case_map.get(item.legal_case_id)
            if not legal_case:
                continue
            client_actions.append(
                {
                    "date": _to_local_naive(item.created_at) or item.created_at,
                    "title": "Клиент написал в чат",
                    "description": item.message[:110],
                    "case": legal_case,
                    "icon": "icon-message",
                }
            )
        for item in relevant_documents:
            legal_case = case_map.get(item.legal_case_id)
            if not legal_case:
                continue
            client_user = _find_client_user_for_case(db, legal_case)
            if client_user and item.uploaded_by_user_id != client_user.id:
                continue
            if not client_user and item.uploaded_by_user_id is not None:
                continue
            client_actions.append(
                {
                    "date": _to_local_naive(item.created_at) or item.created_at,
                    "title": "Клиент загрузил документ",
                    "description": item.original_filename,
                    "case": legal_case,
                    "icon": "icon-file",
                }
            )
        client_actions.sort(key=lambda item: item["date"], reverse=True)

        stage_distribution = []
        for stage in CaseStage:
            count = len([item for item in staff_cases if item.stage == stage])
            share = int((count / cases_count) * 100) if cases_count else 0
            stage_distribution.append(
                {
                    "stage": stage,
                    "label": STAGE_LABELS[stage],
                    "count": count,
                    "share": share,
                }
            )

        upcoming_hearings_count = len(
            [
                item
                for item in relevant_events
                if (item.event_type or "CUSTOM").upper() == "COURT" and 0 <= (item.starts_at.date() - today).days <= 14
            ]
        )
        new_documents_count = len(
            [
                item
                for item in relevant_documents
                if (_to_local_naive(item.created_at) or item.created_at).date() >= today - timedelta(days=7)
            ]
        )
        attention_cards = []
        if overdue:
            attention_cards.append(
                {
                    "title": "Просроченные задачи",
                    "text": "Есть задачи с пропущенным сроком. Их лучше вынести в первый фокус дня.",
                    "badge": str(len(overdue)),
                }
            )
        if critical_notifications:
            attention_cards.append(
                {
                    "title": "Критичные уведомления",
                    "text": "Среди последних уведомлений есть сигналы, которые требуют реакции без откладывания.",
                    "badge": str(critical_notifications),
                }
            )

        pending_intakes = []
        intake_accepted = request.query_params.get("intake_accepted") == "1"
        if user.role == Role.ADMIN:
            pending_cases = db.scalars(
                select(LegalCase)
                .where(LegalCase.intake_approved.is_(False))
                .order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
            ).all()
            for item in pending_cases:
                intake_status = normalize_intake_status(item.intake_status, item.intake_approved)
                if intake_status != "PENDING_REVIEW":
                    continue
                eligible_lawyers = filter_lawyers_by_specialization(
                    lawyers,
                    item.category or "",
                    item.title,
                    item.description or "",
                )
                recommendations = topsis_rank(item.category or "", eligible_lawyers)[:3]
                pending_intakes.append(
                    {
                        "case": item,
                        "intake_status": intake_status,
                        "intake_status_label": INTAKE_STATUS_LABELS[intake_status],
                        "all_lawyers": lawyers,
                        "eligible_lawyers": eligible_lawyers,
                        "recommendations": recommendations,
                    }
                )
            if pending_intakes:
                attention_cards.insert(
                    0,
                    {
                        "title": "Новые заявки",
                        "text": "В очереди есть обращения, которые еще не приняты в работу и не распределены по юристам.",
                        "badge": str(len(pending_intakes)),
                    },
                )
        if not attention_cards:
            attention_cards.append(
                {
                    "title": "Ситуация стабильна",
                    "text": "Критичных отклонений по задачам и делам сейчас не обнаружено. Можно сфокусироваться на плановой работе.",
                    "badge": "OK",
                }
            )

        data = {
            "cases_count": cases_count,
            "active_cases": active_cases,
            "clients_count": clients_count,
            "tasks_todo": len([t for t in tasks if t.status != TaskStatus.DONE]),
            "overdue_tasks": len(overdue),
            "recent_tasks": sorted([item for item in tasks if item.status != TaskStatus.DONE], key=lambda item: item.due_date)[:8],
            "last_notifications": relevant_notifications[:5],
            "lawyers": lawyers,
            "pending_intakes": pending_intakes,
            "intake_accepted": intake_accepted,
            "intake_clarified": request.query_params.get("intake_clarified") == "1",
            "intake_closed": request.query_params.get("intake_closed") == "1",
            "intake_error": request.query_params.get("intake_error", "").strip(),
            "lawyer_created": request.query_params.get("lawyer_created") == "1",
            "lawyer_error": request.query_params.get("lawyer_error", "").strip(),
            "is_client_dashboard": is_client,
            "today_tasks": today_tasks[:6],
            "upcoming_events": upcoming_events[:8],
            "recent_case_changes": recent_case_changes[:8],
            "client_actions": client_actions[:8],
            "stage_distribution": stage_distribution,
            "attention_cards": attention_cards,
            "new_documents_count": new_documents_count,
            "critical_notifications": critical_notifications,
            "upcoming_hearings_count": upcoming_hearings_count,
            "unread_notifications": unread_notifications,
            "staff_cases": staff_cases,
            "case_insights": case_insights,
        }
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, **data, "user": user, "status_labels": STATUS_LABELS, "stage_labels": STAGE_LABELS, "today": today},
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


def _safe_staff_return_path(raw_path: str, fallback: str) -> str:
    candidate = (raw_path or "").strip()
    if (
        candidate.startswith("/clients")
        or candidate.startswith("/cases")
        or candidate.startswith("/app")
        or candidate.startswith("/kanban")
        or candidate.startswith("/calendar")
        or candidate.startswith("/reports")
        or candidate.startswith("/settings")
        or candidate.startswith("/notifications")
        or candidate.startswith("/tasks")
    ):
        return candidate
    return fallback


def _append_query_flag(path: str, flag: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{flag}=1"


def _remove_document_file(document: CaseDocument) -> None:
    file_path = UPLOADS_DIR / (document.stored_filename or "")
    if file_path.exists():
        try:
            file_path.unlink()
        except OSError:
            pass


def delete_case_tree(db: Session, legal_case: LegalCase) -> None:
    documents = db.scalars(select(CaseDocument).where(CaseDocument.legal_case_id == legal_case.id)).all()
    for document in documents:
        _remove_document_file(document)
        db.delete(document)

    db.execute(delete(CaseComment).where(CaseComment.legal_case_id == legal_case.id))
    db.execute(delete(CaseTask).where(CaseTask.legal_case_id == legal_case.id))
    db.execute(delete(CalendarEvent).where(CalendarEvent.legal_case_id == legal_case.id))
    db.execute(delete(Invoice).where(Invoice.legal_case_id == legal_case.id))
    db.execute(delete(ClientChatMessage).where(ClientChatMessage.legal_case_id == legal_case.id))
    db.execute(delete(case_lawyers).where(case_lawyers.c.case_id == legal_case.id))
    db.delete(legal_case)


def delete_client_tree(db: Session, client: Client) -> None:
    cases = db.scalars(select(LegalCase).where(LegalCase.client_id == client.id)).all()
    for legal_case in cases:
        delete_case_tree(db, legal_case)

    db.execute(delete(ClientChatMessage).where(ClientChatMessage.client_id == client.id))

    linked_user = db.get(User, client.user_id) if client.user_id else None
    db.delete(client)

    if linked_user and linked_user.role == Role.CLIENT:
        db.execute(delete(Notification).where(Notification.recipient_id == linked_user.id))
        logs = db.scalars(select(AuditLog).where(AuditLog.user_id == linked_user.id)).all()
        for item in logs:
            item.user_id = None
        db.delete(linked_user)


@app.post("/admin/intake/{case_id}/accept")
def accept_client_intake(
    case_id: int,
    request: Request,
    priority: str = Form("MEDIUM"),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    if legal_case.intake_approved:
        return RedirectResponse(_safe_staff_return_path(return_to, "/app?intake_accepted=1"), status_code=303)

    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER)).all()
    eligible_lawyers = filter_lawyers_by_specialization(
        lawyers,
        legal_case.category,
        legal_case.title,
        legal_case.description or "",
    )
    ranking_pool = eligible_lawyers or lawyers
    ranked = topsis_rank(legal_case.category or legal_case.title, ranking_pool) if ranking_pool else []
    if not ranked:
        target = _safe_staff_return_path(return_to, "/app?intake_error=no_lawyer")
        if "intake_error=" not in target:
            separator = "&" if "?" in target else "?"
            target = f"{target}{separator}intake_error=no_lawyer"
        return RedirectResponse(target, status_code=303)
    team_lawyers = [ranked[0].user]
    lawyer = team_lawyers[0]

    priority_value = (priority or "MEDIUM").strip().upper()
    if priority_value not in {"LOW", "MEDIUM", "HIGH"}:
        priority_value = "MEDIUM"

    legal_case.responsible_lawyer_id = lawyer.id
    legal_case.priority = priority_value
    legal_case.intake_approved = True
    legal_case.intake_status = "APPROVED"
    legal_case.intake_admin_comment = ""
    legal_case.lawyers = team_lawyers

    team_names = case_team_display(legal_case)
    notify_case_team(
        db,
        legal_case,
        "Обращение принято и назначено вам",
        f"{legal_case.case_number}: {legal_case.title}" + (" • дело-консультация" if legal_case.is_consultation else ""),
    )
    client_user = _find_client_user_for_case(db, legal_case)
    if client_user:
        db.add(
            Notification(
                recipient_id=client_user.id,
                title="Ваше обращение принято",
                message=f"{legal_case.case_number}: обращение принято в работу. Юридическая команда: {team_names}",
            )
        )

    log_action(db, admin, "Принято обращение клиента", f"{legal_case.case_number} -> {team_names}")
    db.commit()
    return RedirectResponse(_safe_staff_return_path(return_to, "/app?intake_accepted=1"), status_code=303)


@app.post("/admin/intake/{case_id}/clarify")
def request_intake_clarification(
    case_id: int,
    request: Request,
    comment: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    if legal_case.intake_approved:
        return RedirectResponse(_safe_staff_return_path(return_to, "/app?intake_error=already_approved"), status_code=303)

    admin_comment = comment.strip() or "Администратор запросил уточнение по обращению."
    legal_case.intake_status = "NEEDS_CLARIFICATION"
    legal_case.intake_admin_comment = admin_comment
    db.add(CaseComment(legal_case_id=legal_case.id, user_id=admin.id, message=f"Уточнение: {admin_comment}", is_internal=False))

    client_user = _find_client_user_for_case(db, legal_case)
    if client_user:
        db.add(
            Notification(
                recipient_id=client_user.id,
                title="Нужно уточнение по обращению",
                message=f"{legal_case.case_number}: {admin_comment}",
            )
        )

    log_action(db, admin, "Обращение отправлено на уточнение", f"{legal_case.case_number}")
    db.commit()
    return RedirectResponse(_safe_staff_return_path(return_to, "/app?intake_clarified=1"), status_code=303)


@app.post("/admin/intake/{case_id}/close")
def close_intake_request(
    case_id: int,
    request: Request,
    comment: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    if legal_case.intake_approved:
        return RedirectResponse(_safe_staff_return_path(return_to, "/app?intake_error=already_approved"), status_code=303)

    close_comment = comment.strip() or "Обращение закрыто администратором."
    legal_case.intake_status = "CLOSED"
    legal_case.intake_admin_comment = close_comment
    legal_case.stage = CaseStage.COMPLETED
    db.add(CaseComment(legal_case_id=legal_case.id, user_id=admin.id, message=f"Закрыто: {close_comment}", is_internal=False))

    client_user = _find_client_user_for_case(db, legal_case)
    if client_user:
        db.add(
            Notification(
                recipient_id=client_user.id,
                title="Обращение закрыто",
                message=f"{legal_case.case_number}: {close_comment}",
            )
        )

    log_action(db, admin, "Обращение закрыто", f"{legal_case.case_number}")
    db.commit()
    return RedirectResponse(_safe_staff_return_path(return_to, "/app?intake_closed=1"), status_code=303)


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
            "password_saved": request.query_params.get("password_saved") == "1",
            "password_error": request.query_params.get("password_error", "").strip(),
        },
    )


@app.post("/client/profile")
def client_profile_update(
    request: Request,
    name: str = Form(...),
    client_type: str = Form("PERSON"),
    email: str = Form(...),
    phone: str = Form(""),
    address: str = Form(""),
    inn: str = Form(""),
    ogrn: str = Form(""),
    bank_details: str = Form(""),
    passport_details: str = Form(""),
    other_details: str = Form(""),
    notes: str = Form(""),
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
    normalized_client_type = (client_type or "PERSON").strip().upper()
    client.client_type = normalized_client_type if normalized_client_type in {"PERSON", "ORGANIZATION"} else "PERSON"
    client.email = cleaned_email
    client.phone = phone.strip()
    client.address = address.strip()
    client.inn = re.sub(r"\D", "", inn)[:12]
    client.ogrn = re.sub(r"\D", "", ogrn)[:15]
    client.bank_details = bank_details.strip()
    client.passport_details = passport_details.strip()
    client.other_details = other_details.strip()
    client.notes = notes.strip()
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


@app.post("/client/profile/password")
def client_profile_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    user, _ = require_client_account(request, db)
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse("/client/profile?password_error=current", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse("/client/profile?password_error=short", status_code=303)
    if not re.search(r"[A-ZА-ЯЁ]", new_password) or not re.search(r"[a-zа-яё]", new_password) or not re.search(r"\d", new_password):
        return RedirectResponse("/client/profile?password_error=weak", status_code=303)
    if new_password != new_password_confirm:
        return RedirectResponse("/client/profile?password_error=mismatch", status_code=303)

    user.password_hash = hash_password(new_password)
    db.add(Notification(recipient_id=user.id, title="Пароль изменен", message="Пароль клиента успешно обновлен"))
    log_action(db, user, "Смена пароля клиента")
    db.commit()
    return RedirectResponse("/client/profile?password_saved=1", status_code=303)


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
    message: str = Form(...),
    preferred_contact_method: str = Form("CHAT"),
    allow_phone_contact: str = Form(""),
    is_consultation: str = Form(""),
    documents: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)
    problem_text = message.strip()
    if not problem_text:
        return RedirectResponse("/client/intake?error=message", status_code=303)

    preferred_method = (preferred_contact_method or "CHAT").strip().upper()
    if preferred_method not in PREFERRED_CONTACT_METHOD_LABELS:
        preferred_method = "CHAT"
    allow_phone_contact_value = allow_phone_contact.strip().lower() in {"1", "true", "yes", "on"}
    is_consultation_value = is_consultation.strip().lower() in {"1", "true", "yes", "on"}
    client_phone = (client.phone or "").strip()
    if allow_phone_contact_value and not client_phone:
        return RedirectResponse("/client/intake?error=phone_missing", status_code=303)
    if preferred_method == "PHONE" and not client_phone:
        return RedirectResponse("/client/intake?error=phone_missing", status_code=303)

    legal_case = LegalCase(
        case_number=next_case_number(db),
        title=case_title.strip(),
        category=category.strip() or "Общее",
        description=problem_text,
        stage=CaseStage.NEW_REQUEST,
        priority="MEDIUM",
        intake_approved=False,
        intake_status="PENDING_REVIEW",
        intake_admin_comment="",
        is_consultation=is_consultation_value,
        allow_phone_contact=allow_phone_contact_value,
        preferred_contact_method=preferred_method,
        opened_at=date.today(),
        deadline=None,
        client_id=client.id,
        responsible_lawyer_id=None,
    )
    client.notes = problem_text
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
                message=(
                    f"{legal_case.case_number}: {legal_case.title}"
                    + (" • консультация" if is_consultation_value else "")
                    + f" • контакт: {PREFERRED_CONTACT_METHOD_LABELS[preferred_method]}"
                ),
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
            "intake_status_labels": INTAKE_STATUS_LABELS,
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
            "intake_status_labels": INTAKE_STATUS_LABELS,
            "preferred_contact_method_labels": PREFERRED_CONTACT_METHOD_LABELS,
        },
    )


@app.post("/client/cases/{case_id}/withdraw")
def withdraw_client_case(
    case_id: int,
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=404, detail="Обращение не найдено")

    intake_status = normalize_intake_status(legal_case.intake_status, legal_case.intake_approved)
    default_redirect = f"/client/cases/{case_id}"
    redirect_base = _safe_client_return_path(return_to, default_redirect)
    if legal_case.intake_approved or intake_status != "PENDING_REVIEW":
        return RedirectResponse(_append_query_flag(redirect_base, "withdraw_forbidden"), status_code=303)

    legal_case.intake_status = "WITHDRAWN"
    legal_case.intake_admin_comment = ""

    db.add(
        CaseComment(
            legal_case_id=legal_case.id,
            user_id=user.id,
            message="Клиент отозвал обращение до рассмотрения администратором.",
            is_internal=False,
        )
    )

    admins = db.scalars(select(User).where(User.role == Role.ADMIN)).all()
    for admin in admins:
        db.add(
            Notification(
                recipient_id=admin.id,
                title="Клиент отозвал обращение",
                message=f"{legal_case.case_number}: клиент отозвал обращение до рассмотрения",
            )
        )

    db.add(
        Notification(
            recipient_id=user.id,
            title="Вы отозвали обращение",
            message=f"{legal_case.case_number}: заявка отозвана и снята с проверки администратора",
        )
    )

    log_action(db, user, "Клиент отозвал обращение", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()
    return RedirectResponse(_append_query_flag(redirect_base, "withdrawn"), status_code=303)


@app.post("/client/cases/{case_id}/edit")
def edit_client_case(
    case_id: int,
    request: Request,
    case_title: str = Form(...),
    category: str = Form(...),
    message: str = Form(...),
    preferred_contact_method: str = Form("CHAT"),
    allow_phone_contact: str = Form(""),
    is_consultation: str = Form(""),
    documents: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=404, detail="Обращение не найдено")

    intake_status = normalize_intake_status(legal_case.intake_status, legal_case.intake_approved)
    if legal_case.intake_approved or intake_status != "NEEDS_CLARIFICATION":
        return RedirectResponse(f"/client/cases/{case_id}?error=edit_forbidden", status_code=303)

    problem_text = message.strip()
    if not problem_text:
        return RedirectResponse(f"/client/cases/{case_id}?error=message", status_code=303)

    preferred_method = (preferred_contact_method or "CHAT").strip().upper()
    if preferred_method not in PREFERRED_CONTACT_METHOD_LABELS:
        preferred_method = "CHAT"
    allow_phone_contact_value = allow_phone_contact.strip().lower() in {"1", "true", "yes", "on"}
    is_consultation_value = is_consultation.strip().lower() in {"1", "true", "yes", "on"}
    client_phone = (client.phone or "").strip()
    if (allow_phone_contact_value or preferred_method == "PHONE") and not client_phone:
        return RedirectResponse(f"/client/cases/{case_id}?error=phone_missing", status_code=303)

    legal_case.title = case_title.strip()
    legal_case.category = category.strip() or "Общее"
    legal_case.description = problem_text
    legal_case.preferred_contact_method = preferred_method
    legal_case.allow_phone_contact = allow_phone_contact_value
    legal_case.is_consultation = is_consultation_value
    legal_case.intake_status = "PENDING_REVIEW"
    legal_case.intake_admin_comment = ""
    legal_case.stage = CaseStage.NEW_REQUEST
    client.notes = problem_text

    db.add(
        CaseComment(
            legal_case_id=legal_case.id,
            user_id=user.id,
            message=f"Клиент обновил обращение и повторно отправил его на проверку: {problem_text[:300]}",
            is_internal=False,
        )
    )

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
                description="Документ после доработки обращения",
            )
        )

    admins = db.scalars(select(User).where(User.role == Role.ADMIN)).all()
    for admin in admins:
        db.add(
            Notification(
                recipient_id=admin.id,
                title="Обращение обновлено клиентом",
                message=f"{legal_case.case_number}: клиент внес исправления и повторно отправил обращение на проверку",
            )
        )

    log_action(db, user, "Клиент обновил обращение", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()
    return RedirectResponse(f"/client/cases/{case_id}?edited=1", status_code=303)


@app.post("/client/cases/{case_id}/supplement")
def supplement_client_case(
    case_id: int,
    request: Request,
    message: str = Form(...),
    documents: list[UploadFile] = File(default=[]),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user, client = require_client_account(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case or legal_case.client_id != client.id:
        raise HTTPException(status_code=404, detail="Обращение не найдено")

    intake_status = normalize_intake_status(legal_case.intake_status, legal_case.intake_approved)
    if legal_case.intake_approved or intake_status != "NEEDS_CLARIFICATION":
        return RedirectResponse("/client/cases?error=supplement_forbidden", status_code=303)

    extra_text = message.strip()
    if not extra_text:
        return RedirectResponse(f"/client/cases/{case_id}?error=supplement_message", status_code=303)

    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    legal_case.description = (legal_case.description or "").strip() + f"\n\n[Дополнение клиента {timestamp}]\n{extra_text}"
    legal_case.intake_status = "PENDING_REVIEW"
    legal_case.intake_admin_comment = ""

    db.add(
        CaseComment(
            legal_case_id=legal_case.id,
            user_id=user.id,
            message=f"Клиент дополнил обращение: {extra_text[:300]}",
            is_internal=False,
        )
    )

    uploaded_count = 0
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
                description="Дополнение клиента по запросу администратора",
            )
        )
        uploaded_count += 1

    admins = db.scalars(select(User).where(User.role == Role.ADMIN)).all()
    for admin in admins:
        db.add(
            Notification(
                recipient_id=admin.id,
                title="Клиент дополнил обращение",
                message=f"{legal_case.case_number}: получено уточнение" + (f" • файлов: {uploaded_count}" if uploaded_count else ""),
            )
        )
    db.add(
        Notification(
            recipient_id=user.id,
            title="Дополнение отправлено",
            message=f"{legal_case.case_number}: уточнение направлено администратору на повторную проверку",
        )
    )
    log_action(db, user, "Клиент дополнил обращение", legal_case.case_number)
    db.commit()

    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse(f"/client/cases/{case_id}?supplemented=1", status_code=303)


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
    notify_case_team(
        db,
        legal_case,
        "Клиент добавил документы" if uploaded_count > 1 else "Клиент добавил документ",
        f"{legal_case.case_number}: {summary}",
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
    notify_case_team(
        db,
        legal_case,
        "Клиент удалил документ",
        f"{legal_case.case_number}: {document_name}",
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


@app.post("/cases/{case_id}/documents/upload")
def staff_case_documents_upload(
    case_id: int,
    request: Request,
    description: str = Form(""),
    documents: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case or not legal_case.intake_approved:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case)

    valid_documents = [item for item in documents if item and item.filename]
    if not valid_documents:
        return RedirectResponse(f"/cases/{case_id}?tab=documents&error=file#documents-panel", status_code=303)

    for document in valid_documents:
        original_name, stored_name, mime_type, file_bytes = _store_uploaded_file(document)
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

    client_user = _find_client_user_for_case(db, legal_case)
    if client_user:
        db.add(
            Notification(
                recipient_id=client_user.id,
                title="Юрист добавил документы",
                message=f"{legal_case.case_number}: загружено файлов {len(valid_documents)}",
            )
        )
    db.add(
        Notification(
            recipient_id=user.id,
            title="Документы добавлены",
            message=f"{legal_case.case_number}: {len(valid_documents)}",
        )
    )
    log_action(db, user, "Добавлены документы по делу", f"{legal_case.case_number}: {len(valid_documents)}")
    db.commit()
    return RedirectResponse(f"/cases/{case_id}?tab=documents&uploaded=1#documents-panel", status_code=303)


@app.post("/documents/{document_id}/delete")
def staff_document_delete(document_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    document = db.get(CaseDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    legal_case = db.get(LegalCase, document.legal_case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case, "Нет доступа к документу")

    document_name = document.original_filename or "документ"
    db.delete(document)
    db.add(
        Notification(
            recipient_id=user.id,
            title="Документ удален",
            message=f"{legal_case.case_number}: {document_name}",
        )
    )
    log_action(db, user, "Удален документ по делу", f"{legal_case.case_number}: {document_name}")
    db.commit()
    return RedirectResponse(f"/cases/{legal_case.id}?tab=documents&deleted=1#documents-panel", status_code=303)


@app.get("/documents/{document_id}/download")
def staff_document_download(document_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    document = db.get(CaseDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    legal_case = db.get(LegalCase, document.legal_case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case, "Нет доступа к документу")

    file_bytes, mime_type = _load_document_bytes(document)
    if file_bytes is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    headers = {"Content-Disposition": _build_content_disposition(document.original_filename or "document.bin")}
    return Response(content=file_bytes, media_type=mime_type, headers=headers)


@app.get("/documents/{document_id}/view", response_class=HTMLResponse)
def staff_document_view(document_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    document = db.get(CaseDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    legal_case = db.get(LegalCase, document.legal_case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case, "Нет доступа к документу")

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
    my_cases = [item for item in my_cases if case_visible_in_client_chat(item)]
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
    if not case_visible_in_client_chat(legal_case):
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            raise HTTPException(status_code=400, detail="Переписка для этого обращения недоступна")
        return RedirectResponse("/client/chat", status_code=303)
    if not case_team_members(legal_case):
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
    notify_case_team(
        db,
        legal_case,
        "Новое сообщение клиента",
        f"{legal_case.case_number}: {text[:120]}",
    )
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
    user = require_staff(request, db)
    stmt = select(Client)
    if q.strip():
        stmt = stmt.where(Client.name.ilike(f"%{q.strip()}%"))
    assigned_client_ids: set[int] | None = None
    if user.role == Role.LAWYER:
        assigned_client_ids = {
            item[0]
            for item in db.execute(
                select(LegalCase.client_id)
                .where(LegalCase.intake_approved.is_(True))
                .where(case_assignment_condition(user.id))
            ).all()
        }
        if assigned_client_ids:
            stmt = stmt.where(Client.id.in_(assigned_client_ids))
        else:
            stmt = stmt.where(Client.id == -1)
    clients = db.scalars(stmt.order_by(Client.id.desc())).all()
    client_ids = [item.id for item in clients]
    client_case_stmt = select(LegalCase).where(LegalCase.client_id.in_(client_ids))
    if user.role == Role.LAWYER:
        client_case_stmt = client_case_stmt.where(LegalCase.intake_approved.is_(True)).where(case_assignment_condition(user.id))
    client_cases = db.scalars(client_case_stmt).all() if client_ids else []
    client_cases = [item for item in client_cases if case_visible_in_staff_cabinet(item)]
    case_ids = [item.id for item in client_cases]
    client_documents = db.scalars(select(CaseDocument).where(CaseDocument.legal_case_id.in_(case_ids))).all() if case_ids else []
    client_message_stmt = select(ClientChatMessage).where(ClientChatMessage.client_id.in_(client_ids))
    if user.role == Role.LAWYER:
        client_message_stmt = client_message_stmt.where(ClientChatMessage.legal_case_id.in_(case_ids) if case_ids else ClientChatMessage.id == -1)
    client_messages = db.scalars(
        client_message_stmt.order_by(ClientChatMessage.created_at.desc(), ClientChatMessage.id.desc())
    ).all() if client_ids else []
    client_invoices = db.scalars(select(Invoice).where(Invoice.legal_case_id.in_(case_ids))).all() if case_ids else []

    case_map_by_client: dict[int, list[LegalCase]] = defaultdict(list)
    documents_by_case: dict[int, list[CaseDocument]] = defaultdict(list)
    messages_by_client: dict[int, list[ClientChatMessage]] = defaultdict(list)
    invoices_by_case: dict[int, list[Invoice]] = defaultdict(list)

    for item in client_cases:
        case_map_by_client[item.client_id].append(item)
    for item in client_documents:
        documents_by_case[item.legal_case_id].append(item)
    for item in client_messages:
        messages_by_client[item.client_id].append(item)
    for item in client_invoices:
        invoices_by_case[item.legal_case_id].append(item)

    client_rows = []
    for client in clients:
        rows_cases = sorted(case_map_by_client.get(client.id, []), key=lambda item: item.opened_at, reverse=True)
        rows_case_ids = [item.id for item in rows_cases]
        open_cases = [item for item in rows_cases if item.stage != CaseStage.COMPLETED]
        docs_count = sum(len(documents_by_case.get(case_id, [])) for case_id in rows_case_ids)
        unpaid_invoices = 0
        for case_id in rows_case_ids:
            unpaid_invoices += len([item for item in invoices_by_case.get(case_id, []) if (item.status or "").upper() != "PAID"])

        last_message = messages_by_client.get(client.id, [None])[0]
        last_case = rows_cases[0] if rows_cases else None
        last_message_dt = _to_local_naive(last_message.created_at) if last_message else None
        last_case_dt = datetime.combine(last_case.opened_at, datetime.min.time()) if last_case else None
        last_interaction_dt = last_message_dt or last_case_dt
        if last_message_dt and last_case_dt:
            last_interaction_dt = max(last_message_dt, last_case_dt)

        if last_message and last_interaction_dt == last_message_dt:
            last_interaction_text = f"Чат: {last_message.message[:80]}"
        elif last_case:
            last_interaction_text = f"Дело: {last_case.case_number} — {last_case.title}"
        else:
            last_interaction_text = "Еще нет активности"

        client_rows.append(
            {
                "client": client,
                "cases_total": len(rows_cases),
                "cases_active": len(open_cases),
                "documents_count": docs_count,
                "unpaid_invoices": unpaid_invoices,
                "last_interaction_dt": last_interaction_dt,
                "last_interaction_text": last_interaction_text,
                "last_case": last_case,
                "contact_line": client.email or client.phone or "Контакты не заполнены",
            }
        )

    initial_chat_client_id = None
    if chat_client_id and any(client.id == chat_client_id for client in clients):
        initial_chat_client_id = chat_client_id
    created_client_name = request.session.pop("created_client_name", "") if "created_client_name" in request.session else ""
    created_client_email = request.session.pop("created_client_email", "") if "created_client_email" in request.session else ""
    created_client_password = request.session.pop("created_client_password", "") if "created_client_password" in request.session else ""
    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "clients": clients,
            "client_rows": client_rows,
            "q": q,
            "initial_chat_client_id": initial_chat_client_id,
            "user": user,
            "created": request.query_params.get("created") == "1",
            "deleted": request.query_params.get("deleted") == "1",
            "created_client_name": created_client_name,
            "created_client_email": created_client_email,
            "created_client_password": created_client_password,
            "error": request.query_params.get("error", "").strip(),
            "lawyer_removed": request.query_params.get("lawyer_removed") == "1",
            "totals": {
                "clients": len(clients),
                "active_cases": sum(item["cases_active"] for item in client_rows),
                "open_invoices": sum(item["unpaid_invoices"] for item in client_rows),
            },
        },
    )


@app.post("/clients/new")
def create_client(
    request: Request,
    name: str = Form(...),
    client_type: str = Form("PERSON"),
    email: str = Form(...),
    phone: str = Form(""),
    address: str = Form(""),
    inn: str = Form(""),
    ogrn: str = Form(""),
    bank_details: str = Form(""),
    passport_details: str = Form(""),
    other_details: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    clean_name = name.strip()
    if len(clean_name) < 2:
        return RedirectResponse("/clients?error=invalid", status_code=303)
    cleaned_email = email.strip().lower()
    if not EMAIL_RE.fullmatch(cleaned_email):
        return RedirectResponse("/clients?error=email", status_code=303)
    if find_user_by_email(db, cleaned_email) or find_client_by_email(db, cleaned_email):
        return RedirectResponse("/clients?error=email_exists", status_code=303)

    normalized_client_type = (client_type or "PERSON").strip().upper()
    if normalized_client_type not in {"PERSON", "ORGANIZATION"}:
        normalized_client_type = "PERSON"
    generated_password = generate_secure_password(12)
    username = generate_unique_username(db, cleaned_email)

    new_user = User(
        username=username,
        full_name=clean_name,
        email=cleaned_email,
        phone=phone.strip(),
        password_hash=hash_password(generated_password),
        role=Role.CLIENT,
    )
    db.add(new_user)
    db.flush()

    new_client = Client(
        user_id=new_user.id,
        name=clean_name,
        client_type=normalized_client_type,
        email=cleaned_email,
        phone=phone.strip(),
        address=address.strip(),
        inn=re.sub(r"\D", "", inn)[:12],
        ogrn=re.sub(r"\D", "", ogrn)[:15],
        bank_details=bank_details.strip(),
        passport_details=passport_details.strip(),
        other_details=other_details.strip(),
        notes=notes.strip(),
    )
    new_client.requisites = "\n".join(
        part
        for part in [
            f"ИНН: {new_client.inn}" if new_client.inn else "",
            f"ОГРН: {new_client.ogrn}" if new_client.ogrn else "",
            f"Банковские реквизиты: {new_client.bank_details}" if new_client.bank_details else "",
            f"Паспортные данные: {new_client.passport_details}" if new_client.passport_details else "",
            f"Иные сведения: {new_client.other_details}" if new_client.other_details else "",
        ]
        if part
    )
    db.add(new_client)
    db.add(Notification(recipient_id=user.id, title="Новый клиент", message=f"Добавлен клиент: {clean_name}"))
    log_action(db, user, "Создан клиент", clean_name)
    db.commit()
    request.session["created_client_name"] = clean_name
    request.session["created_client_email"] = cleaned_email
    request.session["created_client_password"] = generated_password
    return RedirectResponse("/clients?created=1", status_code=303)


@app.post("/clients/{client_id}/delete")
def delete_client(
    client_id: int,
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    client_name = client.name
    delete_client_tree(db, client)
    log_action(db, user, "Удален клиент", client_name)
    db.commit()

    target = _safe_staff_return_path(return_to, "/clients")
    return RedirectResponse(_append_query_flag(target, "deleted"), status_code=303)


@app.get("/clients/{client_id}/chat")
def client_chat(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    if user.role == Role.LAWYER:
        accessible_case = db.scalar(
            select(LegalCase.id)
            .where(LegalCase.client_id == client.id)
            .where(LegalCase.intake_approved.is_(True))
            .where(case_assignment_condition(user.id))
        )
        if not accessible_case:
            raise HTTPException(status_code=403, detail="Нет доступа к обращениям клиента")
    return JSONResponse(build_client_chat_payload(db, client, user))


@app.post("/clients/{client_id}/chat")
def add_client_chat_message(
    client_id: int,
    request: Request,
    message: str = Form(...),
    is_from_client: str = Form("false"),
    legal_case_id: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    text = message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")

    from_client = is_from_client.strip().lower() in {"1", "true", "yes", "on"}
    case_id_value = None
    legal_case = None
    if legal_case_id.strip():
        if not legal_case_id.strip().isdigit():
            raise HTTPException(status_code=400, detail="Некорректное дело")
        case_id_value = int(legal_case_id.strip())
        legal_case = db.get(LegalCase, case_id_value)
        if not legal_case or legal_case.client_id != client.id:
            raise HTTPException(status_code=400, detail="Дело не относится к клиенту")
        ensure_staff_case_access(user, legal_case)
    elif user.role == Role.LAWYER:
        raise HTTPException(status_code=400, detail="Для сообщения юриста нужно выбрать назначенное дело")

    db.add(
        ClientChatMessage(
            client_id=client.id,
            legal_case_id=case_id_value,
            user_id=None if from_client else user.id,
            message=text,
            is_from_client=from_client,
        )
    )
    log_action(db, user, "Сообщение в чате клиента", f"{client.name}: {text[:80]}")
    db.commit()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(build_client_chat_payload(db, client, user))
    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse("/clients", status_code=303)


@app.get("/cases", response_class=HTMLResponse)
def cases_page(request: Request, client_id: int | None = None, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    stmt = select(LegalCase).where(LegalCase.intake_approved.is_(True)).order_by(LegalCase.id.desc())
    if user.role == Role.LAWYER:
        stmt = stmt.where(case_assignment_condition(user.id))
    cases = db.scalars(stmt).all()
    client_stmt = select(Client).order_by(Client.name)
    if user.role == Role.LAWYER:
        visible_client_ids = {item.client_id for item in cases}
        client_stmt = client_stmt.where(Client.id.in_(visible_client_ids) if visible_client_ids else Client.id == -1)
    clients = db.scalars(client_stmt).all()
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name)).all()
    case_insights = build_case_insights(db, cases, lawyers)
    return templates.TemplateResponse(
        "cases.html",
        {
            "request": request,
            "cases": cases,
            "case_insights": case_insights,
            "stage_labels": STAGE_LABELS,
            "stages": list(CaseStage),
            "clients": clients,
            "lawyers": lawyers,
            "generated_case_number": next_case_number(db),
            "user": user,
            "created": request.query_params.get("created") == "1",
            "deleted": request.query_params.get("deleted") == "1",
            "prefill_client_id": client_id,
            "today": date.today(),
        },
    )


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def staff_case_detail_page(case_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case or not legal_case.intake_approved:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case, "Нет доступа к карточке дела")

    detail = build_staff_case_detail(db, legal_case)
    return templates.TemplateResponse(
        "case_detail_staff.html",
        {
            "request": request,
            "user": user,
            "legal_case": legal_case,
            "detail": detail,
            "stage_labels": STAGE_LABELS,
            "status_labels": STATUS_LABELS,
            "stages": list(CaseStage),
            "today": date.today(),
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
    preferred_contact_method: str = Form("CHAT"),
    allow_phone_contact: str = Form(""),
    is_consultation: str = Form(""),
    documents: list[UploadFile] = File(default=[]),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    client = db.get(Client, client_id)
    if not client:
        return RedirectResponse("/cases?error=client", status_code=303)

    preferred_method = (preferred_contact_method or "CHAT").strip().upper()
    if preferred_method not in PREFERRED_CONTACT_METHOD_LABELS:
        preferred_method = "CHAT"
    allow_phone_contact_value = allow_phone_contact.strip().lower() in {"1", "true", "yes", "on"}
    is_consultation_value = is_consultation.strip().lower() in {"1", "true", "yes", "on"}
    client_phone = (client.phone or "").strip()
    if (allow_phone_contact_value or preferred_method == "PHONE") and not client_phone:
        return RedirectResponse("/cases?error=phone_missing", status_code=303)

    deadline_value = parse_iso_date(deadline)
    opened_at = date.today()
    number = case_number.strip() or next_case_number(db)

    legal_case = LegalCase(
        case_number=number,
        title=title.strip(),
        category=category.strip() or "Общее",
        description=description.strip(),
        stage=CaseStage.NEW_REQUEST,
        priority=priority.strip().upper() or "MEDIUM",
        intake_approved=False,
        intake_status="PENDING_REVIEW",
        intake_admin_comment="",
        opened_at=opened_at,
        deadline=deadline_value,
        client_id=client.id,
        responsible_lawyer_id=None,
        is_consultation=is_consultation_value,
        allow_phone_contact=allow_phone_contact_value,
        preferred_contact_method=preferred_method,
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
                description="Документ из формы создания обращения",
            )
        )

    admins = db.scalars(select(User).where(User.role == Role.ADMIN)).all()
    for admin in admins:
        db.add(
            Notification(
                recipient_id=admin.id,
                title="Новое обращение на проверке",
                message=(
                    f"{legal_case.case_number}: {legal_case.title}"
                    + (" • консультация" if is_consultation_value else "")
                    + f" • контакт: {PREFERRED_CONTACT_METHOD_LABELS[preferred_method]}"
                ),
            )
        )
    if user.role != Role.ADMIN:
        db.add(
            Notification(
                recipient_id=user.id,
                title="Обращение отправлено на проверку",
                message=f"{legal_case.case_number}: обращение зарегистрировано и ожидает одобрения администратора",
            )
        )
    log_action(db, user, "Создано обращение", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()
    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse("/cases?created=1", status_code=303)


@app.post("/cases/{case_id}/stage")
def update_case_stage(
    case_id: int,
    request: Request,
    stage: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case)

    try:
        new_stage = CaseStage(stage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректная стадия") from exc

    previous_stage = legal_case.stage
    legal_case.stage = new_stage
    actor = user.full_name or user.username
    if previous_stage != new_stage:
        stage_message = (
            f"{legal_case.case_number}: {legal_case.title} — стадия изменена "
            f"с «{STAGE_LABELS[previous_stage]}» на «{STAGE_LABELS[new_stage]}». Изменил: {actor}"
        )
        recipient_ids = {user.id}
        recipient_ids.update(lawyer.id for lawyer in case_team_members(legal_case))
        recipient_ids.update(db.scalars(select(User.id).where(User.role == Role.ADMIN)).all())
        if legal_case.client and legal_case.client.user_id:
            recipient_ids.add(legal_case.client.user_id)
        for recipient_id in recipient_ids:
            db.add(Notification(recipient_id=recipient_id, title="Стадия дела изменена", message=stage_message))

    if previous_stage != CaseStage.COMPLETED and legal_case.stage == CaseStage.COMPLETED:
        log_action(db, user, "Завершено дело", f"{legal_case.case_number}: {legal_case.title} — {actor}")
    elif previous_stage != new_stage:
        log_action(db, user, "Обновлена стадия дела", f"{legal_case.case_number} -> {legal_case.stage.value}")
    db.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"ok": True, "label": STAGE_LABELS[legal_case.stage]})
    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
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
    team_lawyer_ids: list[str] = Form(default=[]),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case)

    lawyer_id_value = legal_case.responsible_lawyer_id
    team_lawyers = case_team_members(legal_case)
    if user.role == Role.ADMIN:
        raw_lawyer_id = (responsible_lawyer_id or "").strip()
        parsed_lawyer_id = int(raw_lawyer_id) if raw_lawyer_id.isdigit() else None
        if raw_lawyer_id and parsed_lawyer_id is None:
            raise HTTPException(status_code=400, detail="Некорректный юрист")
        team_lawyers, team_error = resolve_case_team_lawyers(
            db,
            team_lawyer_ids,
            parsed_lawyer_id,
            category,
            title,
            description or "",
        )
        if team_error == "lawyer":
            raise HTTPException(status_code=400, detail="Некорректный юрист")
        if team_error == "lawyer_specialization":
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                raise HTTPException(status_code=400, detail="Специализация юриста не соответствует категории дела")
            target = return_to.strip() or f"/cases/{case_id}?error=lawyer_specialization"
            if target.startswith("/"):
                separator = "&" if "?" in target else "?"
                if "error=" not in target:
                    target = f"{target}{separator}error=lawyer_specialization"
            return RedirectResponse(target, status_code=303)
        lawyer_id_value = parsed_lawyer_id

    deadline_value = parse_iso_date(deadline) if deadline else None
    legal_case.title = title.strip()
    legal_case.category = category.strip()
    legal_case.description = description.strip()
    legal_case.deadline = deadline_value
    legal_case.priority = priority.strip().upper() or "MEDIUM"
    legal_case.responsible_lawyer_id = lawyer_id_value

    if user.role == Role.ADMIN:
        legal_case.lawyers = team_lawyers

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
                    "deadline": legal_case.deadline.strftime("%d.%m.%Y") if legal_case.deadline else "Без срока выполнения",
                    "deadline_input": legal_case.deadline.isoformat() if legal_case.deadline else "",
                    "responsible_lawyer_id": legal_case.responsible_lawyer_id or "",
                    "responsible_lawyer_name": (
                        (assignee.full_name or assignee.username) if assignee else "Не назначен"
                    ),
                    "team_lawyer_names": case_team_display(legal_case),
                },
            }
        )
    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse("/kanban", status_code=303)


@app.post("/cases/{case_id}/lawyers/{lawyer_id}/remove")
def remove_case_lawyer(
    case_id: int,
    lawyer_id: int,
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")

    team_members = case_team_members(legal_case)
    lawyer = next((item for item in team_members if item.id == lawyer_id), None)
    if not lawyer:
        target = _safe_staff_return_path(return_to, "/clients")
        return RedirectResponse(target, status_code=303)

    remaining = [item for item in team_members if item.id != lawyer_id]
    if legal_case.responsible_lawyer_id == lawyer_id:
        legal_case.responsible_lawyer_id = remaining[0].id if remaining else None

    assigned_relation = next((item for item in legal_case.lawyers if item.id == lawyer_id), None)
    if assigned_relation:
        legal_case.lawyers.remove(assigned_relation)

    db.add(
        Notification(
            recipient_id=lawyer.id,
            title="Вы исключены из дела",
            message=f"{legal_case.case_number}: {legal_case.title}",
        )
    )
    log_action(db, user, "Юрист удален из дела", f"{legal_case.case_number}: {lawyer.full_name or lawyer.username}")
    db.commit()

    target = _safe_staff_return_path(return_to, "/clients")
    return RedirectResponse(_append_query_flag(target, "lawyer_removed"), status_code=303)


@app.post("/cases/{case_id}/delete")
def delete_case(
    case_id: int,
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")

    summary = f"{legal_case.case_number}: {legal_case.title}"
    delete_case_tree(db, legal_case)
    log_action(db, user, "Удалено дело", summary)
    db.commit()

    target = _safe_staff_return_path(return_to, "/cases")
    return RedirectResponse(_append_query_flag(target, "deleted"), status_code=303)


@app.post("/cases/{case_id}/chat-lawyers/add")
def add_case_chat_lawyer(
    case_id: int,
    request: Request,
    lawyer_id: int = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case)

    lawyer = db.get(User, lawyer_id)
    if not lawyer or lawyer.role != Role.LAWYER:
        raise HTTPException(status_code=400, detail="Юрист не найден")

    if not any(item.id == lawyer.id for item in case_team_members(legal_case)):
        legal_case.lawyers.append(lawyer)
        if not legal_case.responsible_lawyer_id:
            legal_case.responsible_lawyer_id = lawyer.id
        db.add(
            Notification(
                recipient_id=lawyer.id,
                title="Вы подключены к чату дела",
                message=f"{legal_case.case_number}: {legal_case.title}",
            )
        )
        log_action(db, user, "Юрист подключен к чату дела", f"{legal_case.case_number}: {lawyer.full_name or lawyer.username}")
        db.commit()

    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse(f"/cases/{case_id}?tab=communication#communication-panel", status_code=303)


@app.post("/cases/{case_id}/chat-lawyers/{lawyer_id}/remove")
def remove_case_chat_lawyer(
    case_id: int,
    lawyer_id: int,
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case)

    if legal_case.responsible_lawyer_id == lawyer_id:
        raise HTTPException(status_code=400, detail="Ответственного юриста нельзя удалить из чата")
    if user.role == Role.LAWYER and user.id == lawyer_id:
        raise HTTPException(status_code=400, detail="Нельзя удалить себя из чата")

    lawyer = next((item for item in legal_case.lawyers if item.id == lawyer_id), None)
    if lawyer:
        legal_case.lawyers.remove(lawyer)
        db.add(
            Notification(
                recipient_id=lawyer.id,
                title="Вы отключены от чата дела",
                message=f"{legal_case.case_number}: {legal_case.title}",
            )
        )
        log_action(db, user, "Юрист отключен от чата дела", f"{legal_case.case_number}: {lawyer.full_name or lawyer.username}")
        db.commit()

    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse(f"/cases/{case_id}?tab=communication#communication-panel", status_code=303)


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    if user.role == Role.ADMIN:
        raise HTTPException(status_code=404, detail="Раздел задач доступен в карточках дел")
    case_stmt = select(LegalCase).where(LegalCase.intake_approved.is_(True)).order_by(LegalCase.case_number)
    if user.role == Role.LAWYER:
        case_stmt = case_stmt.where(case_assignment_condition(user.id))
    cases = db.scalars(case_stmt).all()
    cases = [item for item in cases if not item.is_consultation]
    case_ids = [item.id for item in cases]
    task_stmt = select(CaseTask).order_by(CaseTask.due_date, CaseTask.id.desc())
    if case_ids:
        task_stmt = task_stmt.where(CaseTask.legal_case_id.in_(case_ids))
    else:
        task_stmt = task_stmt.where(CaseTask.id == -1)
    if user.role == Role.LAWYER:
        task_stmt = task_stmt.where(or_(CaseTask.assignee_id == user.id, CaseTask.assignee_id.is_(None)))
    tasks = db.scalars(task_stmt).all()
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
            "deleted": request.query_params.get("deleted") == "1",
            "today": date.today(),
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
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    task_date = parse_iso_date(due_date)
    if not task_date:
        return RedirectResponse("/tasks?error=date", status_code=303)

    legal_case = db.get(LegalCase, legal_case_id)
    if not legal_case or not legal_case.intake_approved:
        return RedirectResponse("/tasks?error=case", status_code=303)
    ensure_staff_case_access(user, legal_case)
    if legal_case.is_consultation:
        target = return_to.strip() or "/tasks?error=consultation"
        return RedirectResponse(target, status_code=303)

    assignee_id_value = assignee_id
    if assignee_id_value:
        assignee = db.get(User, assignee_id_value)
        if not assignee or assignee.role != Role.LAWYER:
            target = return_to.strip() or "/tasks?error=lawyer"
            return RedirectResponse(target, status_code=303)
        if assignee.id not in case_team_member_ids(legal_case):
            if user.role == Role.ADMIN:
                legal_case.lawyers.append(assignee)
            else:
                target = return_to.strip() or "/tasks?error=lawyer"
                return RedirectResponse(target, status_code=303)

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
        assignee_id=assignee_id_value,
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
    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse("/tasks?created=1", status_code=303)


@app.post("/tasks/{task_id}/status")
def update_task_status(
    task_id: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    task = db.get(CaseTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.legal_case:
        ensure_staff_case_access(user, task.legal_case)
    if user.role == Role.LAWYER and task.assignee_id not in {None, user.id}:
        raise HTTPException(status_code=403, detail="Нет доступа к задаче")

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


@app.post("/tasks/{task_id}/delete")
def delete_task(
    task_id: int,
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    task = db.get(CaseTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.legal_case:
        ensure_staff_case_access(user, task.legal_case)
    if user.role == Role.LAWYER and task.assignee_id not in {None, user.id}:
        raise HTTPException(status_code=403, detail="Нет доступа к задаче")

    task_title = task.title
    db.delete(task)
    log_action(db, user, "Удалена задача", task_title)
    db.commit()

    target = _safe_staff_return_path(return_to, "/tasks")
    return RedirectResponse(_append_query_flag(target, "deleted"), status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, lawyer_id: int | None = None, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
    selected_lawyer = None

    if user.role == Role.LAWYER:
        selected_lawyer = user
    elif lawyers:
        selected_lawyer = next((item for item in lawyers if item.id == lawyer_id), lawyers[0])

    legal_case_stmt = select(LegalCase).where(LegalCase.intake_approved.is_(True)).order_by(LegalCase.case_number)
    if selected_lawyer:
        legal_case_stmt = legal_case_stmt.where(case_assignment_condition(selected_lawyer.id))
    legal_cases = db.scalars(legal_case_stmt).all()
    case_ids = [item.id for item in legal_cases]

    stmt = select(CaseTask).order_by(CaseTask.due_date, CaseTask.id)
    if case_ids:
        stmt = stmt.where(CaseTask.legal_case_id.in_(case_ids))
    else:
        stmt = stmt.where(CaseTask.id == -1)
    if selected_lawyer:
        stmt = stmt.where(CaseTask.assignee_id == selected_lawyer.id)
    tasks = db.scalars(stmt).all()
    case_map = {item.id: item for item in legal_cases}
    event_stmt = select(CalendarEvent).order_by(CalendarEvent.starts_at, CalendarEvent.id)
    if case_ids:
        event_stmt = event_stmt.where(
            or_(
                CalendarEvent.legal_case_id.in_(case_ids),
                CalendarEvent.legal_case_id.is_(None),
            )
        )
    else:
        event_stmt = event_stmt.where(CalendarEvent.legal_case_id.is_(None))
    calendar_events = db.scalars(event_stmt).all()

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
            "today": date.today(),
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
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
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
        legal_case = db.get(LegalCase, case_id_value)
        if not legal_case:
            redirect = "/calendar?error=case"
            if lawyer_id.strip().isdigit():
                redirect = f"{redirect}&lawyer_id={lawyer_id.strip()}"
            return RedirectResponse(redirect, status_code=303)
        ensure_staff_case_access(user, legal_case)

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
    if return_to.strip():
        redirect = return_to.strip()
    return RedirectResponse(redirect, status_code=303)


@app.get("/cases/{case_id}/workspace")
def case_workspace(case_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case)
    return JSONResponse(build_case_workspace(db, legal_case))


@app.post("/cases/{case_id}/comments")
def add_case_comment(
    case_id: int,
    request: Request,
    message: str = Form(...),
    is_internal: str = Form("false"),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case)

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
    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse("/kanban", status_code=303)


@app.get("/kanban", response_class=HTMLResponse)
def kanban_page(request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    today = datetime.now().date()
    stmt = select(LegalCase).where(LegalCase.intake_approved.is_(True)).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())
    if user.role == Role.LAWYER:
        stmt = stmt.where(case_assignment_condition(user.id))
    cases = db.scalars(stmt).all()
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
    case_insights = build_case_insights(db, cases, lawyers)
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
            "case_insights": case_insights,
            "today": today,
            "user": user,
        },
    )


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    today = date.today()
    all_cases = db.scalars(select(LegalCase).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())).all()
    approved_cases = [item for item in all_cases if item.intake_approved]
    open_cases = [item for item in approved_cases if item.stage != CaseStage.COMPLETED]
    completed_cases = [item for item in approved_cases if item.stage == CaseStage.COMPLETED]
    tasks = db.scalars(select(CaseTask).order_by(CaseTask.due_date.asc(), CaseTask.id.asc())).all()
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
    case_insights = build_case_insights(db, approved_cases, lawyers)

    all_tasks_open = [item for item in tasks if item.status != TaskStatus.DONE]
    overdue_tasks = [item for item in all_tasks_open if item.due_date < today]
    case_ids = [item.id for item in approved_cases]
    documents = db.scalars(select(CaseDocument).where(CaseDocument.legal_case_id.in_(case_ids))).all() if case_ids else []
    comments = db.scalars(select(CaseComment).where(CaseComment.legal_case_id.in_(case_ids))).all() if case_ids else []
    messages = db.scalars(select(ClientChatMessage).where(ClientChatMessage.legal_case_id.in_(case_ids))).all() if case_ids else []
    audit_logs = db.scalars(
        select(AuditLog)
        .where(AuditLog.action == "Завершено дело")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    ).all()

    documents_by_case: dict[int, list[CaseDocument]] = defaultdict(list)
    comments_by_case: dict[int, list[CaseComment]] = defaultdict(list)
    messages_by_case: dict[int, list[ClientChatMessage]] = defaultdict(list)
    tasks_by_case: dict[int, list[CaseTask]] = defaultdict(list)
    for item in documents:
        documents_by_case[item.legal_case_id].append(item)
    for item in comments:
        comments_by_case[item.legal_case_id].append(item)
    for item in messages:
        if item.legal_case_id:
            messages_by_case[item.legal_case_id].append(item)
    for item in tasks:
        tasks_by_case[item.legal_case_id].append(item)

    completion_log_by_case_number: dict[str, AuditLog] = {}
    for item in audit_logs:
        case_number = (item.details or "").split(":", 1)[0].strip()
        if case_number and case_number not in completion_log_by_case_number:
            completion_log_by_case_number[case_number] = item

    completion_days: list[int] = []
    first_action_hours: list[float] = []
    for legal_case in approved_cases:
        completion_log = completion_log_by_case_number.get(legal_case.case_number)
        if completion_log:
            completion_dt = (_to_local_naive(completion_log.created_at) or completion_log.created_at).date()
            completion_days.append(max((completion_dt - legal_case.opened_at).days, 0))

        first_action_candidates: list[datetime] = []
        first_action_candidates.extend(
            (_to_local_naive(item.created_at) or item.created_at)
            for item in comments_by_case.get(legal_case.id, [])
        )
        first_action_candidates.extend(
            (_to_local_naive(item.created_at) or item.created_at)
            for item in documents_by_case.get(legal_case.id, [])
        )
        first_action_candidates.extend(
            (_to_local_naive(item.created_at) or item.created_at)
            for item in messages_by_case.get(legal_case.id, [])
            if not item.is_from_client
        )
        if first_action_candidates:
            first_action = min(first_action_candidates)
            created_dt = _to_local_naive(legal_case.created_at) or legal_case.created_at
            first_action_hours.append(max((first_action - created_dt).total_seconds() / 3600, 0))

    stage_distribution = []
    total_approved = len(approved_cases) or 1
    for stage in CaseStage:
        count = len([item for item in approved_cases if item.stage == stage])
        stage_distribution.append(
            {
                "label": STAGE_LABELS[stage],
                "count": count,
                "share": round((count / total_approved) * 100, 1),
            }
        )

    category_distribution = []
    for category, count in Counter((item.category or "Без категории") for item in approved_cases).most_common(8):
        category_distribution.append(
            {
                "label": category,
                "count": count,
                "share": round((count / total_approved) * 100, 1),
            }
        )

    priority_distribution = []
    priority_labels = {"LOW": "Низкий", "MEDIUM": "Средний", "HIGH": "Высокий"}
    for key in ["HIGH", "MEDIUM", "LOW"]:
        count = len([item for item in approved_cases if (item.priority or "MEDIUM").upper() == key])
        priority_distribution.append(
            {
                "label": priority_labels[key],
                "count": count,
                "share": round((count / total_approved) * 100, 1),
            }
        )

    month_labels = []
    completion_by_month = Counter()
    month_start = date(today.year, today.month, 1)
    for offset in range(5, -1, -1):
        current_month = (month_start.replace(day=15) - timedelta(days=offset * 31)).replace(day=1)
        month_key = current_month.strftime("%Y-%m")
        month_labels.append((month_key, current_month.strftime("%m.%Y")))
    for item in audit_logs:
        created_local = _to_local_naive(item.created_at) or item.created_at
        completion_by_month[created_local.strftime("%Y-%m")] += 1
    completion_dynamics = [
        {"label": label, "count": completion_by_month.get(key, 0)}
        for key, label in month_labels
    ]
    completion_max = max((item["count"] for item in completion_dynamics), default=0)

    first_case_by_client: dict[int, date] = {}
    for legal_case in approved_cases:
        first_case_by_client.setdefault(legal_case.client_id, legal_case.opened_at)
        if legal_case.opened_at < first_case_by_client[legal_case.client_id]:
            first_case_by_client[legal_case.client_id] = legal_case.opened_at
    new_clients_count = len([item for item in first_case_by_client.values() if item >= today - timedelta(days=30)])

    lawyer_rows = []
    for lawyer in lawyers:
        lawyer_cases = [item for item in approved_cases if lawyer_assigned_to_case(item, lawyer.id)]
        lawyer_open_cases = [item for item in lawyer_cases if item.stage != CaseStage.COMPLETED]
        lawyer_completed = [item for item in lawyer_cases if item.stage == CaseStage.COMPLETED]
        lawyer_tasks = [item for item in tasks if item.assignee_id == lawyer.id]
        lawyer_overdue = [item for item in lawyer_tasks if item.status != TaskStatus.DONE and item.due_date < today]
        lawyer_rows.append(
            {
                "name": lawyer.full_name or lawyer.username,
                "specialization": lawyer.specialization or "Общая практика",
                "active_cases": len(lawyer_open_cases),
                "completed_cases": len(lawyer_completed),
                "open_tasks": len([item for item in lawyer_tasks if item.status != TaskStatus.DONE]),
                "overdue_tasks": len(lawyer_overdue),
                "deadline_success_rate": round(float(lawyer.deadline_success_rate or 0), 1),
                "current_load": lawyer.current_load,
                "efficiency": round((len(lawyer_completed) / len(lawyer_cases)) * 100, 1) if lawyer_cases else 0.0,
            }
        )
    lawyer_rows.sort(key=lambda item: (-item["active_cases"], -item["open_tasks"], item["name"]))

    return templates.TemplateResponse(
        "reports.html",
        {
            "request": request,
            "user": user,
            "today": today,
            "summary": {
                "total_cases": len(all_cases),
                "approved_cases": len(approved_cases),
                "requests_total": len([item for item in all_cases if not item.intake_approved]),
                "active_cases": len(open_cases),
                "closed_cases": len(completed_cases),
                "overdue_tasks": len(overdue_tasks),
                "avg_case_days": round(sum(completion_days) / len(completion_days), 1) if completion_days else 0,
                "avg_first_action_hours": round(sum(first_action_hours) / len(first_action_hours), 1) if first_action_hours else 0,
                "new_clients_count": new_clients_count,
                "success_rate": round((len(completed_cases) / len(approved_cases)) * 100, 1) if approved_cases else 0,
                "documents_count": len(documents),
            },
            "stage_distribution": stage_distribution,
            "category_distribution": category_distribution,
            "priority_distribution": priority_distribution,
            "completion_dynamics": completion_dynamics,
            "completion_max": completion_max,
            "lawyer_rows": lawyer_rows,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, preview_category: str = "Договорная работа", db: Session = Depends(get_db)):
    user = require_admin(request, db)
    topsis_settings = load_topsis_settings()
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name, User.username)).all()
    preview_ranking = topsis_rank(preview_category.strip() or "Договорная работа", lawyers)[:5]
    impact_rows = [
        {
            "label": item["label"],
            "description": item["description"],
            "mode_label": "Повышает итоговый балл" if item["mode"] == "benefit" else "Сдерживает итоговый балл",
            "share": round(item["normalized_weight"] * 100, 1),
            "enabled": item["enabled"],
        }
        for item in topsis_settings["criteria"]
    ]
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "today": date.today(),
            "saved": request.query_params.get("saved") == "1",
            "preview_category": preview_category.strip() or "Договорная работа",
            "topsis_settings": topsis_settings,
            "impact_rows": impact_rows,
            "preview_ranking": preview_ranking,
        },
    )


@app.post("/settings/topsis")
def update_topsis_settings_route(
    request: Request,
    preview_category: str = Form("Договорная работа"),
    spec_weight: str = Form("0.30"),
    spec_enabled: str = Form("0"),
    load_weight: str = Form("0.20"),
    load_enabled: str = Form("0"),
    exp_weight: str = Form("0.20"),
    exp_enabled: str = Form("0"),
    avg_days_weight: str = Form("0.15"),
    avg_days_enabled: str = Form("0"),
    deadline_weight: str = Form("0.15"),
    deadline_enabled: str = Form("0"),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    save_topsis_settings(
        [
            {"id": "spec", "weight": spec_weight, "enabled": spec_enabled.lower() in {"1", "true", "on", "yes"}},
            {"id": "load", "weight": load_weight, "enabled": load_enabled.lower() in {"1", "true", "on", "yes"}},
            {"id": "exp", "weight": exp_weight, "enabled": exp_enabled.lower() in {"1", "true", "on", "yes"}},
            {"id": "avg_days", "weight": avg_days_weight, "enabled": avg_days_enabled.lower() in {"1", "true", "on", "yes"}},
            {"id": "deadline", "weight": deadline_weight, "enabled": deadline_enabled.lower() in {"1", "true", "on", "yes"}},
        ]
    )
    log_action(db, user, "Обновлены настройки TOPSIS", "Администратор изменил критерии и веса ранжирования")
    db.commit()
    return RedirectResponse(
        f"/settings?saved=1&preview_category={quote(preview_category.strip() or 'Договорная работа')}",
        status_code=303,
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
    if user.role == Role.LAWYER:
        notifications = [item for item in notifications if not is_finance_related_text(f"{item.title} {item.message}")]
    feed_items = [
        {
            "kind": "notification",
            "id": item.id,
            "title": item.title,
            "message": item.message,
            "created_at": item.created_at,
            "is_read": item.is_read,
            "is_critical": notification_is_critical(item),
            "action_url": f"/notifications/{item.id}/read",
            "source": "Уведомление",
        }
        for item in notifications
    ]
    if user.role == Role.ADMIN:
        audit_entries = [
            item
            for item in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(200)).all()
            if audit_log_is_case_related(item)
        ][:80]
        users_map = {item.id: item for item in db.scalars(select(User)).all()}
        for item in audit_entries:
            actor = users_map.get(item.user_id) if item.user_id else None
            actor_name = (actor.full_name or actor.username) if actor else "Система"
            feed_items.append(
                {
                    "kind": "audit",
                    "id": item.id,
                    "title": item.action,
                    "message": item.details or "Событие по делу или обращению",
                    "created_at": item.created_at,
                    "is_read": True,
                    "is_critical": False,
                    "action_url": "",
                    "source": actor_name,
                }
            )
    feed_items.sort(key=lambda item: item["created_at"], reverse=True)
    unread = len([item for item in notifications if not item.is_read])
    critical_count = len([item for item in notifications if notification_is_critical(item)])
    return templates.TemplateResponse(
        "notifications.html",
        {
            "request": request,
            "notifications": notifications,
            "feed_items": feed_items[:160],
            "unread": unread,
            "critical_count": critical_count,
            "user": user,
            "today": date.today(),
        },
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
    if user.role == Role.LAWYER:
        raise HTTPException(status_code=404, detail="Раздел недоступен")
    invoices = db.scalars(select(Invoice).order_by(Invoice.due_date.desc(), Invoice.id.desc())).all()
    cases = db.scalars(select(LegalCase).order_by(LegalCase.case_number)).all()
    return templates.TemplateResponse(
        "invoices.html",
        {
            "request": request,
            "invoices": invoices,
            "cases": cases,
            "case_map": {item.id: item for item in cases},
            "user": user,
            "created": request.query_params.get("created") == "1",
            "today": date.today(),
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
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    if user.role == Role.LAWYER:
        raise HTTPException(status_code=404, detail="Раздел недоступен")
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
    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse("/invoices?created=1", status_code=303)


@app.post("/invoices/{invoice_id}/status")
def update_invoice_status(
    invoice_id: int,
    request: Request,
    status: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    if user.role == Role.LAWYER:
        raise HTTPException(status_code=404, detail="Раздел недоступен")
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Счет не найден")

    invoice.status = (status or "ISSUED").strip().upper()
    db.add(
        Notification(
            recipient_id=user.id,
            title="Статус счета обновлен",
            message=f"{invoice.number}: {invoice.status}",
        )
    )
    log_action(db, user, "Обновлен статус счета", f"{invoice.number} -> {invoice.status}")
    db.commit()
    if return_to.strip():
        return RedirectResponse(return_to.strip(), status_code=303)
    return RedirectResponse("/invoices", status_code=303)


@app.get("/invoices/{invoice_id}/download")
def download_invoice(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    if user.role == Role.LAWYER:
        raise HTTPException(status_code=404, detail="Раздел недоступен")
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Счет не найден")

    legal_case = db.get(LegalCase, invoice.legal_case_id)
    client = legal_case.client if legal_case else None
    text = "\n".join(
        [
            "ЮрНавигатор",
            "Счет",
            "",
            f"Номер: {invoice.number}",
            f"Сумма: {float(invoice.amount):.2f} ₽",
            f"Срок оплаты: {invoice.due_date.strftime('%d.%m.%Y')}",
            f"Статус: {invoice.status}",
            f"Дело: {legal_case.case_number if legal_case else '-'}",
            f"Тема дела: {legal_case.title if legal_case else '-'}",
            f"Клиент: {client.name if client else '-'}",
        ]
    )
    filename = f"invoice-{invoice.number}.txt"
    headers = {"Content-Disposition": _build_content_disposition(filename)}
    return Response(content=text.encode("utf-8"), media_type="text/plain; charset=utf-8", headers=headers)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    if user.role == Role.LAWYER:
        raise HTTPException(status_code=404, detail="Раздел недоступен")
    return RedirectResponse("/notifications", status_code=303)
    entries = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(200)).all()
    users = {item.id: item for item in db.scalars(select(User)).all()}
    action_types = sorted({item.action for item in entries})
    return templates.TemplateResponse(
        "audit.html",
        {
            "request": request,
            "entries": entries,
            "users_map": users,
            "action_types": action_types,
            "user": user,
            "today": date.today(),
        },
    )


@app.get("/portal/intake", response_class=HTMLResponse)
def intake_page(request: Request, db: Session = Depends(get_db)):
    current_user(request, db)
    raise HTTPException(status_code=404, detail="Раздел публичной заявки отключен")


@app.post("/portal/intake")
def intake_submit(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    case_title: str = Form(...),
    category: str = Form(...),
    message: str = Form(""),
    documents: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    current_user(request, db)
    raise HTTPException(status_code=404, detail="Раздел публичной заявки отключен")


@app.get("/cases/{case_id}/topsis", response_class=HTMLResponse)
def topsis_page(case_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_staff(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    ensure_staff_case_access(user, legal_case)
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER)).all()
    ranking = topsis_rank(legal_case.category, lawyers)
    return templates.TemplateResponse(
        "topsis.html",
        {"request": request, "legal_case": legal_case, "ranking": ranking, "user": user},
    )
