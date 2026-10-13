# Real-Time Industrial Gas Monitoring and Alert System

A software-only, four-zone gas monitoring application for a B.Tech final-year project. The FastAPI service owns simulation, threshold classification, incidents, persistence, and notifications. React displays server state and provides protected operator controls. PostgreSQL is hosted by Supabase.

**Safety:** readings are software-generated, not physical sensor measurements. Thresholds are illustrative application defaults, not certified industrial exposure or alarm limits. This application does not replace certified gas detectors, a safety instrumented system, or site emergency procedures.

## Architecture

```text
React + Vite + TypeScript + Tailwind + Recharts
       | REST / read-only WebSocket (polling fallback)
FastAPI + asyncpg (one worker; simulator is process-local)
       | PostgreSQL TLS
Supabase PostgreSQL
```

The frontend never writes readings or makes threshold decisions. Read APIs remain public for the read-only dashboard. Local development defaults to no sign-in only for loopback clients; privileged requests from other hosts require strong configured credentials. Production requires operator sign-in using a signed HTTP-only cookie with an eight-hour session, HTTPS, and an exact HTTPS origin allowlist. The live WebSocket also requires an authorized session and an allowed origin. Mutation requests require an allowed Origin from non-loopback clients. Browsers that block third-party cookies may need the frontend and backend on custom subdomains of the same site. Use a single backend instance/worker: the simulator is guarded to one loop in one process. The simulator automatically stops after its configured maximum runtime. Do not horizontally scale it without replacing that guard with a database/advisory lock and a dedicated scheduler.

### Default zones

| Zone | Gas | Unit | Warning | Critical | Normal simulator range |
|---|---|---:|---:|---:|---:|
| Zone 1 | Carbon Monoxide (CO) | ppm | 35 | 100 | 0–12 |
| Zone 2 | Methane (CH4) | % LEL | 10 | 20 | 0–4 |
| Zone 3 | Hydrogen Sulfide (H2S) | ppm | 10 | 20 | 0–3 |
| Zone 4 | Ammonia (NH3) | ppm | 25 | 50 | 0–8 |

These values only demonstrate gas-specific configuration and status transitions. Have a qualified safety professional set any values for a real facility. Thresholds can be changed through the authenticated `PUT /api/zones/{zone_id}/thresholds` API; an incident stores both thresholds active at creation and the threshold crossed for its initial and any critical escalation event. The database enforces `critical > warning`.

## Local setup

Prerequisites: Python 3.12, Node.js 20+, npm, and a Supabase project (or compatible PostgreSQL database for local development). No hardware is required.

1. Create a Supabase project. In **Project Settings → Database**, obtain the connection string. Use the session/pooler connection if the chosen backend host requires IPv4; retain TLS (`sslmode=require`). Keep this credential on the backend only.
2. In Supabase **SQL Editor**, run the checked-in migrations in numeric order: [`database/migrations/001_initial.sql`](database/migrations/001_initial.sql), then [`database/migrations/002_severity_notifications.sql`](database/migrations/002_severity_notifications.sql). The second migration adds separate warning/critical notification records while preserving existing history. Schema setup is explicit; backend startup never applies migrations or clears history.
3. Create a dedicated backend database login instead of using Supabase's `postgres` or service-role credential. Generate a strong password and run this in SQL Editor after replacing the quoted placeholder (do not commit the real password):

   ```sql
   CREATE ROLE gas_monitor_app WITH LOGIN PASSWORD 'REPLACE_WITH_A_GENERATED_SECRET';
   GRANT CONNECT ON DATABASE postgres TO gas_monitor_app;
   GRANT USAGE ON SCHEMA public TO gas_monitor_app;
   GRANT SELECT ON zones, readings, incidents, notifications TO gas_monitor_app;
   GRANT INSERT ON readings, incidents, notifications, audit_events TO gas_monitor_app;
   GRANT UPDATE ON zones, incidents TO gas_monitor_app;
   GRANT USAGE, SELECT ON SEQUENCE readings_id_seq, audit_events_id_seq TO gas_monitor_app;
   ```

   Store the password in a secret manager, then use this role in `DATABASE_URL`. For a **separate development/test database only**, the demo reset endpoint also needs this least-scope grant:

   ```sql
   GRANT DELETE ON readings, incidents, notifications TO gas_monitor_app;
   ```

   Never grant this permission to a production database role. The reset route is unavailable when `APP_ENV=production`; use distinct database credentials for development and production. Do not grant `TRUNCATE`, schema ownership, or DDL.

   The migrations do not configure Supabase Data API grants or RLS. Before exposing the Supabase project, verify effective Data API grants and prevent `anon`/`authenticated` direct table access unless intentionally required. If enabling RLS, ensure the dedicated backend database role still has narrowly scoped access through grants and policies. The application connects from the backend using its dedicated PostgreSQL role; it does not need a Supabase service-role key in the frontend.
