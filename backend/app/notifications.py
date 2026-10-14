
from datetime import timezone
import json


async def notify_incident(conn, incident: dict, severity: str) -> None:
    """Persist one mock SMS notification per incident severity. No network calls are made."""
    existing = await conn.fetchval(
        "SELECT id FROM notifications WHERE incident_id=$1 AND event_severity=$2",
        incident["id"], severity,
    )
    if existing:
        return

    message = (
        f"{severity} gas alert: {incident['zone_name']} / {incident['gas_name']} "
        f"{incident['reading']} {incident['unit']} at "
        f"{(incident.get('critical_at') or incident['created_at']).astimezone(timezone.utc).isoformat()}. "
        "Follow site safety procedures."
    )

    await conn.execute(
        """INSERT INTO notifications
        (incident_id, event_severity, mode, status, message, provider_message_id, provider_response)
        VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)""",
        incident["id"], severity, "mock", "MOCK_RECORDED", message, None,
        json.dumps({"message": message, "detail": "Mock only; no SMS sent."}),
    )
