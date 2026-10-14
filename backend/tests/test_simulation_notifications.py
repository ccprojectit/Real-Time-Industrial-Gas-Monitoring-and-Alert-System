import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import pytest
from pydantic import ValidationError
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main, notifications
from app.security import current_operator
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def disable_external_database_for_api_tests(monkeypatch):
    async def no_database_connect():
        return None

    async def no_database_close():
        return None

    monkeypatch.setattr(main, "connect", no_database_connect)
    monkeypatch.setattr(main, "close", no_database_close)
    main.critical_simulation_last_trigger.clear()
    yield
    main.critical_simulation_last_trigger.clear()


class MemoryConnection:
    def __init__(self, active=None):
        self.active = active
        self.readings = []
        self.created = []
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "INSERT INTO readings" in sql:
            self.readings.append(args)

    async def fetchval(self, sql, *args):
        if "FROM notifications" in sql:
            return None
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        if "SELECT * FROM incidents" in sql:
            return self.active if self.active and self.active.get("state") != "RESOLVED" else None
        if "INSERT INTO incidents" in sql:
            self.active = {
                "id": f"incident-{len(self.created) + 1}", "severity": args[3],
                "reading": args[4], "unit": args[5], "warning_threshold_at_creation": args[6],
                "critical_threshold_at_creation": args[7], "threshold": args[8],
                "created_at": args[9], "critical_at": args[10], "gas_name": args[1],
            }
            self.created.append(self.active)
            return self.active
        if "UPDATE incidents SET severity" in sql:
            self.active = {**self.active, "severity": "CRITICAL", "reading": args[0], "escalation_threshold": args[1]}
            return self.active
        if "UPDATE incidents SET state='RESOLVED'" in sql:
            self.active = {**self.active, "state": "RESOLVED", "resolved_at": args[0], "resolved_by": "Simulator recovery"}
            return {"id": self.active["id"]}
        raise AssertionError(sql)


class CriticalBatchConnection:
    def __init__(self, fail_notification_number=None):
        self.zones = [
            {"id":1,"name":"Zone 1","gas_name":"Carbon Monoxide","formula":"CO","unit":"ppm","warning_threshold":35,"critical_threshold":100,"normal_min":0,"normal_max":12},
            {"id":2,"name":"Zone 2","gas_name":"Methane","formula":"CH4","unit":"% LEL","warning_threshold":10,"critical_threshold":20,"normal_min":0,"normal_max":4},
            {"id":3,"name":"Zone 3","gas_name":"Hydrogen Sulfide","formula":"H2S","unit":"ppm","warning_threshold":10,"critical_threshold":20,"normal_min":0,"normal_max":3},
            {"id":4,"name":"Zone 4","gas_name":"Ammonia","formula":"NH3","unit":"ppm","warning_threshold":25,"critical_threshold":50,"normal_min":0,"normal_max":8},
        ]
        self.readings=[]; self.incidents=[]; self.notifications=[]; self.audit=[]
        self.fail_notification_number=fail_notification_number; self.notification_attempts=0

    class Tx:
        def __init__(self, conn): self.conn=conn
        async def __aenter__(self):
            self.snapshot=copy.deepcopy((self.conn.readings,self.conn.incidents,self.conn.notifications,self.conn.audit))
            return self
        async def __aexit__(self, exc_type, *_):
            if exc_type:
                self.conn.readings,self.conn.incidents,self.conn.notifications,self.conn.audit=self.snapshot
            return False

    def transaction(self): return self.Tx(self)

    async def fetch(self, sql, zone_ids):
        if "FROM zones" not in sql: raise AssertionError(sql)
        selected=set(zone_ids)
        return [zone for zone in self.zones if zone["id"] in selected]

    async def fetchval(self, sql, *args):
        if "FROM notifications" in sql:
            return next((n["id"] for n in self.notifications if n["incident_id"]==args[0] and n["event_severity"]==args[1]),None)
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "SELECT * FROM incidents" in sql:
            return next((i for i in self.incidents if i["zone_id"]==args[0] and i["state"]!="RESOLVED"),None)
        if "INSERT INTO incidents" in sql:
            incident={"id":f"incident-{len(self.incidents)+1}","zone_id":args[0],"gas_name":args[1],"formula":args[2],
                "severity":args[3],"reading":args[4],"unit":args[5],"warning_threshold_at_creation":args[6],
                "critical_threshold_at_creation":args[7],"threshold":args[8],"created_at":args[9],"critical_at":args[10],"state":"OPEN"}
            self.incidents.append(incident)
            return incident
        if "UPDATE incidents SET severity" in sql:
            incident=next(i for i in self.incidents if i["id"]==args[3])
            incident.update({"severity":"CRITICAL","reading":args[0],"escalation_threshold":args[1],"critical_at":args[2]})
            return incident
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "INSERT INTO readings" in sql:
            self.readings.append({"zone_id":args[0],"value":args[1],"unit":args[2],"measured_at":args[3]})
        elif "INSERT INTO notifications" in sql:
            self.notification_attempts+=1
            if self.fail_notification_number==self.notification_attempts:
                raise RuntimeError("simulated notification persistence failure")
            self.notifications.append({"id":f"notification-{len(self.notifications)+1}","incident_id":args[0],
                "event_severity":args[1],"mode":args[2],"status":args[3],"message":args[4]})
        elif "INSERT INTO audit_events" in sql:
            self.audit.append((sql,args))
        else:
            raise AssertionError(sql)


