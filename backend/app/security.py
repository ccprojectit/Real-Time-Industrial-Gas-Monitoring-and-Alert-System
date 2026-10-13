import ipaddress
from fastapi import HTTPException, Request, WebSocket
from itsdangerous import BadSignature, URLSafeTimedSerializer
from .config import settings

serializer = URLSafeTimedSerializer(settings.session_secret, salt="gas-monitor-session-v1")
COOKIE = "gas_monitor_session"


def create_session(username: str) -> str:
    return serializer.dumps({"username": username})


def is_loopback_connection(connection: Request | WebSocket) -> bool:
    client = connection.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def local_demo_allowed(connection: Request | WebSocket) -> bool:
    return (
        not settings.auth_required
        and settings.app_env.strip().lower() in {"development", "test"}
        and is_loopback_connection(connection)
    )


def validate_remote_auth_configuration(connection: Request | WebSocket) -> None:
    if is_loopback_connection(connection):
        return
    if (len(settings.session_secret) < 32
            or settings.session_secret == "development-only-change-this-secret"
            or not settings.admin_username
            or settings.admin_username.strip().lower() in {"admin", "operator"}
            or len(settings.admin_password) < 16):
        raise HTTPException(status_code=503, detail="Remote operator authentication is not securely configured")


def current_operator(request: Request | WebSocket) -> str:
    if local_demo_allowed(request):
        return "Local demo operator"
    validate_remote_auth_configuration(request)
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        data = serializer.loads(token, max_age=8 * 60 * 60)
        return str(data["username"])
    except (BadSignature, KeyError, TypeError):
        raise HTTPException(status_code=401, detail="Session expired; sign in again")
