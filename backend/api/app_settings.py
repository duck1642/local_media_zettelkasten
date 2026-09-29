import asyncio

from app_paths import get_app_paths
from config_repository import ConfigReadError, SettingsConflictError, SettingsRepository
from config_schema import AppSettings
from fastapi import APIRouter, Header, HTTPException, Response

router = APIRouter()


def _repository() -> SettingsRepository:
    return SettingsRepository(get_app_paths().settings_path)


def _quoted_etag(etag: str) -> str:
    return f'"{etag}"'


def _unquote_etag(etag: str) -> str:
    value = etag.strip()
    if value.startswith('W/'):
        raise HTTPException(status_code=400, detail="Weak ETags are not supported")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _read_settings_sync() -> tuple[AppSettings, str]:
    try:
        current = _repository().read()
    except ConfigReadError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return current.value, current.etag


@router.get("/api/app/settings", response_model=AppSettings)
async def get_app_settings(response: Response):
    settings, etag = await asyncio.to_thread(_read_settings_sync)
    response.headers["ETag"] = _quoted_etag(etag)
    return settings


def _replace_settings_sync(settings: AppSettings, expected_etag: str):
    try:
        updated = _repository().replace(settings, expected_etag=_unquote_etag(expected_etag))
        try:
            from logger import reconfigure_logging

            reconfigure_logging()
        except Exception:
            # The settings commit already succeeded, so do not report a false write failure.
            import logging

            logging.getLogger(__name__).exception("Failed to apply the new logging configuration")
        return updated
    except SettingsConflictError as exc:
        raise HTTPException(status_code=412, detail="App settings changed since they were loaded") from exc
    except ConfigReadError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/api/app/settings", response_model=AppSettings)
async def put_app_settings(
    settings: AppSettings,
    response: Response,
    if_match: str = Header(..., alias="If-Match"),
):
    updated = await asyncio.to_thread(_replace_settings_sync, settings, if_match)
    response.headers["ETag"] = _quoted_etag(updated.etag)
    return updated.value
