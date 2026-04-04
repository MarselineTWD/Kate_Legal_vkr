from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import mimetypes
from pathlib import Path
import re

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
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
    CaseDocument,
    CaseStage,
    CaseMessage,
    CaseTask,
    Client,
    DocumentSource,
    DocumentTemplate,
    DocumentVersion,
    Invoice,
    LegalCase,
    MessageVisibility,
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
DOCUMENTS_DIR = Path("storage/documents")


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

VISIBILITY_LABELS = {
    MessageVisibility.PUBLIC: "Переписка с клиентом",
    MessageVisibility.INTERNAL: "Внутренний комментарий",
}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё\-\s]{2,60}$")
PHONE_RE = re.compile(r"^\+?[0-9\s()\-]{10,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


@app.on_event("startup")
def startup_event():
    create_schema()
    seed_data()
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)


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


def require_role(user: User, *roles: Role) -> User:
    if user.role not in roles:
        raise HTTPException(status_code=403, detail="Недостаточно прав для выполнения операции.")
    return user


def is_admin(user: User) -> bool:
    return user.role == Role.ADMIN


def is_lawyer(user: User) -> bool:
    return user.role == Role.LAWYER


def is_client(user: User) -> bool:
    return user.role == Role.CLIENT


def safe_next_url(next_url: str | None, default: str = "/cases") -> str:
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return default


def log_action(db: Session, user: User | None, action: str, details: str = "") -> None:
    db.add(AuditLog(user_id=user.id if user else None, action=action, details=details))


def send_notification(db: Session, recipient_id: int, title: str, message: str) -> None:
    db.add(Notification(recipient_id=recipient_id, title=title, message=message))


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


def get_client_profile(user: User, db: Session) -> Client | None:
    if not user.client_id:
        return None
    return db.get(Client, user.client_id)


def case_is_accessible(user: User, legal_case: LegalCase) -> bool:
    if is_admin(user):
        return True
    if is_client(user):
        return bool(user.client_id and legal_case.client_id == user.client_id)
    if is_lawyer(user):
        if legal_case.responsible_lawyer_id == user.id:
            return True
        return any(lawyer.id == user.id for lawyer in legal_case.lawyers)
    return False


def require_case_access(user: User, legal_case: LegalCase) -> LegalCase:
    if not case_is_accessible(user, legal_case):
        raise HTTPException(status_code=403, detail="Доступ к этому делу запрещен.")
    return legal_case


def task_is_accessible(user: User, task: CaseTask) -> bool:
    if is_admin(user):
        return True
    if is_client(user):
        return False
    return case_is_accessible(user, task.legal_case)


def get_cases_for_user(db: Session, user: User) -> list[LegalCase]:
    cases = db.scalars(select(LegalCase).order_by(LegalCase.id.desc())).all()
    return [item for item in cases if case_is_accessible(user, item)]


def get_tasks_for_user(db: Session, user: User) -> list[CaseTask]:
    tasks = db.scalars(select(CaseTask).order_by(CaseTask.due_date, CaseTask.id.desc())).all()
    return [item for item in tasks if task_is_accessible(user, item)]


def get_notifications_for_user(db: Session, user: User, limit: int = 100) -> list[Notification]:
    ensure_deadline_notifications(db, user)
    items = db.scalars(
        select(Notification).where(Notification.recipient_id == user.id).order_by(Notification.id.desc()).limit(limit)
    ).all()
    return list(items)


def get_visible_clients(db: Session, user: User, q: str = "") -> list[Client]:
    clients = db.scalars(select(Client).order_by(Client.id.desc())).all()
    if is_admin(user):
        visible = clients
    else:
        visible_ids = {item.client_id for item in get_cases_for_user(db, user)}
        visible = [client for client in clients if client.id in visible_ids]
    if q.strip():
        pattern = q.strip().lower()
        visible = [client for client in visible if pattern in client.name.lower()]
    return visible


def get_client_owner(db: Session, client_id: int) -> User | None:
    return db.scalar(select(User).where(User.role == Role.CLIENT, User.client_id == client_id))