class CriticalBatchPool:
    def __init__(self, conn): self.conn=conn
    def acquire(self): return self
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *_): return False


def test_critical_simulation_creates_one_incident_per_selected_zone(monkeypatch):
    calls = []

    async def no_sms(conn, incident, severity):
        calls.append((incident, severity))

    monkeypatch.setattr(main, "notify_incident", no_sms)
    for zone_id, gas, formula, unit, warning, critical in [
        (1, "Carbon Monoxide", "CO", "ppm", 35, 100),
        (2, "Methane", "CH4", "% LEL", 10, 20),
        (3, "Hydrogen Sulfide", "H2S", "ppm", 10, 20),
        (4, "Ammonia", "NH3", "ppm", 25, 50),
    ]:
        async def run_one():
            conn = MemoryConnection()
            zone = {"id": zone_id, "name": f"Zone {zone_id}", "gas_name": gas, "formula": formula,
                    "unit": unit, "warning_threshold": warning, "critical_threshold": critical}
            await main.save_reading(conn, zone, critical * 1.08)
            assert len(conn.readings) == 1
            assert conn.created[0]["severity"] == "CRITICAL"
            assert conn.created[0]["threshold"] == critical
            assert conn.created[0]["warning_threshold_at_creation"] == warning
            assert conn.created[0]["critical_threshold_at_creation"] == critical
            assert any("INSERT INTO readings" in sql for sql, _ in conn.executed)
            assert any("INSERT INTO incidents" in sql for sql, _ in conn.executed)

        asyncio.run(run_one())
    assert len(calls) == 4
    assert all(severity == "CRITICAL" for _, severity in calls)


def test_critical_api_persists_immediately_and_rejects_rapid_duplicate(monkeypatch):
    zone = {"id":1, "name":"Zone 1", "gas_name":"Carbon Monoxide", "formula":"CO", "unit":"ppm",
            "warning_threshold":35, "critical_threshold":100, "normal_min":0, "normal_max":12}

    class Conn(MemoryConnection):
        class Tx:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False

        def transaction(self): return self.Tx()

        async def fetch(self, sql, *args):
            if "FROM zones" in sql:
                return [zone]
            raise AssertionError(sql)

        async def fetchrow(self, sql, *args):
            return await super().fetchrow(sql, *args)

    class Pool:
        def __init__(self): self.conn = Conn()
        def acquire(self): return self
        async def __aenter__(self): return self.conn
        async def __aexit__(self, *_): return False

    pool = Pool()
    notified = []
    created_tasks = []
    async def no_sms(_conn, incident, severity): notified.append((incident, severity))
    def hold_task(coro):
        coro.close()
        task = object()
        created_tasks.append(task)
        return task

    monkeypatch.setattr(main, "require_pool", lambda: pool)
    monkeypatch.setattr(main, "notify_incident", no_sms)
    monkeypatch.setattr(main, "current_operator", lambda _request: "demo-operator")
    monkeypatch.setattr(main.asyncio, "create_task", hold_task)
    monkeypatch.setattr(main, "simulator_enabled", False)
    monkeypatch.setattr(main, "simulator_task", None)
    monkeypatch.setattr(main.settings, "critical_simulation_cooldown_seconds", 5)
    main.critical_simulation_last_trigger.clear()
    from starlette.requests import Request
    request = Request({"type":"http", "method":"POST", "path":"/api/simulation/critical", "headers":[],
                       "client":("127.0.0.1",1234), "server":("127.0.0.1",8000), "scheme":"http"})

    async def run():
        first = await main.critical_simulation(main.SimulationCriticalRequest(zone_id=1), request)
        assert first["zone_id"] == 1
        assert first["reading"] == 108.0
        assert first["status"] == "CRITICAL"
        assert datetime.fromisoformat(first["measured_at"]).tzinfo is not None
        assert len(pool.conn.readings) == 1
        assert len(pool.conn.created) == 1
        assert notified[0][1] == "CRITICAL"
        assert len(created_tasks) == 2  # simulator loop plus immediate WebSocket broadcast
        with pytest.raises(main.HTTPException) as error:
            await main.critical_simulation(main.SimulationCriticalRequest(zone_id=1), request)
        assert error.value.status_code == 429
        assert len(pool.conn.readings) == 1

    asyncio.run(run())
    main.critical_simulation_last_trigger.clear()


def hold_background_task_factory():
    created=[]
    def hold(coro):
        coro.close()
        created.append(object())
        return created[-1]
    return created,hold


