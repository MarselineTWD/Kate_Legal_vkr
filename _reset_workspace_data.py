# -*- coding: utf-8 -*-
from pathlib import Path
from sqlalchemy import delete, text

from app.database import SessionLocal, engine
from app.models import (
    AuditLog,
    CalendarEvent,
    CaseComment,
    CaseDocument,
    CaseTask,
    Client,
    ClientChatMessage,
    Invoice,
    LegalCase,
    Role,
    Notification,
    User,
)
from app.security import hash_password
from app.seed import create_schema


def reset_workspace_data() -> None:
    create_schema()
    db = SessionLocal()
    try:
        db.execute(delete(ClientChatMessage))
        db.execute(delete(CaseDocument))
        db.execute(delete(CaseComment))
        db.execute(delete(CaseTask))
        db.execute(delete(CalendarEvent))
        db.execute(delete(Invoice))
        db.execute(delete(Notification))
        db.execute(delete(AuditLog))
        db.execute(text('DELETE FROM case_lawyers'))
        db.execute(delete(LegalCase))
        db.execute(delete(Client))
        db.execute(delete(User))

        admin_password = 'Admin2026!'
        lawyer_password = 'Lawyer2026!'
        admin = User(
            username='admin',
            full_name='Системный администратор',
            first_name='Системный',
            last_name='Администратор',
            middle_name='',
            email='admin@lawworkspace.ru',
            phone='+7 (900) 000-00-01',
            password_hash=hash_password(admin_password),
            role=Role.ADMIN,
            specialization='',
            current_load=0,
            similar_cases_experience=0,
            avg_task_days=0,
            deadline_success_rate=100,
        )

        lawyers = [
            User(
                username='lawyer_trud',
                full_name='Иван Соколов',
                first_name='Иван',
                last_name='Соколов',
                middle_name='',
                email='ivan.sokolov@lawworkspace.ru',
                phone='+7 (900) 000-00-11',
                password_hash=hash_password(lawyer_password),
                role=Role.LAWYER,
                specialization='Трудовое право, Защита прав потребителей, Исполнительное производство',
                current_load=3,
                similar_cases_experience=34,
                avg_task_days=5,
                deadline_success_rate=95,
            ),
            User(
                username='lawyer_family',
                full_name='Мария Волкова',
                first_name='Мария',
                last_name='Волкова',
                middle_name='',
                email='maria.volkova@lawworkspace.ru',
                phone='+7 (900) 000-00-12',
                password_hash=hash_password(lawyer_password),
                role=Role.LAWYER,
                specialization='Семейное право, Наследственное право, Жилищное право',
                current_load=4,
                similar_cases_experience=29,
                avg_task_days=6,
                deadline_success_rate=93,
            ),
            User(
                username='lawyer_corporate',
                full_name='Алексей Ковалёв',
                first_name='Алексей',
                last_name='Ковалёв',
                middle_name='',
                email='alexey.kovalev@lawworkspace.ru',
                phone='+7 (900) 000-00-13',
                password_hash=hash_password(lawyer_password),
                role=Role.LAWYER,
                specialization='Договорное право, Корпоративное право, Арбитражные споры',
                current_load=2,
                similar_cases_experience=37,
                avg_task_days=5,
                deadline_success_rate=96,
            ),
            User(
                username='lawyer_state',
                full_name='Дмитрий Орлов',
                first_name='Дмитрий',
                last_name='Орлов',
                middle_name='',
                email='dmitry.orlov@lawworkspace.ru',
                phone='+7 (900) 000-00-14',
                password_hash=hash_password(lawyer_password),
                role=Role.LAWYER,
                specialization='Уголовное право, Административное право, Миграционное право',
                current_load=3,
                similar_cases_experience=31,
                avg_task_days=6,
                deadline_success_rate=92,
            ),
            User(
                username='lawyer_finance',
                full_name='Елена Морозова',
                first_name='Елена',
                last_name='Морозова',
                middle_name='',
                email='elena.morozova@lawworkspace.ru',
                phone='+7 (900) 000-00-15',
                password_hash=hash_password(lawyer_password),
                role=Role.LAWYER,
                specialization='Налоговое право, Банкротство, Споры с банками',
                current_load=1,
                similar_cases_experience=35,
                avg_task_days=4,
                deadline_success_rate=97,
            ),
            User(
                username='lawyer_property',
                full_name='Ольга Белова',
                first_name='Ольга',
                last_name='Белова',
                middle_name='',
                email='olga.belova@lawworkspace.ru',
                phone='+7 (900) 000-00-16',
                password_hash=hash_password(lawyer_password),
                role=Role.LAWYER,
                specialization='Земельное право, Жилищное право, Страховые споры',
                current_load=2,
                similar_cases_experience=28,
                avg_task_days=5,
                deadline_success_rate=94,
            ),
            User(
                username='lawyer_ip',
                full_name='Павел Романов',
                first_name='Павел',
                last_name='Романов',
                middle_name='',
                email='pavel.romanov@lawworkspace.ru',
                phone='+7 (900) 000-00-17',
                password_hash=hash_password(lawyer_password),
                role=Role.LAWYER,
                specialization='Интеллектуальная собственность, Международное право, Другое',
                current_load=2,
                similar_cases_experience=26,
                avg_task_days=6,
                deadline_success_rate=93,
            ),
            User(
                username='lawyer_process',
                full_name='Наталья Лебедева',
                first_name='Наталья',
                last_name='Лебедева',
                middle_name='',
                email='natalia.lebedeva@lawworkspace.ru',
                phone='+7 (900) 000-00-18',
                password_hash=hash_password(lawyer_password),
                role=Role.LAWYER,
                specialization='Арбитражные споры, Банкротство, Исполнительное производство',
                current_load=4,
                similar_cases_experience=32,
                avg_task_days=5,
                deadline_success_rate=94,
            ),
        ]

        db.add(admin)
        db.add_all(lawyers)

        db.commit()
    finally:
        db.close()

    uploads_dir = Path('app/uploads')
    if uploads_dir.exists():
        for item in uploads_dir.rglob('*'):
            if item.is_file():
                item.unlink()
        for item in sorted(uploads_dir.rglob('*'), reverse=True):
            if item.is_dir():
                item.rmdir()

    sqlite_db = Path('fastapi.db')
    if sqlite_db.exists():
        sqlite_db.unlink()


if __name__ == '__main__':
    reset_workspace_data()
    print('dialect:', engine.dialect.name)
    print('reset complete')
    print('admin email: admin@lawworkspace.ru | password: Admin2026!')
    print('lawyer password: Lawyer2026!')
