from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text

from .database import Base, SessionLocal, engine
from .models import CaseComment, CaseStage, CaseTask, Client, LegalCase, Role, TaskStatus, User
from .security import hash_password


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_user_role_enum_values()
    _ensure_user_registration_columns()
    _ensure_legal_case_intake_column()
    _ensure_legal_case_created_at_column()
    _ensure_client_profile_columns()
    _ensure_client_chat_columns()
    _ensure_case_document_storage_columns()
    _ensure_notification_timestamp_column()


def _ensure_user_role_enum_values() -> None:
    with engine.begin() as conn:
        if conn.dialect.name != "postgresql":
            return
        conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'role')
                    AND NOT EXISTS (
                        SELECT 1
                        FROM pg_type t
                        JOIN pg_enum e ON e.enumtypid = t.oid
                        WHERE t.typname = 'role' AND e.enumlabel = 'CLIENT'
                    )
                    THEN
                        ALTER TYPE role ADD VALUE 'CLIENT';
                    END IF;
                END $$;
                """
            )
        )


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


def _ensure_notification_timestamp_column() -> None:
    with engine.begin() as conn:
        dialect = conn.dialect.name

        if dialect == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(notifications)")).fetchall()
            }
            if "created_at" not in existing:
                conn.execute(text("ALTER TABLE notifications ADD COLUMN created_at DATETIME"))
            conn.execute(text("UPDATE notifications SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
            return

        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS created_at TIMESTAMP"))
            conn.execute(text("UPDATE notifications SET created_at = NOW() WHERE created_at IS NULL"))
            return

        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'notifications'"
                )
            ).fetchall()
        }
        if "created_at" not in existing:
            conn.execute(text("ALTER TABLE notifications ADD COLUMN created_at TIMESTAMP"))
        conn.execute(text("UPDATE notifications SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))


def _ensure_client_profile_columns() -> None:
    required_columns = {
        "user_id": "INTEGER",
        "inn": "TEXT DEFAULT ''",
        "ogrn": "TEXT DEFAULT ''",
        "bank_details": "TEXT DEFAULT ''",
        "passport_details": "TEXT DEFAULT ''",
        "other_details": "TEXT DEFAULT ''",
        "requisites": "TEXT DEFAULT ''",
    }

    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(clients)")).fetchall()
            }
            for column_name, sql_type in required_columns.items():
                if column_name not in existing:
                    conn.execute(text(f"ALTER TABLE clients ADD COLUMN {column_name} {sql_type}"))
            return

        if dialect == "postgresql":
            for column_name, sql_type in required_columns.items():
                conn.execute(text(f"ALTER TABLE clients ADD COLUMN IF NOT EXISTS {column_name} {sql_type}"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_clients_user_id ON clients (user_id)"))
            return

        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'clients'"
                )
            ).fetchall()
        }
        for column_name, sql_type in required_columns.items():
            if column_name not in existing:
                conn.execute(text(f"ALTER TABLE clients ADD COLUMN {column_name} {sql_type}"))


def _ensure_client_chat_columns() -> None:
    with engine.begin() as conn:
        dialect = conn.dialect.name

        if dialect == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(client_chat_messages)")).fetchall()
            }
            if "legal_case_id" not in existing:
                conn.execute(text("ALTER TABLE client_chat_messages ADD COLUMN legal_case_id INTEGER"))
            return

        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE client_chat_messages ADD COLUMN IF NOT EXISTS legal_case_id INTEGER"))
            return

        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'client_chat_messages'"
                )
            ).fetchall()
        }
        if "legal_case_id" not in existing:
            conn.execute(text("ALTER TABLE client_chat_messages ADD COLUMN legal_case_id INTEGER"))


def _ensure_legal_case_intake_column() -> None:
    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(legal_cases)")).fetchall()
            }
            if "intake_approved" not in existing:
                conn.execute(text("ALTER TABLE legal_cases ADD COLUMN intake_approved BOOLEAN DEFAULT 1"))
            if "intake_status" not in existing:
                conn.execute(text("ALTER TABLE legal_cases ADD COLUMN intake_status TEXT DEFAULT 'APPROVED'"))
            if "intake_admin_comment" not in existing:
                conn.execute(text("ALTER TABLE legal_cases ADD COLUMN intake_admin_comment TEXT DEFAULT ''"))
            if "is_consultation" not in existing:
                conn.execute(text("ALTER TABLE legal_cases ADD COLUMN is_consultation BOOLEAN DEFAULT 0"))
            if "allow_phone_contact" not in existing:
                conn.execute(text("ALTER TABLE legal_cases ADD COLUMN allow_phone_contact BOOLEAN DEFAULT 0"))
            if "preferred_contact_method" not in existing:
                conn.execute(text("ALTER TABLE legal_cases ADD COLUMN preferred_contact_method TEXT DEFAULT 'CHAT'"))
            conn.execute(text("UPDATE legal_cases SET intake_approved = 1 WHERE intake_approved IS NULL"))
            conn.execute(text("UPDATE legal_cases SET intake_status = CASE WHEN intake_approved = 1 THEN 'APPROVED' ELSE 'PENDING_REVIEW' END WHERE intake_status IS NULL OR intake_status = ''"))
            conn.execute(text("UPDATE legal_cases SET intake_admin_comment = '' WHERE intake_admin_comment IS NULL"))
            conn.execute(text("UPDATE legal_cases SET is_consultation = 0 WHERE is_consultation IS NULL"))
            conn.execute(text("UPDATE legal_cases SET allow_phone_contact = 0 WHERE allow_phone_contact IS NULL"))
            conn.execute(text("UPDATE legal_cases SET preferred_contact_method = 'CHAT' WHERE preferred_contact_method IS NULL OR preferred_contact_method = ''"))
            return

        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN IF NOT EXISTS intake_approved BOOLEAN DEFAULT TRUE"))
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN IF NOT EXISTS intake_status TEXT DEFAULT 'APPROVED'"))
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN IF NOT EXISTS intake_admin_comment TEXT DEFAULT ''"))
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN IF NOT EXISTS is_consultation BOOLEAN DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN IF NOT EXISTS allow_phone_contact BOOLEAN DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN IF NOT EXISTS preferred_contact_method TEXT DEFAULT 'CHAT'"))
            conn.execute(text("UPDATE legal_cases SET intake_approved = TRUE WHERE intake_approved IS NULL"))
            conn.execute(text("UPDATE legal_cases SET intake_status = CASE WHEN intake_approved = TRUE THEN 'APPROVED' ELSE 'PENDING_REVIEW' END WHERE intake_status IS NULL OR intake_status = ''"))
            conn.execute(text("UPDATE legal_cases SET intake_admin_comment = '' WHERE intake_admin_comment IS NULL"))
            conn.execute(text("UPDATE legal_cases SET is_consultation = FALSE WHERE is_consultation IS NULL"))
            conn.execute(text("UPDATE legal_cases SET allow_phone_contact = FALSE WHERE allow_phone_contact IS NULL"))
            conn.execute(text("UPDATE legal_cases SET preferred_contact_method = 'CHAT' WHERE preferred_contact_method IS NULL OR preferred_contact_method = ''"))
            return

        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'legal_cases'"
                )
            ).fetchall()
        }
        if "intake_approved" not in existing:
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN intake_approved BOOLEAN DEFAULT TRUE"))
        if "intake_status" not in existing:
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN intake_status VARCHAR(32) DEFAULT 'APPROVED'"))
        if "intake_admin_comment" not in existing:
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN intake_admin_comment TEXT DEFAULT ''"))
        if "is_consultation" not in existing:
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN is_consultation BOOLEAN DEFAULT FALSE"))
        if "allow_phone_contact" not in existing:
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN allow_phone_contact BOOLEAN DEFAULT FALSE"))
        if "preferred_contact_method" not in existing:
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN preferred_contact_method VARCHAR(24) DEFAULT 'CHAT'"))
        conn.execute(text("UPDATE legal_cases SET intake_approved = TRUE WHERE intake_approved IS NULL"))
        conn.execute(text("UPDATE legal_cases SET intake_status = CASE WHEN intake_approved = TRUE THEN 'APPROVED' ELSE 'PENDING_REVIEW' END WHERE intake_status IS NULL OR intake_status = ''"))
        conn.execute(text("UPDATE legal_cases SET intake_admin_comment = '' WHERE intake_admin_comment IS NULL"))
        conn.execute(text("UPDATE legal_cases SET is_consultation = FALSE WHERE is_consultation IS NULL"))
        conn.execute(text("UPDATE legal_cases SET allow_phone_contact = FALSE WHERE allow_phone_contact IS NULL"))
        conn.execute(text("UPDATE legal_cases SET preferred_contact_method = 'CHAT' WHERE preferred_contact_method IS NULL OR preferred_contact_method = ''"))


def _ensure_legal_case_created_at_column() -> None:
    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(legal_cases)")).fetchall()
            }
            if "created_at" not in existing:
                conn.execute(text("ALTER TABLE legal_cases ADD COLUMN created_at DATETIME"))
            conn.execute(
                text(
                    """
                    UPDATE legal_cases
                    SET created_at = COALESCE(
                        (SELECT MIN(created_at) FROM case_documents d WHERE d.legal_case_id = legal_cases.id),
                        (SELECT MIN(created_at) FROM client_chat_messages m WHERE m.legal_case_id = legal_cases.id),
                        (SELECT MIN(created_at) FROM case_comments c WHERE c.legal_case_id = legal_cases.id),
                        (SELECT MIN(starts_at) FROM calendar_events e WHERE e.legal_case_id = legal_cases.id),
                        datetime(opened_at || ' 00:00:00'),
                        CURRENT_TIMESTAMP
                    )
                    WHERE created_at IS NULL
                    """
                )
            )
            return

        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN IF NOT EXISTS created_at TIMESTAMP"))
            conn.execute(
                text(
                    """
                    UPDATE legal_cases lc
                    SET created_at = COALESCE(
                        (SELECT MIN(d.created_at) FROM case_documents d WHERE d.legal_case_id = lc.id),
                        (SELECT MIN(m.created_at) FROM client_chat_messages m WHERE m.legal_case_id = lc.id),
                        (SELECT MIN(c.created_at) FROM case_comments c WHERE c.legal_case_id = lc.id),
                        (SELECT MIN(e.starts_at) FROM calendar_events e WHERE e.legal_case_id = lc.id),
                        (lc.opened_at::timestamp),
                        NOW()
                    )
                    WHERE lc.created_at IS NULL
                    """
                )
            )
            return

        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'legal_cases'"
                )
            ).fetchall()
        }
        if "created_at" not in existing:
            conn.execute(text("ALTER TABLE legal_cases ADD COLUMN created_at TIMESTAMP"))
        conn.execute(text("UPDATE legal_cases SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))


def _ensure_case_document_storage_columns() -> None:
    required_columns = {
        "mime_type": "TEXT DEFAULT ''",
        "file_size": "INTEGER DEFAULT 0",
        "file_content": "BLOB",
    }

    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(case_documents)")).fetchall()
            }
            for column_name, sql_type in required_columns.items():
                if column_name not in existing:
                    conn.execute(text(f"ALTER TABLE case_documents ADD COLUMN {column_name} {sql_type}"))
            return

        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE case_documents ADD COLUMN IF NOT EXISTS mime_type TEXT DEFAULT ''"))
            conn.execute(text("ALTER TABLE case_documents ADD COLUMN IF NOT EXISTS file_size INTEGER DEFAULT 0"))
            conn.execute(text("ALTER TABLE case_documents ADD COLUMN IF NOT EXISTS file_content BYTEA"))
            return

        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'case_documents'"
                )
            ).fetchall()
        }
        for column_name, sql_type in required_columns.items():
            if column_name not in existing:
                conn.execute(text(f"ALTER TABLE case_documents ADD COLUMN {column_name} {sql_type}"))


def _ensure_demo_board_data(db) -> None:
    lawyers = db.scalars(select(User).where(User.role == Role.LAWYER).order_by(User.id)).all()
    if not lawyers:
        return

    clients_by_name = {client.name: client for client in db.scalars(select(Client)).all()}
    demo_clients = [
        {
            "name": "ООО Ромашка",
            "client_type": "ORGANIZATION",
            "email": "contact@romashka.local",
            "phone": "+7 900 111-11-11",
            "address": "г. Екатеринбург, ул. Ленина, 10",
            "notes": "Приоритетный корпоративный клиент",
        },
        {
            "name": "АО Вектор",
            "client_type": "ORGANIZATION",
            "email": "office@vector.local",
            "phone": "+7 900 222-22-22",
            "address": "г. Екатеринбург, ул. Малышева, 21",
            "notes": "Корпоративный спор и сопровождение документов",
        },
        {
            "name": "ИП Соколова Е.А.",
            "client_type": "PERSON",
            "email": "sokolova@client.local",
            "phone": "+7 900 333-33-33",
            "address": "г. Екатеринбург, ул. Белинского, 14",
            "notes": "Нужна помощь по трудовому спору",
        },
    ]

    for payload in demo_clients:
        if payload["name"] not in clients_by_name:
            client = Client(**payload)
            db.add(client)
            db.flush()
            clients_by_name[payload["name"]] = client

    lawyer_primary = lawyers[0]
    lawyer_secondary = lawyers[1] if len(lawyers) > 1 else lawyers[0]
    lawyer_third = lawyers[2] if len(lawyers) > 2 else lawyer_secondary

    demo_cases = [
        {
            "case_number": "CASE-2026-001",
            "title": "Взыскание задолженности по договору поставки",
            "category": "договоры",
            "description": "Подготовка претензии, искового заявления и полного пакета документов по просроченной оплате поставки.",
            "stage": CaseStage.DOC_ANALYSIS,
            "priority": "HIGH",
            "opened_at": date.today(),
            "deadline": date.today() + timedelta(days=30),
            "client_name": "ООО Ромашка",
            "responsible_lawyer": lawyer_primary,
            "team": [lawyer_primary, lawyer_secondary],
        },
        {
            "case_number": "CASE-2026-002",
            "title": "Корпоративный конфликт участников общества",
            "category": "корпоративное право",
            "description": "Подготовка правовой позиции, анализ документов общества и сопровождение переговоров между участниками.",
            "stage": CaseStage.NEW_REQUEST,
            "priority": "MEDIUM",
            "opened_at": date.today() - timedelta(days=3),
            "deadline": date.today() + timedelta(days=14),
            "client_name": "АО Вектор",
            "responsible_lawyer": lawyer_secondary,
            "team": [lawyer_secondary, lawyer_primary],
        },
        {
            "case_number": "CASE-2026-003",
            "title": "Трудовой спор о восстановлении на работе",
            "category": "трудовое право",
            "description": "Подготовка позиции по делу, сбор доказательств, расчёт выплат и подготовка к судебному заседанию.",
            "stage": CaseStage.DOC_PREPARATION,
            "priority": "HIGH",
            "opened_at": date.today() - timedelta(days=7),
            "deadline": date.today() + timedelta(days=7),
            "client_name": "ИП Соколова Е.А.",
            "responsible_lawyer": lawyer_primary,
            "team": [lawyer_primary],
        },
        {
            "case_number": "CASE-2026-004",
            "title": "Судебное взыскание неустойки с подрядчика",
            "category": "судебное производство",
            "description": "Ведение активной судебной стадии, подготовка возражений, контроль сроков и сопровождение заседаний.",
            "stage": CaseStage.COURT,
            "priority": "MEDIUM",
            "opened_at": date.today() - timedelta(days=12),
            "deadline": date.today() + timedelta(days=4),
            "client_name": "АО Вектор",
            "responsible_lawyer": lawyer_third,
            "team": [lawyer_secondary, lawyer_third],
        },
    ]

    case_ids_with_tasks = {
        item[0]
        for item in db.execute(text("SELECT legal_case_id FROM case_tasks")).fetchall()
        if item[0] is not None
    }

    for payload in demo_cases:
        legal_case = db.scalar(select(LegalCase).where(LegalCase.case_number == payload["case_number"]))
        if not legal_case:
            legal_case = LegalCase(
                case_number=payload["case_number"],
                title=payload["title"],
                category=payload["category"],
                description=payload["description"],
                stage=payload["stage"],
                priority=payload["priority"],
                opened_at=payload["opened_at"],
                deadline=payload["deadline"],
                client_id=clients_by_name[payload["client_name"]].id,
                responsible_lawyer_id=payload["responsible_lawyer"].id,
            )
            db.add(legal_case)
            db.flush()

        legal_case.title = payload["title"]
        legal_case.category = payload["category"]
        legal_case.description = payload["description"]
        legal_case.stage = payload["stage"]
        legal_case.priority = payload["priority"]
        legal_case.opened_at = payload["opened_at"]
        legal_case.deadline = payload["deadline"]
        legal_case.client_id = clients_by_name[payload["client_name"]].id
        legal_case.responsible_lawyer_id = payload["responsible_lawyer"].id

        current_ids = {lawyer.id for lawyer in legal_case.lawyers}
        seen_team_ids = set()
        for lawyer in payload["team"]:
            if lawyer.id in seen_team_ids:
                continue
            seen_team_ids.add(lawyer.id)
            if lawyer.id not in current_ids:
                legal_case.lawyers.append(lawyer)

        if legal_case.id not in case_ids_with_tasks:
            db.add(
                CaseTask(
                    legal_case_id=legal_case.id,
                    title=f"Подготовить материалы по делу {legal_case.case_number}",
                    due_date=(legal_case.deadline or date.today()) - timedelta(days=2),
                    status=TaskStatus.IN_PROGRESS,
                    priority=legal_case.priority,
                    assignee_id=legal_case.responsible_lawyer_id,
                    description="Демонстрационная задача для канбан-доски и календаря.",
                )
            )
            case_ids_with_tasks.add(legal_case.id)

        if not db.scalar(select(func.count(CaseComment.id)).where(CaseComment.legal_case_id == legal_case.id)):
            db.add_all(
                [
                    CaseComment(
                        legal_case_id=legal_case.id,
                        user_id=payload["responsible_lawyer"].id,
                        message="Карточка проверена. Начинаем работу по текущей стадии.",
                        is_internal=True,
                    ),
                    CaseComment(
                        legal_case_id=legal_case.id,
                        user_id=payload["responsible_lawyer"].id,
                        message="Нужно держать под контролем ближайший срок выполнения и комплект документов.",
                        is_internal=True,
                    ),
                ]
            )

    db.commit()


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

