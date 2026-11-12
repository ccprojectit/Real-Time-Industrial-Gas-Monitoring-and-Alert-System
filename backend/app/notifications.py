
from datetime import timezone
import json


async def notify_incident(conn, incident: dict):
    """Persist or reuse one Critical notification record without network calls."""
    message = (
        f"CRITICAL gas alert: {incident['zone_name']} / {incident['gas_name']} "
        f"{incident['reading']} {incident['unit']} at "
        f"{(incident.get('critical_at') or incident['created_at']).astimezone(timezone.utc).isoformat()}. "
        "Follow site safety procedures."
    )

    return await conn.fetchrow(
        """WITH inserted AS (
          INSERT INTO notifications
          (incident_id, event_severity, mode, status, message, provider_message_id, provider_response)
          VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)
          ON CONFLICT (incident_id,event_severity) DO NOTHING
          RETURNING id::text AS id,incident_id::text AS incident_id,event_severity,mode,status,message,provider_message_id,created_at
        )
        SELECT * FROM inserted
        UNION ALL
        SELECT id::text AS id,incident_id::text AS incident_id,event_severity,mode,status,message,provider_message_id,created_at
        FROM notifications WHERE incident_id=$1 AND event_severity='CRITICAL'
          AND NOT EXISTS (SELECT 1 FROM inserted)
        LIMIT 1""",
        incident["id"], "CRITICAL", "mock", "MOCK_RECORDED", message, None,
        json.dumps({"message": message, "detail": "Mock only; no SMS sent."}),
    )