def display_user_name(user: User | None) -> str:
    if not user:
        return "Система"
    return user.full_name or user.username


def get_case_staff_users(db: Session, legal_case: LegalCase) -> list[User]:
    staff_map: dict[int, User] = {}
    for admin in db.scalars(select(User).where(User.role == Role.ADMIN)).all():
        staff_map[admin.id] = admin
    if legal_case.responsible_lawyer:
        staff_map[legal_case.responsible_lawyer.id] = legal_case.responsible_lawyer
    for lawyer in legal_case.lawyers:
        staff_map[lawyer.id] = lawyer
    return list(staff_map.values())


def get_visible_case_messages(user: User, legal_case: LegalCase) -> list[CaseMessage]:
    messages = sorted(legal_case.messages, key=lambda item: item.created_at)
    if is_client(user):
        return [item for item in messages if item.visibility == MessageVisibility.PUBLIC]
    return messages


def sanitize_file_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._")
    return cleaned or "document"


def render_document_template(template: DocumentTemplate, legal_case: LegalCase) -> str:
    lawyer_name = display_user_name(legal_case.responsible_lawyer)
    client_name = legal_case.client.name if legal_case.client else "Не указан"
    replacements = {
        "{{case_number}}": legal_case.case_number,
        "{{case_title}}": legal_case.title,
        "{{case_category}}": legal_case.category,
        "{{client_name}}": client_name,
        "{{lawyer_name}}": lawyer_name,
        "{{today}}": date.today().strftime("%d.%m.%Y"),
    }
    rendered = template.body
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered


def create_document_version(
    db: Session,
    document: CaseDocument,
    created_by: User | None,
    original_name: str,
    mime_type: str,
    payload: bytes | str,
    source: DocumentSource,
) -> DocumentVersion:
    version_number = len(document.versions) + 1
    suffix = Path(original_name).suffix if "." in original_name else ""
    if not suffix and source == DocumentSource.TEMPLATE:
        suffix = ".txt"
    safe_name = sanitize_file_component(Path(original_name).stem)
    target_dir = DOCUMENTS_DIR / f"case_{document.legal_case_id}" / f"document_{document.id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_path = target_dir / f"v{version_number}_{safe_name}{suffix}"
    if isinstance(payload, bytes):
        stored_path.write_bytes(payload)
    else:
        stored_path.write_text(payload, encoding="utf-8")

    version = DocumentVersion(
        document_id=document.id,
        version_number=version_number,
        source=source,
        original_name=original_name,
        stored_path=str(stored_path),
        mime_type=mime_type or "application/octet-stream",
        created_by_id=created_by.id if created_by else None,
    )
    db.add(version)
    db.flush()
    return version


def ensure_deadline_notifications(db: Session, user: User) -> None:
    if is_client(user):
        return
    today = date.today()
    created = False
    for task in get_tasks_for_user(db, user):
        if task.status == TaskStatus.DONE:
            continue
        if is_lawyer(user) and task.assignee_id not in (None, user.id):
            continue
        days_left = (task.due_date - today).days
        if days_left not in (0, 1, 2):
            continue
        title = "Приближается срок задачи"
        message = f"{task.title} ({task.legal_case.case_number}) — срок {task.due_date.strftime('%d.%m.%Y')}"
        exists = db.scalar(
            select(Notification).where(
                Notification.recipient_id == user.id,
                Notification.title == title,
                Notification.message == message,
            )
        )
        if not exists:
            send_notification(db, user.id, title, message)
            created = True
    if created:
        db.flush()


def notify_case_participants_about_document(db: Session, legal_case: LegalCase, author: User, document: CaseDocument) -> None:
    recipients: dict[int, User] = {item.id: item for item in get_case_staff_users(db, legal_case)}
    owner = get_client_owner(db, legal_case.client_id)
    if owner:
        recipients[owner.id] = owner
    recipients.pop(author.id, None)
    for recipient in recipients.values():
        send_notification(
            db,
            recipient.id,
            "Загружен документ",
            f"{document.title} ({legal_case.case_number})",
        )