def test_multi_zone_critical_creates_records_only_for_selected_zones_and_deduplicates(monkeypatch):
    conn=CriticalBatchConnection(); pool=CriticalBatchPool(conn); created,hold_task=hold_background_task_factory()
    monkeypatch.setattr(main,"require_pool",lambda:pool)
    monkeypatch.setattr(main,"current_operator",lambda _request:"signed-in-operator")
    monkeypatch.setattr(main.asyncio,"create_task",hold_task)
    monkeypatch.setattr(main,"simulator_enabled",False); monkeypatch.setattr(main,"simulator_task",None)
    monkeypatch.setattr(main.settings,"critical_simulation_cooldown_seconds",5)
    monkeypatch.setattr(main,"broadcast",lambda:asyncio.sleep(0))
    from starlette.requests import Request
    request=Request({"type":"http","method":"POST","path":"/api/simulation/critical/batch","headers":[],
                     "client":("203.0.113.20",1234),"server":("127.0.0.1",8000),"scheme":"https"})

    async def run():
        result=await main.critical_simulation_batch(main.SimulationCriticalBatchRequest(zone_ids=[1,3]),request)
        assert result["simulator_running"] is True
        assert {item["zone_id"] for item in result["zones"]}=={1,3}
        assert len(conn.readings)==len(conn.incidents)==len(conn.notifications)==2
        for reading in conn.readings:
            zone=next(z for z in conn.zones if z["id"]==reading["zone_id"])
            assert reading["value"]>zone["critical_threshold"]
        assert {item["zone_id"] for item in conn.readings}=={1,3}
        assert all(item["status"]=="MOCK_RECORDED" and item["mode"]=="mock" for item in conn.notifications)
        assert len(created)==2  # one simulator task and one WebSocket broadcast
        with pytest.raises(main.HTTPException) as repeated:
            await main.critical_simulation_batch(main.SimulationCriticalBatchRequest(zone_ids=[1,3]),request)
        assert repeated.value.status_code==429
        assert len(conn.readings)==len(conn.incidents)==len(conn.notifications)==2

    asyncio.run(run())
    main.critical_simulation_last_trigger.clear()


def test_multi_zone_critical_all_four_zones_and_request_validation(monkeypatch):
    conn=CriticalBatchConnection(); pool=CriticalBatchPool(conn); _,hold_task=hold_background_task_factory()
    monkeypatch.setattr(main,"require_pool",lambda:pool)
    monkeypatch.setattr(main,"current_operator",lambda _request:"signed-in-operator")
    monkeypatch.setattr(main.asyncio,"create_task",hold_task)
    monkeypatch.setattr(main,"simulator_enabled",False); monkeypatch.setattr(main,"simulator_task",None)
    monkeypatch.setattr(main,"broadcast",lambda:asyncio.sleep(0))
    from starlette.requests import Request
    request=Request({"type":"http","method":"POST","path":"/api/simulation/critical/batch","headers":[],
                     "client":("127.0.0.1",1234),"server":("127.0.0.1",8000),"scheme":"http"})

    async def run():
        result=await main.critical_simulation_batch(main.SimulationCriticalBatchRequest(zone_ids=[1,2,3,4]),request)
        assert len(result["zones"])==4
        assert {i["zone_id"] for i in conn.readings}=={1,2,3,4}
        assert len(conn.incidents)==len(conn.notifications)==4
        for reading in conn.readings:
            zone=next(z for z in conn.zones if z["id"]==reading["zone_id"])
            assert reading["value"]>zone["critical_threshold"]
        with pytest.raises(main.HTTPException) as invalid:
            await main.critical_simulation_for_zones([99],"signed-in-operator")
        assert invalid.value.status_code==422
        assert len(conn.readings)==4

    with pytest.raises(ValidationError):
        main.SimulationCriticalBatchRequest(zone_ids=[])
    with pytest.raises(ValidationError):
        main.SimulationCriticalBatchRequest(zone_ids=[1,1])
    asyncio.run(run())
    main.critical_simulation_last_trigger.clear()


def test_multi_zone_critical_transaction_rolls_back_on_notification_failure(monkeypatch):
    conn=CriticalBatchConnection(fail_notification_number=2); pool=CriticalBatchPool(conn); _,hold_task=hold_background_task_factory()
    monkeypatch.setattr(main,"require_pool",lambda:pool)
    monkeypatch.setattr(main.asyncio,"create_task",hold_task)
    monkeypatch.setattr(main,"simulator_enabled",False); monkeypatch.setattr(main,"simulator_task",None)
    async def run():
        with pytest.raises(RuntimeError,match="notification persistence failure"):
            await main.critical_simulation_for_zones([1,2],"signed-in-operator")
    asyncio.run(run())
    assert conn.readings==[] and conn.incidents==[] and conn.notifications==[] and conn.audit==[]
    assert main.critical_simulation_last_trigger=={}
    assert main.simulator_enabled is False


def test_repeated_start_requests_create_only_one_simulator_task(monkeypatch):
    created = []
    def hold_task(coro):
        coro.close()
        task = object()
        created.append(task)
        return task

    monkeypatch.setattr(main, "current_operator", lambda _request: "demo-operator")
    monkeypatch.setattr(main.asyncio, "create_task", hold_task)
    monkeypatch.setattr(main, "simulator_enabled", False)
    monkeypatch.setattr(main, "simulator_task", None)
    from starlette.requests import Request
    request = Request({"type":"http", "method":"POST", "path":"/api/simulation/start", "headers":[],
                       "client":("127.0.0.1",1234), "server":("127.0.0.1",8000), "scheme":"http"})

    async def run():
        assert await main.start_simulation(request) == {"simulator_running":True}
        assert await main.start_simulation(request) == {"simulator_running":True}

    asyncio.run(run())
    assert len(created) == 1


