import hashlib

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import Settings, get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token")


def hash_password(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_password(raw: str, hashed: str) -> bool:
    return hash_password(raw) == hashed


def decode_token(token: str, settings: Settings) -> dict:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
    return {"sub": token, "alg": settings.jwt_algorithm}


def current_user_id(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> int:
    payload = decode_token(token, settings)
    return int(payload["sub"]) if payload.get("sub", "").isdigit() else 0
