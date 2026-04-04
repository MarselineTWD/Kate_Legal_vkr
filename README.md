# ЮрНавигатор (FastAPI)

Новый фронтенд взят из `C:\Users\Marse\OneDrive\Рабочий стол\Site2` и интегрирован в проект.

## Что сделано

- Все файлы `Site2` скопированы в `app/site2`
- Подключена раздача ассетов: `/site2static/*`
- Главная теперь берется из `app/site2/index.html`
- Добавлены маршруты:
  - `/o-nas` → `/#about`
  - `/kontakty` → `/#contacts`
- Поддержаны старые алиасы (редиректы):
  - `/О-нас.html`
  - `/Контакты.html`
  - `/Страница-1.html`
- Текст на сайте адаптирован под ваш проект
- Добавлен строгий стиль: `app/site2/strict.css`

## Запуск

```bash
pip install -r requirements.txt
python -m app.seed
uvicorn app.main:app --reload
или
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Открыть: `http://127.0.0.1:8000`

## Где править контент

- Главная: `app/site2/index.html`
- Стиль: `app/site2/strict.css`
- Внутренние страницы кабинета: `app/templates/*`