def test_broadcast_sends_one_serializable_state_frame_per_connected_client(monkeypatch):
    class Conn:
        async def fetch(self, _sql):
            return [{"id":1, "name":"Zone 1", "gas_name":"Carbon Monoxide", "formula":"CO", "unit":"ppm",
                     "warning_threshold":35, "critical_threshold":100, "current_reading":12,
                     "previous_reading":11, "last_updated":datetime.now(timezone.utc),
                     "status":"NORMAL", "active_incident_id":None}]

    class Pool:
        def acquire(self): return self
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *_): return False

    class Socket:
        def __init__(self): self.frames=[]
        async def send_text(self, value): self.frames.append(value)

    socket=Socket()
    monkeypatch.setattr(main, "require_pool", lambda: Pool())
    monkeypatch.setattr(main, "clients", {socket})
    monkeypatch.setattr(main, "simulator_enabled", True)
    asyncio.run(main.broadcast())
    assert len(socket.frames)==1
    frame=json.loads(socket.frames[0])
    assert frame["type"]=="state"
    assert frame["simulator_running"] is True
    assert frame["zones"][0]["current_reading"]==12


def test_incident_deduplication_while_zone_condition_stays_active(monkeypatch):
    calls = []

    async def no_sms(conn, incident, severity):
        calls.append((incident, severity))

    monkeypatch.setattr(main, "notify_incident", no_sms)

    async def run():
        conn = MemoryConnection()
        zone = {"id": 1, "name": "Zone 1", "gas_name": "Carbon Monoxide", "formula": "CO",
                "unit": "ppm", "warning_threshold": 35, "critical_threshold": 100}
        await main.save_reading(conn, zone, 101)
        await main.save_reading(conn, zone, 105)
        assert len(conn.created) == 1
        assert len(calls) == 1

    asyncio.run(run())


def test_warning_crossing_creates_one_incident_and_mock_notification(monkeypatch):
    notified=[]
    async def record_mock(_conn, incident, severity):
        notified.append((incident["id"],severity))
    monkeypatch.setattr(main,"notify_incident",record_mock)

    async def run():
        conn=MemoryConnection()
        zone={"id":1,"name":"Zone 1","gas_name":"Carbon Monoxide","formula":"CO","unit":"ppm",
              "warning_threshold":35,"critical_threshold":100}
        await main.save_reading(conn,zone,36)
        await main.save_reading(conn,zone,37)
        assert len(conn.created)==1
        assert conn.created[0]["severity"]=="WARNING"
        assert notified==[(conn.created[0]["id"],"WARNING")]
    asyncio.run(run())


def test_warning_incident_escalates_once_to_critical(monkeypatch):
    calls = []

    async def no_sms(conn, incident, severity):
        calls.append((incident, severity))

    monkeypatch.setattr(main, "notify_incident", no_sms)

    async def run():
        conn = MemoryConnection()
        zone = {"id": 1, "name": "Zone 1", "gas_name": "Carbon Monoxide", "formula": "CO",
                "unit": "ppm", "warning_threshold": 35, "critical_threshold": 100}
        await main.save_reading(conn, zone, 40)
        assert conn.created[0]["severity"] == "WARNING"
        assert len(calls) == 1 and calls[0][1] == "WARNING"
        await main.save_reading(conn, zone, 100)
        assert conn.active["severity"] == "CRITICAL"
        assert conn.active["threshold"] == 35
        assert conn.active["escalation_threshold"] == 100
        assert len(calls) == 2
        assert calls[1][1] == "CRITICAL"

    asyncio.run(run())


class NotificationConnection:
    def __init__(self):
        self.saved = None

    async def fetchval(self, *_):
        return None

    async def execute(self, sql, *args):
        self.saved = args


@pytest.mark.parametrize("severity", ["WARNING", "CRITICAL"])
def test_warning_and_critical_incidents_create_mock_records_without_network(monkeypatch, severity):
    def forbidden_network_client(*_args, **_kwargs):
        raise AssertionError("Mock notification must never create an external HTTP client")

    # If a future change reintroduces an HTTP client call in this module, fail this test.
    monkeypatch.setattr(notifications, "httpx", type("Httpx", (), {"AsyncClient": forbidden_network_client}), raising=False)
    conn = NotificationConnection()
    incident = {"id": f"{severity.lower()}-1", "zone_name": "Zone 2", "gas_name": "Methane", "reading": 11,
                "unit": "% LEL", "created_at": datetime.now(timezone.utc)}

    asyncio.run(notifications.notify_incident(conn, incident, severity))

    assert conn.saved[0] == incident["id"]
    assert conn.saved[1] == severity
    assert conn.saved[2] == "mock"
    assert conn.saved[3] == "MOCK_RECORDED"
    assert f"{severity} gas alert" in conn.saved[4]
    assert conn.saved[5] is None
    assert "Mock only; no SMS sent" in json.loads(conn.saved[6])["detail"]


