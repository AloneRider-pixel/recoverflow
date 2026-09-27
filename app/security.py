import base64
import hashlib
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, BadTimeSignature
from .config import settings

COOKIE_NAME = "recoverflow_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7

def _serializer():
    return URLSafeTimedSerializer(settings.session_secret, salt="recoverflow-session")

def _encryption_key():
    return base64.urlsafe_b64encode(hashlib.sha256(settings.session_secret.encode()).digest())

def encrypt(value: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_encryption_key()).encrypt(value.encode()).decode()

def decrypt(value: str | None) -> str:
    if not value:
        return ""
    from cryptography.fernet import Fernet
    try:
        return Fernet(_encryption_key()).decrypt(value.encode()).decode()
    except Exception:
        return ""

def new_csrf():
    return secrets.token_urlsafe(32)

def set_session(response, user_id: int, csrf: str | None = None):
    token = _serializer().dumps({"user_id": user_id, "csrf": csrf or new_csrf()})
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_MAX_AGE, httponly=True,
        samesite="lax", secure=True, path="/"
    )

def clear_session(response):
    response.delete_cookie(COOKIE_NAME, path="/")

def read_session(request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, BadTimeSignature):
        return None
