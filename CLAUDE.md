# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**Valoro**: Flask + SQLite (Flask-SQLAlchemy) + APScheduler web app (AdminLTE UI, Jinja templates) that sends **SMS (Infobip)** and **email (esmsv.com API)** to bases read from **Google BigQuery** (project `capable-arbor-209819`) or, for SMS, from an Excel file. Code, comments, logs and UI are in Spanish — keep new code in the same language and style. `README.md` has the user-facing reference (config keys, flow, files).

## Running

```bash
pip install -r requirements.txt
python app.py                      # http://localhost:5000 (PORT env var overrides)
```

- The app runs with `debug=False, use_reloader=False` on purpose: the reloader spawned two processes → two schedulers → every job ran twice. **After editing any `.py` file, restart the process manually.**
- BigQuery credentials: `config/google_key.json` (service account) or gcloud ADC — see `conexion_bigquery.get_bigquery_client()`.
- `config.json` is not in git (Infobip and email API keys, general parameters, SMS `senuelos`). New copies start from `config.example.json` (same structure, empty values) and fill the keys by hand. Code must read credentials from `config.json`, never hardcode them.
- There is no test suite, linter or build step. `consulta_segura.py`, `services/sms_exclusiones.py` and `datos_personales.py` are free of Flask/DB/network so they can be exercised with fake objects; keep new pure rules in the same style.

### Test copy mode (`modo_prueba.py`)

`APP_COPIA_PRUEBA=1` runs the app as a test copy: it must run from its own folder and on a port other than 5000 (checked in `proteger_arranque_copia_prueba`, called at the top of `app.py` *before* `backend` is imported). It skips the BigQuery writes of the scheduled jobs (SMS status polling, Infobip→`Temporal.SmsInfobip` sync) and does not reload pending SMS/email programaciones at startup. It does **not** block manual SMS/email sends.

## Architecture

- **`backend.py`** — importing it has side effects: creates the Flask `app`, configures SQLite (`app.db`), logging (`app_log.txt`, `execution_log.txt`, and per-channel `logs/sms.log` / `logs/email.log` via `logger_sms` / `logger_email`), loads `config.json`, and **starts the global `BackgroundScheduler` (timezone `America/Bogota`)** with the SMS status polling, console heartbeat and log maintenance jobs. `APP_NOMBRE` (shown in the UI through a context processor) lives here. Everything else imports `app`, `db`, `scheduler` from here.
- **`app.py`** — all HTTP routes plus scheduler jobs registered at import time: per-programación SMS jobs `sms_simple_<id>` (DateTrigger) / `sms_recurrente_<id>` (every 10 min, sends once a day at `hora_inicio`), `email_programado_<id>`, startup reloads (`sms_recargar_programaciones`, `email_recargar_programaciones`), `sms_infobip_sync` / `sms_infobip_sync_dia`. No blueprints. When editing/removing a programación, also replace/remove its job by id (`agendar_programacion_sms`, `_sms_desagendar`).
- **`database.py`** — SQLAlchemy models: `ProgramacionSms` (SMS programaciones, states `pendiente`/`enviado`/`completado`/`error`/`cancelado`, `ultimo_error`), `MensajeOperacion` (SMS templates, table `Mensajes_`), `PlantillaEmail`. No migration tool: new columns go in the model **and** in `_ensure_programacion_sms_columns()` (`ALTER TABLE ... ADD COLUMN` on startup).
- **Email programaciones** live in BigQuery (`Temporal.ProgramacionEmail_Valoro`, separate from QNT's `ProgramacionEmail` so one app never sends the other's), not SQLite. `tipo` = simple/recurrente (recurring = daily, re-scheduled +1 day after each send); `CREATE TABLE` in README.
- **Send flow** (SMS and email): query → read-only check + BigQuery dry run (`consulta_segura`, `bigquery.fetch_sms_query_rows`, real BigQuery message via `mensaje_error_bigquery`) → template (`{{Columna}}`; SMS templates from the constructor or typed) → Infobip link shortening (`urlOptions.shortenUrl`) → blacklists (`listas_negras.py`) and same-day duplicates → send or schedule. If the blacklist can't be read, the send is cancelled.
- **`services/`** — `sms_service.py` (Infobip, SmsLog, polling), `sms_excel.py`, `sms_exclusiones.py`, `email_service.py`, `email_client.py`.
- **SMS dashboard** — `dashboard_sms.py`, `infobip_sync.py`, `infobip_logs.py` (`/dashboards`).
- Required fields: `_faltantes_sms` / `_faltantes_email` in `app.py` return the names of the missing fields; the templates check the same names before calling the API.

## Working notes

- `app.db` is the live database; back it up before schema or data changes.
- `execution_log.txt` is what the home page live log (`/api/recent_logs`) reads; use `backend.log_activity` / `log_task` so messages show up there. SMS errors go to `logger_sms`, email errors to `logger_email`.

## Team workflow

The repo is maintained by **rodri** and **bcrodriguez**. This file is the shared memory of the Claude Code sessions: when a team rule changes, change it here and push it.

- **Ownership.** SMS, email and the shared core → bcrodriguez (with rodri). Before touching an area someone else is working on, ask your user to check with them.
- **Branches.** Every change goes in its own branch off an up-to-date main branch, small commits, then a PR or merge. Pull before starting and before pushing. Never force-push the main branch.
- **No credentials in git.** `config.json` (Infobip/email keys, `senuelos`), `config/google_key.json`, `app.db` and the logs are ignored and stay local to each copy. New config keys go in `config.example.json` with empty values. Scripts read credentials from `config.json`, never hardcoded. Before every commit check `git diff --cached` for tokens, passwords or client data (phones, emails, names).
- **One production app.** Only one instance with the scheduler may run against Infobip/email (production machine, port 5000). Each `app.db` holds its own SMS programaciones; email programaciones are shared in BigQuery. Two running apps with active programaciones = duplicate messages.
- **Developing.** Use the test copy mode (`APP_COPIA_PRUEBA=1`, `PORT=5001`, different folder) with an `app.db` that has no active SMS programaciones and without real send credentials when possible. After editing any `.py`, the app must be restarted by hand; tell your user when a change needs a restart.