@pytest.mark.parametrize("zone", [
    {"warning_threshold":35,"critical_threshold":100,"normal_min":0,"normal_max":12},
    {"warning_threshold":10,"critical_threshold":20,"normal_min":0,"normal_max":4},
    {"warning_threshold":10,"critical_threshold":20,"normal_min":0,"normal_max":3},
    {"warning_threshold":25,"critical_threshold":50,"normal_min":0,"normal_max":8},
    {"warning_threshold":19.9995,"critical_threshold":20,"normal_min":0,"normal_max":19.9},
])
def test_automatic_simulation_samples_never_reach_critical(zone, monkeypatch):
    monkeypatch.setattr(main.random, "random", lambda: 1.0)
    monkeypatch.setattr(main.random, "uniform", lambda low, high: (low + high) / 2)
    for prior in (None, zone["normal_max"] * 0.4):
        reading = main.simulation_reading(zone, prior)
        assert zone["normal_min"] <= reading <= zone["normal_max"]
        assert reading < zone["warning_threshold"]
    for prior in (zone["warning_threshold"], zone["critical_threshold"] * 1.08):
        reading=main.simulation_reading(zone,prior)
        assert zone["normal_min"] <= reading < zone["critical_threshold"]


@pytest.mark.parametrize("zone", [
    {"warning_threshold":35,"critical_threshold":100,"normal_min":0,"normal_max":12},
    {"warning_threshold":10,"critical_threshold":20,"normal_min":0,"normal_max":4},
    {"warning_threshold":10,"critical_threshold":20,"normal_min":0,"normal_max":3},
    {"warning_threshold":25,"critical_threshold":50,"normal_min":0,"normal_max":8},
    {"warning_threshold":19.9995,"critical_threshold":20,"normal_min":0,"normal_max":19.9},
])
def test_automatic_simulation_can_enter_warning_but_stays_below_critical(zone, monkeypatch):
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    monkeypatch.setattr(main.random, "uniform", lambda low, high: high)
    reading = main.simulation_reading(zone, zone["normal_max"])
    assert zone["warning_threshold"] <= reading < zone["critical_threshold"]


def test_warning_recovery_resolves_only_the_warning_incident(monkeypatch):
    async def run():
        conn = MemoryConnection(active={"id":"warning-1","severity":"WARNING","state":"OPEN"})
        zone = {"id":1,"name":"Zone 1","gas_name":"Carbon Monoxide","formula":"CO","unit":"ppm",
                "warning_threshold":35,"critical_threshold":100}
        await main.save_reading(conn, zone, 10, recover_warning=True)
        assert conn.active["state"] == "RESOLVED"
        assert conn.active["resolved_by"] == "Simulator recovery"
        assert any("AUTO_RECOVERY" in sql for sql, _ in conn.executed)

    asyncio.run(run())


def test_critical_simulation_cooldown_returns_retry_after(monkeypatch):
    monkeypatch.setattr(main.settings, "critical_simulation_cooldown_seconds", 5)
    main.critical_simulation_last_trigger[1] = 100.0
    with pytest.raises(main.HTTPException) as error:
        main.check_critical_simulation_cooldown(1, 101.0)
    assert error.value.status_code == 429
    assert "4 seconds" in error.value.detail
    main.check_critical_simulation_cooldown(1, 105.0)


def test_development_no_auth_bypass_is_loopback_only(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main.settings, "auth_required", False)
    from starlette.requests import Request

    local = Request({"type":"http", "method":"GET", "path":"/", "headers":[],
                     "client":("127.0.0.1",1234), "server":("127.0.0.1",8000), "scheme":"http"})
    remote = Request({"type":"http", "method":"GET", "path":"/", "headers":[],
                      "client":("203.0.113.20",1234), "server":("127.0.0.1",8000), "scheme":"http"})

    assert current_operator(local) == "Local demo operator"
    with pytest.raises(main.HTTPException) as error:
        current_operator(remote)
    assert error.value.status_code == 503


def test_remote_development_still_requires_a_signed_in_operator(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main.settings, "auth_required", False)
    monkeypatch.setattr(main.settings, "session_secret", "a-unique-test-session-secret-with-more-than-32-chars")
    monkeypatch.setattr(main.settings, "admin_username", "project-operator")
    monkeypatch.setattr(main.settings, "admin_password", "secure-test-password-with-32-chars")
    from starlette.requests import Request
    remote = Request({"type":"http", "method":"GET", "path":"/", "headers":[],
                      "client":("203.0.113.20",1234), "server":("127.0.0.1",8000), "scheme":"http"})

    with pytest.raises(main.HTTPException) as error:
        current_operator(remote)
    assert error.value.status_code == 401


def test_websocket_rejects_untrusted_origin_before_accept():
    class Socket:
        headers = {"origin": "https://untrusted.example"}
        closed = None

        async def close(self, **kwargs):
            self.closed = kwargs

    socket = Socket()
    asyncio.run(main.websocket_state(socket))
    assert socket.closed["code"] == 1008