def notify_case_participants_about_message(
    db: Session,
    legal_case: LegalCase,
    author: User,
    visibility: MessageVisibility,
) -> None:
    recipients: dict[int, User] = {}
    if visibility == MessageVisibility.PUBLIC:
        if is_client(author):
            recipients = {item.id: item for item in get_case_staff_users(db, legal_case)}
        else:
            owner = get_client_owner(db, legal_case.client_id)
            if owner:
                recipients[owner.id] = owner
            for item in get_case_staff_users(db, legal_case):
                recipients[item.id] = item
    else:
        recipients = {item.id: item for item in get_case_staff_users(db, legal_case)}

    recipients.pop(author.id, None)
    title = "Новое сообщение по делу" if visibility == MessageVisibility.PUBLIC else "Новый внутренний комментарий"
    message = f"{legal_case.case_number}: {legal_case.title}"
    for recipient in recipients.values():
        if visibility == MessageVisibility.INTERNAL and recipient.role == Role.CLIENT:
            continue
        send_notification(db, recipient.id, title, message)


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
            "show_chat": True,
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
                "show_chat": True,
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
        client = Client(
            name=full_name,
            client_type="PERSON",
            email=email,
            phone=phone,
            notes="Клиент зарегистрирован через веб-портал",
        )
        db.add(client)
        db.flush()

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
            client_id=client.id,
        )
        db.add(new_user)
        log_action(db, new_user, "Регистрация клиента", f"email={email}")
        db.commit()
        return RedirectResponse("/login?registered=1", status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "user": None,
            "show_footer": False,
            "show_chat": True,
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

    if is_client(user):
        cases = get_cases_for_user(db, user)
        notifications = get_notifications_for_user(db, user, limit=100)
        client_profile = get_client_profile(user, db)
        return templates.TemplateResponse(
            "client_dashboard.html",
            {
                "request": request,
                "user": user,
                "client_profile": client_profile,
                "cases": cases[:5],
                "active_cases": len([item for item in cases if item.stage != CaseStage.COMPLETED]),
                "new_requests": len([item for item in cases if item.stage == CaseStage.NEW_REQUEST]),
                "completed_cases": len([item for item in cases if item.stage == CaseStage.COMPLETED]),
                "notifications_unread": len([item for item in notifications if not item.is_read]),
                "last_notifications": notifications[:5],
                "stage_labels": STAGE_LABELS,
            },
        )

    tasks = get_tasks_for_user(db, user)
    cases = get_cases_for_user(db, user)
    overdue = [item for item in tasks if item.due_date < today and item.status != TaskStatus.DONE]
    notifications = get_notifications_for_user(db, user, limit=5)

    clients_count = db.scalar(select(func.count(Client.id))) or 0
    if is_lawyer(user):
        clients_count = len({item.client_id for item in cases})

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "cases_count": len(cases),
            "active_cases": len([item for item in cases if item.stage != CaseStage.COMPLETED]),
            "clients_count": clients_count,
            "tasks_todo": len([item for item in tasks if item.status != TaskStatus.DONE]),
            "overdue_tasks": len(overdue),
            "recent_tasks": tasks[:8],
            "last_notifications": notifications,
            "user": user,
            "status_labels": STATUS_LABELS,
        },
    )


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN, Role.LAWYER)
    clients = get_visible_clients(db, user, q=q)
    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "clients": clients,
            "q": q,
            "user": user,
            "created": request.query_params.get("created") == "1",
            "can_create_client": is_admin(user),
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
    require_role(user, Role.ADMIN)
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
    send_notification(db, user.id, "Новый клиент", f"Добавлен клиент: {clean_name}")
    log_action(db, user, "Создан клиент", clean_name)
    db.commit()
    return RedirectResponse("/clients?created=1", status_code=303)