4. Copy `backend/.env.example` to `backend/.env`. For a local loopback demo, keep `APP_ENV=development`, `AUTH_REQUIRED=false`, `SMS_MODE=mock`, and `SMS_ENABLED=false`. No-login controls and WebSocket access work only for loopback clients. The simulator stops after `SIMULATOR_MAX_RUNTIME_SECONDS` (default 900; maximum 3600), and critical simulations for one zone have a short cooldown. Keep credentials out of source control.
5. Install and run the API (PowerShell):

   ```powershell
   cd backend
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.lock
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

   API docs are at `http://localhost:8000/docs`; health at `/api/health`.
6. In another terminal, copy `frontend/.env.example` to `frontend/.env.local` (the default API URL is correct for the commands above), then run:

   ```powershell
   cd frontend
   npm ci
   npm run dev
   ```

   Open the Vite URL shown (normally `http://localhost:5173`). With the local example settings, loopback controls work without sign-in. To exercise operator login locally, set `AUTH_REQUIRED=true` and configure credentials. Read-only dashboard/history remains public. The simulator stops when the common Stop control is used, the API process shuts down, or its maximum runtime is reached. Starting it again does not clear readings, incidents, or notifications.

## API overview

`/docs` provides the generated OpenAPI reference.

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/health` | Public | API, DB and simulator health |
| GET | `/api/state`, `/api/zones` | Public | Live zone states and configured thresholds |
| GET | `/api/readings?zone_id=&limit=` | Public | Reading history (maximum 1000) |
| GET | `/api/zones/{id}/readings` | Public | Zone reading history |
| GET | `/api/incidents?limit=` | Public | Incident history and latest notification state |
| GET | `/api/notifications?limit=` | Public | Notification attempt history |
| GET | `/api/auth/session` | Public | Current operator sign-in state |
| POST | `/api/auth/login`, `/api/auth/logout` | Login / logout | Establish / clear HTTP-only operator cookie |
| POST | `/api/simulation/start`, `/api/simulation/stop` | Loopback demo or signed-in non-loopback operator | Control all four zones together |
| POST | `/api/simulation/critical` | Loopback demo or signed-in non-loopback operator | JSON `{"zone_id":1}`; per-zone cooldown |
| POST | `/api/demo/reset` | Loopback client in development/test only | Stops simulation and transactionally deletes readings, incidents, and notifications; preserves zones, thresholds, settings, and audit history |
| PUT | `/api/zones/{id}/thresholds` | Loopback demo or signed-in non-loopback operator | JSON `{"warning_threshold":35,"critical_threshold":100}` |
| POST | `/api/incidents/{id}/acknowledge`, `/resolve` | Loopback demo or signed-in non-loopback operator | Server records configured local actor or signed-in account |
| WS | `/api/ws` | Loopback demo or signed-in non-loopback operator; exact allowed origin | Live state stream; UI also polls while disconnected |

An event creates at most one unresolved incident per zone; a warning can escalate in place to critical. Warning notifications are recorded immediately on crossing the warning threshold; critical notifications are recorded immediately on a critical reading or escalation. There is one notification per incident per severity, so repeated readings do not spam messages. The software simulator stays at or above the critical threshold while that critical incident remains unresolved. Explicitly resolving the incident allows subsequent simulated readings to return to the normal range; Resolve itself does not change the current reading/status. Uncertain provider requests are not automatically retried, avoiding duplicate SMS.

## SMS configuration

Mock mode is the default: the database stores each warning/critical alert and `MOCK_RECORDED` state, and the UI explicitly says no SMS was sent. Real sending is not enabled by default, and this repository does not send a message during setup. The backend makes an Exotel API request for warning and critical severities only when `SMS_ENABLED=true`, `SMS_MODE=real`, `AUTH_REQUIRED=true`, and account SID, API key/token, approved sender ID, and authorized recipient number are configured. The request begins during threshold-crossing incident processing; actual receipt still depends on provider/network response time. Credentials and number remain server-side. Even a successful API response is recorded as **accepted**, not delivered; delivery confirmation needs provider status callbacks/polling, which are not implemented.

Before the owner separately authorizes and configures real messages, confirm current Exotel account eligibility, KYC/account verification, Indian destination and route availability, sender/ExoPhone approval, and applicable TRAI/DLT entity, header, content-template and consent requirements. For India, configure `EXOTEL_DLT_ENTITY_ID`, and `EXOTEL_DLT_TEMPLATE_ID` / `EXOTEL_SMS_TYPE` as required by the account and approved template; content must match the approved DLT template. `EXOTEL_SUBDOMAIN=api.in` selects Exotel's Mumbai endpoint; choose the subdomain assigned to the account. The integration uses API key/token Basic Authentication and the documented `/v1/Accounts/{sid}/Sms/send.json` endpoint. See [Exotel's SMS API documentation](https://developer.exotel.com/api/send-sms). Check current Exotel tariffs and applicable operator/telecom charges first; this setup can incur charges. Do not enable real SMS for a presentation environment without authorization, a controlled test recipient, approved sender/template, and a reviewed production configuration. Provider failures/timeouts are stored as `FAILED`; there is intentionally no automatic retry.

## Tests and build

From `backend/`, with its virtual environment active: `python -m pytest`. Unit tests cover classification boundaries, gas-specific thresholds, per-zone critical events, incident deduplication/escalation, mock and provider-failure notification behavior, login authorization, simulator start/stop controls, request validation, reading/incident write calls, and demo-reset scope/environment restrictions. They use fake database connections; persistence against a real PostgreSQL instance still needs an integration environment. Frontend production build: from `frontend/`, run `npm ci`, then `npm run build`.

## Deployment

This repository does **not** deploy services, create cloud resources, or send SMS.

1. **Supabase:** create the project and apply migrations `001_initial.sql` and `002_severity_notifications.sql`, in that order. Use a database password that is not reused elsewhere. Configure network access and TLS for the backend host; never expose this URL or a Supabase service-role key to Vite.
2. **Backend:** deploy the `backend/` directory to Render (or another Python ASGI host) using [`backend/render.yaml`](backend/render.yaml): install `requirements.lock`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`. Set `DATABASE_URL`, `SESSION_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `CORS_ORIGINS` in the host's secret environment settings. Use a unique session secret of at least 32 characters, a non-default username, and a password of at least 16 characters. `CORS_ORIGINS` must be a comma-separated exact list of HTTPS frontend origins without paths. The manifest keeps `AUTH_REQUIRED=true`, caps simulation at 900 seconds (configurable up to 3600), and explicitly keeps SMS mock/disabled. Configure persistent host availability appropriate to the presentation and confirm its current free-plan sleep, runtime, connection, and usage limits; free tiers may sleep or change and do not guarantee continuous monitoring.
3. **Vercel:** import the repository and set the project root directory to `frontend`. Build command `npm run build`, output directory `dist`. Set `VITE_API_BASE_URL` to the deployed backend origin (no secret), then deploy through Vercel. Add the final Vercel origin to backend `CORS_ORIGINS` and redeploy/restart the backend.
4. Check `https://<backend>/api/health`, open `https://<backend>/docs`, then verify the dashboard connection, login, simulator start/stop, zone-specific critical event, incident actions, and notification history. Test only with SMS disabled.
5. Enabling real SMS is a separate owner decision after provider account, approved configuration, credentials, pricing and telecom compliance have been verified. It is intentionally outside deployment automation.

