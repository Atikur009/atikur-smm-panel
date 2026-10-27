# Atikur SMM Panel

Flask + Postgres (Neon) / SQLite SMM panel for panel.atikur.cyou.

## Run locally
```
python -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python app.py   # http://127.0.0.1:5050
```

## Deploy (Render)
Uses `render.yaml` blueprint. Set env vars: `DATABASE_URL` (Neon Postgres),
`PROVIDER_API_URL`, `PROVIDER_API_KEY`, `BKASH_NUMBER`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SECRET_KEY`.
