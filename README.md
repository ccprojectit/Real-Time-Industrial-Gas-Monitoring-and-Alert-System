# Real-Time Industrial Gas Monitoring and Alert System

A software-only, four-zone gas monitoring application for a B.Tech final-year project. The FastAPI service owns simulation, Critical threshold decisions, incidents, persistence, and mock notifications. React displays server state and provides protected operator controls. PostgreSQL is hosted by Supabase.

**Safety:** readings are software-generated, not physical sensor measurements. Thresholds are illustrative application defaults, not certified industrial exposure or alarm limits. This application does not replace certified gas detectors, a safety instrumented system, or site emergency procedures.

## Architecture

```text
React + Vite + TypeScript + Tailwind + Recharts
       | REST / read-only WebSocket (polling fallback)
FastAPI + asyncpg (one worker; simulator is process-local)
       | PostgreSQL TLS
Supabase PostgreSQL
```

The frontend never writes readings or makes threshold decisions. Read APIs remain public for the read-only dashboard. Local development defaults to no sign-in only for loopback clients; privileged requests from other hosts require strong configured credentials. Production privileged routes require a signed HTTP-only cookie with an eight-hour session, HTTPS, and an exact HTTPS origin allowlist. The dashboard provides a sign-in dialog for administrator credentials configured on the backend and only enables controls after `/api/auth/session` confirms the session. Do not disable backend authentication to make a public presentation interactive. The live WebSocket also requires an authorized session and an allowed origin. Mutation requests require an allowed Origin from non-loopback clients. Browsers that block third-party cookies may need the frontend and backend on custom subdomains of the same site. Use a single backend instance/worker: the simulator is guarded to one loop in one process. The simulator automatically stops after its configured maximum runtime. Do not horizontally scale it without replacing that guard with a database/advisory lock and a dedicated scheduler.

### Default zones

| Zone | Gas | Unit | Critical | Automatic simulation range |
|---|---|---:|---:|---:|
| Zone 1 | Carbon Monoxide (CO) | ppm | 100 | 0–12 |
| Zone 2 | Methane (CH4) | % LEL | 20 | 0–4 |
| Zone 3 | Hydrogen Sulfide (H2S) | ppm | 20 | 0–3 |
| Zone 4 | Ammonia (NH3) | ppm | 50 | 0–8 |

These values are illustrative software settings, not certified safety limits. Automatic simulation produces Normal readings only and never creates incidents or notifications. Only an authenticated manual Critical Simulation creates incidents and mock notification records. Have a qualified safety professional set values for any real facility.

## Local setup

Prerequisites: Python 3.12, Node.js 20+, npm, and a Supabase project (or compatible PostgreSQL database for local development). No hardware is required.

1. Create a Supabase project. In **Project Settings → Database**, obtain the connection string. Use the session/pooler connection if the chosen backend host requires IPv4; retain TLS (`sslmode=require`). Keep this credential on the backend only.
2. In Supabase **SQL Editor**, run the checked-in migrations in numeric order: [`database/migrations/001_initial.sql`](database/migrations/001_initial.sql), then [`database/migrations/002_severity_notifications.sql`](database/migrations/002_severity_notifications.sql). The database schema retains legacy threshold columns, severity constraints, and existing records for compatibility with already-provisioned databases. Runtime APIs expose only Normal/Critical states; legacy records are presented as Historical and ignored in current zone counts. Do not rewrite or rerun applied migrations. Backend startup never applies migrations or clears history.
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

   Store the password in a secret manager, then use this role in `DATABASE_URL`. Reset All Data transactionally deletes only readings, incidents, and notifications while preserving zones, settings, and audit events. If you intend to enable this operation in production, the dedicated backend role also needs this least-scope grant; do not apply it automatically:

   ```sql
   GRANT DELETE ON readings, incidents, notifications TO gas_monitor_app;
   ```

   Grant `DELETE` only when the authenticated production reset feature is intentionally required. The operation is destructive and is recorded in `audit_events`; the API still requires an authorized operator and allowed origin. Do not grant `TRUNCATE`, schema ownership, or DDL. Use distinct database credentials for development and production.

   The migrations do not configure Supabase Data API grants or RLS. Before exposing the Supabase project, verify effective Data API grants and prevent `anon`/`authenticated` direct table access unless intentionally required. If enabling RLS, ensure the dedicated backend database role still has narrowly scoped access through grants and policies. The application connects from the backend using its dedicated PostgreSQL role; it does not need a Supabase service-role key in the frontend.