def test_websocket_requires_a_session_for_remote_connections(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "auth_required", True)
    monkeypatch.setattr(main.settings, "session_secret", "a-secure-test-secret-with-more-than-32-characters")
    monkeypatch.setattr(main.settings, "admin_username", "project-operator")
    monkeypatch.setattr(main.settings, "admin_password", "secure-test-password-with-32-chars")
    monkeypatch.setattr(main.settings, "cors_origins", "https://dashboard.example")

    class Socket:
        headers = {"origin": "https://dashboard.example"}
        client = SimpleNamespace(host="203.0.113.20", port=1234)
        cookies = {}
        closed = None

        async def close(self, **kwargs):
            self.closed = kwargs

    socket = Socket()
    asyncio.run(main.websocket_state(socket))
    assert socket.closed["code"] == 1008


def test_simulator_stops_after_configured_maximum_runtime(monkeypatch):
    monkeypatch.setattr(main.settings, "simulator_max_runtime_seconds", 0.12)
    monkeypatch.setattr(main.settings, "simulator_interval_seconds", 0.05)
    monkeypatch.setattr(main, "simulator_enabled", True)

    async def run():
        main.simulator_task = asyncio.current_task()
        await main.simulator_loop()

    asyncio.run(run())
    assert main.simulator_enabled is False
    assert main.simulator_task is None


def test_simulator_batches_zone_readings_in_one_database_write(monkeypatch):
    zones = [
        {"id": zone_id, "name": f"Zone {zone_id}", "gas_name": "Gas", "unit": "ppm",
         "warning_threshold": 50, "critical_threshold": 100, "normal_min": 0, "normal_max": 20,
         "prior_reading": 5, "active_incident_severity": None}
        for zone_id in range(1, 5)
    ]

    class Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class Conn:
        def __init__(self): self.batch = []
        def transaction(self): return Tx()
        async def fetch(self, _sql): return zones
        async def executemany(self, _sql, rows): self.batch = list(rows)
        async def fetchval(self, *_args): return None

    class Pool:
        def __init__(self): self.conn = Conn()
        def acquire(self): return self
        async def __aenter__(self): return self.conn
        async def __aexit__(self, *_): return False

    pool = Pool()
    monkeypatch.setattr(main, "require_pool", lambda: pool)
    monkeypatch.setattr(main, "simulator_enabled", True)
    monkeypatch.setattr(main, "simulator_task", None)
    monkeypatch.setattr(main, "broadcast", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main.random, "random", lambda: 1.0)

    async def end_after_one_cycle(_delay):
        main.simulator_enabled = False

    monkeypatch.setattr(main.asyncio, "sleep", end_after_one_cycle)
    asyncio.run(main.simulator_loop())

    assert len(pool.conn.batch) == 4
    assert {row[0] for row in pool.conn.batch} == {1, 2, 3, 4}
    assert all(row[1] < 50 for row in pool.conn.batch)


