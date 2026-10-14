import asyncio
import hmac
import json
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from math import ceil
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .config import settings
from .database import connect, close, require_pool
from .notifications import notify_incident
from .schemas import ActorRequest, LoginRequest, SimulationCriticalRequest, ThresholdUpdate, classify
from .security import COOKIE, create_session, current_operator, is_loopback_connection, local_demo_allowed, validate_remote_auth_configuration

simulator_task: asyncio.Task | None = None
simulator_enabled = False
simulator_control_lock = asyncio.Lock()
clients: set[WebSocket] = set()
login_failures: dict[str, tuple[int, float]] = {}
critical_simulation_last_trigger: dict[int, float] = {}


def api_json(value):
    """Convert database values to JSON-safe API values, keeping decimals numeric."""
    return jsonable_encoder(value, custom_encoder={Decimal: float})


def check_critical_simulation_cooldown(zone_id: int, now: float) -> None:
    last_trigger = critical_simulation_last_trigger.get(zone_id)
    if last_trigger is not None and now - last_trigger < settings.critical_simulation_cooldown_seconds:
        retry_after = max(1, ceil(settings.critical_simulation_cooldown_seconds - (now - last_trigger)))
        raise HTTPException(429, f"Wait {retry_after} seconds before triggering this zone again")


def validate_production_configuration() -> None:
    """Fail closed instead of starting a publicly deployed API with unsafe defaults."""
    problems = []
    environment = settings.app_env.strip().lower()
    if environment not in {"development", "test", "production"}:
        problems.append("APP_ENV must be development, test, or production")
    if settings.sms_enabled and settings.sms_mode.lower() == "real" and not settings.real_sms_ready:
        problems.append("Real SMS is enabled but provider credentials, sender, or recipient configuration is incomplete")
    if settings.real_sms_ready and not settings.auth_required:
        problems.append("AUTH_REQUIRED must be true before real SMS can be enabled")
    if environment in {"development", "test"}:
        if problems:
            raise RuntimeError("Unsafe notification configuration: " + "; ".join(problems))
        return
    if not settings.auth_required:
        problems.append("AUTH_REQUIRED must be true in production")
    if not settings.database_url:
        problems.append("DATABASE_URL is required")
    if len(settings.session_secret) < 32 or settings.session_secret == "development-only-change-this-secret":
        problems.append("SESSION_SECRET must be a unique secret of at least 32 characters")
    if not settings.admin_username or settings.admin_username.strip().lower() in {"admin", "operator"}:
        problems.append("ADMIN_USERNAME must be configured and must not use a default account name")
    if len(settings.admin_password) < 16:
        problems.append("ADMIN_PASSWORD must be at least 16 characters")
    if settings.admin_password == "replace-with-a-strong-password":
        problems.append("ADMIN_PASSWORD must not use the example placeholder")
    origins = settings.allowed_origins
    if not origins or "*" in origins or any("localhost" in origin or "127.0.0.1" in origin for origin in origins):
        problems.append("CORS_ORIGINS must contain exact production origins, not a wildcard or localhost")
    for origin in origins:
        parsed_origin = urlsplit(origin)
        if (parsed_origin.scheme != "https" or not parsed_origin.netloc or parsed_origin.path not in {"", "/"}
                or parsed_origin.query or parsed_origin.fragment or parsed_origin.username or parsed_origin.password):
            problems.append("CORS_ORIGINS entries must be exact HTTPS origins without paths or credentials")
            break
    if not 60 <= settings.simulator_max_runtime_seconds <= 3600:
        problems.append("SIMULATOR_MAX_RUNTIME_SECONDS must be between 60 and 3600")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


async def snapshot(conn):
    rows = await conn.fetch("""SELECT z.id,z.name,z.gas_name,z.formula,z.unit,z.warning_threshold,z.critical_threshold,
        latest.value AS current_reading, prev.value AS previous_reading,latest.measured_at AS last_updated,
        CASE WHEN latest.value IS NULL THEN 'NORMAL'
             WHEN latest.value >= z.critical_threshold THEN 'CRITICAL'
             WHEN latest.value >= z.warning_threshold THEN 'WARNING' ELSE 'NORMAL' END AS status,
        i.id::text AS active_incident_id
        FROM zones z LEFT JOIN LATERAL (SELECT value,measured_at FROM readings WHERE zone_id=z.id ORDER BY measured_at DESC,id DESC LIMIT 1) latest ON true
        LEFT JOIN LATERAL (SELECT value FROM readings WHERE zone_id=z.id ORDER BY measured_at DESC,id DESC OFFSET 1 LIMIT 1) prev ON true
        LEFT JOIN LATERAL (SELECT id,severity FROM incidents WHERE zone_id=z.id AND state <> 'RESOLVED' ORDER BY created_at DESC LIMIT 1) i ON true
        ORDER BY z.id""")
    return api_json([dict(r) for r in rows])


