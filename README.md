# ЮрНавигатор (FastAPI)

Веб-приложение на FastAPI для работы юридической компании: сайт, личные кабинеты, дела, задачи, документы, уведомления и счета.

Проект можно запустить на другом ПК без PostgreSQL: по умолчанию используется локальная база SQLite `fastapi.db`.

## Что есть в проекте

- backend: `app/main.py`
- шаблоны: `app/templates/`
- статика: `app/static/`
- публичный сайт: `app/site2/`
- локальное хранилище документов: `storage/documents/`
- файл зависимостей: `requirements.txt`
- пример переменных окружения: `.env.example`

## Требования

- Windows + PowerShell
- Python 3.11+ 
- доступ к интернету для установки зависимостей

## Быстрый запуск на другом ПК

1. Скопируйте проект на новый компьютер.

2. Откройте PowerShell в папке проекта.

3. Создайте и активируйте виртуальное окружение:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Если PowerShell блокирует активацию, выполните:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.venv\Scripts\Activate.ps1
```

4. Установите зависимости:

```powershell
pip install -r requirements.txt
```

5. Создайте файл `.env` на основе примера:

```powershell
Copy-Item .env.example .env
```

6. Сгенерируйте значения для ключей:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

7. Откройте `.env` и заполните минимум такие поля:

```env
APP_DEBUG=1
APP_SECRET_KEY=вставьте_сюда_первый_сгенерированный_ключ
FIELD_ENCRYPTION_KEY=вставьте_сюда_второй_сгенерированный_ключ

# Для локального запуска можно оставить пустым.
# Тогда будет использована SQLite-база fastapi.db в корне проекта.
DATABASE_URL=

DEMO_ADMIN_USERNAME=admin
DEMO_ADMIN_PASSWORD=Admin12345
DEMO_LAWYER1_USERNAME=lawyer_ivan
DEMO_LAWYER1_PASSWORD=Lawyer12345
DEMO_LAWYER2_USERNAME=lawyer_anna
DEMO_LAWYER2_PASSWORD=Lawyer12345
```

8. Инициализируйте базу и демо-данные:

```powershell
python -m app.seed
```

9. Запустите приложение:

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

10. Откройте в браузере:

```text
http://127.0.0.1:8000
```

## Запуск через скрипт

После настройки окружения можно использовать готовый скрипт:

```powershell
.\run_fastapi.ps1
```

Скрипт только выполняет `python -m app.seed` и запускает `uvicorn`. Зависимости и `.env` должны быть подготовлены заранее.

## Как перенести проект на другой ПК

Есть 2 нормальных сценария.

### 1. Чистый запуск без старых данных

Переносите:

- папку `app/`
- файл `requirements.txt`
- файл `run_fastapi.ps1`
- файл `.env.example`
- папку `media/`, если она нужна вашему проекту
- этот `README.md`

Необязательно переносить:

- `.venv/`
- `app/__pycache__/`
- `fastapi.db`
- `storage/`
- старый `.env`

После этого выполните шаги из раздела "Быстрый запуск на другом ПК".

### 2. Перенос вместе с существующими данными

Если нужно сохранить уже созданные аккаунты, документы и записи, перенесите:

- весь исходный код проекта
- файл `.env`
- файл `fastapi.db`
- папку `storage/`

Важно:

- `APP_SECRET_KEY` и `FIELD_ENCRYPTION_KEY` должны остаться теми же, что были на старом ПК
- если поменять ключи, уже сохраненные зашифрованные поля могут читаться некорректно
- при переносе SQLite-базы приложение продолжит работать без PostgreSQL

После переноса на новом ПК достаточно:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## PostgreSQL вместо SQLite

Если хотите запускать проект не на встроенной SQLite, а на PostgreSQL, укажите в `.env`:

```env
DATABASE_URL=postgresql+psycopg://postgres:пароль@127.0.0.1:5432/legal_workspace
```

После этого снова выполните:

```powershell
python -m app.seed
```

## Где менять контент

- главная страница сайта: `app/site2/index.html`
- стили публичного сайта: `app/site2/strict.css`
- страницы кабинета: `app/templates/`
- backend-маршруты и логика: `app/main.py`

## Частые проблемы

`ModuleNotFoundError`

Убедитесь, что активировано виртуальное окружение и выполнен `pip install -r requirements.txt`.

`PowerShell запрещает запуск .ps1`

Используйте:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Приложение запускается, но логин не подходит

- для новой базы сначала выполните `python -m app.seed`
- логины и пароли берутся из `.env`
- если база уже старая, `seed` не перезапишет существующего администратора
