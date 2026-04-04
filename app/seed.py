from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import select, text

from .database import Base, SessionLocal, engine
from .models import CaseStage, CaseTask, Client, LegalCase, Role, TaskStatus, User
from .security import hash_password


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
    }

    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()
            }
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
        existing_admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        if existing_admin:
            return {
                "admin_username": existing_admin.username,
                "admin_password": "(уже существует)"
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
        db.flush()

        client = Client(
            name="ООО Ромашка",
            client_type="ORGANIZATION",
            email="contact@romashka.local",
            phone="+7 900 111-11-11",
            address="г. Екатеринбург, ул. Ленина, 10",
            notes="Приоритетный корпоративный клиент",
        )
        db.add(client)
        db.flush()

        case = LegalCase(
            case_number="CASE-2026-001",
            title="Взыскание задолженности по договору поставки",
            category="договоры",
            description="Подготовка претензии и иска по просроченной оплате.",
            stage=CaseStage.DOC_ANALYSIS,
            priority="HIGH",
            opened_at=date.today(),
            deadline=date.today() + timedelta(days=30),
            client_id=client.id,
            responsible_lawyer_id=lawyer1.id,
        )
        case.lawyers.extend([lawyer1, lawyer2])
        db.add(case)
        db.flush()

        tasks = [
            "Подготовить досудебную претензию",
            "Собрать первичные доказательства",
            "Проверить срок исковой давности",
            "Подготовить проект иска",
            "Согласовать пакет документов",
            "Подготовить правовое заключение",
            "Сформировать судебную папку",
            "Подготовить ходатайство",
            "Проверить контрагента",
            "Подготовить выступление",
            "Изучить судебную практику",
            "Рассчитать неустойку",
            "Направить копии ответчику",
            "Подготовить замечания на отзыв",
            "Обновить стратегию защиты",
            "Собрать доверенности",
            "Проверить документы на подлинность",
            "Сформировать таймлайн событий",
            "Подготовить ответы клиенту",
            "Проверить финальный пакет",
        ]
        for i, title in enumerate(tasks, start=1):
            db.add(
                CaseTask(
                    legal_case_id=case.id,
                    title=title,
                    due_date=date.today() + timedelta(days=i),
                    status=TaskStatus.DONE if i % 5 == 0 else TaskStatus.IN_PROGRESS,
                    priority="HIGH" if i % 3 == 0 else "MEDIUM",
                    assignee_id=lawyer1.id if i % 2 else lawyer2.id,
                    description="Задача создана автоматически для демонстрации календаря и сроков.",
                )
            )

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


if __name__ == "__main__":
    creds = seed_data()
    print("Демо-данные созданы")
    for k, v in creds.items():
        print(f"{k}: {v}")
