"""
Settings and raw config/accounts routes.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import CONFIG, accounts_data, _CONFIG_KEYS, save_config, save_accounts


router = APIRouter()


class ConfigUpdate(BaseModel):
    key: str
    value: float


@router.post("/api/settings")
async def api_settings(update: ConfigUpdate):
    if update.key in _CONFIG_KEYS:
        old_val = getattr(CONFIG, update.key)
        try:
            setattr(CONFIG, update.key, type(old_val)(update.value))
        except (ValueError, TypeError):
            return {"ok": False, "error": "Invalid value type"}
        save_config()
        return {"ok": True, "key": update.key, "value": getattr(CONFIG, update.key)}
    return {"ok": False, "error": "Unknown key"}


@router.get("/api/raw/config")
async def api_raw_config_get():
    """Вернуть текущий config как объект."""
    cfg = {k: getattr(CONFIG, k) for k in _CONFIG_KEYS}
    cfg["questionnaire_templates"] = CONFIG.questionnaire_templates
    cfg["letter_templates"] = CONFIG.letter_templates
    cfg["url_pool"] = CONFIG.url_pool
    return cfg


@router.post("/api/raw/config")
async def api_raw_config_set(request: Request):
    """Перезаписать config из JSON-объекта."""
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Невалидный JSON"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "Ожидается объект"}
    for key, value in data.items():
        if key in _CONFIG_KEYS:
            try:
                field_type = type(getattr(CONFIG, key))
                setattr(CONFIG, key, field_type(value))
            except Exception:
                setattr(CONFIG, key, value)
        elif key == "questionnaire_templates" and isinstance(value, list):
            CONFIG.questionnaire_templates = value
        elif key == "letter_templates" and isinstance(value, list):
            CONFIG.letter_templates = value
        elif key == "url_pool" and isinstance(value, list):
            CONFIG.url_pool = value
    save_config()
    return {"ok": True}


@router.get("/api/raw/accounts")
async def api_raw_accounts_get():
    """Вернуть accounts без значений cookies (только ключи)."""
    safe = []
    for acc in accounts_data:
        a = {k: v for k, v in acc.items() if k != "cookies"}
        a["cookies"] = {k: "***" for k in acc.get("cookies", {})}
        safe.append(a)
    return safe


@router.post("/api/raw/accounts")
async def api_raw_accounts_set(request: Request):
    """Перезаписать accounts. Значение cookies '***' сохраняет старое."""
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Невалидный JSON"}
    if not isinstance(data, list):
        return {"ok": False, "error": "Ожидается массив"}
    old_by_name = {a.get("name", ""): a for a in accounts_data}
    merged = []
    for acc in data:
        if not isinstance(acc, dict):
            continue
        name = acc.get("name", "")
        old = old_by_name.get(name, {})
        new_cookies = acc.get("cookies", {})
        merged_cookies = {
            k: (old.get("cookies", {}).get(k, "") if v == "***" else v)
            for k, v in new_cookies.items()
        }
        for k, v in old.get("cookies", {}).items():
            if k not in merged_cookies:
                merged_cookies[k] = v
        acc = dict(acc)
        acc["cookies"] = merged_cookies
        merged.append(acc)
    accounts_data.clear()
    accounts_data.extend(merged)
    save_accounts()
    return {"ok": True, "count": len(merged)}
