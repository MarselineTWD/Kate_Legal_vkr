from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import re

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .database import get_db
from .models import (
    AuditLog,
    CaseStage,
    CaseTask,
    Client,
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
        "last_notifications": db.scalars(select(Notification).order_by(Notification.id.desc()).limit(5)).all(),
    }
    return templates.TemplateResponse("dashboard.html", {"request": request, **data, "user": user, "status_labels": STATUS_LABELS})


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = require_auth(request, db)
    stmt = select(Client)
    if q.strip():
        stmt = stmt.where(Client.name.ilike(f"%{q.strip()}%"))
    clients = db.scalars(stmt.order_by(Client.id.desc())).all()
    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "clients": clients,
            "q": q,
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
    return RedirectResponse("/cases", status_code=303)


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
    return RedirectResponse("/tasks", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, db: Session = Depends(get_db)):
    user = require_auth(request, db)
    tasks = db.scalars(select(CaseTask).order_by(CaseTask.due_date)).all()
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
    cases = db.scalars(select(LegalCase)).all()
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
    notifications = db.scalars(select(Notification).order_by(Notification.id.desc()).limit(100)).all()
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