def test_unauthenticated_local_demo_can_start_and_stop_simulation(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main.settings, "auth_required", False)

    async def no_broadcast():
        return None

    monkeypatch.setattr(main, "broadcast", no_broadcast)
    with TestClient(main.app, client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/auth/session").json()["auth_required"] is False
        assert client.get("/api/auth/session").json()["demo_reset_allowed"] is True
        assert client.post("/api/simulation/start").status_code == 200
        assert client.post("/api/simulation/stop").status_code == 200


def test_operator_auth_required_and_simulator_start_stop(monkeypatch):
    monkeypatch.setattr(main.settings, "admin_username", "operator-test")
    monkeypatch.setattr(main.settings, "admin_password", "correct-horse")
    monkeypatch.setattr(main.settings, "auth_required", True)

    async def no_broadcast():
        return None

    monkeypatch.setattr(main, "broadcast", no_broadcast)
    with TestClient(main.app, client=("127.0.0.1", 1234)) as client:
        denied = client.post("/api/simulation/start")
        assert denied.status_code == 401
        invalid_credentials = client.post("/api/auth/login", json={"username":"operator-test", "password":"incorrect-password"})
        assert invalid_credentials.status_code == 401
        signed_in = client.post("/api/auth/login", json={"username":"operator-test", "password":"correct-horse"})
        assert signed_in.status_code == 200
        assert client.get("/api/auth/session").json()["authenticated"] is True
        started = client.post("/api/simulation/start")
        assert started.status_code == 200
        assert started.json()["simulator_running"] is True
        stopped = client.post("/api/simulation/stop")
        assert stopped.status_code == 200
        assert stopped.json()["simulator_running"] is False
        assert client.post("/api/auth/logout").status_code == 200
        assert client.post("/api/simulation/start").status_code == 401


def test_all_privileged_api_routes_require_authentication(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main.settings, "auth_required", True)
    with TestClient(main.app, client=("127.0.0.1", 1234)) as client:
        assert client.put("/api/zones/1/thresholds", json={"warning_threshold":35,"critical_threshold":100}).status_code == 401
        assert client.post("/api/simulation/critical", json={"zone_id":1}).status_code == 401
        assert client.post("/api/simulation/critical/batch", json={"zone_ids":[1,3]}).status_code == 401
        assert client.post("/api/demo/reset").status_code == 401
        assert client.post("/api/incidents/event-1/acknowledge", json={"actor":"Operator"}).status_code == 401
        assert client.post("/api/incidents/event-1/resolve", json={"actor":"Operator"}).status_code == 401


def test_api_validation_rejects_invalid_login_and_critical_zone():
    with TestClient(main.app, client=("127.0.0.1", 1234)) as client:
        invalid_login = client.post("/api/auth/login", json={"username":"", "password":""})
        assert invalid_login.status_code == 422
        invalid_zone = client.post("/api/simulation/critical", json={"zone_id":5})
        assert invalid_zone.status_code == 422


def test_mutating_requests_reject_unlisted_browser_origins(monkeypatch):
    monkeypatch.setattr(main.settings, "auth_required", True)
    with TestClient(main.app, client=("127.0.0.1", 1234)) as client:
        blocked = client.post("/api/simulation/start", headers={"Origin":"https://untrusted.example"})
        assert blocked.status_code == 403
        allowed_but_unauthenticated = client.post("/api/simulation/start", headers={"Origin":"http://localhost:5173"})
        assert allowed_but_unauthenticated.status_code == 401


def test_remote_production_mutations_require_an_allowed_origin(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "auth_required", True)
    monkeypatch.setattr(main.settings, "database_url", "postgresql://example")
    monkeypatch.setattr(main.settings, "session_secret", "a-unique-test-session-secret-with-more-than-32-chars")
    monkeypatch.setattr(main.settings, "admin_username", "project-operator")
    monkeypatch.setattr(main.settings, "admin_password", "strong-password-for-test")
    monkeypatch.setattr(main.settings, "cors_origins", "https://dashboard.example")
    with TestClient(main.app, client=("203.0.113.20", 1234)) as client:
        no_origin = client.post("/api/simulation/start")
        assert no_origin.status_code == 403
        denied_session = client.post("/api/simulation/start", headers={"Origin":"https://dashboard.example"})
        assert denied_session.status_code == 401


def test_acknowledge_and_resolve_record_actor_without_touching_zone_state(monkeypatch):
    class Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class Conn:
        def __init__(self):
            self.incident = {"id":"event-1", "state":"OPEN", "acknowledged_at":None,
                             "acknowledged_by":None, "resolved_at":None, "resolved_by":None}
            self.audit = []
            self.statements = []
        def transaction(self): return Tx()
        async def fetchrow(self, sql, *args):
            self.statements.append(sql)
            if sql.startswith("UPDATE incidents SET"):
                assert "zones" not in sql.lower()
                column, state = ("acknowledged", "ACKNOWLEDGED") if "acknowledged_at" in sql else ("resolved", "RESOLVED")
                self.incident.update({"state":state, f"{column}_at":"recorded", f"{column}_by":args[1]})
                return dict(self.incident)
            raise AssertionError(sql)
        async def fetchval(self, *_): return None
        async def execute(self, sql, *args): self.audit.append(args)

    class Pool:
        def __init__(self): self.conn = Conn()
        def acquire(self): return self
        async def __aenter__(self): return self.conn
        async def __aexit__(self, *_): return False

    pool = Pool()
    monkeypatch.setattr(main, "require_pool", lambda: pool)

    async def run():
        ack = await main.change_incident("event-1", "operator-test", "acknowledge")
        assert ack["state"] == "ACKNOWLEDGED"
        assert ack["acknowledged_by"] == "operator-test"
        resolved = await main.change_incident("event-1", "operator-test", "resolve")
        assert resolved["state"] == "RESOLVED"
        assert resolved["resolved_by"] == "operator-test"
        assert resolved["acknowledged_at"] == "recorded"
        assert len(pool.conn.audit) == 2

    asyncio.run(run())


def test_demo_reset_is_post_only_and_no_generic_delete_routes_are_registered():
    paths = main.app.openapi()["paths"]
    assert paths["/api/demo/reset"]["post"]
    assert all("delete" not in methods for methods in paths.values())
    assert "delete" not in paths["/api/demo/reset"]


def test_demo_reset_clears_history_preserves_zones_and_audits(monkeypatch):
    class Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class Conn:
        def __init__(self): self.deleted = []; self.audit = []
        def transaction(self): return Tx()
        async def fetchval(self, sql):
            self.deleted.append(sql)
            return 2
        async def execute(self, sql, *args): self.audit.append((sql, args))

    class Pool:
        def __init__(self): self.conn = Conn()
        def acquire(self): return self
        async def __aenter__(self): return self.conn
        async def __aexit__(self, *_): return False

    pool = Pool()
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main, "current_operator", lambda _request: "demo-operator")
    monkeypatch.setattr(main, "require_pool", lambda: pool)
    monkeypatch.setattr(main, "broadcast", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main, "simulator_enabled", False)
    monkeypatch.setattr(main, "simulator_task", None)
    from starlette.requests import Request
    request = Request({"type":"http", "method":"POST", "path":"/api/demo/reset", "headers":[],
                       "client":("127.0.0.1",1234), "server":("127.0.0.1",8000), "scheme":"http"})

    result = asyncio.run(main.reset_demo_data(request))

    assert ["notifications" in sql for sql in pool.conn.deleted] == [True, False, False]
    assert all("DELETE FROM zones" not in sql for sql in pool.conn.deleted)
    assert result["deleted"] == {"readings":2, "incidents":2, "notifications":2}
    assert "thresholds" in result["preserved"]
    assert len(pool.conn.audit) == 1
    assert "RESET_DEMO_DATA" in pool.conn.audit[0][0]