## Configuration reference

Backend values are listed in [`backend/.env.example`](backend/.env.example); frontend value in [`frontend/.env.example`](frontend/.env.example). Core settings include `DATABASE_URL`, `CORS_ORIGINS`, `APP_ENV`, `SESSION_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `AUTH_REQUIRED`, `SIMULATOR_INTERVAL_SECONDS`, `SIMULATOR_MAX_RUNTIME_SECONDS`, `CRITICAL_SIMULATION_COOLDOWN_SECONDS`, `SMS_MODE`, `SMS_ENABLED`, Exotel credentials/sender/template fields, and `SMS_RECIPIENT_NUMBER`. Secrets belong in the backend host secret manager in production. Never use a `VITE_` variable for a private value.

## Known limitations

- The local simulator is process-local and this service must run one worker and one instance. Multi-instance scheduling/locking is future work.
- Public read APIs expose monitoring state. The WebSocket and privileged APIs require authentication for non-loopback clients; local no-login mode is loopback-only. Login uses one environment-configured account and an in-process rate limit; add individual accounts and shared rate limiting before real operational use.
- Notification provider acceptance is not delivery receipt; callbacks and automated retries are not implemented.
- Database retention/partitioning, observability, backup/restore drills, and production integration/load tests need operational planning.
- Vercel and Python/Supabase free tier availability and quotas vary by provider, region, and date; an always-on system may require paid resources.
