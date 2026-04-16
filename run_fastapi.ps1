$env:APP_DEBUG = "1"
python -m app.seed
uvicorn app.main:app --reload
