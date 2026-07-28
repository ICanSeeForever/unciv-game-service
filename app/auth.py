"""API key authentication dependency."""
from fastapi import Header, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import settings

_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(x_api_key: str | None = Security(_scheme)) -> None:
    if not settings.api_key:
        return  # auth disabled
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