async def broadcast():
    try:
        async with require_pool().acquire() as conn:
            payload = json.dumps({"type": "state", "simulator_running": simulator_enabled, "zones": await snapshot(conn)})
        stale = []
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        clients.difference_update(stale)
    except Exception:
        return


async def save_reading(conn, zone, value: float, critical_latched: bool = False):
    now = datetime.now(timezone.utc)
    status = classify(value, zone["warning_threshold"], zone["critical_threshold"])
    await conn.execute("INSERT INTO readings(zone_id,value,unit,measured_at) VALUES($1,$2,$3,$4)", zone["id"], value, zone["unit"], now)
    # An unresolved critical incident already represents this continuous event.
    # Keep recording readings without querying/updating the same incident each tick.
    if status == "NORMAL" or (status == "CRITICAL" and critical_latched):
        return
    if status != "NORMAL":
        incident = await conn.fetchrow("""SELECT * FROM incidents WHERE zone_id=$1 AND state <> 'RESOLVED' ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", zone["id"])
        if incident is None:
            incident = await conn.fetchrow("""INSERT INTO incidents(zone_id,gas_name,formula,severity,reading,unit,
                warning_threshold_at_creation,critical_threshold_at_creation,threshold,created_at,critical_at,state)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'OPEN') RETURNING *""", zone["id"], zone["gas_name"], zone["formula"], status,
                value, zone["unit"], zone["warning_threshold"], zone["critical_threshold"],
                zone["critical_threshold"] if status == "CRITICAL" else zone["warning_threshold"],
                now, now if status == "CRITICAL" else None)
            await notify_incident(conn, {**dict(incident), "zone_name": zone["name"]}, status)
        elif incident["severity"] == "WARNING" and status == "CRITICAL":
            incident = await conn.fetchrow("UPDATE incidents SET severity='CRITICAL',reading=$1,escalation_threshold=$2,critical_at=$3 WHERE id=$4 RETURNING *",
                                           value, zone["critical_threshold"], now, incident["id"])
            await notify_incident(conn, {**dict(incident), "zone_name": zone["name"]}, "CRITICAL")


def simulation_reading(zone, prior: float | None, critical_latched: bool) -> float:
    step = max(float(zone["normal_max"]) * 0.035, 0.01)
    if critical_latched:
        floor = float(zone["critical_threshold"])
        baseline = max(float(prior) if prior is not None else floor, floor)
        increase = max(step, floor * 0.01, 0.01)
        return round(baseline + increase, 3)
    center = min(float(zone["warning_threshold"]) * 0.48, float(zone["normal_max"]))
    base = float(prior) if prior is not None and prior < zone["warning_threshold"] else center
    return round(min(max(base + random.uniform(-step, step), float(zone["normal_min"])), float(zone["normal_max"])), 3)


async def simulator_loop():
    global simulator_enabled, simulator_task
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.simulator_max_runtime_seconds
    while simulator_enabled and loop.time() < deadline:
        cycle_started = loop.time()
        try:
            async with require_pool().acquire() as conn:
                async with conn.transaction():
                    zones = await conn.fetch("""SELECT z.*,
                        latest.value AS prior_reading,
                        EXISTS(SELECT 1 FROM incidents i WHERE i.zone_id=z.id AND i.severity='CRITICAL' AND i.state <> 'RESOLVED') AS critical_latched
                        FROM zones z
                        LEFT JOIN LATERAL (SELECT value FROM readings WHERE zone_id=z.id ORDER BY measured_at DESC,id DESC LIMIT 1) latest ON true
                        ORDER BY z.id""")
                    for zone in zones:
                        critical_latched = bool(zone["critical_latched"])
                        await save_reading(conn, zone, simulation_reading(zone, zone["prior_reading"], critical_latched), critical_latched)
            await broadcast()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep the task alive across transient DB errors; health remains available and next cycle retries.
            pass
        interval = max(0.5, settings.simulator_interval_seconds)
        elapsed = loop.time() - cycle_started
        await asyncio.sleep(max(0.05, interval - elapsed))
    if simulator_enabled and loop.time() >= deadline:
        simulator_enabled = False
        simulator_task = None
        await broadcast()


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_production_configuration()
    await connect()
    yield
    global simulator_enabled, simulator_task
    simulator_enabled = False
    if simulator_task:
        simulator_task.cancel()
        try:
            await simulator_task
        except asyncio.CancelledError:
            pass
    await close()


app = FastAPI(title="Real-Time Industrial Gas Monitoring and Alert System API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def check_mutation_origin(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        own_origin = str(request.base_url).rstrip("/")
        if not is_loopback_connection(request) and not origin:
            return JSONResponse(status_code=403, content={"detail": "Origin header is required"})
        if origin and origin not in {*settings.allowed_origins, own_origin}:
            return JSONResponse(status_code=403, content={"detail": "Origin is not allowed"})
    return await call_next(request)


# Add CORS after the origin guard so Starlette places it outermost. This ensures
# browser preflights are handled before route/auth checks and CORS headers are
# present even on origin-guard or authentication error responses.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type"],
)


@app.get("/api/health")
async def health():
    db = "disconnected"
    try:
        if require_pool():
            async with require_pool().acquire() as conn:
                await conn.fetchval("SELECT 1")
            db = "connected"
    except Exception:
        pass
    return {"status": "ok" if db == "connected" else "degraded", "database": db,
            "simulator_running": simulator_enabled, "sms_mode": "real" if settings.real_sms_ready else "mock"}


@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request, response: Response):
    validate_remote_auth_configuration(request)
    if not settings.admin_password:
        raise HTTPException(503, "Set ADMIN_PASSWORD before enabling operator actions")
    ip = request.client.host if request.client else "unknown"
    now = asyncio.get_running_loop().time()
    for failed_ip, (_, first_failure) in list(login_failures.items()):
        if now - first_failure >= 300:
            login_failures.pop(failed_ip, None)
    if ip not in login_failures and len(login_failures) >= 10000:
        raise HTTPException(429, "Sign-in is temporarily unavailable. Retry shortly.")
    count, first_failure = login_failures.get(ip, (0, 0.0))
    if count >= 5 and now - first_failure < 300:
        raise HTTPException(429, "Too many sign-in attempts. Wait five minutes and retry.")
    if not (hmac.compare_digest(body.username, settings.admin_username) and hmac.compare_digest(body.password, settings.admin_password)):
        login_failures[ip] = (count + 1 if now - first_failure < 300 else 1, first_failure if now - first_failure < 300 else now)
        raise HTTPException(401, "Invalid username or password")
    login_failures.pop(ip, None)
    secure_cookie = settings.app_env.strip().lower() == "production" or not is_loopback_connection(request)
    response.set_cookie(COOKIE, create_session(body.username), httponly=True, secure=secure_cookie,
                        samesite="none" if secure_cookie else "lax", max_age=28800, path="/")
    return {"authenticated": True, "username": body.username}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"authenticated": False}


@app.get("/api/auth/session")
async def session(request: Request):
    demo_reset_allowed = (
        settings.app_env.strip().lower() in {"development", "test"}
        and is_loopback_connection(request)
    )
    if local_demo_allowed(request):
        return {"authenticated": False, "auth_required": False, "demo_reset_allowed": demo_reset_allowed}
    auth_required = True
    try:
        return {"authenticated": True, "username": current_operator(request), "auth_required": auth_required,
                "demo_reset_allowed": demo_reset_allowed}
    except HTTPException:
        return {"authenticated": False, "auth_required": auth_required, "demo_reset_allowed": demo_reset_allowed}


@app.get("/api/zones")
async def zones():
    async with require_pool().acquire() as conn:
        return await snapshot(conn)


@app.get("/api/state")
async def state():
    async with require_pool().acquire() as conn:
        return {"simulator_running": simulator_enabled, "zones": await snapshot(conn)}


@app.get("/api/readings")
async def readings(zone_id: int | None = None, limit: int = 120):
    if limit < 1 or limit > 1000:
        raise HTTPException(422, "limit must be between 1 and 1000")
    async with require_pool().acquire() as conn:
        if zone_id is not None and not 1 <= zone_id <= 4:
            raise HTTPException(422, "Unknown zone")
        rows = await conn.fetch("""SELECT r.id,r.zone_id,z.name AS zone_name,z.gas_name,r.value,r.unit,r.measured_at
            FROM readings r JOIN zones z ON z.id=r.zone_id WHERE ($1::int IS NULL OR r.zone_id=$1)
            ORDER BY r.measured_at DESC LIMIT $2""", zone_id, limit)
        return api_json([dict(r) for r in reversed(rows)])


@app.get("/api/incidents")
async def incidents(limit: int = 100):
    if not 1 <= limit <= 500:
        raise HTTPException(422, "limit must be between 1 and 500")
    async with require_pool().acquire() as conn:
        rows = await conn.fetch("""SELECT i.*,COALESCE(i.escalation_threshold,i.threshold) AS display_threshold,i.id::text AS incident_id,z.name AS zone_name,
            n.mode AS notification_mode,n.status AS notification_status
            FROM incidents i JOIN zones z ON z.id=i.zone_id LEFT JOIN LATERAL
            (SELECT mode,status FROM notifications WHERE incident_id=i.id ORDER BY created_at DESC LIMIT 1) n ON true
            ORDER BY i.created_at DESC LIMIT $1""", limit)
        return api_json([dict(r) for r in rows])


@app.get("/api/notifications")
async def notifications(limit: int = 100):
    if not 1 <= limit <= 500:
        raise HTTPException(422, "limit must be between 1 and 500")
    async with require_pool().acquire() as conn:
        rows = await conn.fetch("""SELECT n.id,n.incident_id,n.event_severity,n.mode,n.status,n.message,n.provider_message_id,n.created_at,
            i.zone_id,z.name AS zone_name,i.gas_name,i.reading,i.unit,i.created_at AS event_time
            FROM notifications n JOIN incidents i ON i.id=n.incident_id JOIN zones z ON z.id=i.zone_id
            ORDER BY n.created_at DESC LIMIT $1""", limit)
        return api_json([dict(r) for r in rows])


@app.get("/api/zones/{zone_id}/readings")
async def zone_readings(zone_id: int, limit: int = 300):
    return await readings(zone_id, limit)


@app.put("/api/zones/{zone_id}/thresholds")
async def update_thresholds(zone_id: int, body: ThresholdUpdate, request: Request):
    actor = current_operator(request)
    if body.critical_threshold <= body.warning_threshold:
        raise HTTPException(422, "Critical threshold must be greater than warning threshold")
    async with require_pool().acquire() as conn:
        row = await conn.fetchrow("UPDATE zones SET warning_threshold=$1,critical_threshold=$2,normal_min=LEAST(normal_min,$1*0.4),normal_max=LEAST(normal_max,$1*0.8),updated_at=now() WHERE id=$3 RETURNING id",
                                  body.warning_threshold, body.critical_threshold, zone_id)
        if not row:
            raise HTTPException(404, "Zone not found")
        await conn.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,metadata) VALUES($1,'UPDATE_THRESHOLDS','zone',$2,$3::jsonb)",
                           actor, str(zone_id), json.dumps(body.model_dump()))
        return api_json(dict(await conn.fetchrow("SELECT id,warning_threshold,critical_threshold FROM zones WHERE id=$1", zone_id)))


@app.post("/api/simulation/start")
async def start_simulation(request: Request):
    global simulator_task, simulator_enabled
    current_operator(request)
    async with simulator_control_lock:
        if not simulator_enabled:
            simulator_enabled = True
            simulator_task = asyncio.create_task(simulator_loop())
    return {"simulator_running": True}


@app.post("/api/simulation/stop")
async def stop_simulation(request: Request):
    global simulator_task, simulator_enabled
    current_operator(request)
    async with simulator_control_lock:
        simulator_enabled = False
        task = simulator_task
        simulator_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    asyncio.create_task(broadcast())
    return {"simulator_running": False}


@app.post("/api/demo/reset")
async def reset_demo_data(request: Request):
    """Clear generated demo history without deleting zone or threshold configuration."""
    global simulator_task, simulator_enabled
    actor = current_operator(request)
    if settings.app_env.strip().lower() not in {"development", "test"} or not is_loopback_connection(request):
        raise HTTPException(404, "Demo data reset is only available in development or test")

    async with simulator_control_lock:
        simulator_enabled = False
        task = simulator_task
        simulator_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        async with require_pool().acquire() as conn:
            async with conn.transaction():
                notifications_deleted = await conn.fetchval(
                    "WITH deleted AS (DELETE FROM notifications RETURNING 1) SELECT count(*) FROM deleted"
                )
                readings_deleted = await conn.fetchval(
                    "WITH deleted AS (DELETE FROM readings RETURNING 1) SELECT count(*) FROM deleted"
                )
                incidents_deleted = await conn.fetchval(
                    "WITH deleted AS (DELETE FROM incidents RETURNING 1) SELECT count(*) FROM deleted"
                )
                await conn.execute(
                    "INSERT INTO audit_events(actor,action,entity_type,entity_id,metadata) "
                    "VALUES($1,'RESET_DEMO_DATA','demo','all',$2::jsonb)",
                    actor,
                    json.dumps({
                        "notifications_deleted": notifications_deleted,
                        "readings_deleted": readings_deleted,
                        "incidents_deleted": incidents_deleted,
                    }),
                )
        critical_simulation_last_trigger.clear()

    await broadcast()
    return {
        "message": "Demo data reset successfully",
        "simulator_running": False,
        "deleted": {
            "readings": readings_deleted,
            "incidents": incidents_deleted,
            "notifications": notifications_deleted,
        },
        "preserved": ["zones", "thresholds", "application settings", "audit history"],
    }


@app.post("/api/simulation/critical")
async def critical_simulation(body: SimulationCriticalRequest, request: Request):
    global simulator_task, simulator_enabled
    actor = current_operator(request)
    async with simulator_control_lock:
        now = asyncio.get_running_loop().time()
        check_critical_simulation_cooldown(body.zone_id, now)
        async with require_pool().acquire() as conn:
            async with conn.transaction():
                zone = await conn.fetchrow("SELECT * FROM zones WHERE id=$1 FOR UPDATE", body.zone_id)
                if not zone:
                    raise HTTPException(404, "Zone not found")
                value = round(float(zone["critical_threshold"]) * 1.08, 3)
                await save_reading(conn, zone, value)
                await conn.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id) VALUES($1,'CRITICAL_SIMULATION','zone',$2)", actor, str(zone["id"]))
        critical_simulation_last_trigger[body.zone_id] = now
        if not simulator_enabled:
            simulator_enabled = True
            simulator_task = asyncio.create_task(simulator_loop())
    asyncio.create_task(broadcast())
    return {"zone_id": body.zone_id, "reading": value, "status": "CRITICAL"}


async def change_incident(incident_id: str, actor: str, action: str):
    column, state = ("acknowledged_at", "ACKNOWLEDGED") if action == "acknowledge" else ("resolved_at", "RESOLVED")
    async with require_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(f"UPDATE incidents SET {column}=COALESCE({column},now()),{column.replace('_at','_by')}=COALESCE({column.replace('_at','_by')},$2),state=$3 WHERE id=$1 AND state <> 'RESOLVED' RETURNING *", incident_id, actor, state)
            if not row:
                exists = await conn.fetchval("SELECT 1 FROM incidents WHERE id=$1", incident_id)
                if not exists:
                    raise HTTPException(404, "Incident not found")
                raise HTTPException(409, "Incident is already resolved")
            await conn.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id) VALUES($1,$2,'incident',$3)", actor, action.upper(), incident_id)
            return api_json(dict(row))


@app.post("/api/incidents/{incident_id}/acknowledge")
async def acknowledge(incident_id: str, body: ActorRequest, request: Request):
    actor = current_operator(request)
    result = await change_incident(incident_id, actor, "acknowledge")
    asyncio.create_task(broadcast())
    return result


@app.post("/api/incidents/{incident_id}/resolve")
async def resolve(incident_id: str, body: ActorRequest, request: Request):
    actor = current_operator(request)
    result = await change_incident(incident_id, actor, "resolve")
    asyncio.create_task(broadcast())
    return result


@app.websocket("/api/ws")
async def websocket_state(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin not in settings.allowed_origins:
        await ws.close(code=1008)
        return
    try:
        current_operator(ws)
    except HTTPException:
        await ws.close(code=1008)
        return
    await ws.accept()
    clients.add(ws)
    try:
        async with require_pool().acquire() as conn:
            await ws.send_json({"type": "state", "simulator_running": simulator_enabled, "zones": await snapshot(conn)})
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                # Revalidate the signed cookie periodically so expired sessions are closed.
                current_operator(ws)
    except (WebSocketDisconnect, RuntimeError, HTTPException):
        pass
    finally:
        clients.discard(ws)
