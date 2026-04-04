from __future__ import annotations

import os
import secrets

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .models import DocumentTemplate, Role, User
from .security import hash_password


DEFAULT_DOCUMENT_TEMPLATES = [
    {
        "title": "Шаблон претензии",
        "category": "Претензионная работа",
        "body": (
            "Претензия по делу {{case_number}}\n\n"
            "Клиент: {{client_name}}\n"
            "Категория: {{case_category}}\n"
            "Дата: {{today}}\n\n"
            "Излагаем обстоятельства дела {{case_title}} и формулируем требования.\n"
        ),
    },
    {
        "title": "Шаблон ходатайства",
        "category": "Судебные документы",
        "body": (
            "Ходатайство по делу {{case_number}}\n\n"
            "Клиент: {{client_name}}\n"
            "Ответственный юрист: {{lawyer_name}}\n"
            "Дата: {{today}}\n\n"
            "Просим суд принять во внимание следующие обстоятельства по делу {{case_title}}.\n"
        ),
    },
    {
        "title": "Шаблон сопроводительного письма",
        "category": "Переписка",
        "body": (
            "Сопроводительное письмо\n\n"
            "Дело: {{case_number}}\n"
            "Клиент: {{client_name}}\n"
            "Дата: {{today}}\n\n"
            "Направляем документы по делу {{case_title}} для рассмотрения и дальнейшей работы.\n"
        ),
    },
]


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_user_registration_columns()


def _ensure_user_registration_columns() -> None:
    required_columns = {
        "first_name": "VARCHAR(80) DEFAULT ''",
        "last_name": "VARCHAR(80) DEFAULT ''",
        "middle_name": "VARCHAR(80) DEFAULT ''",
        "email": "TEXT DEFAULT ''",
        "phone": "TEXT DEFAULT ''",
        "client_id": "INTEGER",
    }

    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "sqlite":
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
            for column_name, sql_type in required_columns.items():
                if column_name not in existing:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {column_name} {sql_type}"))
            return

        if dialect == "postgresql":
            for column_name, sql_type in required_columns.items():
                conn.execute(text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column_name} {sql_type}"))
            return

        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'users'"
                )
            ).fetchall()
        }
        for column_name, sql_type in required_columns.items():
            if column_name not in existing:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {column_name} {sql_type}"))


def seed_data() -> dict[str, str]:
    db = SessionLocal()
    try:
        create_schema()
        ensure_default_document_templates(db)
        existing_admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        if existing_admin:
            return {
                "admin_username": existing_admin.username,
                "admin_password": "(уже существует)",
            }

        admin_username = os.getenv("DEMO_ADMIN_USERNAME", "admin")
        admin_password = os.getenv("DEMO_ADMIN_PASSWORD", "") or secrets.token_urlsafe(10)
        lawyer1_username = os.getenv("DEMO_LAWYER1_USERNAME", "lawyer_ivan")
        lawyer1_password = os.getenv("DEMO_LAWYER1_PASSWORD", "") or secrets.token_urlsafe(10)
        lawyer2_username = os.getenv("DEMO_LAWYER2_USERNAME", "lawyer_anna")
        lawyer2_password = os.getenv("DEMO_LAWYER2_PASSWORD", "") or secrets.token_urlsafe(10)
        admin = User(
            username=admin_username,
            full_name="Системный администратор",
            password_hash=hash_password(admin_password),
            role=Role.ADMIN,
        )
        lawyer1 = User(
            username=lawyer1_username,
            full_name="Иван Петров",
            password_hash=hash_password(lawyer1_password),
            role=Role.LAWYER,
            specialization="договоры, гражданские споры",
            current_load=5,
            similar_cases_experience=22,
            avg_task_days=6,
            deadline_success_rate=93,
        )
        lawyer2 = User(
            username=lawyer2_username,
            full_name="Анна Смирнова",
            password_hash=hash_password(lawyer2_password),
            role=Role.LAWYER,
            specialization="корпоративное право, договоры",
            current_load=3,
            similar_cases_experience=17,
            avg_task_days=5,
            deadline_success_rate=90,
        )
        db.add_all([admin, lawyer1, lawyer2])

        db.commit()
        return {
            "admin_username": admin_username,
            "admin_password": admin_password,
            "lawyer1_username": lawyer1_username,
            "lawyer1_password": lawyer1_password,
            "lawyer2_username": lawyer2_username,
            "lawyer2_password": lawyer2_password,
        }
    finally:
        db.close()


def ensure_default_document_templates(db: Session) -> None:
    existing_titles = {item.title for item in db.scalars(select(DocumentTemplate)).all()}
    for template in DEFAULT_DOCUMENT_TEMPLATES:
        if template["title"] not in existing_titles:
            db.add(
                DocumentTemplate(
                    title=template["title"],
                    category=template["category"],
                    body=template["body"],
                    is_active=True,
                )
            )
    db.commit()


if __name__ == "__main__":
    creds = seed_data()
    print("Демо-данные созданы")
    for k, v in creds.items():
        print(f"{k}: {v}")
