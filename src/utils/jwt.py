import jwt
import os
from flask import Request

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"

def decode_jwt(token: str):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def extract_token_from_request(req: Request | None) -> str | None:
    if req is None:
        return None

    auth_header = req.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            return token

    cookie_token = req.cookies.get("auth_token")
    if cookie_token:
        return cookie_token

    return None


def decode_request_token(req: Request | None):
    token = extract_token_from_request(req)
    if not token:
        return None
    return decode_jwt(token)