4. Copy `backend/.env.example` to `backend/.env`. For a local loopback demo, keep `APP_ENV=development` and `AUTH_REQUIRED=false`. Notifications are database-backed mock records only; the application has no outbound SMS integration. No-login controls and WebSocket access work only for loopback clients. Automatic readings fluctuate within the Normal range and never create incidents or notifications. Critical values can only be produced by the protected single-zone or multi-zone Critical Simulation API. The simulator interval defaults to 0.35 seconds and can be adjusted with `SIMULATOR_INTERVAL_SECONDS` (0.25–10 seconds); it stops after `SIMULATOR_MAX_RUNTIME_SECONDS` (default 900; maximum 3600). Critical simulations have a short per-zone cooldown. Keep credentials out of source control.
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

   Open the Vite URL shown (normally `http://localhost:5173`). With the local example settings, loopback controls work without sign-in. On a remote deployment, use **Operator sign in** and the backend's `ADMIN_USERNAME` and `ADMIN_PASSWORD`; the frontend confirms `/api/auth/session` before enabling controls. Read-only dashboard/history remains public. The simulator stops when the common Stop control is used, the API process shuts down, or its maximum runtime is reached. Starting it again does not clear readings, incidents, or notifications.

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
| POST | `/api/simulation/critical/batch` | Loopback demo or signed-in non-loopback operator | JSON `{"zone_ids":[1,3]}`; validates and commits all selected zones atomically; returns `{"zones":[{"zone_id":1,"reading":108,"status":"CRITICAL","measured_at":"…"}],"simulator_running":true}` |
| POST | `/api/demo/reset` | Loopback development client or signed-in operator in production | Stops simulation and transactionally deletes readings, incidents, and notifications; preserves zones, thresholds, settings, and audit history |
| POST | `/api/incidents/{id}/acknowledge`, `/resolve` | Loopback demo or signed-in non-loopback operator | Server records configured local actor or signed-in account |
| WS | `/api/ws` | Loopback demo or signed-in non-loopback operator; exact allowed origin | Live state stream; UI also polls while disconnected |

Automatic simulation writes readings only and is constrained to Normal values. A manual Critical event creates at most one unresolved Critical incident per zone and one mock notification for that incident. Existing unresolved legacy records are retained in history; legacy records are not treated as current zone statuses or open Critical incidents. After a manual Critical event, subsequent automatic readings remain Normal and do not resolve the Critical incident; an operator must explicitly resolve it. Resolve itself does not change the current reading/status.

## SMS notifications

The dashboard retains notification history, but notifications are simulated records only. A manual Critical incident stores one `MOCK_RECORDED` notification with its message and timestamp in Supabase. No external SMS service, network request, recipient configuration, or delivery claim is used by this project. Any preserved legacy notification rows are displayed as historical records.

## Tests and build

From `backend/`, with its virtual environment active: `python -m pytest`. Unit tests cover Normal/Critical classification, gas-specific critical settings, Normal-only simulation for all zones, multi-zone critical events, incident deduplication, Critical mock notifications and the absence of outbound HTTP calls, authentication, simulator start/stop controls, request validation, reading/incident writes, and reset authorization/scope. They use fake database connections; persistence against a real PostgreSQL instance still needs an integration environment. Frontend production build: from `frontend/`, run `npm ci`, then `npm run build`.

## Deployment

This repository does **not** deploy services, create cloud resources, or send SMS.

1. **Supabase:** create the project and apply migrations `001_initial.sql` and `002_severity_notifications.sql`, in that order. Use a database password that is not reused elsewhere. Configure network access and TLS for the backend host; never expose this URL or a Supabase service-role key to Vite.
2. **Backend:** deploy the `backend/` directory to Render (or another Python ASGI host) using [`backend/render.yaml`](backend/render.yaml): install `requirements.lock`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`. Set `DATABASE_URL`, `SESSION_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `CORS_ORIGINS` in the host's secret environment settings. Use a unique session secret of at least 32 characters, a non-default username, and a password of at least 16 characters. `CORS_ORIGINS` must be a comma-separated exact list of HTTPS frontend origins without paths. The manifest keeps `AUTH_REQUIRED=true`, caps simulation at 900 seconds (configurable up to 3600). Notifications always remain mock-only, with no SMS provider credentials or configuration. Configure persistent host availability appropriate to the presentation and confirm its current free-plan sleep, runtime, connection, and usage limits; free tiers may sleep or change and do not guarantee continuous monitoring.
3. **Vercel:** import the repository and set the project root directory to `frontend`. Build command `npm run build`, output directory `dist`. Set `VITE_API_BASE_URL` to the deployed backend origin (no secret), then deploy through Vercel. Add the final Vercel origin to backend `CORS_ORIGINS` and redeploy/restart the backend.
4. Check `https://<backend>/api/health`, open `https://<backend>/docs`, then verify the dashboard connection and read-only state. Operator actions require an authorized session; the frontend intentionally does not provide a sign-in control. Test only with SMS disabled.
5. Notification history is mock-only; this repository cannot send real SMS.

## Configuration reference

Backend values are listed in [`backend/.env.example`](backend/.env.example); frontend value in [`frontend/.env.example`](frontend/.env.example). Core settings include `DATABASE_URL`, `CORS_ORIGINS`, `APP_ENV`, `SESSION_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `AUTH_REQUIRED`, `SIMULATOR_INTERVAL_SECONDS`, `SIMULATOR_MAX_RUNTIME_SECONDS`, and `CRITICAL_SIMULATION_COOLDOWN_SECONDS`. Secrets belong in the backend host secret manager in production. Never use a `VITE_` variable for a private value.

## Known limitations

- The local simulator is process-local and this service must run one worker and one instance. Multi-instance scheduling/locking is future work.
- Public read APIs expose monitoring state. The WebSocket and privileged APIs require authentication for non-loopback clients; local no-login mode is loopback-only. Login uses one environment-configured account and an in-process rate limit; add individual accounts and shared rate limiting before real operational use.
- Notification records are simulated and do not represent messages sent to a recipient.
- Database retention/partitioning, observability, backup/restore drills, and production integration/load tests need operational planning.
- Vercel and Python/Supabase free tier availability and quotas vary by provider, region, and date; an always-on system may require paid resources.
