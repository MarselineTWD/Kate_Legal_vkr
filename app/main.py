from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import re

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .database import get_db
from .models import (
    AuditLog,
    CalendarEvent,
    CaseComment,
    CaseStage,
    CaseTask,
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


@app.middleware("http")
async def disable_cache_in_debug(request: Request, call_next):
    response = await call_next(request)
    if settings.debug and request.method in {"GET", "HEAD"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


STAGE_LABELS = {
    CaseStage.NEW_REQUEST: "Новая заявка",
    CaseStage.DOC_ANALYSIS: "Анализ документов",
    CaseStage.DOC_PREPARATION: "Подготовка документов",
    CaseStage.COURT: "Судебное производство",
    CaseStage.COMPLETED: "Завершено",
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
        ("суд", "Судебное производство"),
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
            role=Role.LAWYER,
        )
        db.add(new_user)
        log_action(db, new_user, "Регистрация пользователя", f"email={email}")
        db.commit()
        return RedirectResponse("/login?registered=1", status_code=303)

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

    tasks = db.scalars(select(CaseTask)).all()
    overdue = [t for t in tasks if t.due_date < today and t.status != TaskStatus.DONE]

    data = {
        "cases_count": db.scalar(select(func.count(LegalCase.id))) or 0,
        "active_cases": db.scalar(select(func.count(LegalCase.id)).where(LegalCase.stage != CaseStage.COMPLETED)) or 0,
        "clients_count": db.scalar(select(func.count(Client.id))) or 0,
        "tasks_todo": len([t for t in tasks if t.status != TaskStatus.DONE]),
        "overdue_tasks": len(overdue),
        "recent_tasks": sorted(tasks, key=lambda t: t.due_date)[:8],
        "last_notifications": db.scalars(
            select(Notification)
            .order_by(Notification.is_read.asc(), Notification.created_at.desc(), Notification.id.desc())
            .limit(5)
        ).all(),
    }
    return templates.TemplateResponse("dashboard.html", {"request": request, **data, "user": user, "status_labels": STATUS_LABELS})


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
    if from_client:
        db.add(Notification(recipient_id=user.id, title="Новое сообщение от клиента", message=client.name))
    log_action(db, user, "Сообщение в чате клиента", f"{client.name}: {text[:80]}")
    db.commit()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(build_client_chat_payload(db, client))
    return RedirectResponse("/clients", status_code=303)


@app.get("/cases", response_class=HTMLResponse)
def cases_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    cases = db.scalars(select(LegalCase).order_by(LegalCase.id.desc())).all()
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
    cases = db.scalars(select(LegalCase).order_by(LegalCase.opened_at.desc(), LegalCase.id.desc())).all()
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
            "user": user,
        },
    )


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    notifications = db.scalars(
        select(Notification)
        .order_by(Notification.is_read.asc(), Notification.created_at.desc(), Notification.id.desc())
        .limit(100)
    ).all()
    unread = len([item for item in notifications if not item.is_read])
    return templates.TemplateResponse(
        "notifications.html",
        {"request": request, "notifications": notifications, "unread": unread, "user": user},
    )


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, request: Request, db: Session = Depends(get_db)):
    _ = require_auth(request, db)
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")
    notification.is_read = True
    db.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        unread = db.scalar(select(func.count(Notification.id)).where(Notification.is_read.is_(False))) or 0
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
        deadline=date.today() + timedelta(days=7),
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