def test_authenticated_production_operator_can_reset_data_transactionally(monkeypatch):
    class Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class Conn:
        def __init__(self): self.deleted = []; self.audit = []
        def transaction(self): return Tx()
        async def fetchval(self, sql):
            self.deleted.append(sql)
            return 1
        async def execute(self, sql, *args): self.audit.append((sql, args))

    class Pool:
        def __init__(self): self.conn = Conn()
        def acquire(self): return self
        async def __aenter__(self): return self.conn
        async def __aexit__(self, *_): return False

    pool = Pool()
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main, "current_operator", lambda _request: "signed-in-operator")
    monkeypatch.setattr(main, "require_pool", lambda: pool)
    monkeypatch.setattr(main, "broadcast", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main, "simulator_enabled", False)
    monkeypatch.setattr(main, "simulator_task", None)
    from starlette.requests import Request
    request = Request({"type":"http", "method":"POST", "path":"/api/demo/reset", "headers":[],
                       "client":("127.0.0.1",1234), "server":("127.0.0.1",8000), "scheme":"http"})
    result = asyncio.run(main.reset_demo_data(request))
    assert result["simulator_running"] is False
    assert result["deleted"] == {"readings":1, "incidents":1, "notifications":1}
    assert all("DELETE FROM zones" not in sql for sql in pool.conn.deleted)
    assert len(pool.conn.audit) == 1


def test_duplicate_reset_is_rejected_without_waiting(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main, "current_operator", lambda _request: "signed-in-operator")
    from starlette.requests import Request
    request = Request({"type":"http", "method":"POST", "path":"/api/demo/reset", "headers":[],
                       "client":("203.0.113.20",1234), "server":("127.0.0.1",8000), "scheme":"https"})

    async def run():
        await main.demo_reset_lock.acquire()
        try:
            with pytest.raises(main.HTTPException) as error:
                await main.reset_demo_data(request)
            assert error.value.status_code == 409
        finally:
            main.demo_reset_lock.release()

    asyncio.run(run())


def test_demo_reset_is_unavailable_to_remote_development_clients(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main, "current_operator", lambda _request: "signed-in-operator")
    from starlette.requests import Request
    request = Request({"type":"http", "method":"POST", "path":"/api/demo/reset", "headers":[],
                       "client":("203.0.113.20",1234), "server":("127.0.0.1",8000), "scheme":"http"})

    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.reset_demo_data(request))
    assert error.value.status_code == 404


def test_production_configuration_rejects_unsafe_defaults(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "database_url", "postgresql://example")
    monkeypatch.setattr(main.settings, "session_secret", "development-only-change-this-secret")
    monkeypatch.setattr(main.settings, "admin_username", "operator")
    monkeypatch.setattr(main.settings, "admin_password", "strong-password-for-test")
    monkeypatch.setattr(main.settings, "cors_origins", "https://dashboard.example")
    try:
        main.validate_production_configuration()
    except RuntimeError as exc:
        assert "SESSION_SECRET" in str(exc)
    else:
        raise AssertionError("unsafe default session secret must fail production validation")


def test_production_configuration_accepts_explicit_secure_values(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "database_url", "postgresql://example")
    monkeypatch.setattr(main.settings, "session_secret", "a-unique-test-session-secret-with-more-than-32-chars")
    monkeypatch.setattr(main.settings, "admin_username", "project-operator")
    monkeypatch.setattr(main.settings, "admin_password", "strong-password-for-test")
    monkeypatch.setattr(main.settings, "auth_required", True)
    monkeypatch.setattr(main.settings, "cors_origins", "https://dashboard.example")
    main.validate_production_configuration()


def test_production_configuration_rejects_unknown_environment(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "staging")
    monkeypatch.setattr(main.settings, "auth_required", False)
    with pytest.raises(RuntimeError, match="APP_ENV"):
        main.validate_production_configuration()


def test_production_configuration_rejects_authentication_bypass(monkeypatch):
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "auth_required", False)
    monkeypatch.setattr(main.settings, "database_url", "postgresql://example")
    monkeypatch.setattr(main.settings, "session_secret", "a-unique-test-session-secret-with-more-than-32-chars")
    monkeypatch.setattr(main.settings, "admin_username", "project-operator")
    monkeypatch.setattr(main.settings, "admin_password", "strong-password-for-test")
    monkeypatch.setattr(main.settings, "cors_origins", "https://dashboard.example")
    with pytest.raises(RuntimeError, match="AUTH_REQUIRED"):
        main.validate_production_configuration()