@app.get("/cases", response_class=HTMLResponse)
def cases_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    cases = get_cases_for_user(db, user)
    client_profile = get_client_profile(user, db)
    clients = db.scalars(select(Client).order_by(Client.name)).all() if is_admin(user) else []
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name)).all() if is_admin(user) else []
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
            "client_profile": client_profile,
            "created": request.query_params.get("created") == "1",
            "can_create_case": is_admin(user),
            "can_assign_lawyer": is_admin(user),
            "can_change_stage": is_admin(user) or is_lawyer(user),
            "can_view_topsis": is_admin(user),
            "is_client_view": is_client(user),
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
    require_role(user, Role.ADMIN)
    client = db.get(Client, client_id)
    if not client:
        return RedirectResponse("/cases?error=client", status_code=303)

    stage_value = CaseStage.NEW_REQUEST
    try:
        stage_value = CaseStage(stage)
    except ValueError:
        pass

    deadline_value = parse_iso_date(deadline)
    number = case_number.strip() or next_case_number(db)

    legal_case = LegalCase(
        case_number=number,
        title=title.strip(),
        category=category.strip() or "Общее",
        description=description.strip(),
        stage=stage_value,
        priority=priority.strip().upper() or "MEDIUM",
        opened_at=date.today(),
        deadline=deadline_value,
        client_id=client.id,
        responsible_lawyer_id=None,
    )
    db.add(legal_case)
    db.flush()

    if responsible_lawyer_id:
        lawyer = db.get(User, responsible_lawyer_id)
        if lawyer and lawyer.role == Role.LAWYER:
            legal_case.responsible_lawyer_id = lawyer.id
            if all(item.id != lawyer.id for item in legal_case.lawyers):
                legal_case.lawyers.append(lawyer)
            send_notification(db, lawyer.id, "Новое дело", f"Вам назначено дело {legal_case.case_number}")

    owner = get_client_owner(db, client.id)
    if owner:
        send_notification(db, owner.id, "Создано дело", f"По вашему обращению создано дело {legal_case.case_number}")

    send_notification(db, user.id, "Создано новое дело", f"{legal_case.case_number}: {legal_case.title}")
    log_action(db, user, "Создано дело", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()
    return RedirectResponse("/cases?created=1", status_code=303)


@app.post("/cases/{case_id}/assign")
def assign_case_lawyer(
    case_id: int,
    request: Request,
    responsible_lawyer_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")

    lawyer = db.get(User, responsible_lawyer_id)
    if not lawyer or lawyer.role != Role.LAWYER:
        raise HTTPException(status_code=400, detail="Выбран некорректный исполнитель")

    legal_case.responsible_lawyer_id = lawyer.id
    if all(item.id != lawyer.id for item in legal_case.lawyers):
        legal_case.lawyers.append(lawyer)
    if legal_case.stage == CaseStage.NEW_REQUEST:
        legal_case.stage = CaseStage.DOC_ANALYSIS

    send_notification(db, lawyer.id, "Вам назначено дело", f"{legal_case.case_number}: {legal_case.title}")
    owner = get_client_owner(db, legal_case.client_id)
    if owner:
        send_notification(db, owner.id, "Заявка принята в работу", f"Назначен ответственный юрист по делу {legal_case.case_number}")
    log_action(db, user, "Назначен ответственный юрист", f"{legal_case.case_number} -> {lawyer.username}")
    db.commit()
    return RedirectResponse("/cases", status_code=303)


@app.post("/cases/{case_id}/stage")
def update_case_stage(
    case_id: int,
    request: Request,
    stage: str = Form(...),
    next_url: str = Form("/cases"),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN, Role.LAWYER)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    require_case_access(user, legal_case)

    try:
        legal_case.stage = CaseStage(stage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректная стадия") from exc

    owner = get_client_owner(db, legal_case.client_id)
    if owner:
        send_notification(db, owner.id, "Обновлен статус дела", f"{legal_case.case_number}: {STAGE_LABELS[legal_case.stage]}")
    send_notification(db, user.id, "Стадия дела изменена", legal_case.case_number)
    log_action(db, user, "Обновлена стадия дела", f"{legal_case.case_number} -> {legal_case.stage.value}")
    db.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(
            {
                "ok": True,
                "case_id": legal_case.id,
                "case_number": legal_case.case_number,
                "stage": legal_case.stage.value,
                "stage_label": STAGE_LABELS[legal_case.stage],
            }
        )
    return RedirectResponse(safe_next_url(next_url, "/cases"), status_code=303)


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail_page(case_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    require_case_access(user, legal_case)

    documents = sorted(legal_case.documents, key=lambda item: item.created_at, reverse=True)
    document_rows = []
    for document in documents:
        versions = sorted(document.versions, key=lambda item: item.version_number, reverse=True)
        document_rows.append(
            {
                "document": document,
                "versions": versions,
                "latest": versions[0] if versions else None,
                "versions_count": len(versions),
            }
        )

    visible_messages = get_visible_case_messages(user, legal_case)
    public_messages = [item for item in visible_messages if item.visibility == MessageVisibility.PUBLIC]
    internal_messages = [item for item in visible_messages if item.visibility == MessageVisibility.INTERNAL]
    templates_list = db.scalars(
        select(DocumentTemplate).where(DocumentTemplate.is_active == True).order_by(DocumentTemplate.title)
    ).all()

    return templates.TemplateResponse(
        "case_detail.html",
        {
            "request": request,
            "user": user,
            "legal_case": legal_case,
            "stage_labels": STAGE_LABELS,
            "visibility_labels": VISIBILITY_LABELS,
            "document_rows": document_rows,
            "document_templates": templates_list,
            "existing_documents": documents,
            "public_messages": public_messages,
            "internal_messages": internal_messages,
            "can_leave_internal_comment": not is_client(user),
        },
    )


@app.get("/cases/{case_id}/communication", response_class=HTMLResponse)
def case_communication_redirect(case_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    require_case_access(user, legal_case)
    return RedirectResponse(f"/cases/{case_id}#communication", status_code=303)


@app.post("/cases/{case_id}/documents/upload")
def upload_case_document(
    case_id: int,
    request: Request,
    title: str = Form(""),
    document_id: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    require_case_access(user, legal_case)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Не выбран файл для загрузки.")

    document: CaseDocument | None = None
    document_id_value = int(document_id) if document_id.strip().isdigit() else None
    if document_id_value:
        document = db.get(CaseDocument, document_id_value)
        if not document or document.legal_case_id != legal_case.id:
            raise HTTPException(status_code=404, detail="Документ не найден")
    else:
        document_title = title.strip() or file.filename
        document = CaseDocument(
            legal_case_id=legal_case.id,
            title=document_title,
            created_by_id=user.id,
        )
        db.add(document)
        db.flush()

    mime_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
    create_document_version(
        db,
        document,
        user,
        file.filename,
        mime_type,
        file.file.read(),
        DocumentSource.UPLOAD,
    )
    notify_case_participants_about_document(db, legal_case, user, document)
    log_action(db, user, "Загружен документ", f"{legal_case.case_number}: {document.title}")
    db.commit()
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.post("/cases/{case_id}/documents/template")
def create_case_document_from_template(
    case_id: int,
    request: Request,
    template_id: int = Form(...),
    title: str = Form(""),
    document_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    require_case_access(user, legal_case)

    template = db.get(DocumentTemplate, template_id)
    if not template or not template.is_active:
        raise HTTPException(status_code=404, detail="Шаблон документа не найден")

    document: CaseDocument | None = None
    document_id_value = int(document_id) if document_id.strip().isdigit() else None
    if document_id_value:
        document = db.get(CaseDocument, document_id_value)
        if not document or document.legal_case_id != legal_case.id:
            raise HTTPException(status_code=404, detail="Документ не найден")
    else:
        document = CaseDocument(
            legal_case_id=legal_case.id,
            title=title.strip() or template.title,
            created_by_id=user.id,
        )
        db.add(document)
        db.flush()

    payload = render_document_template(template, legal_case)
    create_document_version(
        db,
        document,
        user,
        f"{sanitize_file_component(document.title)}.txt",
        "text/plain; charset=utf-8",
        payload,
        DocumentSource.TEMPLATE,
    )
    notify_case_participants_about_document(db, legal_case, user, document)
    log_action(db, user, "Создан документ по шаблону", f"{legal_case.case_number}: {document.title}")
    db.commit()
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.get("/documents/{document_id}/versions/{version_id}/download")
def download_document_version(document_id: int, version_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    document = db.get(CaseDocument, document_id)
    version = db.get(DocumentVersion, version_id)
    if not document or not version or version.document_id != document.id:
        raise HTTPException(status_code=404, detail="Версия документа не найдена")
    legal_case = db.get(LegalCase, document.legal_case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    require_case_access(user, legal_case)

    file_path = Path(version.stored_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл документа не найден на диске")
    return FileResponse(file_path, media_type=version.mime_type, filename=version.original_name)


@app.post("/cases/{case_id}/messages")
def post_case_message(
    case_id: int,
    request: Request,
    body: str = Form(...),
    visibility: str = Form(MessageVisibility.PUBLIC.value),
    db: Session = Depends(get_db),
):
    user = require_auth(request, db)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    require_case_access(user, legal_case)

    message_body = body.strip()
    if not message_body:
        raise HTTPException(status_code=400, detail="Сообщение не должно быть пустым")

    visibility_value = MessageVisibility.PUBLIC
    if not is_client(user):
        try:
            visibility_value = MessageVisibility(visibility)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Некорректный тип сообщения") from exc

    message = CaseMessage(
        legal_case_id=legal_case.id,
        author_id=user.id,
        body=message_body,
        visibility=visibility_value,
    )
    db.add(message)
    notify_case_participants_about_message(db, legal_case, user, visibility_value)
    log_action(db, user, "Добавлено сообщение по делу", f"{legal_case.case_number}: {visibility_value.value}")
    db.commit()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(
            {
                "ok": True,
                "message": {
                    "id": message.id,
                    "author_id": user.id,
                    "author_name": display_user_name(user),
                    "body": message.body,
                    "visibility": message.visibility.value,
                    "created_at": message.created_at.strftime("%d.%m.%Y %H:%M"),
                },
            }
        )
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN, Role.LAWYER)
    tasks = get_tasks_for_user(db, user)
    cases = get_cases_for_user(db, user)
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.full_name)).all() if is_admin(user) else [user]
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
            "can_assign_other_lawyers": is_admin(user),
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
    require_role(user, Role.ADMIN, Role.LAWYER)
    task_date = parse_iso_date(due_date)
    if not task_date:
        return RedirectResponse("/tasks?error=date", status_code=303)

    legal_case = db.get(LegalCase, legal_case_id)
    if not legal_case:
        return RedirectResponse("/tasks?error=case", status_code=303)
    require_case_access(user, legal_case)

    status_value = TaskStatus.TODO
    try:
        status_value = TaskStatus(status)
    except ValueError:
        pass

    if is_lawyer(user):
        assignee_id = user.id

    assignee = db.get(User, assignee_id) if assignee_id else None
    if assignee and assignee.role != Role.LAWYER:
        raise HTTPException(status_code=400, detail="Исполнителем задачи может быть только юрист")

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
    if assignee_id:
        send_notification(db, assignee_id, "Новая задача", f"{new_task.title} ({legal_case.case_number})")
    else:
        send_notification(db, user.id, "Новая задача", f"{new_task.title} ({legal_case.case_number})")
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
    require_role(user, Role.ADMIN, Role.LAWYER)
    task = db.get(CaseTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not task_is_accessible(user, task):
        raise HTTPException(status_code=403, detail="Доступ к задаче запрещен.")

    try:
        task.status = TaskStatus(status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный статус") from exc

    if task.assignee_id:
        send_notification(db, task.assignee_id, "Статус задачи обновлен", f"{task.title}: {STATUS_LABELS[task.status]}")
    log_action(db, user, "Обновлен статус задачи", f"{task.title} -> {task.status.value}")
    db.commit()
    return RedirectResponse("/tasks", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN, Role.LAWYER)
    tasks = get_tasks_for_user(db, user)
    events = []
    for task in tasks:
        events.append(
            {
                "title": task.title,
                "date": task.due_date.isoformat(),
                "status": STATUS_LABELS[task.status],
                "is_done": task.status == TaskStatus.DONE,
            }
        )
    return templates.TemplateResponse("calendar.html", {"request": request, "events": events, "user": user})


@app.get("/kanban", response_class=HTMLResponse)
def kanban_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN, Role.LAWYER)
    cases = get_cases_for_user(db, user)
    grouped = defaultdict(list)
    for legal_case in cases:
        grouped[legal_case.stage].append(legal_case)
    return templates.TemplateResponse(
        "kanban.html",
        {"request": request, "grouped": grouped, "stages": list(CaseStage), "stage_labels": STAGE_LABELS, "user": user},
    )


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    notifications = get_notifications_for_user(db, user, limit=100)
    unread = len([item for item in notifications if not item.is_read])
    return templates.TemplateResponse(
        "notifications.html",
        {"request": request, "notifications": notifications, "unread": unread, "user": user},
    )


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    notification = db.get(Notification, notification_id)
    if not notification or notification.recipient_id != user.id:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")
    notification.is_read = True
    db.commit()
    return RedirectResponse("/notifications", status_code=303)


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN)
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
    require_role(user, Role.ADMIN)
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
    send_notification(db, user.id, "Выставлен счет", f"Счет {number.strip()}")
    log_action(db, user, "Создан счет", number.strip())
    db.commit()
    return RedirectResponse("/invoices?created=1", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN)
    entries = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(200)).all()
    users = {item.id: item for item in db.scalars(select(User)).all()}
    return templates.TemplateResponse(
        "audit.html",
        {"request": request, "entries": entries, "users_map": users, "user": user},
    )


@app.get("/portal/intake", response_class=HTMLResponse)
def intake_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    client_profile = get_client_profile(user, db) if user else None
    return templates.TemplateResponse(
        "intake.html",
        {
            "request": request,
            "user": user,
            "client_profile": client_profile,
            "show_chat": True,
            "show_footer": not bool(user),
            "success": request.query_params.get("success") == "1",
            "can_submit": bool(user and is_client(user) and client_profile),
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
    user = require_auth(request, db)
    require_role(user, Role.CLIENT)
    client = get_client_profile(user, db)
    if not client:
        raise HTTPException(status_code=400, detail="Для аккаунта клиента не найдена карточка клиента.")

    client.name = full_name.strip() or client.name
    client.email = email.strip().lower()
    client.phone = phone.strip()
    client.notes = "Заявка через личный кабинет клиента"

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

    admins = db.scalars(select(User).where(User.role == Role.ADMIN)).all()
    for admin in admins:
        send_notification(db, admin.id, "Новая входящая заявка", f"{legal_case.case_number}: {legal_case.title}")

    send_notification(db, user.id, "Заявка отправлена", f"Обращение {legal_case.case_number} принято и ожидает обработки администратором.")
    log_action(db, user, "Создана клиентская заявка", f"{legal_case.case_number}: {legal_case.title}")
    db.commit()
    return RedirectResponse("/portal/intake?success=1", status_code=303)


@app.get("/cases/{case_id}/topsis", response_class=HTMLResponse)
def topsis_page(case_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    require_role(user, Role.ADMIN)
    legal_case = db.get(LegalCase, case_id)
    if not legal_case:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER)).all()
    ranking = topsis_rank(legal_case.category, lawyers)
    return templates.TemplateResponse(
        "topsis.html",
        {"request": request, "legal_case": legal_case, "ranking": ranking, "user": user, "stage_labels": STAGE_LABELS},
    )
