
from datetime import timezone
import json
import httpx
from .config import settings


async def notify_incident(conn, incident: dict, severity: str) -> None:
    """Record one notification per incident severity; never claim delivery prematurely."""
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

    mode = "real" if settings.real_sms_ready else "mock"
    status = "MOCK_RECORDED"
    provider_id = None
    response = {"message": message, "detail": "No SMS sent."}

    if mode == "real":
        try:
            if settings.sms_provider.lower() == "textbee":
                url = f"{settings.textbee_base_url.rstrip('/')}/gateway/send-sms"
                headers = {
                    "x-api-key": settings.textbee_api_key,
                    "Content-Type": "application/json",
                }
                payload = {
                    "message": message,
                    "recipients": [settings.sms_recipient_number],
                    "deviceId": settings.textbee_device_id,
                }
                async with httpx.AsyncClient(timeout=10) as client:
                    result = await client.post(url, headers=headers, json=payload)
                response = {"http_status": result.status_code, "body": result.text[:2000]}
                if result.is_success:
                    data = result.json().get("data", {})
                    batch_id = data.get("smsBatchId")
                    provider_id = str(batch_id) if batch_id else None
                    status = "ACCEPTED" if batch_id else "PROVIDER_RESPONSE_UNCONFIRMED"
                else:
                    status = "FAILED"
            else:
                url = (
                    f"https://{settings.exotel_subdomain}.exotel.com/"
                    f"v1/Accounts/{settings.exotel_account_sid}/Sms/send.json"
                )
                data = {
                    "From": settings.exotel_sender_id,
                    "To": settings.sms_recipient_number,
                    "Body": message,
                }
                if settings.exotel_dlt_entity_id:
                    data["DltEntityId"] = settings.exotel_dlt_entity_id
                if settings.exotel_dlt_template_id:
                    data["DltTemplateId"] = settings.exotel_dlt_template_id
                if settings.exotel_sms_type:
                    data["SmsType"] = settings.exotel_sms_type
                async with httpx.AsyncClient(timeout=10) as client:
                    result = await client.post(
                        url,
                        auth=(settings.exotel_api_key, settings.exotel_api_token),
                        data=data,
                    )
                response = {"http_status": result.status_code, "body": result.text[:2000]}
                if result.is_success:
                    payload = result.json()
                    sid = payload.get("SMSMessage", {}).get("Sid")
                    provider_id = str(sid) if sid else None
                    status = "ACCEPTED" if sid else "PROVIDER_RESPONSE_UNCONFIRMED"
                else:
                    status = "FAILED"
        except Exception as exc:
            status = "FAILED"
            response = {"error": type(exc).__name__}

    await conn.execute(
        """INSERT INTO notifications
        (incident_id, event_severity, mode, status, message, provider_message_id, provider_response)
        VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)""",
        incident["id"], severity, mode, status, message, provider_id, json.dumps(response),
    )
