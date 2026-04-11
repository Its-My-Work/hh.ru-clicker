"""
HH.RU Auto Response Bot - FastAPI Web Dashboard
================================================
Browser-accessible dashboard with real-time updates and full bot control.
"""

import asyncio
import aiohttp
import ssl
from bs4 import BeautifulSoup
import re
import random
from datetime import datetime, timedelta
from glom import glom
import json
from pathlib import Path
import requests
from collections import deque
import urllib.parse
import time
import threading

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

try:
    import openai as _openai_mod
    _openai_available = True
except ImportError:
    _openai_available = False

_llm_rr_index = 0  # round-robin counter for multi-profile LLM
_llm_rr_lock = threading.Lock()


def _randomize_text(template: str) -> str:
    """Replace {opt1|opt2|opt3} with random choice from alternatives."""
    def pick(m):
        options = [o.strip() for o in m.group(1).split('|')]
        return random.choice(options)
    return re.sub(r'\{([^}]+\|[^}]+)\}', pick, template)


# ── OAuth via official Android app credentials ──
_HH_OAUTH_CLIENT_ID = "HIOMIAS39CA9DICTA7JIO64LQKQJF5AGIK74G9ITJKLNEDAOH5FHS5G1JI7FOEGD"
_HH_OAUTH_CLIENT_SECRET = "V9M870DE342BGHFRUJ5FTCGCUA1482AN0DI8C5TFI9ULMA89H10N60NOP8I4JMVS"
_HH_OAUTH_REDIRECT = "hhandroid://oauthresponse"
_oauth_tokens: dict = {}  # {resume_hash: {access_token, refresh_token, expires_at}}
_oauth_lock = threading.Lock()


def _obtain_oauth_token(acc: dict) -> str:
    """Get OAuth access_token for account. Auto-refresh if expired. Returns token or empty string."""
    resume_hash = acc.get("resume_hash", "")
    if not resume_hash:
        return ""

    with _oauth_lock:
        cached = _oauth_tokens.get(resume_hash)
        if cached and cached.get("expires_at", 0) > time.time() + 300:
            return cached["access_token"]

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    # Try refresh first
    with _oauth_lock:
        cached = _oauth_tokens.get(resume_hash, {})
    refresh = cached.get("refresh_token", "")
    if refresh:
        try:
            r = requests.post("https://hh.ru/oauth/token", data={
                "grant_type": "refresh_token",
                "client_id": _HH_OAUTH_CLIENT_ID,
                "client_secret": _HH_OAUTH_CLIENT_SECRET,
                "refresh_token": refresh,
            }, headers={"User-Agent": ua}, verify=False, timeout=15)
            if r.status_code == 200:
                d = r.json()
                with _oauth_lock:
                    _oauth_tokens[resume_hash] = {
                        "access_token": d["access_token"],
                        "refresh_token": d.get("refresh_token", refresh),
                        "expires_at": time.time() + d.get("expires_in", 1209599),
                    }
                log_debug(f"OAuth: refreshed token for {resume_hash[:12]}")
                return d["access_token"]
        except Exception as e:
            log_debug(f"OAuth refresh error: {e}")

    # Full authorize flow using cookies
    try:
        cookies = acc.get("cookies", {})
        # Step 1: GET authorize
        r1 = requests.get("https://hh.ru/oauth/authorize", params={
            "response_type": "code",
            "client_id": _HH_OAUTH_CLIENT_ID,
            "redirect_uri": _HH_OAUTH_REDIRECT,
            "state": "botstate",
        }, headers={"User-Agent": ua}, cookies=cookies, verify=False, timeout=15, allow_redirects=False)

        code = None
        loc = r1.headers.get("Location", "")
        m = re.search(r"code=([^&]+)", loc)
        if m:
            code = m.group(1)
        elif r1.status_code == 200 and ("разрешить" in r1.text.lower() or "approve" in r1.text.lower() or "grant" in r1.text.lower()):
            # Submit approve form
            r2 = requests.post("https://hh.ru/oauth/authorize", data={
                "response_type": "code",
                "client_id": _HH_OAUTH_CLIENT_ID,
                "redirect_uri": _HH_OAUTH_REDIRECT,
                "state": "botstate",
                "action": "approve",
                "_xsrf": cookies.get("_xsrf", ""),
            }, headers={"User-Agent": ua}, cookies=cookies, verify=False, timeout=15, allow_redirects=False)
            loc2 = r2.headers.get("Location", "")
            m2 = re.search(r"code=([^&]+)", loc2)
            if m2:
                code = m2.group(1)

        if not code:
            log_debug(f"OAuth: failed to get code for {resume_hash[:12]}")
            return ""

        # Step 2: Exchange code for token
        r3 = requests.post("https://hh.ru/oauth/token", data={
            "grant_type": "authorization_code",
            "client_id": _HH_OAUTH_CLIENT_ID,
            "client_secret": _HH_OAUTH_CLIENT_SECRET,
            "redirect_uri": _HH_OAUTH_REDIRECT,
            "code": code,
        }, headers={"User-Agent": ua, "Content-Type": "application/x-www-form-urlencoded"}, verify=False, timeout=15)

        if r3.status_code == 200:
            d = r3.json()
            with _oauth_lock:
                _oauth_tokens[resume_hash] = {
                    "access_token": d["access_token"],
                    "refresh_token": d.get("refresh_token", ""),
                    "expires_at": time.time() + d.get("expires_in", 1209599),
                }
            log_debug(f"OAuth: obtained token for {resume_hash[:12]}, expires in {d.get('expires_in',0)}s")
            return d["access_token"]
        else:
            log_debug(f"OAuth: token exchange failed {r3.status_code}: {r3.text[:200]}")
    except Exception as e:
        log_debug(f"OAuth: authorize error: {e}")
    return ""


def _oauth_apply(acc: dict, vid: str, message: str = "") -> tuple:
    """Apply to vacancy via OAuth API. Returns (result_str, info_dict)."""
    token = _obtain_oauth_token(acc)
    if not token:
        return "error", {"exception": "OAuth token не получен"}
    resume_hash = acc.get("resume_hash", "")
    try:
        message = _randomize_text(message) if message else message
        data = {"vacancy_id": vid, "resume_id": resume_hash}
        if message:
            data["message"] = message
        r = requests.post(
            "https://api.hh.ru/negotiations",
            headers={"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {token}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data=data, verify=False, timeout=15,
        )
        if r.status_code in (200, 201, 204):
            # Success — try to get vacancy info
            info = {}
            try:
                d = r.json()
                info = {"title": d.get("vacancy", {}).get("name", ""),
                        "company": d.get("vacancy", {}).get("employer", {}).get("name", "")}
            except Exception:
                pass
            return "sent", info
        elif r.status_code == 400:
            try:
                d = r.json()
            except Exception:
                return "error", {"raw": r.text[:100]}
            err = d.get("errors", [{}])[0].get("value", d.get("description", ""))
            if "limit" in err.lower():
                return "limit", {}
            if "already" in err.lower() or "exist" in err.lower():
                return "already", {}
            if "test" in err.lower():
                return "test", {}
            return "error", {"raw": err}
        elif r.status_code in (401, 403):
            return "auth_error", {}
        elif r.status_code == 404:
            return "error", {"raw": "Вакансия не найдена"}
        else:
            return "error", {"raw": f"HTTP {r.status_code}: {r.text[:100]}"}
    except Exception as e:
        return "error", {"exception": str(e)}


def _oauth_touch_resume(acc: dict) -> tuple:
    """Touch resume via OAuth API (no captcha). Returns (success, message)."""
    token = _obtain_oauth_token(acc)
    if not token:
        return False, "OAuth token не получен"
    resume_hash = acc.get("resume_hash", "")
    try:
        r = requests.post(
            f"https://api.hh.ru/resumes/{resume_hash}/publish",
            headers={"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {token}"},
            verify=False, timeout=15,
        )
        if r.status_code in (200, 204):
            return True, "✅ Резюме поднято через OAuth API!"
        elif r.status_code == 429:
            return False, "Кулдаун (429) — подождите 4 часа"
        else:
            return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return False, f"Ошибка: {str(e)[:50]}"
_resume_cache: dict = {}   # {resume_hash: (text, timestamp)}
_resume_cache_lock = threading.Lock()
_RESUME_CACHE_TTL = 4 * 3600  # 4 hours

# ============================================================
# ХРАНИЛИЩЕ ДАННЫХ
# ============================================================

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

APPLIED_FILE = DATA_DIR / "applied_vacancies.json"
TEST_REQUIRED_FILE = DATA_DIR / "test_required_vacancies.json"
INTERVIEWS_FILE = DATA_DIR / "interviews.json"
DEBUG_LOG_FILE = DATA_DIR / "debug.log"
SESSIONS_FILE = DATA_DIR / "browser_sessions.json"
CONFIG_FILE = DATA_DIR / "config.json"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"


def log_debug(message: str):
    """Записать отладочное сообщение в файл"""
    with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"[{timestamp}] {message}\n")


def _is_login_page(html: str) -> bool:
    """Определить, является ли HTML страница страницей входа HH (протухшие куки)."""
    if not html:
        return False
    return (
        '"/account/login"' in html
        or "hh.ru/account/login" in html
        or "Войти в аккаунт" in html
        or '"accountLogin"' in html
    )


# ============================================================
# КЕШ В ПАМЯТИ (избегаем постоянного чтения с диска)
# ============================================================

_cache_applied: dict = None
_cache_tests: dict = None
_cache_interviews: dict = None  # keyed by neg_id
_cache_lock = threading.Lock()


def _load_cache():
    """Загрузить кеш из файлов (один раз при старте)"""
    global _cache_applied, _cache_tests, _cache_interviews
    with _cache_lock:
        if _cache_applied is None:
            if APPLIED_FILE.exists():
                try:
                    with open(APPLIED_FILE, "r", encoding="utf-8") as f:
                        _cache_applied = json.load(f)
                except (json.JSONDecodeError, OSError, ValueError) as e:
                    log_debug(f"⚠️ Ошибка загрузки {APPLIED_FILE}: {e}")
                    _cache_applied = {}
            else:
                _cache_applied = {}
        if _cache_tests is None:
            if TEST_REQUIRED_FILE.exists():
                try:
                    with open(TEST_REQUIRED_FILE, "r", encoding="utf-8") as f:
                        _cache_tests = json.load(f)
                except (json.JSONDecodeError, OSError, ValueError) as e:
                    log_debug(f"⚠️ Ошибка загрузки {TEST_REQUIRED_FILE}: {e}")
                    _cache_tests = {}
            else:
                _cache_tests = {}
        if _cache_interviews is None:
            if INTERVIEWS_FILE.exists():
                try:
                    with open(INTERVIEWS_FILE, "r", encoding="utf-8") as f:
                        _cache_interviews = json.load(f)
                except (json.JSONDecodeError, OSError, ValueError) as e:
                    log_debug(f"⚠️ Ошибка загрузки {INTERVIEWS_FILE}: {e}")
                    _cache_interviews = {}
            else:
                _cache_interviews = {}


_save_applied_lock = threading.Lock()

def _save_applied_async():
    """Сохранить applied в фоне (atomic write)"""
    if not _save_applied_lock.acquire(blocking=False):
        return  # another save in progress
    try:
        import copy
        with _cache_lock:
            data = copy.deepcopy(_cache_applied) if _cache_applied else {}
        tmp = APPLIED_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        tmp.replace(APPLIED_FILE)
    except Exception as e:
        log_debug(f"_save_applied_async error: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    finally:
        _save_applied_lock.release()


_save_tests_lock = threading.Lock()

def _save_tests_async():
    """Сохранить tests в фоне (atomic write)"""
    if not _save_tests_lock.acquire(blocking=False):
        return
    try:
        import copy
        with _cache_lock:
            data = copy.deepcopy(_cache_tests) if _cache_tests else {}
        tmp = TEST_REQUIRED_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        tmp.replace(TEST_REQUIRED_FILE)
    except Exception as e:
        log_debug(f"_save_tests_async error: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    finally:
        _save_tests_lock.release()


_save_interviews_lock = threading.Lock()

def _save_interviews_async():
    """Сохранить interviews в фоне (атомарная запись, с защитой от параллельных записей)"""
    if not _save_interviews_lock.acquire(blocking=False):
        return  # другой поток уже сохраняет — пропускаем
    try:
        with _cache_lock:
            data = _cache_interviews.copy() if _cache_interviews else {}
        tmp = INTERVIEWS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        tmp.replace(INTERVIEWS_FILE)
    finally:
        _save_interviews_lock.release()


def upsert_interview(neg_id: str, acc: str, acc_color: str = "",
                     employer: str = "", vacancy_title: str = "",
                     employer_last_msg: str = None, needs_reply: bool = None,
                     llm_reply: str = None, llm_sent: bool = None,
                     chat_not_found: bool = None, chat_status: str = None):
    """Создать или обновить запись об интервью-переговоре."""
    _load_cache()
    now = datetime.now().isoformat(timespec="seconds")
    with _cache_lock:
        existing = _cache_interviews.get(neg_id, {})
        record = dict(existing)
        record["neg_id"] = neg_id
        if acc:
            record["acc"] = acc
        if acc_color:
            record["acc_color"] = acc_color
        if employer:
            record["employer"] = employer
        if vacancy_title:
            record["vacancy_title"] = vacancy_title
        if "first_seen" not in record:
            record["first_seen"] = now
        record["last_seen"] = now
        if employer_last_msg is not None:
            record["employer_last_msg"] = employer_last_msg
            record["employer_last_msg_date"] = now
        if needs_reply is not None:
            record["needs_reply"] = needs_reply
        if llm_reply is not None:
            record["llm_reply"] = llm_reply
            record["llm_reply_date"] = now
        if llm_sent is not None:
            # Never downgrade: once llm_sent=True, keep it
            if not record.get("llm_sent"):
                record["llm_sent"] = llm_sent
        if chat_not_found is True:
            record["chat_not_found"] = True  # never reset — chat is permanently closed
        if chat_status is not None:
            record["chat_status"] = chat_status
        # Detect new employer message: if employer_last_msg changed vs what was already replied,
        # allow status to go back to pending_reply so the new message gets handled
        employer_msg_changed = (
            employer_last_msg is not None
            and employer_last_msg != existing.get("employer_last_msg")
            and bool(employer_last_msg)
        )
        # Derive status
        if record.get("chat_not_found"):
            record["status"] = "chat_closed"  # 409: permanently closed, never retried
        elif existing.get("status") == "replied" and not employer_msg_changed:
            # Keep "replied" only if no new employer message arrived
            record["status"] = "replied"
        elif record.get("llm_reply") and not employer_msg_changed:
            record["status"] = "replied" if record.get("llm_sent") else "draft"
        elif record.get("needs_reply") is False:
            record["status"] = "no_reply_needed"
        else:
            record["status"] = "pending_reply"
        _cache_interviews[neg_id] = record
    threading.Thread(target=_save_interviews_async, daemon=True).start()


def get_no_chat_neg_ids() -> set:
    """Return set of neg_ids where chatik permanently returned 409 (chat doesn't exist)."""
    _load_cache()
    with _cache_lock:
        return {nid for nid, r in _cache_interviews.items() if r.get("chat_not_found")}


def get_interviews_list(acc: str = "", limit: int = 2000, status: str = "") -> list:
    """Вернуть список интервью, сортировка: pending_reply first, затем по дате desc."""
    _load_cache()
    with _cache_lock:
        items = list(_cache_interviews.values())
    if acc:
        items = [r for r in items if r.get("acc") == acc]
    if status:
        items = [r for r in items if r.get("status") == status or r.get("chat_status") == status]
    status_order = {"pending_reply": 0, "draft": 1, "replied": 2, "chat_closed": 3, "no_reply_needed": 4}
    # Сначала по дате desc, потом stable sort по статусу — pending всегда первые
    items.sort(key=lambda r: r.get("last_seen", "") or "", reverse=True)
    items.sort(key=lambda r: status_order.get(r.get("status", ""), 9))
    return items[:limit]


def load_browser_sessions() -> list:
    """Загрузить браузерные сессии из файла."""
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_browser_sessions(sessions: list):
    """Сохранить браузерные сессии в файл (в фоновом потоке)."""
    import copy
    snapshot = copy.deepcopy(sessions)
    def _write():
        tmp = SESSIONS_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            tmp.replace(SESSIONS_FILE)
        except Exception as e:
            log_debug(f"save_browser_sessions error: {e}")
            tmp.unlink(missing_ok=True)
    threading.Thread(target=_write, daemon=True).start()


def save_accounts():
    """Сохранить accounts_data на диск (в фоновом потоке)."""
    snapshot = [
        {k: v for k, v in acc.items() if not k.startswith("_")}
        for acc in accounts_data
    ]
    def _write():
        tmp = ACCOUNTS_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            tmp.replace(ACCOUNTS_FILE)
        except Exception as e:
            log_debug(f"save_accounts error: {e}")
            tmp.unlink(missing_ok=True)
    threading.Thread(target=_write, daemon=True).start()


def load_accounts():
    """Загрузить accounts_data с диска (если файл есть)."""
    if not ACCOUNTS_FILE.exists():
        save_accounts()  # первый запуск — сохраняем текущие дефолты
        return
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            accounts_data.clear()
            accounts_data.extend(data)
    except Exception as e:
        log_debug(f"load_accounts error: {e}")


_CONFIG_KEYS = [
    "pages_per_url", "max_concurrent", "response_delay", "pause_between_cycles",
    "limit_check_interval", "resume_touch_interval", "batch_responses", "min_salary",
    "auto_pause_errors", "questionnaire_default_answer", "llm_fill_questionnaire",
    "skip_inconsistent", "use_oauth_apply", "daily_apply_limit", "stop_on_hh_limit", "llm_check_interval",
    "filter_agencies", "filter_low_competition", "search_period_days",
]


def save_config():
    """Сохранить текущий CONFIG на диск."""
    data = {k: getattr(CONFIG, k) for k in _CONFIG_KEYS}
    data["questionnaire_templates"] = CONFIG.questionnaire_templates
    data["letter_templates"] = CONFIG.letter_templates
    data["allowed_schedules"] = CONFIG.allowed_schedules
    data["auto_apply_tests"] = CONFIG.auto_apply_tests
    data["use_oauth_apply"] = CONFIG.use_oauth_apply
    data["url_pool"] = CONFIG.url_pool
    data["llm_api_key"] = CONFIG.llm_api_key
    data["llm_base_url"] = CONFIG.llm_base_url
    data["llm_model"] = CONFIG.llm_model
    data["llm_enabled"] = CONFIG.llm_enabled
    data["llm_auto_send"] = CONFIG.llm_auto_send
    data["llm_use_cover_letter"] = CONFIG.llm_use_cover_letter
    data["llm_use_resume"] = CONFIG.llm_use_resume
    data["llm_system_prompt"] = CONFIG.llm_system_prompt
    data["llm_profiles"] = CONFIG.llm_profiles
    data["llm_profile_mode"] = CONFIG.llm_profile_mode
    def _write():
        tmp = CONFIG_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(CONFIG_FILE)
        except Exception as e:
            log_debug(f"save_config error: {e}")
            tmp.unlink(missing_ok=True)
    threading.Thread(target=_write, daemon=True).start()


def load_config():
    """Загрузить CONFIG с диска (если файл есть)."""
    if not CONFIG_FILE.exists():
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in _CONFIG_KEYS:
            if k in data:
                old_val = getattr(CONFIG, k)
                try:
                    setattr(CONFIG, k, type(old_val)(data[k]))
                except (ValueError, TypeError):
                    log_debug(f"⚠️ Невалидное значение конфига {k}={data[k]!r}, пропуск")
        if "questionnaire_templates" in data and isinstance(data["questionnaire_templates"], list):
            CONFIG.questionnaire_templates = data["questionnaire_templates"]
        if "letter_templates" in data and isinstance(data["letter_templates"], list):
            CONFIG.letter_templates = data["letter_templates"]
        if "url_pool" in data and isinstance(data["url_pool"], list):
            CONFIG.url_pool = data["url_pool"]
        for k in ("llm_api_key", "llm_base_url", "llm_model", "llm_system_prompt"):
            if k in data and isinstance(data[k], str):
                setattr(CONFIG, k, data[k])
        for k in ("llm_enabled", "llm_auto_send", "llm_use_cover_letter", "llm_use_resume", "llm_fill_questionnaire"):
            if k in data:
                setattr(CONFIG, k, bool(data[k]))
        if "allowed_schedules" in data and isinstance(data["allowed_schedules"], list):
            CONFIG.allowed_schedules = data["allowed_schedules"]
        if "auto_apply_tests" in data:
            CONFIG.auto_apply_tests = bool(data["auto_apply_tests"])
        if "use_oauth_apply" in data:
            CONFIG.use_oauth_apply = bool(data["use_oauth_apply"])
        if "llm_profiles" in data and isinstance(data["llm_profiles"], list):
            CONFIG.llm_profiles = data["llm_profiles"]
        if "llm_profile_mode" in data and isinstance(data["llm_profile_mode"], str):
            CONFIG.llm_profile_mode = data["llm_profile_mode"]
        # Migration: if no profiles defined but old-style api_key exists, create one profile
        if not CONFIG.llm_profiles and CONFIG.llm_api_key:
            CONFIG.llm_profiles = [{"name": "Основной", "api_key": CONFIG.llm_api_key,
                "base_url": CONFIG.llm_base_url, "model": CONFIG.llm_model, "enabled": True}]
    except Exception as e:
        log_debug(f"load_config error: {e}")


def add_applied(account_name: str, vacancy_id: str, info: dict = None):
    _load_cache()
    with _cache_lock:
        if account_name not in _cache_applied:
            _cache_applied[account_name] = {}
        existing = _cache_applied[account_name].get(vacancy_id, {})
        new_info = info or {}
        # Preserve existing title/company if new info has empty values
        title = new_info.get("title") or existing.get("title", "")
        company = new_info.get("company") or existing.get("company", "")
        _cache_applied[account_name][vacancy_id] = {
            "url": f"https://hh.ru/vacancy/{vacancy_id}",
            "title": title,
            "company": company,
            "salary_from": new_info.get("salary_from") or existing.get("salary_from"),
            "salary_to": new_info.get("salary_to") or existing.get("salary_to"),
            "at": datetime.now().isoformat()
        }
    threading.Thread(target=_save_applied_async, daemon=True).start()


def add_test_vacancy(vacancy_id: str, title: str = "", company: str = "",
                     account_name: str = "", resume_hash: str = ""):
    _load_cache()
    with _cache_lock:
        if vacancy_id not in _cache_tests:
            _cache_tests[vacancy_id] = {
                "url": f"https://hh.ru/vacancy/{vacancy_id}",
                "title": title,
                "company": company,
                "account_name": account_name,
                "resume_hash": resume_hash,
                "at": datetime.now().isoformat()
            }
    threading.Thread(target=_save_tests_async, daemon=True).start()


def is_applied(account_name: str, vacancy_id: str) -> bool:
    _load_cache()
    with _cache_lock:
        return vacancy_id in _cache_applied.get(account_name, {})


def is_test(vacancy_id: str) -> bool:
    _load_cache()
    with _cache_lock:
        return vacancy_id in _cache_tests


def get_stats() -> dict:
    _load_cache()
    with _cache_lock:
        applied = _cache_applied or {}
        tests = _cache_tests or {}
    total = sum(len(v) for v in applied.values())
    by_acc = {k: len(v) for k, v in applied.items()}
    return {"total": total, "tests": len(tests), "by_acc": by_acc}


def get_applied_list(limit: int = 300) -> list:
    """Получить список последних откликов"""
    _load_cache()
    with _cache_lock:
        applied = {k: dict(v) for k, v in (_cache_applied or {}).items()}
    all_items = []
    for acc_name, vacancies in applied.items():
        for vid, info in vacancies.items():
            all_items.append({
                "account": acc_name,
                "vacancy_id": vid,
                "url": info.get("url", f"https://hh.ru/vacancy/{vid}"),
                "title": info.get("title", ""),
                "company": info.get("company", ""),
                "salary_from": info.get("salary_from"),
                "salary_to": info.get("salary_to"),
                "at": info.get("at", "")
            })
    all_items.sort(key=lambda x: x.get("at", ""), reverse=True)
    return all_items[:limit]


def get_vacancy_db(limit: int = 3000) -> list:
    """Объединённая база: applied + tests, с полем status per account."""
    _load_cache()
    with _cache_lock:
        tests = dict(_cache_tests or {})
        applied = {k: dict(v) for k, v in (_cache_applied or {}).items()}

    # vacancy_id -> {title, company, url, at, is_test, applied_by: [acc_names]}
    db: dict[str, dict] = {}

    # Сначала заполняем из applied
    for acc_name, vacancies in applied.items():
        for vid, info in vacancies.items():
            if vid not in db:
                db[vid] = {
                    "vacancy_id": vid,
                    "url": info.get("url", f"https://hh.ru/vacancy/{vid}"),
                    "title": info.get("title", ""),
                    "company": info.get("company", ""),
                    "at": info.get("at", ""),
                    "is_test": vid in tests,
                    "applied_by": [],
                }
            db[vid]["applied_by"].append(acc_name)
            # Обновляем title/company если были пустые
            if not db[vid]["title"]:
                db[vid]["title"] = info.get("title", "")
            if not db[vid]["company"]:
                db[vid]["company"] = info.get("company", "")

    # Добавляем тест-вакансии которых нет в applied
    for vid, info in tests.items():
        if vid not in db:
            db[vid] = {
                "vacancy_id": vid,
                "url": info.get("url", f"https://hh.ru/vacancy/{vid}"),
                "title": info.get("title", ""),
                "company": info.get("company", ""),
                "at": info.get("at", ""),
                "is_test": True,
                "applied_by": [],
            }
        else:
            db[vid]["is_test"] = True

    # Определяем статус
    for vid, item in db.items():
        if item["applied_by"] and item["is_test"]:
            item["status"] = "test_passed"   # 📝 тест пройден
        elif item["applied_by"]:
            item["status"] = "sent"           # ✅ откликнулись
        else:
            item["status"] = "test_pending"   # 🧪 тест не пройден

    items = sorted(db.values(), key=lambda x: x.get("at", ""), reverse=True)
    return items[:limit]


def get_test_list(limit: int = 300) -> list:
    """Получить список вакансий с тестами"""
    _load_cache()
    with _cache_lock:
        tests = dict(_cache_tests or {})
        applied = dict(_cache_applied or {})
    # Build reverse lookup: vacancy_id -> list of account_names that applied
    applied_by: dict[str, list[str]] = {}
    for acc_name, vacancies in applied.items():
        for vid in vacancies:
            applied_by.setdefault(vid, []).append(acc_name)
    items = []
    for vid, info in tests.items():
        items.append({
            "vacancy_id": vid,
            "url": info.get("url", f"https://hh.ru/vacancy/{vid}"),
            "title": info.get("title", ""),
            "company": info.get("company", ""),
            "account_name": info.get("account_name", ""),
            "resume_hash": info.get("resume_hash", ""),
            "applied_by": applied_by.get(vid, []),
            "at": info.get("at", "")
        })
    items.sort(key=lambda x: x.get("at", ""), reverse=True)
    return items[:limit]


# ============================================================
# АККАУНТЫ
# ============================================================

# Загружается из data/accounts.json при старте (через load_accounts())
accounts_data: list = []


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

class Config:
    """Глобальные настройки (можно менять в runtime)"""
    pages_per_url = 40
    max_concurrent = 20
    response_delay = 1
    pause_between_cycles = 60
    limit_check_interval = 30
    resume_touch_interval = 4
    batch_responses = 3
    min_salary = 0  # Минимальная зарплата в руб (0 = без фильтра)
    auto_pause_errors = 5  # Авто-пауза после N ошибок подряд (0 = выключено)
    auto_apply_tests: bool = False  # Автоматически проходить опросники при откликах
    use_oauth_apply: bool = False  # Использовать OAuth API для откликов (вместо web cookies)
    daily_apply_limit: int = 0  # Жёсткий лимит откликов в день (0 = без ограничения)
    stop_on_hh_limit: bool = True  # Полная остановка при HH лимите (не перепроверять)
    # Фильтр по формату работы (пустой = без фильтра, все форматы)
    # Возможные значения: "fullDay", "remote", "flexible", "shift", "flyInFlyOut"
    allowed_schedules: list = []

    # LLM auto-reply settings
    llm_enabled: bool = False
    llm_auto_send: bool = True        # True = отправлять, False = только логировать черновик
    llm_use_cover_letter: bool = True  # Передавать сопроводительное письмо в контекст
    llm_use_resume: bool = True        # Включать текст резюме в системный промпт
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_profiles: list = None         # [{name, api_key, base_url, model, enabled}]
    llm_profile_mode: str = "fallback"  # "fallback" | "roundrobin"
    skip_inconsistent: bool = False  # Пропускать вакансии с несовпадением опыта
    filter_agencies: bool = False  # Исключить кадровые агентства из поиска
    filter_low_competition: bool = False  # Только вакансии с <10 откликами
    search_period_days: int = 0  # 0 = все, 1-30 = последние N дней
    llm_fill_questionnaire: bool = False  # Использовать LLM для заполнения опросников
    llm_check_interval: int = 5  # Интервал проверки чатов LLM (в минутах, мин 2)
    llm_system_prompt: str = (
        "Ты помощник соискателя работы. Отвечай вежливо и кратко (2-4 предложения) "
        "на сообщения от HR и работодателей. Пиши от первого лица, женский род. "
        "Соглашайся на предложенное время собеседования или уточни детали. "
        "Не используй слишком формальный язык."
    )

    # Шаблонные ответы на опросы (list of {keywords: [...], answer: "..."})
    questionnaire_templates: list = []
    # Ответ по умолчанию (когда ни один шаблон не подошёл)
    questionnaire_default_answer: str = "Готова рассказать подробнее на собеседовании."

    # Глобальный пул поисковых URL (выбираются на карточке каждого аккаунта)
    url_pool: list = []  # [{url, pages}, ...] или plain строки (legacy)

    # Шаблоны сопроводительных писем (list of {name: str, text: str})
    letter_templates: list = [
        {
            "name": "Стандартное",
            "text": (
                "Здравствуйте!\n\n"
                "Я выражаю искренний интерес к возможности присоединиться к вашей компании.\n\n"
                "Я ознакомился с деятельностью вашей организации и уверен, что мой опыт и навыки "
                "смогут внести вклад в вашу команду.\n\n"
                "Хочу отметить, что я всегда готов обучаться новому и развиваться в профессиональном плане.\n\n"
                "Считаю, что ваша компания предоставляет отличные возможности для роста и "
                "самосовершенствования, и мне бы хотелось стать частью вашей команды.\n\n"
                "С уважением,\n[ИМЯ]\n[t.me: @username]\n[📞 телефон]"
            ),
        }
    ]


CONFIG = Config()
CONFIG.llm_profiles = []


def _url_entry(item) -> dict:
    """Нормализует элемент url_pool в {url, pages}."""
    if isinstance(item, str):
        return {"url": item.strip(), "pages": CONFIG.pages_per_url}
    return {"url": item.get("url", "").strip(), "pages": int(item.get("pages", CONFIG.pages_per_url))}


def _url_pages_map() -> dict:
    """Возвращает {url_str: pages} из CONFIG.url_pool."""
    return {e["url"]: e["pages"] for u in CONFIG.url_pool for e in [_url_entry(u)]}


# ============================================================
# API ФУНКЦИИ
# ============================================================

def get_headers(xsrf: str) -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://hh.ru",
        "X-XsrfToken": xsrf
    }


def parse_ids(html: str) -> set:
    soup = BeautifulSoup(html, "html.parser")
    ids = set()
    for link in soup.find_all("a", href=re.compile(r"/vacancy/\d+")):
        m = re.search(r"/vacancy/(\d+)", link["href"])
        if m:
            ids.add(m.group(1))
    log_debug(f"🔍 Парсинг: найдено {len(ids)} вакансий")
    return ids


def parse_vacancy_meta(html: str) -> dict:
    """
    Из HTML страницы поиска извлекает {vacancy_id: {title, company}}.
    Используется как fallback когда API-ответ не содержит shortVacancy.
    """
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    # Основной путь: ищем элементы вакансий по data-qa
    for item in soup.find_all(attrs={"data-qa": re.compile(r"vacancy-serp__vacancy$")}):
        title_el = item.find("a", attrs={"data-qa": re.compile(r"serp-item__title|vacancy-serp__vacancy-title")})
        if not title_el:
            title_el = item.find("a", href=re.compile(r"/vacancy/\d+"))
        if not title_el:
            continue
        m = re.search(r"/vacancy/(\d+)", title_el.get("href", ""))
        if not m:
            continue
        vid = m.group(1)
        title = title_el.get_text(strip=True)
        company = ""
        comp_el = item.find(attrs={"data-qa": re.compile(r"vacancy-serp__vacancy-employer")})
        if comp_el:
            company = comp_el.get_text(strip=True)
        if title:
            result[vid] = {"title": title, "company": company}

    # Fallback: любая ссылка на вакансию с непустым текстом
    if not result:
        for link in soup.find_all("a", href=re.compile(r"/vacancy/\d+")):
            m = re.search(r"/vacancy/(\d+)", link.get("href", ""))
            if not m:
                continue
            vid = m.group(1)
            if vid in result:
                continue
            title = link.get_text(strip=True)
            if title and len(title) > 4:
                result[vid] = {"title": title, "company": ""}

    return result


def parse_salaries(html: str, ids: set) -> dict:
    """
    Извлекает зарплату (from, в рублях) для вакансий из HTML поисковой выдачи.
    Возвращает {vacancy_id: salary_from_rub_or_None}.
    Работает только если CONFIG.min_salary > 0 (иначе пустой результат).
    """
    result = {vid: None for vid in ids}
    if not ids or not CONFIG.min_salary:
        return result

    # Ищем все блоки compensation в HTML и смотрим какая вакансия была упомянута
    # ближайшей по тексту перед каждым блоком
    for m in re.finditer(r'"compensation"\s*:\s*\{([^}]{0,400})\}', html):
        comp_str = m.group(1)
        if '"noCompensation"' in comp_str or '"from"' not in comp_str:
            continue
        from_m = re.search(r'"from"\s*:\s*(\d+)', comp_str)
        if not from_m:
            continue

        # Ищем последний упомянутый vacancy ID в 2000 символах перед этим блоком
        context = html[max(0, m.start() - 2000): m.start()]
        vid_matches = re.findall(r'/vacancy/(\d+)', context)
        if not vid_matches:
            continue
        vid = vid_matches[-1]
        if vid not in result or result[vid] is not None:
            continue

        salary = int(from_m.group(1))
        curr_m = re.search(r'"currencyCode"\s*:\s*"(\w+)"', comp_str)
        curr = curr_m.group(1) if curr_m else "RUR"
        if curr == "USD":
            salary = salary * 90
        elif curr == "EUR":
            salary = salary * 100
        result[vid] = salary

    return result


_SCHEDULE_LABELS = {
    "удалённая": "remote", "удаленная": "remote", "remote": "remote",
    "полный день": "fullDay", "full day": "fullDay", "fullday": "fullDay",
    "гибкий": "flexible", "flexible": "flexible",
    "сменный": "shift", "shift": "shift",
    "вахтовый": "flyInFlyOut", "flyinflyout": "flyInFlyOut", "вахта": "flyInFlyOut",
}


def parse_work_schedules(html: str, ids: set) -> dict:
    """
    Извлекает формат работы для вакансий из HTML поисковой выдачи.
    Возвращает {vacancy_id: set_of_schedule_ids} (e.g. {"remote", "flexible"}).
    """
    result = {vid: set() for vid in ids}
    if not ids or not CONFIG.allowed_schedules:
        return result

    # Approach 1: JSON — ищем "workSchedules":[{"id":"remote"}] или "scheduleTypeNames":["Удалённая"]
    for m in re.finditer(r'"(?:workSchedule(?:Type)?s?|scheduleType(?:Name)?s?)"\s*:\s*\[([^\]]{0,500})\]', html):
        block = m.group(1)
        # Ищем ближайший vacancy ID перед этим блоком
        context = html[max(0, m.start() - 2000): m.start()]
        vid_matches = re.findall(r'/vacancy/(\d+)', context)
        if not vid_matches:
            continue
        vid = vid_matches[-1]
        if vid not in result:
            continue
        # Извлекаем id из JSON-объектов {"id":"remote"} или строк "Удалённая работа"
        for sid in re.findall(r'"id"\s*:\s*"(\w+)"', block):
            result[vid].add(sid)
        for label in re.findall(r'"([^"]+)"', block):
            label_lower = label.lower().strip()
            for key, code in _SCHEDULE_LABELS.items():
                if key in label_lower:
                    result[vid].add(code)
                    break

    # Approach 2: HTML labels — data-qa элементы с расписанием
    for m in re.finditer(r'data-qa="[^"]*(?:schedule|work-mode|work-format)[^"]*"[^>]*>([^<]{2,50})<', html):
        label = m.group(1).strip().lower()
        context = html[max(0, m.start() - 3000): m.start()]
        vid_matches = re.findall(r'/vacancy/(\d+)', context)
        if not vid_matches:
            continue
        vid = vid_matches[-1]
        if vid not in result:
            continue
        for key, code in _SCHEDULE_LABELS.items():
            if key in label:
                result[vid].add(code)
                break

    return result


def extract_search_query(url: str) -> str:
    """Извлекает поисковый запрос из URL"""
    if "text=" in url:
        match = re.search(r"text=([^&]+)", url)
        if match:
            return urllib.parse.unquote_plus(match.group(1))
    if "resume=" in url:
        return "По резюме"
    return "Поиск"


async def fetch_page(session, url, sem):
    async with sem:
        try:
            await asyncio.sleep(0.05)
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                html = await r.text()
                log_debug(f"✅ URL: {url} | Статус: {r.status} | Размер: {len(html)}")
                return html
        except Exception as e:
            log_debug(f"❌ ОШИБКА при загрузке: {url} | {type(e).__name__}: {e}")
            return ""


def get_questionnaire_answer(question_text: str) -> str:
    """Найти подходящий шаблонный ответ по ключевым словам вопроса."""
    q_lower = question_text.lower()
    for tmpl in CONFIG.questionnaire_templates:
        keywords = tmpl.get("keywords", [])
        if not keywords:
            continue
        if any(kw.lower() in q_lower for kw in keywords):
            return tmpl["answer"]
    return CONFIG.questionnaire_default_answer


def _parse_questionnaire_fields(html: str) -> tuple:
    """
    Парсит форму опросника. Возвращает (questions, field_answers):
      questions: list of str (тексты вопросов по порядку)
      field_answers: dict {field_name: answer_value} — готовые значения для POST
    Поддерживает textarea, radio, checkbox.
    """
    # Тексты вопросов
    q_blocks = re.findall(
        r'data-qa="task-question">(.*?)(?=data-qa="task-question"|</(?:div|section|form)>)',
        html, re.DOTALL
    )
    questions = []
    for block in q_blocks:
        clean = re.sub(r'<[^>]+>', ' ', block)
        clean = re.sub(r'\s+', ' ', clean).strip()
        questions.append(clean)

    field_answers = {}

    # ── Textarea (task_*_text) ──────────────────────────────────
    for i, name in enumerate(re.findall(r'<textarea[^>]+name="(task_\d+_text)"', html)):
        q_text = questions[i] if i < len(questions) else ""
        field_answers[name] = get_questionnaire_answer(q_text)

    # ── Radio (task_*) ──────────────────────────────────────────
    # Собираем группы: {name: [(value, label), ...]}
    radio_groups: dict = {}
    for inp in re.findall(r'<input[^>]+type="radio"[^>]+>', html, re.IGNORECASE):
        name_m = re.search(r'name="([^"]+)"', inp)
        val_m  = re.search(r'value="([^"]+)"', inp)
        if name_m and val_m and re.match(r'task_\d+', name_m.group(1)):
            radio_groups.setdefault(name_m.group(1), []).append(val_m.group(1))

    # Для каждой radio-группы выбираем значение
    # Порядок radio-групп ≈ порядку вопросов после textarea
    textarea_count = len([k for k in field_answers])
    for i, (name, values) in enumerate(radio_groups.items()):
        if name in field_answers:
            continue
        q_idx = textarea_count + i
        q_text = questions[q_idx] if q_idx < len(questions) else ""
        tmpl_answer = get_questionnaire_answer(q_text).lower()

        if not values:
            continue
        chosen = values[0]  # дефолт — первый вариант

        # Ищем label-текст для каждого value чтобы сопоставить с шаблоном
        # Порядок: первый input = "да", второй = "нет" (типичная раскладка HH)
        # Если шаблон содержит "нет"/"no" — берём второй
        if any(w in tmpl_answer for w in ("нет", "no", "не готов", "не готова", "не могу")):
            chosen = values[1] if len(values) > 1 else values[0]

        field_answers[name] = chosen

    # ── Checkbox (task_*) ───────────────────────────────────────
    checkbox_groups: dict = {}
    for inp in re.findall(r'<input[^>]+type="checkbox"[^>]+>', html, re.IGNORECASE):
        name_m = re.search(r'name="([^"]+)"', inp)
        val_m  = re.search(r'value="([^"]+)"', inp)
        if name_m and val_m and re.match(r'task_\d+', name_m.group(1)):
            checkbox_groups.setdefault(name_m.group(1), []).append(val_m.group(1))

    cb_idx = len(field_answers)
    for name, values in checkbox_groups.items():
        if name in field_answers:
            continue
        q_idx = cb_idx
        cb_idx += 1
        q_text = questions[q_idx] if q_idx < len(questions) else ""
        answer = get_questionnaire_answer(q_text).lower()
        # Pick values that match keywords in the answer
        selected = [v for v in values if any(kw in v.lower() for kw in answer.split() if len(kw) > 2)]
        if not selected:
            selected = [values[0]]  # fallback to first
        field_answers[name] = selected[0]

    # ── Select (dropdown) fields ───────────────────────────────
    select_idx = cb_idx
    for m in re.finditer(r'<select[^>]+name="(task_\d+)"[^>]*>([\s\S]*?)</select>', html):
        sel_name = m.group(1)
        options_html = m.group(2)
        options = re.findall(r'<option[^>]+value="([^"]*)"[^>]*>([^<]*)</option>', options_html)
        if options and sel_name not in field_answers:
            q_text = questions[select_idx] if select_idx < len(questions) else ""
            select_idx += 1
            answer = get_questionnaire_answer(q_text).lower()
            # Pick best matching option
            best = options[0][0]  # default first
            for val, label in options:
                if any(kw in label.lower() for kw in answer.split() if len(kw) > 2):
                    best = val
                    break
            field_answers[sel_name] = best

    return questions, field_answers


def _parse_questionnaire_rich(html: str) -> list:
    """Парсит форму опросника и возвращает богатую структуру для LLM:
    list of {field, type, text, options: [{value, label}]}
    """
    q_blocks = re.findall(
        r'data-qa="task-question">(.*?)(?=data-qa="task-question"|</(?:div|section|form)>)',
        html, re.DOTALL
    )
    q_texts = []
    for b in q_blocks:
        c = re.sub(r'<[^>]+>', ' ', b)
        c = re.sub(r'&quot;', '"', re.sub(r'&ndash;', '–', re.sub(r'&nbsp;', ' ', c)))
        c = re.sub(r'\s+', ' ', c).strip()
        q_texts.append(c)

    result = []
    q_idx = 0

    for name in re.findall(r'<textarea[^>]+name="(task_\d+_text)"', html):
        result.append({"field": name, "type": "textarea",
                       "text": q_texts[q_idx] if q_idx < len(q_texts) else "", "options": []})
        q_idx += 1

    radio_groups: dict = {}
    radio_order: list = []
    for inp in re.findall(r'<input[^>]+type="radio"[^>]+>', html, re.I):
        nm = re.search(r'name="([^"]+)"', inp)
        vl = re.search(r'value="([^"]+)"', inp)
        if nm and vl and re.match(r'task_\d+', nm.group(1)):
            n, v = nm.group(1), vl.group(1)
            if n not in radio_groups:
                radio_groups[n] = []
                radio_order.append(n)
            radio_groups[n].append(v)

    label_map: dict = {}
    for inp_with_id in re.findall(r'<input[^>]+type="radio"[^>]+id="([^"]+)"[^>]*>', html, re.I):
        label_m = re.search(rf'<label[^>]+for="{re.escape(inp_with_id)}"[^>]*>(.*?)</label>', html, re.DOTALL)
        if label_m:
            lbl = re.sub(r'<[^>]+>', '', label_m.group(1)).strip()
            label_map[inp_with_id] = lbl
    default_labels = ["да", "нет"]

    for name in radio_order:
        vals = radio_groups[name]
        options = [{"value": v, "label": label_map.get(v, default_labels[i] if i < len(default_labels) else v)}
                   for i, v in enumerate(vals)]
        result.append({"field": name, "type": "radio",
                       "text": q_texts[q_idx] if q_idx < len(q_texts) else "", "options": options})
        q_idx += 1

    checkbox_groups: dict = {}
    checkbox_order: list = []
    for inp in re.findall(r'<input[^>]+type="checkbox"[^>]+>', html, re.I):
        nm = re.search(r'name="([^"]+)"', inp)
        vl = re.search(r'value="([^"]+)"', inp)
        if nm and vl and re.match(r'task_\d+', nm.group(1)):
            n, v = nm.group(1), vl.group(1)
            if n not in checkbox_groups:
                checkbox_groups[n] = []
                checkbox_order.append(n)
            checkbox_groups[n].append(v)

    for name in checkbox_order:
        vals = checkbox_groups[name]
        options = [{"value": v, "label": v} for v in vals]
        result.append({"field": name, "type": "checkbox",
                       "text": q_texts[q_idx] if q_idx < len(q_texts) else "", "options": options})
        q_idx += 1

    # Select (dropdown) fields
    for m in re.finditer(r'<select[^>]+name="(task_\d+)"[^>]*>([\s\S]*?)</select>', html):
        sel_name = m.group(1)
        options_html = m.group(2)
        options = re.findall(r'<option[^>]+value="([^"]*)"[^>]*>([^<]*)</option>', options_html)
        q_text = q_texts[q_idx] if q_idx < len(q_texts) else ""
        q_idx += 1
        result.append({"field": sel_name, "type": "select", "text": q_text,
                       "options": [{"value": v, "label": l} for v, l in options]})

    return result


async def fill_and_submit_questionnaire(acc: dict, vid: str,
                                        vacancy_title: str = "", company: str = "") -> tuple:
    """
    Получает страницу опроса, заполняет шаблонными ответами и отправляет.
    Поддерживает textarea, radio, checkbox.
    Возвращает (result, info): result = sent | limit | test | error
    """
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    headers_get = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": f"https://hh.ru/vacancy/{vid}",
    }

    try:
        async with aiohttp.ClientSession(
            cookies=acc["cookies"], connector=connector,
            headers=headers_get
        ) as session:
            url_form = f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}&withoutTest=no"

            # Шаг 1: GET форма опроса
            async with session.get(url_form, timeout=aiohttp.ClientTimeout(total=15)) as r:
                html = await r.text()

            # Hidden поля
            hidden = dict(re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html))
            hidden.update(dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]+type="hidden"[^>]+value="([^"]*)"', html)))

            # Парсим все поля опроса
            questions, field_answers = _parse_questionnaire_fields(html)

            if not field_answers:
                log_debug(f"Questionnaire: no task fields found for {vid}")
                return "test", {}

            # LLM-заполнение опросника (если включено)
            if CONFIG.llm_fill_questionnaire and CONFIG.llm_enabled and _openai_available and questions:
                rich_qs = _parse_questionnaire_rich(html)
                resume_text = ""
                if CONFIG.llm_use_resume:
                    resume_text = fetch_resume_text(acc)
                llm_ans = generate_llm_questionnaire_answers(rich_qs, vacancy_title, company, resume_text=resume_text)
                if llm_ans:
                    # Validate LLM answers against actual options
                    rich_fields = {q["field"]: q for q in rich_qs}
                    validated_ans = {}
                    for field, llm_val in llm_ans.items():
                        if field in rich_fields:
                            q = rich_fields[field]
                            if q["type"] in ("radio", "checkbox", "select") and q["options"]:
                                valid_values = [o["value"] for o in q["options"]]
                                if llm_val in valid_values:
                                    validated_ans[field] = llm_val
                                else:
                                    # Try fuzzy match (case-insensitive strip)
                                    matched = [v for v in valid_values if v.lower().strip() == llm_val.lower().strip()]
                                    if matched:
                                        validated_ans[field] = matched[0]
                                    else:
                                        log_debug(f"LLM answer '{llm_val}' not in options {valid_values}, skipping field {field}")
                            else:
                                validated_ans[field] = llm_val
                        else:
                            validated_ans[field] = llm_val
                    overridden = [f for f in validated_ans if f in field_answers]
                    for f in overridden:
                        field_answers[f] = validated_ans[f]
                    log_debug(f"Questionnaire {vid}: LLM заполнил {len(overridden)}/{len(field_answers)} полей: {overridden}")
                else:
                    log_debug(f"Questionnaire {vid}: LLM вернул пустой ответ, используем шаблоны")

            log_debug(f"Questionnaire {vid}: {len(field_answers)} fields, {len(questions)} questions")
            for name, val in field_answers.items():
                log_debug(f"  {name} = {str(val)[:80]}")

            # Шаг 2: POST данные
            data = aiohttp.FormData()
            data.add_field("resume_hash", acc["resume_hash"])
            data.add_field("vacancy_id", vid)
            data.add_field("letter", _randomize_text(acc.get("letter", "")))
            data.add_field("lux", "true")

            for name in ("_xsrf", "uidPk", "guid", "startTime", "testRequired"):
                if name in hidden:
                    data.add_field(name, hidden[name])

            for name, value in field_answers.items():
                data.add_field(name, str(value))

            # Шаг 3: POST
            async with session.post(
                url_form,
                headers={"X-Xsrftoken": acc.get("cookies", {}).get("_xsrf", ""), "Referer": url_form},
                data=data,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=False,
            ) as r2:
                status = r2.status
                location = r2.headers.get("location", "")
                txt = await r2.text()

            log_debug(f"Questionnaire submit {vid}: HTTP {status} location={location}")

            if status in (302, 303):
                if "negotiations-limit-exceeded" in location or "negotiations-limit-exceeded" in txt:
                    return "limit", {}
                # Редирект назад на форму — ошибка валидации
                if "withoutTest=no" in location or f"vacancyId={vid}" in location:
                    log_debug(f"Questionnaire {vid}: form rejected, redirect back")
                    return "test", {}
                return "sent", {}

            if status == 200:
                if "negotiations-limit-exceeded" in txt:
                    return "limit", {}
                if "test-required" in txt:
                    return "test", {}
                return "sent", {}

            return "test", {}

    except Exception as e:
        log_debug(f"fill_and_submit_questionnaire error: {e}")
        return "error", {"exception": str(e)}


def _check_vacancy_before_apply(acc: dict, vid: str) -> dict:
    """Pre-check vacancy before applying: detect impossible responses and experience mismatches.
    Returns {"ok": bool, "reason": str}
    """
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    try:
        r = requests.get(
            f"https://hh.ru/applicant/vacancy_response/popup?vacancyId={vid}",
            headers={"User-Agent": ua, "Accept": "application/json, */*",
                     "Referer": f"https://hh.ru/vacancy/{vid}"},
            cookies=acc.get("cookies", {}),
            timeout=10, verify=False,
        )
        if r.status_code not in (200,):
            return {"ok": True, "reason": ""}  # can't check, allow
        data = r.json()
        # Check responseImpossible
        resp_status = data.get("responseStatus") or {}
        if resp_status.get("responseImpossible"):
            reason = resp_status.get("responseImpossibleReason", "responseImpossible")
            return {"ok": False, "reason": str(reason)}
        # Check resume inconsistencies (experience type mismatch)
        body = data.get("body", {})
        inner_rs = body.get("responseStatus", resp_status)
        incon_data = inner_rs.get("resumeInconsistencies", resp_status.get("resumeInconsistencies", {}))
        if isinstance(incon_data, dict):
            for resume_entry in incon_data.get("resume", []):
                for inc in (resume_entry.get("inconsistencies", {}).get("inconsistency", [])):
                    if inc.get("type") == "EXPERIENCE":
                        return {"ok": False, "reason": f"опыт: нужен {inc.get('required','?')}, есть {inc.get('actual','?')}"}
        elif isinstance(incon_data, list):
            for inc in incon_data:
                if isinstance(inc, dict) and inc.get("type") == "EXPERIENCE":
                    return {"ok": False, "reason": f"несовпадение опыта"}

        # Extract contactInfo if available
        contact = {}
        sv = inner_rs.get("shortVacancy", {})
        ci = sv.get("contactInfo", {})
        if ci and (ci.get("email") or ci.get("fio")):
            contact = {
                "fio": ci.get("fio", ""),
                "email": ci.get("email", ""),
                "phone": "",
            }
            phones = ci.get("phones", {}).get("phones", [])
            if phones:
                p = phones[0]
                contact["phone"] = f"+{p.get('country','')}{p.get('city','')}{p.get('number','')}"

        return {"ok": True, "reason": "", "contact": contact}
    except Exception as e:
        log_debug(f"_check_vacancy_before_apply {vid}: {e}")
        return {"ok": True, "reason": ""}  # on error, allow apply


async def send_response_async(acc: dict, vid: str) -> tuple:
    """Асинхронная отправка отклика. Возвращает (результат, инфо)"""
    log_debug(f"📤 ОТПРАВКА ОТКЛИКА на вакансию {vid} | Аккаунт: {acc['name']}")

    xsrf = acc.get("cookies", {}).get("_xsrf", "")
    if not xsrf:
        return "error", {"exception": "Missing _xsrf token"}
    headers = get_headers(xsrf)

    letter = _randomize_text(acc.get("letter", ""))

    data = aiohttp.FormData()
    data.add_field("resume_hash", acc["resume_hash"])
    data.add_field("vacancy_id", vid)
    data.add_field("letterRequired", "true")
    data.add_field("letter", letter)
    data.add_field("lux", "true")
    data.add_field("ignore_postponed", "true")

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_context)

    try:
        async with aiohttp.ClientSession(headers=headers, cookies=acc["cookies"], connector=connector) as session:
            async with session.post(
                "https://hh.ru/applicant/vacancy_response/popup",
                data=data,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                txt = await r.text()
                status_code = r.status

        log_debug(f"   Ответ HTTP: {status_code} | Размер: {len(txt)}")
        if status_code >= 400:
            log_debug(f"   Тело ответа: {txt[:300]}")

        if status_code in (401, 403):
            return "auth_error", {}

        if status_code == 200 and _is_login_page(txt):
            return "auth_error", {}

        if status_code == 200:
            if "shortVacancy" in txt:
                try:
                    p = json.loads(txt)
                    info = {
                        "title": glom(p, "responseStatus.shortVacancy.name", default="?"),
                        "company": glom(p, "responseStatus.shortVacancy.company.name", default="?"),
                        "salary_from": glom(p, "responseStatus.shortVacancy.compensation.from", default=None),
                        "salary_to": glom(p, "responseStatus.shortVacancy.compensation.to", default=None),
                    }
                    # Extract HR contact info
                    ci = glom(p, "responseStatus.shortVacancy.contactInfo", default={})
                    if ci and (ci.get("email") or ci.get("fio")):
                        contact = {"fio": ci.get("fio", ""), "email": ci.get("email", ""), "phone": ""}
                        phones = (ci.get("phones") or {}).get("phones", [])
                        if phones:
                            ph = phones[0]
                            contact["phone"] = f"+{ph.get('country','')}{ph.get('city','')}{ph.get('number','')}"
                        info["contact"] = contact
                    return "sent", info
                except Exception as e:
                    return "sent", {}

            if '"success":true' in txt or '"status":"ok"' in txt or '"responded":true' in txt:
                return "sent", {}

            return "sent", {}

        if "negotiations-limit-exceeded" in txt:
            return "limit", {}

        if "test-required" in txt:
            info = {}
            if "shortVacancy" in txt:
                try:
                    p = json.loads(txt)
                    info = {
                        "title": glom(p, "responseStatus.shortVacancy.name", default=""),
                        "company": glom(p, "responseStatus.shortVacancy.company.name", default=""),
                    }
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            return "test", info

        if "alreadyApplied" in txt:
            return "already", {}

        return "error", {"raw": txt[:200]}
    except Exception as e:
        return "error", {"exception": str(e)}


def check_limit(acc: dict) -> bool:
    """True если лимит активен. Uses GET popup (no side effects)."""
    # GET popup — safe, no side effects, no wasted apply slots
    xsrf = acc.get("cookies", {}).get("_xsrf", "")
    if not xsrf:
        return True
    try:
        r_search = requests.get(
            "https://hh.ru/search/vacancy?text=&area=1&page=0",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                     "Accept": "text/html"},
            cookies=acc["cookies"], verify=False, timeout=10,
        )
        vids = re.findall(r'/vacancy/(\d+)', r_search.text)
        if not vids:
            return True
        vid = vids[0]
        # Use GET popup — safe, no side effects
        r = requests.get(
            f"https://hh.ru/applicant/vacancy_response/popup?vacancyId={vid}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                     "Accept": "application/json", "X-Xsrftoken": xsrf},
            cookies=acc["cookies"], verify=False, timeout=10,
        )
        return "negotiations-limit-exceeded" in r.text
    except Exception:
        return True


def touch_resume(acc: dict) -> tuple:
    """
    Поднять резюме в поиске.
    Tries OAuth API first (no captcha), falls back to web.
    Возвращает (success: bool, message: str)
    """
    # Try OAuth first — no captcha!
    ok, msg = _oauth_touch_resume(acc)
    if ok:
        return True, msg
    if "429" not in msg:
        log_debug(f"touch_resume OAuth failed: {msg}, trying web fallback")

    # Fallback to web (may hit captcha)
    xsrf = acc.get("cookies", {}).get("_xsrf", "")
    if not xsrf:
        return False, msg or "Missing _xsrf token"
    headers = get_headers(xsrf)
    resume_hash = acc["resume_hash"]

    touch_files = {
        "resume": (None, resume_hash),
        "undirectable": (None, "true")
    }

    try:
        response = requests.post(
            "https://hh.ru/applicant/resumes/touch",
            headers=headers,
            cookies=acc["cookies"],
            files=touch_files,
            timeout=10
        )

        if response.status_code == 200:
            return True, "Резюме поднято (web)!"
        elif response.status_code == 429:
            return False, "Слишком часто (429)"
        else:
            return False, msg or f"HTTP {response.status_code}"

    except Exception as e:
        return False, msg or f"Ошибка: {str(e)[:30]}"


def fetch_hh_negotiations_stats(acc: dict, max_pages: int = 20) -> dict:
    """Получить статистику откликов с hh.ru (парсинг HTML)"""
    cookies = acc["cookies"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://hh.ru/applicant/negotiations",
    }

    result = {
        "interview": 0,
        "recent_interview": 0,  # только последние 60 дней
        "viewed": 0,
        "not_viewed": 0,
        "discard": 0,
        "interviews_list": [],
        "neg_ids": [],
        "auth_error": False,
        "unread_by_employer": 0,  # count of negotiations where employer hasn't read our messages
    }
    cutoff = datetime.now().astimezone() - timedelta(days=60)

    # ── Шаг 1: точный счёт интервью через state=INTERVIEW фильтр ──────────────
    for page in range(max_pages):
        try:
            resp = requests.get(
                f"https://hh.ru/applicant/negotiations?filter=all&state=INTERVIEW&page={page}",
                cookies=cookies,
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                if resp.status_code in (401, 403) or _is_login_page(resp.text):
                    result["auth_error"] = True
                break
            body = resp.text
            if _is_login_page(body):
                result["auth_error"] = True
                break

            # Extract negotiation IDs: HH renders items as buttons (no href links),
            # IDs are stored as chatId in the page's embedded JSON
            page_neg_ids = re.findall(r'"chatId"\s*:\s*(\d+)', body)
            for nid in page_neg_ids:
                if nid not in result["neg_ids"]:
                    result["neg_ids"].append(nid)

            parts = re.split(r'data-qa="negotiations-item"', body)
            if len(parts) <= 1:
                break

            items_on_page = 0
            for item in parts[1:]:
                items_on_page += 1
                item = re.sub(r'^[^>]*>', '', item, count=1)

                result["interview"] += 1

                # chatId per item
                neg_id_match = re.search(r'"chatId"\s*:\s*(\d+)', item)
                item_neg_id = neg_id_match.group(1) if neg_id_match else ""
                if item_neg_id and item_neg_id not in result["neg_ids"]:
                    result["neg_ids"].append(item_neg_id)

                # Извлекаем текст для списка
                clean = re.sub(r'<svg[\s\S]*?</svg>', '', item)
                clean = re.sub(r'<[^>]*>', ' ', clean, flags=re.DOTALL)
                clean = re.sub(r'\s+', ' ', clean).strip()
                text_body = re.sub(r'^(Собеседование|Приглашение|Интервью)\s*', '', clean)
                text_body = re.split(
                    r'\s+(?:сегодня\b|вчера\b|Был\s+онлайн|позавчера\b|\d+\s+\w+\s+назад)',
                    text_body
                )[0].strip()

                date_match = re.search(r'datetime="([^"]+)"', item)
                date_str = ""
                is_recent = True
                if date_match:
                    try:
                        dt = datetime.fromisoformat(date_match.group(1).replace("Z", "+00:00"))
                        date_str = dt.strftime("%d.%m")
                        is_recent = dt >= cutoff
                    except Exception:
                        date_str = date_match.group(1)[:10]
                if is_recent:
                    result["recent_interview"] += 1
                result["interviews_list"].append({
                    "text": text_body[:120],
                    "date": date_str,
                    "recent": is_recent,
                    "neg_id": item_neg_id,
                })

            if items_on_page == 0:
                break

        except Exception as e:
            log_debug(f"fetch_hh_negotiations_stats interviews page={page} error: {e}")
            break

    if result["auth_error"]:
        return result

    # ── Шаг 2: просмотры / отказы с общей страницы ────────────────────────────
    for page in range(max_pages):
        try:
            resp = requests.get(
                f"https://hh.ru/applicant/negotiations?page={page}",
                cookies=cookies,
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                break
            body = resp.text
            if _is_login_page(body):
                break

            # On first page, extract SSR data for conversationUnreadByEmployerCount
            if page == 0:
                try:
                    ssr = parse_hh_lux_ssr(body)
                    topic_list = ssr.get("topicList", [])
                    if isinstance(topic_list, list):
                        for topic in topic_list:
                            if isinstance(topic, dict):
                                cnt = topic.get("conversationUnreadByEmployerCount", 0)
                                if isinstance(cnt, int) and cnt > 0:
                                    result["unread_by_employer"] += 1
                except Exception:
                    pass

            parts = re.split(r'data-qa="negotiations-item"', body)
            if len(parts) <= 1:
                break

            items_on_page = 0
            for item in parts[1:]:
                items_on_page += 1
                item = re.sub(r'^[^>]*>', '', item, count=1)
                clean = re.sub(r'<svg[\s\S]*?</svg>', '', item)
                clean = re.sub(r'<[^>]*>', ' ', clean, flags=re.DOTALL)
                clean = re.sub(r'\s+', ' ', clean).strip()
                first_word = clean.split(' ')[0] if clean else ''

                if first_word in ('Отказ', 'Отклонено', 'Отклонён'):
                    result["discard"] += 1
                elif clean.startswith('Просмотрен'):
                    result["viewed"] += 1
                elif first_word not in ('Собеседование', 'Приглашение', 'Интервью'):
                    result["not_viewed"] += 1

            if items_on_page == 0:
                break

        except Exception as e:
            log_debug(f"fetch_hh_negotiations_stats general page={page} error: {e}")
            break

    return result


def _fetch_chat_list(acc: dict, max_pages: int = 5) -> tuple:
    """Fetch paginated chat list from chatik.hh.ru/chatik/api/chats.
    Returns (items_by_id, display_info, current_participant_id).
    items_by_id: {str(item_id): item_dict}
    """
    _ensure_chatik_cookies(acc)
    xsrf = acc.get("cookies", {}).get("_xsrf", "")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, */*",
        "Origin": "https://chatik.hh.ru",
        "Referer": "https://chatik.hh.ru/",
        "X-XSRFToken": xsrf,
    }
    items_by_id: dict = {}
    display_info: dict = {}
    current_participant_id: str = ""

    for page_num in range(max_pages):
        url = f"https://chatik.hh.ru/chatik/api/chats?page={page_num}"
        try:
            resp = requests.get(url, cookies=acc["cookies"], headers=headers, timeout=15, verify=False)
            if resp.status_code in (401, 403) or _is_login_page(resp.text):
                break
            if resp.status_code != 200:
                break
            data = resp.json()
        except Exception as e:
            log_debug(f"_fetch_chat_list error: {e}")
            break

        chats_obj = data.get("chats", {})
        items = chats_obj.get("items", [])
        display_info.update(data.get("chatsDisplayInfo", {}))

        for item in items:
            item_id = str(item.get("id", ""))
            if item_id:
                items_by_id[item_id] = item
            if not current_participant_id:
                current_participant_id = item.get("currentParticipantId", "")

        # Check pagination: if fewer items than perPage, we've reached the end
        per_page = chats_obj.get("perPage", 20)
        if len(items) < per_page:
            break

    return items_by_id, display_info, current_participant_id


# Phrases that indicate chat messaging is disabled (employer locked or invite-only)
_LOCKED_CHAT_PHRASES = (
    "работодатель отключил переписку",
    "переписка будет доступна после приглашения",
)

def _check_chat_locked(item: dict) -> str:
    """Return lock reason string if chat has messaging disabled, else empty string."""
    last_msg = item.get("lastMessage") or {}
    last_text = (last_msg.get("text") or "").lower()
    for phrase in _LOCKED_CHAT_PHRASES:
        if phrase in last_text:
            return last_text[:80]
    # Also check item-level flags (canSendMessage, locked, state, etc.)
    if item.get("canSendMessage") is False or item.get("locked") is True:
        return "canSendMessage=false"
    state = str(item.get("state") or item.get("chatState") or "").lower()
    if state in ("locked", "closed", "disabled", "invitation_required"):
        return f"state={state}"
    return ""


def _build_thread_from_chat_item(item: dict, display_info: dict, cur_pid: str, neg_id: str) -> dict:
    """Build a thread result dict from a /chat/messages item."""
    result = {"neg_id": neg_id, "employer_name": "Работодатель", "vacancy_title": "",
              "messages": [], "needs_reply": False, "last_msg_id": "", "last_employer_msg": "",
              "topic_id": "", "error": "", "chat_locked": ""}

    info = display_info.get(str(neg_id), display_info.get(str(item.get("id", "")), {}))
    result["employer_name"] = (info.get("subtitle") or "Работодатель").strip(" ,")
    result["vacancy_title"] = (info.get("title") or "").strip()

    last_msg = item.get("lastMessage") or {}
    last_text = (last_msg.get("text") or "").strip()
    last_msg_id = str(last_msg.get("id", ""))
    unread = item.get("unreadCount", 0)

    # Check for chat lock FIRST — if locked, no reply possible regardless of sender
    lock_reason = _check_chat_locked(item)
    if lock_reason:
        result["chat_locked"] = lock_reason
        result["last_msg_id"] = last_msg_id or str(hash(last_text))
        log_debug(f"_build_thread {neg_id}: чат заблокирован — {lock_reason!r}")
        return result

    # Sender: compare participantId with currentParticipantId
    sender_id = last_msg.get("participantId", "")
    from_employer = bool(sender_id and cur_pid and sender_id != cur_pid)

    # Check for workflow transitions: skip only string-type workflow events (REJECTION, APPLICATION, etc.)
    # Numeric wf.id = internal message reference, not a system event — real employer text
    wf = last_msg.get("workflowTransition") or {}
    wf_id = wf.get("id", "") if isinstance(wf, dict) else ""
    is_workflow_msg = isinstance(wf_id, str) and bool(wf_id)  # only string types = system events

    needs_reply = (unread > 0) and from_employer and not is_workflow_msg
    result["needs_reply"] = needs_reply
    result["last_msg_id"] = last_msg_id or str(hash(last_text))

    if last_text:
        sender = "employer" if from_employer else "applicant"
        result["messages"] = [{"sender": sender, "text": last_text, "msg_id": last_msg_id}]
        if from_employer:
            result["last_employer_msg"] = last_text

    resources = (item.get("resources") or {})
    neg_topics = resources.get("NEGOTIATION_TOPIC", [])
    if neg_topics:
        result["topic_id"] = str(neg_topics[0])

    return result


def fetch_negotiation_thread(acc: dict, neg_id: str) -> dict:
    """Fetch info for a single negotiation thread (chatId = neg_id).
    Fetches the full paginated chat list and finds the matching entry.
    Returns {neg_id, employer_name, vacancy_title, messages, needs_reply,
             last_msg_id, last_employer_msg, topic_id, error}
    """
    result = {"neg_id": neg_id, "employer_name": "Работодатель", "vacancy_title": "",
              "messages": [], "needs_reply": False, "last_msg_id": "", "last_employer_msg": "",
              "topic_id": "", "error": ""}
    try:
        items_by_id, display_info, cur_pid = _fetch_chat_list(acc, max_pages=5)
        item = items_by_id.get(str(neg_id))
        if not item:
            result["error"] = "чат не найден"
            log_debug(f"fetch_negotiation_thread {neg_id}: not in {list(items_by_id.keys())[:5]}")
            return result
        return _build_thread_from_chat_item(item, display_info, cur_pid, neg_id)
    except Exception as e:
        result["error"] = str(e)
        log_debug(f"fetch_negotiation_thread {neg_id}: {e}")
    return result


def _fetch_chat_history(acc: dict, chat_id: str, max_messages: int = 20) -> list:
    """Fetch full message history for a specific chat via chatik/api/chat_data.
    Returns list of {"sender": "employer"|"applicant", "text": str} dicts,
    oldest first, skipping system/workflow messages.
    """
    _ensure_chatik_cookies(acc)
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    xsrf = acc["cookies"].get("_xsrf", "")
    try:
        r = requests.get(
            "https://chatik.hh.ru/chatik/api/chat_data",
            params={"chatId": int(chat_id)},
            cookies=acc["cookies"],
            headers={
                "User-Agent": ua,
                "Accept": "application/json",
                "Referer": "https://chatik.hh.ru/",
                "Origin": "https://chatik.hh.ru",
                "X-XSRFToken": xsrf,
            },
            timeout=15,
            verify=False,
        )
        if r.status_code != 200:
            log_debug(f"_fetch_chat_history {chat_id}: HTTP {r.status_code}")
            return []
        data = r.json()
        cur_pid = str(data.get("chat", {}).get("currentParticipantId", ""))
        items = data.get("chat", {}).get("messages", {}).get("items", [])
        conversation = []
        for msg in items:
            # Skip non-text message types
            if msg.get("type") not in ("SIMPLE",):
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            # Skip system workflow events (rejection, offer, etc.) — string wf.id only.
            # Numeric wf.id = internal message reference, not a system event — keep those.
            wf = msg.get("workflowTransition") or {}
            wf_id = wf.get("id", "") if isinstance(wf, dict) else ""
            if isinstance(wf_id, str) and wf_id:
                continue
            sender_pid = str(msg.get("participantId", ""))
            sender = "applicant" if (cur_pid and sender_pid == cur_pid) else "employer"
            pd = msg.get("participantDisplay") or {}
            conversation.append({
                "sender": sender, "text": text,
                "msg_id": str(msg.get("id", "")),
                "actions": msg.get("actions") or {},
                "is_bot": pd.get("isBot", False),
            })
        # Return last max_messages entries (most recent context)
        return conversation[-max_messages:]
    except Exception as e:
        log_debug(f"_fetch_chat_history {chat_id}: {e}")
        return []


def _ensure_chatik_cookies(acc: dict) -> None:
    """Fetch hhuid/crypted_hhuid from hh.ru if missing, storing them in acc['cookies'] in-place."""
    if acc["cookies"].get("hhuid"):
        return
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    try:
        r = requests.get(
            "https://hh.ru/",
            cookies=acc["cookies"],
            headers={"User-Agent": ua},
            timeout=10,
            verify=False,
            allow_redirects=True,
        )
        for cookie in r.cookies:
            if cookie.name in ("hhuid", "crypted_hhuid"):
                acc["cookies"][cookie.name] = cookie.value
                log_debug(f"_ensure_chatik_cookies: got {cookie.name} for {acc.get('name', '?')}")
    except Exception as e:
        log_debug(f"_ensure_chatik_cookies error: {e}")


def send_negotiation_message(acc: dict, neg_id: str, text: str, topic_id: str = "") -> bool:
    """Send a message in an HH negotiation thread via chatik.hh.ru/chatik/api/send."""
    import uuid as _uuid
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    # Ensure we have chatik auth cookies (hhuid/crypted_hhuid)
    _ensure_chatik_cookies(acc)
    xsrf = acc["cookies"].get("_xsrf", "")

    try:
        resp = requests.post(
            "https://chatik.hh.ru/chatik/api/send",
            cookies=acc["cookies"],
            headers={
                "User-Agent": ua,
                "Accept": "application/json, */*",
                "Content-Type": "application/json",
                "Referer": "https://chatik.hh.ru/",
                "Origin": "https://chatik.hh.ru",
                "X-XSRFToken": xsrf,
            },
            json={"chatId": int(str(neg_id).strip()), "idempotencyKey": str(_uuid.uuid4()), "text": text},
            timeout=15,
            verify=False,
        )
        log_debug(f"send via chatik/api/send {neg_id}: HTTP {resp.status_code} | {resp.text[:300]}")
        if resp.status_code in (200, 201, 204):
            return True
        if resp.status_code == 409:
            # Chat no longer exists (archived/closed negotiation)
            return "chat_not_found"
        return False
    except Exception as e:
        log_debug(f"send_negotiation_message {neg_id} error: {e}")
        return False


def _mark_chat_read(acc: dict, chat_id: str, message_id: str):
    """Mark a chatik chat as read up to the given message ID."""
    try:
        cid = int(str(chat_id).strip())
        mid = int(str(message_id).strip())
    except (ValueError, TypeError):
        return
    _ensure_chatik_cookies(acc)
    xsrf = acc.get("cookies", {}).get("_xsrf", "")
    try:
        requests.post(
            "https://chatik.hh.ru/chatik/api/mark_read",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                     "Accept": "application/json", "Content-Type": "application/json",
                     "Origin": "https://chatik.hh.ru", "Referer": "https://chatik.hh.ru/",
                     "X-XSRFToken": xsrf},
            cookies=acc["cookies"],
            json={"chatId": cid, "messageId": mid},
            verify=False, timeout=5
        )
    except Exception as e:
        log_debug(f"_mark_chat_read {cid}: {e}")


def generate_llm_reply(conversation: list, employer_name: str = "", cover_letter: str = "", resume_text: str = "") -> str:
    """Generate a reply to employer using configured LLM (OpenAI-compatible API)."""
    global _llm_rr_index
    if not _openai_available:
        log_debug("generate_llm_reply: openai package not installed")
        return ""

    # Build profiles list: use multi-profile config if available, else fall back to legacy fields
    profiles = [p for p in (CONFIG.llm_profiles or []) if p.get("enabled", True) and p.get("api_key")]
    if not profiles:
        # Legacy fallback: use old single-key config
        if not CONFIG.llm_api_key:
            return ""
        profiles = [{"api_key": CONFIG.llm_api_key, "base_url": CONFIG.llm_base_url,
                     "model": CONFIG.llm_model}]

    # Build messages list (shared across profile attempts)
    system = CONFIG.llm_system_prompt
    if resume_text and resume_text.strip():
        system += (
            f"\n\n---\nРезюме соискателя (используй для персонализации ответов):\n"
            f"{resume_text.strip()}\n---"
        )
    if cover_letter and cover_letter.strip():
        system += (
            f"\n\nКонтекст: соискатель откликнулась на вакансию работодателя «{employer_name}» "
            f"со следующим сопроводительным письмом:\n\"\"\"\n{cover_letter.strip()}\n\"\"\"\n"
            "Учитывай содержание письма при ответе — не противоречь ему и будь последовательна."
        )
    messages = [{"role": "system", "content": system}]
    for msg in conversation[-8:]:
        role = "user" if msg["sender"] == "employer" else "assistant"
        messages.append({"role": role, "content": msg["text"]})

    mode = CONFIG.llm_profile_mode

    if mode == "roundrobin":
        # Pick one profile by round-robin, try only that one
        with _llm_rr_lock:
            idx = _llm_rr_index % len(profiles)
            _llm_rr_index += 1
        profile = profiles[idx]
        pname = profile.get("name") or profile.get("model") or f"профиль {idx}"
        model = profile.get("model") or "gpt-4o-mini"
        log_debug(f"generate_llm_reply: roundrobin → {pname} ({model}), {len(messages)-1} сообщений")
        try:
            client = _openai_mod.OpenAI(api_key=profile["api_key"], base_url=profile.get("base_url") or None)
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=300,
                temperature=0.7,
            )
            result = resp.choices[0].message.content.strip()
            log_debug(f"generate_llm_reply: {pname} → {len(result)} симв.")
            return result
        except Exception as e:
            log_debug(f"generate_llm_reply roundrobin {pname} error: {e}")
            return ""
    else:
        # Fallback mode: try each profile in order, return first successful result
        for i, profile in enumerate(profiles):
            pname = profile.get("name") or profile.get("model") or f"профиль {i}"
            model = profile.get("model") or "gpt-4o-mini"
            log_debug(f"generate_llm_reply: fallback {i+1}/{len(profiles)} → {pname} ({model}), {len(messages)-1} сообщений")
            try:
                client = _openai_mod.OpenAI(api_key=profile["api_key"], base_url=profile.get("base_url") or None)
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=300,
                    temperature=0.7,
                )
                result = resp.choices[0].message.content.strip()
                log_debug(f"generate_llm_reply: {pname} → {len(result)} симв.")
                return result
            except Exception as e:
                log_debug(f"generate_llm_reply fallback {pname} error: {e}")
                continue
        log_debug("generate_llm_reply: все профили вернули ошибку")
        return ""


def generate_llm_questionnaire_answers(rich_questions: list, vacancy_title: str = "", company: str = "",
                                       resume_text: str = "") -> dict:
    """Заполняет ответы на опросник работодателя через LLM.
    rich_questions — список из _parse_questionnaire_rich().
    resume_text — опционально текст резюме для контекста.
    Возвращает {field: value} или {} при ошибке.
    """
    if not _openai_available or not rich_questions:
        return {}
    profiles = [p for p in (CONFIG.llm_profiles or []) if p.get("enabled", True) and p.get("api_key")]
    if not profiles:
        if not CONFIG.llm_api_key:
            return {}
        profiles = [{"api_key": CONFIG.llm_api_key, "base_url": CONFIG.llm_base_url, "model": CONFIG.llm_model}]

    lines = ["Заполни анкету работодателя для отклика на вакансию."]
    if vacancy_title:
        lines.append(f"Вакансия: {vacancy_title}")
    if company:
        lines.append(f"Компания: {company}")
    lines += ["", "Вопросы:"]
    for i, q in enumerate(rich_questions, 1):
        qtext = q.get("text", "")
        qtype = q.get("type", "textarea")
        if qtype == "textarea":
            lines.append(f'{i}. [текст] {qtext}')
        elif qtype == "radio":
            opts = " / ".join(f'"{o["label"]}" (value={o["value"]})' for o in q.get("options", []))
            lines.append(f'{i}. [выбор одного: {opts}] {qtext}')
        elif qtype == "checkbox":
            opts = " / ".join(f'"{o["label"]}" (value={o["value"]})' for o in q.get("options", []))
            lines.append(f'{i}. [чекбокс: {opts}] {qtext}')
        elif qtype == "select":
            opts = " / ".join(f'"{o["label"]}" (value={o["value"]})' for o in q.get("options", []))
            lines.append(f'{i}. [выпадающий список: {opts}] {qtext}')
    lines += [
        "",
        "Заполни анкету от первого лица. Отвечай кратко и профессионально.",
        "Для текста — 1–3 предложения.",
        "Для radio/checkbox/select — верни точное value из скобок (цифру или код).",
        "",
        "Верни ТОЛЬКО JSON без пояснений:",
        "{"
    ]
    for q in rich_questions:
        lines.append(f'  "{q["field"]}": "...",')
    lines.append("}")

    system = "Ты помогаешь заполнять анкеты при трудоустройстве. Возвращай ТОЛЬКО валидный JSON, без markdown и пояснений."
    if resume_text:
        system += f"\n\nРезюме кандидата:\n{resume_text[:2000]}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]

    for i, profile in enumerate(profiles):
        pname = profile.get("name") or f"профиль {i}"
        model = profile.get("model") or "gpt-4o-mini"
        log_debug(f"generate_llm_questionnaire_answers: {pname} ({model}), {len(rich_questions)} вопросов")
        try:
            client = _openai_mod.OpenAI(api_key=profile["api_key"], base_url=profile.get("base_url") or None)
            resp = client.chat.completions.create(
                model=model, messages=messages, max_tokens=600, temperature=0.3,
            )
            raw = resp.choices[0].message.content.strip()
            log_debug(f"generate_llm_questionnaire_answers raw: {raw[:300]}")
            # Извлекаем JSON — ищем {} блок
            json_m = re.search(r'\{[\s\S]*\}', raw)
            if json_m:
                answers = json.loads(json_m.group())
                return {k: str(v) for k, v in answers.items() if v is not None}
        except Exception as e:
            log_debug(f"generate_llm_questionnaire_answers {pname} error: {e}")
            if i < len(profiles) - 1:
                continue
    return {}


def parse_hh_lux_ssr(html: str) -> dict:
    """Извлечь SSR JSON из <template id="HH-Lux-InitialState">"""
    m = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>([\s\S]*?)</template>', html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def fetch_resume_stats(acc: dict) -> dict:
    """
    Статистика резюме за 7 дней + точный таймер поднятия.
    Возвращает dict с ключами: views, views_new, shows, invitations, invitations_new,
    next_touch_seconds, free_touches, global_invitations, new_invitations_total
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://hh.ru/",
    }
    result = {
        "views": 0, "views_new": 0, "shows": 0,
        "invitations": 0, "invitations_new": 0,
        "next_touch_seconds": 0, "free_touches": 0,
        "global_invitations": 0, "new_invitations_total": 0,
    }
    try:
        r = requests.get(
            "https://hh.ru/applicant/resumes",
            headers=headers, cookies=acc["cookies"], verify=False, timeout=15
        )
        ssr = parse_hh_lux_ssr(r.text)

        # userStats counters
        user_stats = ssr.get("userStats", {})
        result["views_new"] = user_stats.get("new-resumes-views", 0)
        result["new_invitations_total"] = user_stats.get("new-applicant-invitations", 0)
        result["global_invitations"] = ssr.get("globalInvitations", 0)

        # Per-resume statistics
        # Structure: applicantResumesStatistics["resumes"][resume_id]["statistics"]
        stats_map = ssr.get("applicantResumesStatistics", {})
        resumes_stats = stats_map.get("resumes", {}) if isinstance(stats_map, dict) else {}
        for resume_id, data in resumes_stats.items():
            st = data.get("statistics", {})
            result["shows"] += (st.get("searchShows") or {}).get("count", 0)
            result["views"] += (st.get("views") or {}).get("count", 0)
            result["views_new"] = max(result["views_new"], (st.get("views") or {}).get("countNew", 0))
            result["invitations"] += (st.get("invitations") or {}).get("count", 0)
            result["invitations_new"] += (st.get("invitations") or {}).get("countNew", 0)

        # Точный таймер поднятия резюме
        resumes = ssr.get("applicantResumes", [])
        for res in resumes:
            to_update = res.get("toUpdate") or {}
            if "value" in to_update:
                result["next_touch_seconds"] = max(result["next_touch_seconds"], to_update["value"])
            if "count" in to_update:
                result["free_touches"] = to_update["count"]

    except Exception as e:
        log_debug(f"fetch_resume_stats error: {e}")
    return result


def fetch_resume_view_history(acc: dict, limit: int = 50) -> list:
    """
    Кто смотрел резюме.
    Возвращает list of {employer_id, name, date}
    """
    resume_hash = acc["resume_hash"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://hh.ru/applicant/resumes",
    }
    result = []
    try:
        r = requests.get(
            f"https://hh.ru/applicant/resumeview/history?resumeHash={resume_hash}",
            headers=headers, cookies=acc["cookies"], verify=False, timeout=15
        )
        html = r.text

        # Парсим через SSR JSON
        ssr = parse_hh_lux_ssr(html)
        hist_data = ssr.get("applicantResumeViewHistory", {})
        hist_views = hist_data.get("historyViews", {})
        years_list = hist_views.get("years", []) if isinstance(hist_views, dict) else []
        for year_entry in years_list:
            for day_entry in year_entry.get("days", []):
                day   = day_entry.get("day", 0)
                month = day_entry.get("month", 0)
                year  = year_entry.get("year", 0)
                date_str = f"{year}-{month:02d}-{day:02d}"
                for company in day_entry.get("companies", []):
                    views_ts = company.get("views", [])
                    ts = views_ts[0][:10] if views_ts else date_str
                    result.append({
                        "employer_id": str(company.get("id", "")),
                        "name": company.get("name", "").strip() or "Аноним",
                        "date": ts,
                        "vacancy": "",
                    })
                    if len(result) >= limit:
                        break
                if len(result) >= limit:
                    break
            if len(result) >= limit:
                break

        # Fallback: парсим HTML если SSR пустой
        if not result:
            entries = re.findall(
                r'href="/employer/(\d+)[^"]*"[^>]*>.*?<span[^>]*>([^<]+)</span>.*?'
                r'(?:<time[^>]*datetime="([^"]*)")?',
                html, re.DOTALL
            )
            seen = set()
            for employer_id, name, date in entries[:limit]:
                if employer_id not in seen:
                    seen.add(employer_id)
                    result.append({
                        "employer_id": employer_id,
                        "name": name.strip(),
                        "date": date[:10] if date else "",
                        "vacancy": "",
                    })
    except Exception as e:
        log_debug(f"fetch_resume_view_history error: {e}")
    return result


def _hh_ssr_str(val) -> str:
    """Извлечь строку из поля HH SSR, которое может быть str, dict или list[dict]."""
    if isinstance(val, str):
        return val
    if isinstance(val, list) and val:
        first = val[0]
        if isinstance(first, dict):
            return str(first.get("string") or first.get("text") or first.get("name") or "")
        return str(first)
    if isinstance(val, dict):
        return str(val.get("string") or val.get("text") or val.get("value") or val.get("name") or "")
    return ""


def _parse_resume_ssr(ssr: dict) -> str:
    """Извлечь текст резюме из SSR JSON страницы резюме HH.ru."""
    resume: dict = {}
    for key in ("applicantResume", "resume", "resumeView"):
        val = ssr.get(key)
        if isinstance(val, dict) and val and not val.get("forbidden"):
            resume = val
            break
    if not resume:
        return ""

    parts = []

    # Name (optional context)
    first = _hh_ssr_str(resume.get("firstName"))
    last = _hh_ssr_str(resume.get("lastName"))
    full_name = " ".join(p for p in [first, last] if p)

    # Desired position — HH stores as [{"string": "..."}]
    title = _hh_ssr_str(resume.get("title"))
    # Also check professionalRole for named roles
    roles = resume.get("professionalRole") or []
    role_names = [r.get("text") for r in roles if isinstance(r, dict) and r.get("text")]
    position_line = title or ", ".join(role_names[:3])
    if position_line:
        parts.append(f"Желаемая должность: {position_line}")

    # Key skills — advancedKeySkills has name field; keySkills has {string: ...}
    adv_skills = resume.get("advancedKeySkills") or []
    if adv_skills and isinstance(adv_skills, list):
        skill_names = [s["name"] for s in adv_skills if isinstance(s, dict) and s.get("name")]
    else:
        raw_skills = resume.get("keySkills") or []
        skill_names = [_hh_ssr_str(s) for s in raw_skills if _hh_ssr_str(s)]
    if skill_names:
        parts.append(f"Ключевые навыки: {', '.join(skill_names[:30])}")

    # Experience
    experience = resume.get("experience") or []
    if experience and isinstance(experience, list):
        exp_parts = ["Опыт работы:"]
        for job in experience[:5]:
            if not isinstance(job, dict):
                continue
            company = str(job.get("companyName") or "")
            position = str(job.get("position") or "")
            description = str(job.get("description") or "")[:200]
            start = str(job.get("startDate") or "")[:7]   # "2024-01"
            end = str(job.get("endDate") or "")[:7] or "н.вр."
            period = f"{start}–{end}" if start else ""
            entry_parts = [p for p in [company, position] if p]
            if period:
                entry_parts.append(period)
            if description:
                entry_parts.append(f"({description})")
            if entry_parts:
                exp_parts.append("  " + " | ".join(entry_parts))
        if len(exp_parts) > 1:
            parts.append("\n".join(exp_parts))

    # Education
    edu_items = resume.get("primaryEducation") or resume.get("additionalEducation") or []
    if edu_items and isinstance(edu_items, list):
        edu_names = []
        for e in edu_items[:3]:
            if not isinstance(e, dict):
                continue
            uni = e.get("universityAcronym") or e.get("name") or ""
            org = e.get("organization") or ""
            result = e.get("result") or ""
            year = e.get("year") or ""
            if uni or result:
                edu_names.append(
                    f"{uni}" + (f" ({org})" if org and org != uni else "")
                    + (f" — {result}" if result else "")
                    + (f", {year}" if year else "")
                )
        if edu_names:
            parts.append(f"Образование: {'; '.join(edu_names)}")

    return "\n\n".join(parts)


def _parse_resume_html(html: str) -> str:
    """Извлечь текст резюме из HTML страницы HH.ru."""
    # Try SSR JSON first (cleaner structured data)
    ssr = parse_hh_lux_ssr(html)
    if ssr:
        result = _parse_resume_ssr(ssr)
        if result:
            return result

    # Fallback: BeautifulSoup HTML parsing
    soup = BeautifulSoup(html, "html.parser")
    parts = []

    # Desired position
    title_el = soup.find(attrs={"data-qa": "resume-block-title-position"})
    if title_el:
        parts.append(f"Желаемая должность: {title_el.get_text(strip=True)}")

    # Key skills
    skill_els = soup.find_all(attrs={"data-qa": "bloko-tag__text"})
    if not skill_els:
        skill_els = soup.find_all(attrs={"data-qa": "skills-element"})
    skills = [el.get_text(strip=True) for el in skill_els if el.get_text(strip=True)]
    if skills:
        parts.append(f"Ключевые навыки: {', '.join(skills[:30])}")

    # Experience
    exp_section = soup.find(attrs={"data-qa": "resume-block-experience"})
    if exp_section:
        exp_parts = ["Опыт работы:"]
        companies = exp_section.find_all(attrs={"data-qa": re.compile(
            r"resume-block-experience-company|resume-block-experience-name")})
        positions = exp_section.find_all(attrs={"data-qa": "resume-block-experience-position"})
        for i, company_el in enumerate(companies[:5]):
            co_text = company_el.get_text(strip=True)
            po_text = positions[i].get_text(strip=True) if i < len(positions) else ""
            entry = co_text + (f" — {po_text}" if po_text else "")
            exp_parts.append(f"  {entry}")
        if len(exp_parts) > 1:
            parts.append("\n".join(exp_parts))

    # About / additional
    for qa in ("resume-block-additional-resume", "resume-block-skills-content"):
        about_el = soup.find(attrs={"data-qa": qa})
        if about_el:
            about_text = about_el.get_text(" ", strip=True)[:500]
            if about_text:
                parts.append(f"О себе: {about_text}")
            break

    # Education
    edu_el = soup.find(attrs={"data-qa": "resume-block-education"})
    if edu_el:
        edu_text = edu_el.get_text(" ", strip=True)[:300]
        if edu_text:
            parts.append(f"Образование: {edu_text}")

    result = "\n\n".join(parts)
    # Last resort: strip all HTML, take leading 1500 chars of body text
    if not result:
        result = soup.get_text(" ", strip=True)[:1500]
    return result


def fetch_resume_text(acc: dict) -> str:
    """
    Получить текстовое представление резюме для LLM-контекста.
    Кэширует результат на 4 часа (_RESUME_CACHE_TTL).
    """
    resume_hash = acc.get("resume_hash", "")
    if not resume_hash:
        return ""

    now = time.time()
    with _resume_cache_lock:
        cached = _resume_cache.get(resume_hash)
        if cached:
            text, ts = cached
            if now - ts < _RESUME_CACHE_TTL:
                return text
        # Clean expired entries periodically (max 50 per call)
        expired = [k for k, (_, ts) in list(_resume_cache.items()) if now - ts >= _RESUME_CACHE_TTL]
        for k in expired:
            _resume_cache.pop(k, None)

    try:
        r = requests.get(
            f"https://hh.ru/resume/{resume_hash}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://hh.ru/applicant/resumes",
            },
            cookies=acc["cookies"],
            verify=False,
            timeout=15,
        )
        if r.status_code != 200:
            log_debug(f"fetch_resume_text: HTTP {r.status_code} для {resume_hash[:8]}")
            return ""
        text = _parse_resume_html(r.text)
        if text:
            with _resume_cache_lock:
                _resume_cache[resume_hash] = (text, now)
            log_debug(f"fetch_resume_text: ✅ {len(text)} симв. для {resume_hash[:8]}")
        else:
            log_debug(f"fetch_resume_text: ⚠️ пустой результат для {resume_hash[:8]}")
        return text
    except Exception as e:
        log_debug(f"fetch_resume_text error: {e}")
        return ""


def auto_decline_discards(acc: dict) -> int:
    """
    Авто-отклонение дискардов в переговорах.
    Возвращает количество отклонённых.
    """
    xsrf = acc.get("cookies", {}).get("_xsrf", "")
    if not xsrf:
        return 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://hh.ru/applicant/negotiations",
        "X-Xsrftoken": xsrf,
    }
    declined = 0
    try:
        # Собираем topic_id дискардов (первые 5 страниц)
        topic_ids = []
        for page in range(5):
            r = requests.get(
                f"https://hh.ru/applicant/negotiations?state=DISCARD&page={page}",
                headers=headers, cookies=acc["cookies"], verify=False, timeout=15
            )
            ssr = parse_hh_lux_ssr(r.text)
            topics = ssr.get("applicantNegotiations", {}).get("topicList", [])
            if not topics:
                break
            for topic in topics:
                actions = topic.get("actions", [])
                for action in actions:
                    if action.get("id") == "decline" or "decline" in action.get("url", ""):
                        topic_ids.append(str(topic.get("id", "")))
                        break
            if len(topics) < 10:
                break

        # Отклоняем
        post_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        for tid in topic_ids[:50]:  # не более 50 за раз
            try:
                r2 = requests.post(
                    "https://hh.ru/applicant/negotiations/decline",
                    headers=post_headers,
                    cookies=acc["cookies"],
                    data=f"topicId={tid}&_xsrf={xsrf}",
                    verify=False, timeout=10
                )
                if r2.status_code in (200, 302):
                    declined += 1
            except Exception:
                pass
    except Exception as e:
        log_debug(f"auto_decline_discards error: {e}")
    return declined


def fetch_hh_possible_offers(acc: dict) -> list:
    """Получить список компаний, готовых пригласить (JSON API)"""
    cookies = acc["cookies"]
    xsrf = cookies.get("_xsrf", "")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-XsrfToken": xsrf,
        "Accept": "application/json",
        "Referer": "https://hh.ru/applicant/negotiations",
    }
    try:
        resp = requests.get(
            "https://hh.ru/shards/applicant/negotiations/possible_job_offers",
            cookies=cookies,
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            offers = []
            for item in data if isinstance(data, list) else data.get("items", []):
                name = item.get("name", "")
                vacancy_names = [v.get("name", "") for v in item.get("vacancies", [])]
                offers.append({"name": name, "vacancyNames": vacancy_names})
            return offers
    except Exception as e:
        log_debug(f"fetch_hh_possible_offers error: {e}")
    return []


# ============================================================
# СОСТОЯНИЕ АККАУНТА
# ============================================================

class AccountState:
    """Полное состояние аккаунта"""

    def __init__(self, acc_data: dict):
        self.acc = acc_data
        self.name = acc_data["name"]
        self.short = acc_data["short"]
        self.color = acc_data["color"]

        self.status = "idle"
        self.status_detail = ""

        self.sent = 0
        self.tests = 0
        self.errors = 0
        self.already_applied = 0
        self.found_vacancies = 0

        self.current_phase = ""
        self.current_url = ""
        self.current_url_idx = 0
        self.total_urls = len(acc_data["urls"])
        self.current_page = 0
        self.total_pages = CONFIG.pages_per_url

        self.current_vacancy_id = ""
        self.current_vacancy_title = ""
        self.current_vacancy_company = ""
        self.current_vacancy_idx = 0
        self.total_vacancies = 0

        self.vacancies_by_url = {}
        self.vacancies_queue = []

        self.limit_exceeded = False
        self.limit_reset_time = None

        self.use_oauth = bool(acc_data.get("use_oauth", False))  # per-account OAuth toggle
        self.oauth_status = ""  # "active", "no_token", "error"

        self.daily_date = datetime.now().strftime("%Y-%m-%d")  # дата сброса счётчика
        # Count today's applies from persisted cache
        self.daily_sent = 0
        try:
            with _cache_lock:
                acc_applied = (_cache_applied or {}).get(self.name, {})
                today = self.daily_date
                for vid, info in acc_applied.items():
                    if isinstance(info, dict) and str(info.get("at", "")).startswith(today):
                        self.daily_sent += 1
        except Exception:
            pass
        self.hard_stopped = False  # жёсткая остановка (лимит или daily)

        self.resume_touch_enabled = True
        self.last_resume_touch = None
        self.next_resume_touch = None
        self.resume_touch_status = ""

        self.last_action_time = None
        self.cycle_start_time = None
        self.wait_until = None

        self.action_history = deque(maxlen=20)
        self.recent_responses = deque(maxlen=10)

        self.salary_skipped = 0       # Пропущено из-за зарплаты
        self.schedule_skipped = 0     # Пропущено из-за формата работы
        self.vacancy_salaries = {}    # {vid: salary_from_rub_or_None}
        self.vacancy_schedules = {}   # {vid: set_of_schedule_ids}
        self.vacancy_meta = {}        # {vid: {title, company}} из HTML поиска
        self.questionnaire_sent = 0   # Успешно пройдено опросов
        self.inconsistent_skipped = 0  # Пропущено из-за несовпадения опыта

        self.hh_interviews = 0
        self.hh_interviews_recent = 0  # за последние 60 дней
        self.hh_viewed = 0
        self.hh_not_viewed = 0
        self.hh_discards = 0
        self.hh_interviews_list = []
        self.hh_possible_offers = []
        self.hh_stats_updated = None
        self.hh_stats_loading = False
        self.hh_unread_by_employer = 0  # count of negotiations where employer hasn't read our messages

        # Resume statistics
        self.resume_views_7d = 0
        self.resume_views_new = 0
        self.resume_shows_7d = 0
        self.resume_invitations_7d = 0
        self.resume_invitations_new = 0
        self.resume_next_touch_seconds = 0
        self.resume_free_touches = 0
        self.resume_global_invitations = 0
        self.resume_new_invitations_total = 0

        # Resume view history
        self.resume_view_history: list = []   # [{employer_id, name, date, vacancy}]

        # Per-account pause (new for web dashboard)
        self.paused = False
        self._deleted = False  # Set True to stop worker thread

        # Per-account event log (last 8 events shown on card)
        self.acc_event_log: deque = deque(maxlen=8)
        # Skip vacancies that require tests
        self.apply_tests: bool = bool(acc_data.get("apply_tests", False))
        # Consecutive errors counter for auto-pause
        self.consecutive_errors: int = 0
        # Per-URL vacancy counts from last collection cycle
        self.url_stats: dict = {}
        # Per-account per-URL pages override {url: pages}
        # (overrides global pool pages for this account)
        # stored in acc["url_pages"]
        # Cookie expiry flag — set when 401/403 or login redirect detected
        self.cookies_expired: bool = False

        # LLM auto-reply tracking
        self.llm_replied_msgs: set = set()   # {(neg_id, last_msg_id)} successfully replied (permanent per session)
        self._llm_temp_skip: dict = {}       # {(neg_id, last_msg_id): expiry_ts} — transient failure, retry after TTL
        self._llm_no_chat: set = set()       # {neg_id} chats that returned 409 (permanently closed/locked)
        self.hh_interview_neg_ids: list = [] # negotiation IDs from last INTERVIEW fetch
        self.llm_enabled: bool = True        # per-account LLM toggle (overridden by global CONFIG.llm_enabled)
        self.llm_status: str = ""            # human-readable LLM status for dashboard display
        self.llm_replied_count: int = 0      # total replies sent this session
        self.llm_pending_chats: int = 0      # chats awaiting reply (from last scan)
        self._llm_lock = threading.Lock()    # prevents concurrent _process_llm_replies for this account
        self._msg_consecutive: dict = {}     # {neg_id: count} consecutive applicant messages without HR reply
        self._test_failures: dict = {}       # {vid: fail_count} questionnaire fill failures


# ============================================================
# WEBSOCKET CONNECTION MANAGER
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except TypeError as e:
                log_debug(f"broadcast serialize error (bug — check datetimes in snapshot): {e}")
                # Don't drop the WS on serialization errors — fix the data instead
            except Exception as e:
                log_debug(f"broadcast ws error: {type(e).__name__}: {e}")
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)


# ============================================================
# BOT MANAGER
# ============================================================

class BotManager:
    def __init__(self):
        self.paused = False
        self._stop_event = threading.Event()
        self.account_states: list[AccountState] = []
        self.activity_log: deque = deque(maxlen=100)
        self.recent_responses: deque = deque(maxlen=100)
        self.llm_log: deque = deque(maxlen=200)    # LLM reply history
        self.vacancy_queues: dict = {}
        self._start_time: datetime = None
        self.temp_sessions: list = load_browser_sessions()  # сессии из браузера (персистентные)
        self.temp_states: dict[int, AccountState] = {}  # temp_idx → AccountState для активных сессий
        # Global dedup across all accounts: {(cur_pid, neg_id, last_msg_id)}
        # Prevents double-sends when multiple accounts share the same HH user (same cur_pid)
        self._llm_sent_global: set = set()
        self._llm_sent_lock = threading.Lock()
        # HR contacts collected from contactInfo during pre-checks
        self.hr_contacts: list = []  # capped at 500
        self._hr_contacts_lock = threading.Lock()

    def _build_session_urls(self, resume_hash: str) -> list[str]:
        """URL поиска для браузерной сессии: resume-URL + keyword-URLs из глобального пула."""
        resume_url = f"https://hh.ru/search/vacancy?resume={resume_hash}&order_by=publication_time&items_on_page=20"
        urls = [resume_url]
        for item in CONFIG.url_pool:
            entry = _url_entry(item)
            if entry["url"] and "resume=" not in entry["url"]:
                urls.append(entry["url"])
        # Добавляем resume-URL в пул, если ещё нет
        pool_urls = [_url_entry(u)["url"] for u in CONFIG.url_pool]
        if resume_url not in pool_urls:
            CONFIG.url_pool.append({"url": resume_url, "pages": CONFIG.pages_per_url})
            save_config()
        return urls

    def activate_session(self, temp_idx: int) -> bool:
        """Запустить браузерную сессию как полноценный бот-аккаунт."""
        if temp_idx < 0 or temp_idx >= len(self.temp_sessions):
            return False
        ts = self.temp_sessions[temp_idx]
        if not ts.get("resume_hash"):
            return False
        if temp_idx in self.temp_states:
            return True  # уже запущен
        acc = {
            "name": ts["name"],
            "short": ts.get("short", ts["name"]),
            "color": "yellow",
            "resume_hash": ts["resume_hash"],
            "letter": ts.get("letter", ""),
            "cookies": ts.get("cookies", {}),
            "urls": self._build_session_urls(ts["resume_hash"]),
        }
        state = AccountState(acc)
        self.temp_states[temp_idx] = state
        ts["bot_active"] = True
        save_browser_sessions(self.temp_sessions)
        log_debug(f"activate_session({temp_idx}): starting threads...")
        t1 = threading.Thread(target=self._run_account_worker, args=(900 + temp_idx, state), daemon=True, name=f"worker-{temp_idx}")
        t2 = threading.Thread(target=self._fetch_hh_stats_worker, args=(900 + temp_idx, state), daemon=True, name=f"stats-{temp_idx}")
        t1.start()
        t2.start()
        log_debug(f"activate_session({temp_idx}): threads started t1={t1.is_alive()} t2={t2.is_alive()}")
        self._add_log(state.short, "yellow", f"🌐 Сессия {ts['name']} запущена как бот", "success")
        return True

    def _get_apply_acc(self, idx: int) -> dict | None:
        """Вернуть acc dict для apply-эндпоинтов (обычный или временный аккаунт)"""
        if 0 <= idx < len(self.account_states):
            return dict(self.account_states[idx].acc)
        temp_idx = idx - len(self.account_states)
        if 0 <= temp_idx < len(self.temp_sessions):
            return dict(self.temp_sessions[temp_idx])
        return None

    def _get_apply_state(self, idx: int):
        """Вернуть AccountState или None для temp-сессий"""
        if 0 <= idx < len(self.account_states):
            return self.account_states[idx]
        return None

    def start(self):
        _load_cache()
        load_config()
        self._start_time = datetime.now()
        # Load recent responses from applied_vacancies into deque
        try:
            with _cache_lock:
                if _cache_applied:
                    all_items = []
                    for acc_name, vacancies in _cache_applied.items():
                        if isinstance(vacancies, dict):
                            for vid, info in vacancies.items():
                                if isinstance(info, dict):
                                    all_items.append({
                                        "id": vid, "title": info.get("title", ""),
                                        "company": info.get("company", ""),
                                        "time": (info.get("at", "") or "")[:16].replace("T", " "),
                                        "icon": "✅", "acc": acc_name,
                                    })
                    # Sort by time, take last 100
                    all_items.sort(key=lambda x: x.get("time", ""), reverse=True)
                    for item in all_items[:100]:
                        self.recent_responses.append(item)
                    log_debug(f"Loaded {len(self.recent_responses)} recent responses from cache")
        except Exception as e:
            log_debug(f"Failed to load recent responses: {e}")
        self.account_states = [AccountState(acc) for acc in accounts_data]
        for i, state in enumerate(self.account_states):
            t1 = threading.Thread(
                target=self._run_account_worker, args=(i, state), daemon=True
            )
            t2 = threading.Thread(
                target=self._fetch_hh_stats_worker, args=(i, state), daemon=True
            )
            t1.start()
            t2.start()
        # Авто-активация браузерных сессий, которые были запущены до перезапуска
        log_debug(f"start(): {len(self.temp_sessions)} temp sessions to check")
        for i, ts in enumerate(self.temp_sessions):
            log_debug(f"start(): session {i}: bot_active={ts.get('bot_active')}, resume_hash={bool(ts.get('resume_hash'))}")
            if ts.get("bot_active") and ts.get("resume_hash"):
                ts["paused"] = False  # Reset pause on startup
                try:
                    result = self.activate_session(i)
                    log_debug(f"start(): activate_session({i}) = {result}")
                except Exception as e:
                    log_debug(f"start(): activate_session({i}) ERROR: {e}")
        self._add_log("", "", "🚀 Бот запущен", "success")

    def stop(self):
        self._stop_event.set()

    def toggle_pause(self):
        self.paused = not self.paused
        msg = "⏸️ Пауза" if self.paused else "▶️ Продолжение"
        level = "warning" if self.paused else "success"
        self._add_log("", "", msg, level)

    def toggle_account_pause(self, idx: int):
        state = None
        if 0 <= idx < len(self.account_states):
            state = self.account_states[idx]
        else:
            temp_idx = idx - len(self.account_states)
            state = self.temp_states.get(temp_idx)
        if state:
            state.paused = not state.paused
            if not state.paused:
                # Reset hard stop / limit so worker can continue
                state.hard_stopped = False
                state.limit_exceeded = False
                state.limit_reset_time = None
            msg = (
                f"⏸️ Аккаунт {state.short} приостановлен"
                if state.paused
                else f"▶️ Аккаунт {state.short} возобновлён"
            )
            self._add_log(state.short, state.color, msg, "warning" if state.paused else "success")

    def toggle_account_llm(self, idx: int):
        state = None
        if 0 <= idx < len(self.account_states):
            state = self.account_states[idx]
        else:
            temp_idx = idx - len(self.account_states)
            state = self.temp_states.get(temp_idx)
        if state:
            state.llm_enabled = not state.llm_enabled
            msg = (
                f"🤖 LLM включён для {state.short}"
                if state.llm_enabled
                else f"🤖 LLM выключен для {state.short}"
            )
            self._add_log(state.short, state.color, msg, "info")

    def toggle_account_oauth(self, idx: int):
        state = None
        if 0 <= idx < len(self.account_states):
            state = self.account_states[idx]
        else:
            temp_idx = idx - len(self.account_states)
            state = self.temp_states.get(temp_idx)
        if state:
            state.use_oauth = not state.use_oauth
            mode = "🔑 OAuth" if state.use_oauth else "🌐 Web"
            self._add_log(state.short, state.color, f"{mode} откликов для {state.short}", "info")
            # Persist to account data
            state.acc["use_oauth"] = state.use_oauth
            if 0 <= idx < len(accounts_data):
                accounts_data[idx]["use_oauth"] = state.use_oauth
                save_accounts()
            else:
                temp_idx = idx - len(self.account_states)
                if 0 <= temp_idx < len(self.temp_sessions):
                    self.temp_sessions[temp_idx]["use_oauth"] = state.use_oauth
                    save_browser_sessions(self.temp_sessions)

    def trigger_resume_touch(self, idx: int):
        if 0 <= idx < len(self.account_states):
            self.account_states[idx].next_resume_touch = datetime.now()
        else:
            temp_idx = idx - len(self.account_states)
            if temp_idx in self.temp_states:
                self.temp_states[temp_idx].next_resume_touch = datetime.now()

    def toggle_resume_touch(self, idx: int) -> bool:
        state = None
        if 0 <= idx < len(self.account_states):
            state = self.account_states[idx]
        else:
            temp_idx = idx - len(self.account_states)
            if temp_idx in self.temp_states:
                state = self.temp_states[temp_idx]
        if state:
            state.resume_touch_enabled = not state.resume_touch_enabled
            return state.resume_touch_enabled
        return False

    def _add_log(self, acc_short: str, acc_color: str, message: str, level: str = "info", neg_id: str = ""):
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "acc": acc_short,
            "color": acc_color,
            "message": message,
            "level": level,
        }
        if neg_id:
            entry["neg_id"] = str(neg_id)
        self.activity_log.appendleft(entry)

    def _add_acc_event(self, state: AccountState, icon: str, etype: str,
                        title: str, company: str, extra: str = ""):
        state.acc_event_log.appendleft({
            "time": datetime.now().strftime("%H:%M"),
            "icon": icon,
            "type": etype,
            "title": title[:45],
            "company": company[:25],
            "extra": extra[:70],
        })

    def _check_auto_pause(self, state: AccountState):
        """Авто-пауза при превышении лимита ошибок подряд."""
        n = CONFIG.auto_pause_errors
        if n > 0 and state.consecutive_errors >= n:
            state.paused = True
            self._add_log(
                state.short, state.color,
                f"⛔ Авто-пауза: {n} ошибок подряд. Снимите вручную.",
                "error",
            )

    def _add_response(
        self,
        state: AccountState,
        vid: str,
        title: str,
        company: str,
        result: str,
        salary: str = "",
    ):
        result_icons = {
            "sent": "✅",
            "test": "🧪",
            "already": "🔄",
            "limit": "🚫",
            "error": "❌",
        }
        self.recent_responses.appendleft({
            "time": datetime.now().strftime("%H:%M:%S"),
            "acc": state.short,
            "color": state.color,
            "id": vid,
            "title": title,
            "company": company,
            "salary": salary,
            "result": result,
            "icon": result_icons.get(result, "❓"),
        })

    def get_state_snapshot(self) -> dict:
        """Full JSON snapshot for WS broadcast"""
        now = datetime.now()
        uptime = int((now - self._start_time).total_seconds()) if self._start_time else 0

        # All states: regular + temp sessions (for global_stats, vacancy_queues)
        all_states = list(self.account_states) + list(self.temp_states.values())

        accounts = []
        for i, s in enumerate(self.account_states):
            next_touch_str = ""
            if s.next_resume_touch:
                rem = (s.next_resume_touch - now).total_seconds()
                if rem > 0:
                    h = int(rem // 3600)
                    m = int((rem % 3600) // 60)
                    next_touch_str = f"{s.next_resume_touch.strftime('%H:%M')} ({h}ч{m}м)"
                else:
                    next_touch_str = "сейчас!"

            hh_updated_str = ""
            if s.hh_stats_updated:
                ago = int((now - s.hh_stats_updated).total_seconds() / 60)
                hh_updated_str = (
                    f"{ago}м назад" if ago < 60 else f"{ago // 60}ч{ago % 60}м назад"
                )

            accounts.append({
                "idx": i,
                "name": s.name,
                "short": s.short,
                "color": s.color,
                "status": s.status,
                "status_detail": s.status_detail,
                "sent": s.sent,
                "total_applied": len((_cache_applied or {}).get(s.name, {})),
                "tests": s.tests,
                "errors": s.errors,
                "already_applied": s.already_applied,
                "found_vacancies": s.found_vacancies,
                "current_vacancy_title": s.current_vacancy_title,
                "current_vacancy_company": s.current_vacancy_company,
                "current_vacancy_idx": s.current_vacancy_idx,
                "total_vacancies": s.total_vacancies,
                "salary_skipped": s.salary_skipped,
                "questionnaire_sent": s.questionnaire_sent,
                "limit_exceeded": s.limit_exceeded,
                "paused": s.paused,
                "next_resume_touch": next_touch_str,
                "resume_touch_status": s.resume_touch_status,
                "resume_touch_enabled": s.resume_touch_enabled,
                "letter": s.acc.get("letter", ""),
                "urls": s.acc.get("urls", []),
                "url_pages": s.acc.get("url_pages", {}),
                "hh_interviews": s.hh_interviews,
                "hh_interviews_recent": s.hh_interviews_recent,
                "hh_viewed": s.hh_viewed,
                "hh_discards": s.hh_discards,
                "hh_not_viewed": s.hh_not_viewed,
                "hh_unread_by_employer": s.hh_unread_by_employer,
                "hh_stats_updated": hh_updated_str,
                "hh_stats_loading": s.hh_stats_loading,
                "hh_interviews_list": s.hh_interviews_list[:20],
                "hh_possible_offers": s.hh_possible_offers[:10],
                "action_history": list(s.action_history),
                "resume_views_7d": s.resume_views_7d,
                "resume_views_new": s.resume_views_new,
                "resume_shows_7d": s.resume_shows_7d,
                "resume_invitations_7d": s.resume_invitations_7d,
                "resume_invitations_new": s.resume_invitations_new,
                "resume_next_touch_seconds": s.resume_next_touch_seconds,
                "resume_free_touches": s.resume_free_touches,
                "resume_global_invitations": s.resume_global_invitations,
                "resume_new_invitations_total": s.resume_new_invitations_total,
                "acc_event_log": list(s.acc_event_log),
                "apply_tests": s.apply_tests,
                "consecutive_errors": s.consecutive_errors,
                "url_stats": dict(s.url_stats),
                "cookies_expired": s.cookies_expired,
                "llm_enabled": s.llm_enabled,
                "llm_status": s.llm_status,
                "llm_replied_count": s.llm_replied_count,
                "llm_pending_chats": s.llm_pending_chats,
                "use_oauth": s.use_oauth,
                "daily_sent": s.daily_sent,
                "daily_limit": CONFIG.daily_apply_limit,
                "hard_stopped": s.hard_stopped,
            })

        # Temp browser sessions — append after regular accounts
        base_idx = len(self.account_states)
        for i, ts in enumerate(self.temp_sessions):
            idx = base_idx + i
            state = self.temp_states.get(i)
            if state:
                # Активная сессия — реальные данные из AccountState
                s = state
                nrt = s.next_resume_touch.strftime("%H:%M") if s.next_resume_touch else ""
                ts_hh_updated_str = ""
                if s.hh_stats_updated:
                    ago = int((now - s.hh_stats_updated).total_seconds() / 60)
                    ts_hh_updated_str = (
                        f"{ago}м назад" if ago < 60 else f"{ago // 60}ч{ago % 60}м назад"
                    )
                accounts.append({
                    "idx": idx,
                    "name": s.acc["name"],
                    "short": s.acc.get("short", ""),
                    "color": "yellow",
                    "temp": True,
                    "bot_active": True,
                    "resume_hash": s.acc.get("resume_hash", ""),
                    "letter": s.acc.get("letter", ""),
                    "urls": s.acc.get("urls", []),
                    "url_pages": s.acc.get("url_pages", {}),
                    "status": s.status,
                    "status_detail": s.status_detail,
                    "sent": s.sent,
                    "total_applied": len((_cache_applied or {}).get(s.acc["name"], {})),
                    "tests": s.tests,
                    "errors": s.errors,
                    "already_applied": s.already_applied,
                    "found_vacancies": s.found_vacancies,
                    "current_vacancy_title": s.current_vacancy_title,
                    "current_vacancy_company": s.current_vacancy_company,
                    "current_vacancy_idx": s.current_vacancy_idx,
                    "total_vacancies": s.total_vacancies,
                    "salary_skipped": s.salary_skipped,
                    "questionnaire_sent": s.questionnaire_sent,
                    "limit_exceeded": s.limit_exceeded,
                    "paused": s.paused,
                    "next_resume_touch": nrt,
                    "resume_touch_status": s.resume_touch_status,
                    "resume_touch_enabled": s.resume_touch_enabled,
                    "hh_interviews": s.hh_interviews,
                    "hh_interviews_recent": s.hh_interviews_recent,
                    "hh_viewed": s.hh_viewed,
                    "hh_discards": s.hh_discards,
                    "hh_not_viewed": s.hh_not_viewed,
                    "hh_unread_by_employer": s.hh_unread_by_employer,
                    "hh_stats_updated": ts_hh_updated_str,
                    "hh_stats_loading": s.hh_stats_loading,
                    "hh_interviews_list": s.hh_interviews_list[:20],
                    "hh_possible_offers": s.hh_possible_offers[:10],
                    "action_history": list(s.action_history),
                    "resume_views_7d": s.resume_views_7d,
                    "resume_views_new": s.resume_views_new,
                    "resume_shows_7d": s.resume_shows_7d,
                    "resume_invitations_7d": s.resume_invitations_7d,
                    "resume_invitations_new": s.resume_invitations_new,
                    "resume_next_touch_seconds": s.resume_next_touch_seconds,
                    "resume_free_touches": s.resume_free_touches,
                    "resume_global_invitations": s.resume_global_invitations,
                    "resume_new_invitations_total": s.resume_new_invitations_total,
                    "acc_event_log": list(s.acc_event_log),
                    "apply_tests": s.apply_tests,
                    "consecutive_errors": s.consecutive_errors,
                    "url_stats": dict(s.url_stats),
                    "cookies_expired": s.cookies_expired,
                    "llm_enabled": s.llm_enabled,
                    "use_oauth": s.use_oauth,
                    "daily_sent": s.daily_sent,
                    "daily_limit": CONFIG.daily_apply_limit,
                    "hard_stopped": s.hard_stopped,
                })
            else:
                # Неактивная сессия — заглушка
                accounts.append({
                    "idx": idx,
                    "name": ts.get("name", f"Браузер #{i+1}"),
                    "short": ts.get("short", f"Браузер#{i+1}"),
                    "color": "yellow",
                    "temp": True,
                    "bot_active": False,
                    "resume_hash": ts.get("resume_hash", ""),
                    "all_resumes": ts.get("all_resumes", []),
                    "letter": ts.get("letter", ""),
                    "status": "—", "status_detail": "", "sent": 0, "tests": 0,
                    "errors": 0, "already_applied": 0, "found_vacancies": 0,
                    "current_vacancy_title": "", "current_vacancy_company": "",
                    "current_vacancy_idx": 0, "total_vacancies": 0,
                    "salary_skipped": 0, "questionnaire_sent": 0,
                    "limit_exceeded": False, "paused": False,
                    "next_resume_touch": "", "resume_touch_status": "",
                    "hh_interviews": 0, "hh_viewed": 0, "hh_discards": 0,
                    "hh_not_viewed": 0, "hh_unread_by_employer": 0,
                    "hh_stats_updated": "", "hh_stats_loading": False,
                    "hh_interviews_list": [], "hh_possible_offers": [], "action_history": [],
                    "resume_views_7d": 0, "resume_views_new": 0, "resume_shows_7d": 0,
                    "resume_invitations_7d": 0, "resume_invitations_new": 0,
                    "resume_next_touch_seconds": 0, "resume_free_touches": 0,
                    "resume_global_invitations": 0, "resume_new_invitations_total": 0,
                    "acc_event_log": [],
                    "apply_tests": bool(ts.get("apply_tests", False)),
                    "consecutive_errors": 0,
                    "url_stats": {},
                    "cookies_expired": False,
                    "llm_enabled": True,
                    "use_oauth": bool(ts.get("use_oauth", False)),
                    "daily_sent": 0,
                    "daily_limit": CONFIG.daily_apply_limit,
                    "hard_stopped": False,
                })

        storage_stats = get_stats()

        return {
            "type": "state_update",
            "uptime_seconds": uptime,
            "paused": self.paused,
            "accounts": accounts,
            "recent_responses": list(self.recent_responses),
            "log": list(self.activity_log),
            "llm_log": list(self.llm_log),
            "config": {
                "pages_per_url": CONFIG.pages_per_url,
                "response_delay": CONFIG.response_delay,
                "pause_between_cycles": CONFIG.pause_between_cycles,
                "batch_responses": CONFIG.batch_responses,
                "limit_check_interval": CONFIG.limit_check_interval,
                "min_salary": CONFIG.min_salary,
                "auto_pause_errors": CONFIG.auto_pause_errors,
                "auto_apply_tests": CONFIG.auto_apply_tests,
                "use_oauth_apply": CONFIG.use_oauth_apply,
                "daily_apply_limit": CONFIG.daily_apply_limit,
                "stop_on_hh_limit": CONFIG.stop_on_hh_limit,
                "llm_check_interval": CONFIG.llm_check_interval,
                "allowed_schedules": CONFIG.allowed_schedules,
                "questionnaire_templates": CONFIG.questionnaire_templates,
                "questionnaire_default_answer": CONFIG.questionnaire_default_answer,
                "letter_templates": CONFIG.letter_templates,
                "url_pool": CONFIG.url_pool,
                "skip_inconsistent": CONFIG.skip_inconsistent,
                "filter_agencies": CONFIG.filter_agencies,
                "filter_low_competition": CONFIG.filter_low_competition,
                "search_period_days": CONFIG.search_period_days,
                "llm_enabled": CONFIG.llm_enabled,
                "llm_auto_send": CONFIG.llm_auto_send,
                "llm_fill_questionnaire": CONFIG.llm_fill_questionnaire,
                "llm_use_cover_letter": CONFIG.llm_use_cover_letter,
                "llm_use_resume": CONFIG.llm_use_resume,
                "llm_model": CONFIG.llm_model,
                "llm_base_url": CONFIG.llm_base_url,
                # Note: don't include llm_api_key in snapshot for security
                "llm_profiles": [
                    {"name": p.get("name", ""), "base_url": p.get("base_url", ""),
                     "model": p.get("model", ""), "enabled": p.get("enabled", True)}
                    for p in (CONFIG.llm_profiles or [])
                ],
                "llm_profile_mode": CONFIG.llm_profile_mode,
            },
            "global_stats": {
                "total_sent": sum(s.sent for s in all_states),
                "total_tests": sum(s.tests for s in all_states),
                "total_errors": sum(s.errors for s in all_states),
                "total_found": sum(s.found_vacancies for s in all_states),
                "storage_total": storage_stats["total"],
                "storage_tests": storage_stats["tests"],
            },
            "vacancy_queues": {
                s.short: {
                    "remaining": max(0, len(s.vacancies_queue) - s.current_vacancy_idx),
                    "next": s.vacancies_queue[s.current_vacancy_idx: s.current_vacancy_idx + 5]
                    if s.vacancies_queue
                    else [],
                }
                for s in all_states
            },
        }

    def _run_account_worker(self, idx: int, state: AccountState) -> None:
        """Thread worker for an account — auto-restarts on crash"""
        while not self._stop_event.is_set() and not getattr(state, '_deleted', False):
            try:
                self._run_account_worker_inner(idx, state)
                break  # normal exit
            except Exception as e:
                log_debug(f"WORKER CRASHED [{state.short}]: {e}")
                import traceback
                log_debug(traceback.format_exc())
                state.status = "error"
                state.status_detail = f"Перезапуск через 30с ({str(e)[:30]})"
                self._add_log(state.short, state.color, f"⚠️ Worker упал: {str(e)[:50]}. Перезапуск через 30с", "error")
                time.sleep(30)
                state.status = "idle"
                state.status_detail = "Перезапущен после ошибки"
                self._add_log(state.short, state.color, "🔄 Worker перезапущен", "info")

    def _run_account_worker_inner(self, idx: int, state: AccountState) -> None:
        acc = state.acc

        while not self._stop_event.is_set() and not state._deleted:
            # Global + per-account pause
            while (self.paused or state.paused) and not self._stop_event.is_set() and not state._deleted:
                # Auto-reset daily limit pause when new day starts
                if state.hard_stopped:
                    today = datetime.now().strftime("%Y-%m-%d")
                    if state.daily_date != today:
                        state.daily_sent = 0
                        state.daily_date = today
                        state.hard_stopped = False
                        state.paused = False
                        state.limit_exceeded = False
                        state.limit_reset_time = None
                        state.status = "idle"
                        state.status_detail = "Новый день — лимит сброшен"
                        self._add_log(state.short, state.color,
                            "🌅 Новый день! Лимит сброшен, продолжаю работу", "success")
                        break
                if state.hard_stopped:
                    state.status = "limit"
                    if CONFIG.daily_apply_limit > 0 and state.daily_sent >= CONFIG.daily_apply_limit:
                        state.status_detail = f"Дневной лимит: {state.daily_sent}/{CONFIG.daily_apply_limit}. Сброс завтра в 00:00"
                    else:
                        state.status_detail = "Лимит HH. Сброс завтра в 00:00"
                elif state.limit_exceeded:
                    state.status = "limit"
                    if state.limit_reset_time:
                        remaining = int((state.limit_reset_time - datetime.now()).total_seconds())
                        if remaining > 0:
                            state.status_detail = f"Лимит HH. Проверка через {remaining // 60}м{remaining % 60:02d}с"
                        else:
                            state.status_detail = "Лимит HH. Проверка сейчас..."
                    else:
                        state.status_detail = "Лимит HH. Проверка через 1м"
                else:
                    state.status = "idle"
                    state.status_detail = "Пауза пользователем"
                time.sleep(1)

            if self._stop_event.is_set():
                break

            now = datetime.now()

            # === АВТОПОДНЯТИЕ РЕЗЮМЕ ===
            if state.resume_touch_enabled:
                should_touch = False
                if state.next_resume_touch is None:
                    should_touch = True
                elif now >= state.next_resume_touch:
                    should_touch = True

                if should_touch:
                    self._add_log(state.short, state.color, "📤 Поднимаю резюме...", "info")
                    success, message = touch_resume(acc)

                    if success:
                        state.resume_touch_status = "✅ Поднято!"
                        state.last_resume_touch = now
                        state.next_resume_touch = now + timedelta(hours=4)
                        self._add_log(
                            state.short, state.color,
                            f"✅ Резюме поднято! Следующее в {state.next_resume_touch.strftime('%H:%M')}",
                            "success",
                        )
                    else:
                        state.resume_touch_status = f"⏳ {message}"
                        state.next_resume_touch = now + timedelta(hours=4)
                        self._add_log(
                            state.short, state.color,
                            f"📤 {message}. Повтор в {state.next_resume_touch.strftime('%H:%M')}",
                            "warning",
                        )

            # === ПРОВЕРКА ЛИМИТА ===
            if state.limit_exceeded:
                # If no reset time set, schedule a check soon
                if not state.limit_reset_time:
                    state.limit_reset_time = now + timedelta(minutes=1)

                if now >= state.limit_reset_time:
                    state.status = "checking"
                    state.status_detail = "Проверка сброса лимита..."
                    self._add_log(state.short, state.color, "🔍 Проверяю сброс лимита...", "info")

                    if not check_limit(acc):
                        state.limit_exceeded = False
                        state.limit_reset_time = None
                        state.paused = False
                        state.hard_stopped = False
                        state.status_detail = ""
                        self._add_log(
                            state.short, state.color, "✅ Лимит сброшен! Продолжаю работу", "success"
                        )
                    else:
                        state.limit_reset_time = now + timedelta(minutes=CONFIG.limit_check_interval)
                        state.status = "limit"
                        state.status_detail = f"Проверка в {state.limit_reset_time.strftime('%H:%M')}"
                        self._add_log(
                            state.short, state.color,
                            f"⏳ Лимит ещё активен, попробую в {state.limit_reset_time.strftime('%H:%M')}",
                            "warning",
                        )
                        time.sleep(60)
                        continue
                else:
                    state.status = "limit"
                    remaining = int((state.limit_reset_time - now).total_seconds())
                    state.status_detail = f"Проверка через {remaining}с"
                    time.sleep(30)
                    continue

            # === СБОР ВАКАНСИЙ (ПАРАЛЛЕЛЬНО) ===
            # Если у аккаунта нет своих URL — используем глобальный пул
            effective_urls = acc.get("urls") or [_url_entry(u)["url"] for u in CONFIG.url_pool]
            state.total_urls = len(effective_urls)

            state.status = "collecting"
            state.status_detail = "Начинаю параллельный сбор..."
            state.cycle_start_time = now
            state.vacancies_by_url = {}
            state.vacancy_meta = {}  # Сброс метаданных вакансий для нового цикла

            self._add_log(
                state.short, state.color,
                f"📥 Параллельный сбор: {len(effective_urls)} URL × {CONFIG.pages_per_url} стр",
                "info",
            )

            try:
                results_by_url, salary_map, schedule_map = asyncio.run(self._collect_all_urls_parallel(state))
            except Exception as e:
                log_debug(f"COLLECT CRASH [{state.short}]: {e}")
                import traceback
                log_debug(traceback.format_exc())
                state.status = "error"
                state.status_detail = f"Ошибка сбора: {str(e)[:50]}"
                time.sleep(60)
                continue
            state.vacancy_salaries = salary_map
            state.vacancy_schedules = schedule_map

            all_vacancies = []
            for url in effective_urls:
                url_vacancies = results_by_url.get(url, set())
                state.vacancies_by_url[url] = len(url_vacancies)
                all_vacancies.extend(url_vacancies)

                query = extract_search_query(url)
                if url_vacancies:
                    self._add_log(state.short, state.color, f"📊 {query}: {len(url_vacancies)}", "info")
            # Сохраняем статистику по URL для снапшота
            state.url_stats = dict(state.vacancies_by_url)

            unique_vacancies = set(all_vacancies)
            total_collected = len(unique_vacancies)

            self._add_log(
                state.short, state.color,
                f"📊 Всего собрано: {len(all_vacancies)} ({total_collected} уникальных)",
                "info",
            )

            if not unique_vacancies:
                state.status = "waiting"
                state.status_detail = "Нет вакансий"
                state.wait_until = now + timedelta(minutes=2)
                self._add_log(
                    state.short, state.color,
                    "⚠️ Не найдено ни одной вакансии, пауза 2 мин",
                    "warning",
                )
                time.sleep(120)
                continue

            # Фильтрация
            filtered = []
            already_count = 0
            test_count = 0
            salary_skipped = 0
            schedule_skipped = 0
            apply_tests = state.apply_tests or CONFIG.auto_apply_tests

            for vid in unique_vacancies:
                if is_applied(acc["name"], vid):
                    already_count += 1
                    state.already_applied += 1
                elif (is_test(vid) or state._test_failures.get(vid, 0) >= 2) and not apply_tests:
                    test_count += 1
                    state.tests += 1
                elif CONFIG.allowed_schedules:
                    sched = schedule_map.get(vid, set())
                    if sched and not sched.intersection(CONFIG.allowed_schedules):
                        schedule_skipped += 1
                        state.schedule_skipped += 1
                    elif CONFIG.min_salary > 0:
                        sal = salary_map.get(vid)
                        if sal is None or sal < CONFIG.min_salary:
                            salary_skipped += 1
                            state.salary_skipped += 1
                        else:
                            filtered.append(vid)
                    else:
                        filtered.append(vid)
                elif CONFIG.min_salary > 0:
                    sal = salary_map.get(vid)
                    if sal is None or sal < CONFIG.min_salary:
                        salary_skipped += 1
                        state.salary_skipped += 1
                    else:
                        filtered.append(vid)
                else:
                    filtered.append(vid)

            sal_msg = f", 💰 зарплата {salary_skipped}" if CONFIG.min_salary > 0 else ""
            sched_msg = f", 🏢 формат {schedule_skipped}" if CONFIG.allowed_schedules else ""
            self._add_log(
                state.short, state.color,
                f"🔍 Фильтрация: ✅ уже {already_count}, 🧪 тест {test_count}{sal_msg}{sched_msg}, 🆕 новые {len(filtered)}",
                "info",
            )

            if not filtered:
                state.status = "waiting"
                state.status_detail = "Нет новых вакансий"
                state.wait_until = now + timedelta(minutes=2)
                self._add_log(
                    state.short, state.color,
                    f"⚠️ Все вакансии уже обработаны ({already_count} откликов, {test_count} тестов), пауза 2 мин",
                    "warning",
                )
                time.sleep(120)
                continue

            random.shuffle(filtered)

            # Hot leads priority: fetch possible_job_offers and put matching vacancies first
            try:
                r_offers = requests.get(
                    "https://hh.ru/shards/applicant/negotiations/possible_job_offers",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/json",
                        "X-Xsrftoken": acc.get("cookies", {}).get("_xsrf", ""),
                        "Referer": "https://hh.ru/applicant/negotiations",
                    },
                    cookies=acc.get("cookies", {}), verify=False, timeout=10,
                )
                if r_offers.status_code == 200:
                    offers_data = r_offers.json()
                    offer_items = offers_data if isinstance(offers_data, list) else offers_data.get("possibleJobOffers", [])
                    offer_vids = set()
                    for o in offer_items:
                        vid_val = o.get("vacancyId", "")
                        if vid_val:
                            offer_vids.add(str(vid_val))
                    if offer_vids:
                        hot = [v for v in filtered if v in offer_vids]
                        cold = [v for v in filtered if v not in offer_vids]
                        filtered = hot + cold
                        if hot:
                            self._add_log(state.short, state.color,
                                f"🔥 {len(hot)} горячих лидов в начале очереди", "success")
            except Exception:
                pass

            state.vacancies_queue = filtered
            state.total_vacancies = len(filtered)
            state.found_vacancies += len(all_vacancies)

            self._add_log(
                state.short, state.color,
                f"✅ Найдено {len(filtered)} новых вакансий для отклика!",
                "success",
            )
            self.vacancy_queues[state.short] = {
                "vacancies": filtered,
                "current": 0,
                "color": state.color,
            }

            # === ОТПРАВКА ОТКЛИКОВ (ПАКЕТАМИ) ===
            state.status = "applying"
            state.status_detail = f"0/{state.total_vacancies}"

            batch_size = CONFIG.batch_responses
            i = 0

            while i < len(filtered):
                if self._stop_event.is_set() or self.paused or state.paused or state.limit_exceeded:
                    break

                batch = filtered[i: i + batch_size]
                state.current_vacancy_idx = i + 1
                state.status_detail = (
                    f"{i + 1}-{min(i + batch_size, len(filtered))}/{state.total_vacancies}"
                )

                if state.short in self.vacancy_queues:
                    self.vacancy_queues[state.short]["current"] = i

                # Daily limit check
                today = datetime.now().strftime("%Y-%m-%d")
                if state.daily_date != today:
                    state.daily_sent = 0
                    state.daily_date = today
                    state.hard_stopped = False
                    # Cleanup unbounded dicts on new day
                    if len(state._test_failures) > 500:
                        state._test_failures.clear()
                    if len(state._msg_consecutive) > 500:
                        state._msg_consecutive.clear()
                if CONFIG.daily_apply_limit > 0 and state.daily_sent >= CONFIG.daily_apply_limit:
                    state.hard_stopped = True
                    state.paused = True
                    state.status = "limit"
                    state.status_detail = f"Дневной лимит: {state.daily_sent}/{CONFIG.daily_apply_limit}. Сброс завтра в 00:00"
                    self._add_log(state.short, state.color,
                        f"🛑 Дневной лимит {CONFIG.daily_apply_limit} откликов. Пауза до завтра 00:00.", "error")
                    break

                # Pre-check: skip inconsistent vacancies if enabled
                if CONFIG.skip_inconsistent:
                    checked_batch = []
                    for vid in batch:
                        precheck = _check_vacancy_before_apply(acc, vid)
                        if not precheck["ok"]:
                            meta = state.vacancy_meta.get(vid, {})
                            display_title = (meta.get("title") or vid)[:40]
                            state.inconsistent_skipped += 1
                            self._add_log(state.short, state.color,
                                f"⏭ {display_title}: пропуск ({precheck['reason']})", "warning")
                        else:
                            checked_batch.append(vid)
                            # Collect HR contact info if available
                            contact = precheck.get("contact")
                            if contact and (contact.get("email") or contact.get("fio")):
                                meta = state.vacancy_meta.get(vid, {})
                                entry = {
                                    "vacancy_id": vid,
                                    "title": meta.get("title", ""),
                                    "company": meta.get("company", ""),
                                    "fio": contact.get("fio", ""),
                                    "email": contact.get("email", ""),
                                    "phone": contact.get("phone", ""),
                                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    "account": state.short,
                                }
                                with self._hr_contacts_lock:
                                    if len(self.hr_contacts) < 500:
                                        self.hr_contacts.append(entry)
                    batch = checked_batch
                    if not batch:
                        i += batch_size
                        continue

                if len(batch) > 1:
                    self._add_log(
                        state.short, state.color,
                        f"📤 Пакет {len(batch)} откликов: {', '.join(batch[:3])}{'...' if len(batch) > 3 else ''}",
                        "info",
                    )

                # Choose apply method: OAuth API or Web (per-account or global)
                if state.use_oauth or CONFIG.use_oauth_apply:
                    # OAuth: synchronous, one by one (API doesn't support batch)
                    results = []
                    for vid in batch:
                        try:
                            result = _oauth_apply(acc, vid, acc.get("letter", ""))
                            results.append(result)
                        except Exception as e:
                            results.append(e)
                        if CONFIG.response_delay > 0:
                            time.sleep(CONFIG.response_delay)
                else:
                    # Web: async batch via aiohttp
                    def _make_send_batch(b):
                        async def send_batch():
                            tasks = [send_response_async(acc, vid) for vid in b]
                            return await asyncio.gather(*tasks, return_exceptions=True)
                        return send_batch
                    results = asyncio.run(_make_send_batch(batch)())

                for j, (vid, result_data) in enumerate(zip(batch, results)):
                    if isinstance(result_data, Exception):
                        state.errors += 1
                        state.consecutive_errors += 1
                        err_msg = str(result_data)[:60]
                        self._add_log(state.short, state.color, f"❌ {vid}: {err_msg}", "error")
                        self._add_acc_event(state, "❌", "error", vid, "", err_msg)
                        self._check_auto_pause(state)
                        continue

                    result, info = result_data
                    state.current_vacancy_id = vid

                    if result == "sent":
                        state.sent += 1
                        # Daily counter
                        today = datetime.now().strftime("%Y-%m-%d")
                        if state.daily_date != today:
                            state.daily_sent = 0
                            state.daily_date = today
                            state.hard_stopped = False
                        state.daily_sent += 1
                        state.consecutive_errors = 0  # сброс счётчика ошибок
                        # Дополняем info мета-данными из поиска если API не вернул title
                        if not info.get("title"):
                            meta_fb = state.vacancy_meta.get(vid, {})
                            info = {**meta_fb, **info}
                        add_applied(acc["name"], vid, info)

                        # Collect HR contact if available
                        contact = info.get("contact", {})
                        if contact and (contact.get("email") or contact.get("fio")):
                            with self._hr_contacts_lock:
                                if len(self.hr_contacts) < 500:
                                    self.hr_contacts.append({
                                        "vacancy_id": vid,
                                        "title": info.get("title", ""),
                                        "company": info.get("company", ""),
                                        "fio": contact.get("fio", ""),
                                        "email": contact.get("email", ""),
                                        "phone": contact.get("phone", ""),
                                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                        "acc": state.short,
                                    })

                        title = info.get("title", "Неизвестно")
                        company = info.get("company", "?")
                        sal_from = info.get("salary_from")
                        sal_to = info.get("salary_to")
                        salary = ""
                        if sal_from or sal_to:
                            salary = f"{sal_from or '?'} - {sal_to or '?'}"

                        state.current_vacancy_title = title
                        state.current_vacancy_company = company
                        state.action_history.append(f"✅ {title[:30]}")

                        self._add_response(state, vid, title, company, "sent", salary)
                        self._add_log(
                            state.short, state.color,
                            f"✅ {title[:40]} @ {company[:20]}",
                            "success",
                        )
                        self._add_acc_event(state, "✅", "sent", title or vid, company,
                                            salary if salary else "")

                    elif result == "test":
                        title = info.get("title", "")
                        company = info.get("company", "")
                        display_title = title[:40] if title else vid

                        if not (state.apply_tests or CONFIG.auto_apply_tests):
                            # Откликаться на тесты выключено — пропускаем
                            state.tests += 1
                            add_test_vacancy(vid, title, company,
                                             acc["name"], acc.get("resume_hash", ""))
                            state.action_history.append(f"⏭️ {display_title[:25]}")
                            self._add_response(state, vid, title, company, "test")
                            self._add_log(state.short, state.color,
                                          f"⏭️ Тест пропущен: {display_title}", "info")
                            self._add_acc_event(state, "⏭️", "test_skip",
                                                title or vid, company, "пропущено")
                        else:
                            # Пробуем автозаполнить опрос
                            q_result, q_info = asyncio.run(fill_and_submit_questionnaire(
                                acc, vid, vacancy_title=title, company=company))
                            if q_result == "sent":
                                state.sent += 1
                                state.questionnaire_sent += 1
                                state.consecutive_errors = 0
                                # Daily counter
                                today = datetime.now().strftime("%Y-%m-%d")
                                if state.daily_date != today:
                                    state.daily_sent = 0
                                    state.daily_date = today
                                    state.hard_stopped = False
                                state.daily_sent += 1
                                state.current_vacancy_title = title
                                state.current_vacancy_company = company
                                state.action_history.append(f"📝 {display_title[:25]}")
                                self._add_response(state, vid, title, company, "sent")
                                self._add_log(state.short, state.color,
                                              f"📝 Опрос пройден: {display_title}", "success")
                                q_info_full = {**state.vacancy_meta.get(vid, {}), **info}
                                add_applied(acc["name"], vid, q_info_full)
                                answer_preview = CONFIG.questionnaire_default_answer[:50]
                                self._add_acc_event(state, "📝", "questionnaire",
                                                    title or vid, company,
                                                    f"Ответ: {answer_preview}")
                            elif q_result == "limit":
                                state.limit_exceeded = True
                                state.limit_reset_time = datetime.now() + timedelta(
                                    minutes=CONFIG.limit_check_interval
                                )
                                state.status = "limit"
                                state.status_detail = f"Проверка в {state.limit_reset_time.strftime('%H:%M')}"
                                self._add_log(state.short, state.color,
                                              f"🚫 ЛИМИТ при опросе! Повторная попытка в {state.limit_reset_time.strftime('%H:%M')}",
                                              "error")
                                break
                            else:
                                # Не удалось — считаем неудачи
                                state._test_failures[vid] = state._test_failures.get(vid, 0) + 1
                                if state._test_failures[vid] >= 2:
                                    # Permanently mark as failed test after 2 attempts
                                    add_test_vacancy(vid, title, company,
                                                     acc["name"], acc.get("resume_hash", ""))
                                state.tests += 1
                                state.action_history.append(f"🧪 {display_title[:25]}")
                                self._add_response(state, vid, title, company, "test")
                                self._add_log(state.short, state.color,
                                              f"🧪 Тест (не пройден, попытка {state._test_failures[vid]}): {display_title}", "warning")
                                self._add_acc_event(state, "🧪", "test",
                                                    title or vid, company, "не пройден")

                    elif result == "already":
                        state.already_applied += 1
                        already_info = state.vacancy_meta.get(vid, {})
                        add_applied(acc["name"], vid, already_info if already_info else None)
                        state.action_history.append(f"🔄 {vid}")
                        self._add_response(state, vid, "", "", "already")

                    elif result == "limit":
                        state.limit_exceeded = True
                        if CONFIG.stop_on_hh_limit:
                            # Hard stop — no retries
                            state.hard_stopped = True
                            state.paused = True
                            state.status = "limit"
                            state.status_detail = "🛑 Лимит HH — остановлен до завтра"
                            self._add_log(
                                state.short, state.color,
                                f"🛑 ЛИМИТ HH! Бот остановлен. Сбросится в 00:00 МСК. Снимите паузу вручную.",
                                "error",
                            )
                        else:
                            state.limit_reset_time = datetime.now() + timedelta(
                                minutes=CONFIG.limit_check_interval
                            )
                            state.status = "limit"
                            state.status_detail = f"Проверка в {state.limit_reset_time.strftime('%H:%M')}"
                            self._add_log(
                                state.short, state.color,
                                f"🚫 ЛИМИТ! Повторная попытка в {state.limit_reset_time.strftime('%H:%M')}",
                                "error",
                            )
                        break

                    elif result == "auth_error":
                        if state.use_oauth or CONFIG.use_oauth_apply:
                            # OAuth mode — don't stop, just log warning
                            self._add_log(
                                state.short, state.color,
                                "⚠️ Web cookies истекли (OAuth откликов продолжает работать)", "warning",
                            )
                            state.consecutive_errors += 1
                            self._check_auto_pause(state)
                        else:
                            state.cookies_expired = True
                            state.paused = True
                            self._add_log(
                                state.short, state.color,
                                "⚠️ Куки протухли! Обновите куки и снимите паузу.", "error",
                            )
                            self._add_acc_event(state, "⚠️", "error", "Авторизация", "", "Обновите куки")
                            break

                    elif result == "error":
                        state.errors += 1
                        state.consecutive_errors += 1
                        state.action_history.append(f"❌ {vid}")
                        self._add_response(state, vid, "", "", "error")
                        raw = info.get("raw", "")[:80] if info else ""
                        exc = info.get("exception", "") if info else ""
                        debug_info = raw or exc or "unknown"
                        self._add_log(state.short, state.color, f"❌ {vid}: {debug_info}", "error")
                        self._add_acc_event(state, "❌", "error", vid, "", debug_info[:60])
                        self._check_auto_pause(state)

                if state.limit_exceeded:
                    break

                i += batch_size
                if i < len(filtered):
                    time.sleep(CONFIG.response_delay)

            # Очистка
            state.current_vacancy_id = ""
            state.current_vacancy_title = ""
            state.current_vacancy_company = ""
            if state.short in self.vacancy_queues:
                self.vacancy_queues[state.short] = {
                    "vacancies": [],
                    "current": 0,
                    "color": state.color,
                }

            if not state.limit_exceeded:
                state.status = "waiting"
                state.status_detail = "Цикл завершён"
                state.wait_until = datetime.now() + timedelta(seconds=CONFIG.pause_between_cycles)
                self._add_log(
                    state.short, state.color,
                    f"⏳ Цикл завершён, пауза {CONFIG.pause_between_cycles}с",
                    "info",
                )
                time.sleep(CONFIG.pause_between_cycles)

    async def _collect_all_urls_parallel(self, state: AccountState) -> tuple:
        """
        Параллельный сбор вакансий со ВСЕХ URL и страниц одновременно.
        Возвращает (results_by_url: dict[url, set[ids]], salary_map: dict[vid, int|None], schedule_map: dict[vid, set])
        """
        acc = state.acc
        xsrf = acc.get("cookies", {}).get("_xsrf", "")
        if not xsrf:
            return {}, {}, {}
        headers = get_headers(xsrf)
        sem = asyncio.Semaphore(CONFIG.max_concurrent * 3)

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context, limit=CONFIG.max_concurrent * 3)

        all_tasks = []
        url_pages = _url_pages_map()
        acc_url_pages = acc.get("url_pages", {})  # per-account override
        effective_urls = acc.get("urls") or [_url_entry(u)["url"] for u in CONFIG.url_pool]
        # Build extra search filter params from config
        # Note: HH only accepts ONE label param; low_competition takes priority
        extra_params = ""
        if CONFIG.filter_low_competition:
            extra_params += "&label=low_performance"
        elif CONFIG.filter_agencies:
            extra_params += "&label=not_from_agency"
        if CONFIG.search_period_days > 0:
            extra_params += f"&search_period={CONFIG.search_period_days}"
        for url_idx, url in enumerate(effective_urls):
            pages = acc_url_pages.get(url) or url_pages.get(url, CONFIG.pages_per_url)
            sep = "&" if "?" in url else "?"
            for page in range(pages):
                page_url = f"{url}{sep}page={page}{extra_params}"
                all_tasks.append((url_idx, url, page, page_url))

        total_tasks = len(all_tasks)
        results_by_url = {url: [] for url in effective_urls}
        salary_map = {}
        completed = 0

        async with aiohttp.ClientSession(
            headers=headers, cookies=acc["cookies"], connector=connector
        ) as session:
            async def fetch_one(url_idx, url, page, page_url):
                nonlocal completed
                html = await fetch_page(session, page_url, sem)
                completed += 1
                state.current_url_idx = url_idx
                state.current_url = url
                state.current_page = page + 1
                state.status_detail = f"Загрузка {completed}/{total_tasks}"
                if html and _is_login_page(html):
                    if not (state.use_oauth or CONFIG.use_oauth_apply):
                        state.cookies_expired = True
                    return url, set(), {}, {}, {}
                if html:
                    ids = parse_ids(html)
                    salaries = parse_salaries(html, ids)
                    meta = parse_vacancy_meta(html)
                    schedules = parse_work_schedules(html, ids)
                    return url, ids, salaries, meta, schedules
                return url, set(), {}, {}, {}

            tasks = [
                fetch_one(url_idx, url, page, page_url)
                for url_idx, url, page, page_url in all_tasks
            ]
            task_results = await asyncio.gather(*tasks, return_exceptions=True)

            schedule_map = {}
            for result in task_results:
                if isinstance(result, Exception):
                    log_debug(f"❌ Ошибка при загрузке: {result}")
                    continue
                url, ids, salaries, meta, schedules = result
                results_by_url[url].extend(ids)
                salary_map.update(salaries)
                state.vacancy_meta.update(meta)
                for vid, sched_set in schedules.items():
                    if sched_set:
                        schedule_map.setdefault(vid, set()).update(sched_set)

        return {url: set(ids) for url, ids in results_by_url.items()}, salary_map, schedule_map

    def _process_llm_replies(self, state: AccountState) -> None:
        """Check recent unread negotiations for employer messages and auto-reply using LLM."""
        if not state.llm_enabled:
            return
        # Non-blocking: if another thread is already processing this account, skip
        if not state._llm_lock.acquire(blocking=False):
            log_debug(f"LLM [{state.short}]: уже выполняется, пропуск")
            return
        try:
            self._process_llm_replies_inner(state)
        finally:
            state._llm_lock.release()

    def _process_llm_replies_inner(self, state: AccountState) -> None:
        """Inner implementation — called only when _llm_lock is held."""
        replied = 0

        # Sync _llm_no_chat from persisted DB (catches 409 failures from previous sessions)
        state._llm_no_chat.update(get_no_chat_neg_ids())

        # Fetch recent chat pages sorted by last activity. Chats needing reply
        # (employer just wrote) will always be near the top.
        self._add_log(state.short, state.color, "🤖 LLM: загружаю список чатов…", "info")
        log_debug(f"LLM [{state.short}]: загружаю чат-лист")
        items_by_id, display_info, cur_pid = _fetch_chat_list(state.acc, max_pages=3)
        log_debug(f"LLM [{state.short}]: чат-лист загружен, {len(items_by_id)} чатов")

        # Process items that need a reply: NEGOTIATION type, unread, from employer, not rejection
        # No filtering by interview_ids — chats sorted by recent activity, old interview IDs
        # are buried deep in the 10000-item list and won't appear in first pages anyway
        candidates = []
        skipped_ours = 0
        skipped_system = 0
        skipped_read = 0
        skipped_locked = 0
        for item_id, item in items_by_id.items():
            if item.get("type") != "NEGOTIATION":
                continue
            unread = item.get("unreadCount", 0)
            last_msg = item.get("lastMessage") or {}
            sender_id = last_msg.get("participantId", "")
            last_text = (last_msg.get("text") or "")[:40]
            wf = last_msg.get("workflowTransition") or {}
            from_employer = bool(sender_id and cur_pid and sender_id != cur_pid)
            # Early check: known 409 (persisted from DB or current session)
            if item_id in state._llm_no_chat:
                skipped_locked += 1
                log_debug(f"LLM [{state.short}] {item_id}: 409-закрыт, пропуск кандидата")
                continue
            # Early check: chat locked via text/flags (employer disabled messaging or invite-only)
            if _check_chat_locked(item):
                skipped_locked += 1
                log_debug(f"LLM [{state.short}] {item_id}: чат заблокирован, пропуск кандидата «{last_text}»")
                continue
            # Early check: writePossibility from chatik API
            write_poss = (item.get("writePossibility") or {}).get("name", "")
            if write_poss not in ("ENABLED_FOR_ALL", "ENABLED_FOR_ALL_BY_EMPLOYER", ""):
                skipped_locked += 1
                log_debug(f"LLM [{state.short}] {item_id}: writePossibility={write_poss}, пропуск")
                continue
            if unread == 0:
                if from_employer and not wf:
                    # unread=0 но последнее от работодателя — юзер прочитал в браузере,
                    # но бот ещё не отвечал. Проверяем по dedup: если last_msg_id ещё
                    # не в llm_replied_msgs — добавляем в кандидаты.
                    last_msg_id_early = str((item.get("lastMessage") or {}).get("id", ""))
                    key_early = (str(item_id), last_msg_id_early)
                    if key_early not in state.llm_replied_msgs:
                        log_debug(f"LLM [{state.short}] {item_id}: unread=0 но от работодателя, не отвечали — добавляю кандидатом: «{last_text}»")
                        # не skipping — fall through to candidates
                    else:
                        skipped_read += 1
                        di = display_info.get(str(item_id), {})
                        upsert_interview(str(item_id), acc=state.short, acc_color=state.color,
                                         employer=di.get("subtitle", ""), vacancy_title=di.get("title", ""),
                                         chat_status="waiting_hr")
                        log_debug(f"LLM [{state.short}] {item_id}: unread=0, от работодателя, уже отвечали, пропуск: «{last_text}»")
                        continue
                else:
                    skipped_read += 1
                    continue
            if cur_pid and sender_id == cur_pid:
                skipped_ours += 1
                log_debug(f"LLM [{state.short}] {item_id}: unread={unread}, последнее наше, пропуск")
                di = display_info.get(str(item_id), {})
                upsert_interview(str(item_id), acc=state.short, acc_color=state.color,
                                 employer=di.get("subtitle", ""), vacancy_title=di.get("title", ""),
                                 chat_status="waiting_hr")
                continue
            if wf:
                wf_id = wf.get("id", "") if isinstance(wf, dict) else ""
                # Числовой wf.id = просто внутренняя ссылка, сообщение реальное
                # Строковый wf.id = тип системного события (REJECTION, APPLICATION, etc.) = пропускаем
                if isinstance(wf_id, str) and wf_id:
                    skipped_system += 1
                    log_debug(f"LLM [{state.short}] {item_id}: unread={unread}, системное событие wf={wf_id!r}, пропуск")
                    continue
                # Числовой wf.id — продолжаем обработку как реальное сообщение
                log_debug(f"LLM [{state.short}] {item_id}: unread={unread}, wf.id={wf_id!r} (числовой, реальное сообщение)")
            # Dump raw item fields to help detect unknown lock indicators
            log_debug(f"LLM [{state.short}] {item_id}: ✅ кандидат unread={unread}, от={sender_id}, «{last_text}» | "
                      f"keys={list(item.keys())} canSend={item.get('canSendMessage')} state={item.get('state')} "
                      f"permissions={item.get('permissions')} actions={item.get('actions')}")
            candidates.append(item_id)

        log_debug(f"LLM [{state.short}]: {len(candidates)} кандидатов (прочитанных: {skipped_read}, наших: {skipped_ours}, системных: {skipped_system})")
        if not candidates:
            state.llm_pending_chats = 0
            state.llm_status = f"💤 Нет новых (наших: {skipped_ours}, закр.: {skipped_locked})"
            self._add_log(state.short, state.color,
                f"🤖 LLM: нет новых сообщений (прочит.: {skipped_read}, наших: {skipped_ours}, сист.: {skipped_system}, закрыт: {skipped_locked})", "info")
            return

        state.llm_pending_chats = len(candidates)
        state.llm_status = f"🔄 Обработка {len(candidates)} чатов..."
        self._add_log(state.short, state.color, f"🤖 LLM: {len(candidates)} чатов требуют ответа", "info")

        for i, neg_id in enumerate(candidates[:15]):  # limit to 15 per cycle
            # Проверяем флаг в начале каждой итерации — пользователь мог выключить LLM во время цикла
            if not state.llm_enabled or not CONFIG.llm_enabled:
                self._add_log(state.short, state.color, f"🤖 LLM: выключен в процессе цикла, прерываю", "warning")
                break
            try:
                # Early skip for chats confirmed closed by 409 in this session
                if neg_id in state._llm_no_chat:
                    item = items_by_id.get(neg_id, {})
                    info = display_info.get(str(neg_id), {})
                    emp = (info.get("subtitle") or neg_id).strip(" ,")[:25]
                    self._add_log(state.short, state.color,
                        f"🤖 [{emp}] 🔒 переписка закрыта, пропуск", "warning", neg_id=neg_id)
                    continue

                item = items_by_id.get(neg_id)
                if not item:
                    log_debug(f"LLM [{state.short}] {neg_id}: не найден в items_by_id, пропуск")
                    continue
                thread = _build_thread_from_chat_item(item, display_info, cur_pid, neg_id)
                employer_short = thread.get("employer_name", neg_id)[:25]
                if thread.get("error"):
                    self._add_log(state.short, state.color, f"🤖 [{employer_short}] ошибка треда: {thread['error']}", "error", neg_id=neg_id)
                    continue

                employer = thread.get("employer_name", neg_id)[:35]
                employer_msg = thread.get("last_employer_msg", "")
                vacancy_title = thread.get("vacancy_title", "")

                # Если чат прошёл ранний фильтр (unread=0 но от работодателя, не отвечали),
                # принудительно ставим needs_reply=True — _build_thread_from_chat_item
                # возвращает False из-за unread=0, но мы уже проверили dedup выше.
                if not thread.get("needs_reply") and not thread.get("chat_locked"):
                    raw_item = items_by_id.get(neg_id, {})
                    raw_unread = raw_item.get("unreadCount", 0)
                    raw_last = raw_item.get("lastMessage") or {}
                    raw_sender = raw_last.get("participantId", "")
                    if raw_unread == 0 and cur_pid and raw_sender and raw_sender != cur_pid:
                        thread["needs_reply"] = True
                        if not employer_msg:
                            employer_msg = (raw_last.get("text") or "").strip()
                            thread["last_employer_msg"] = employer_msg

                # Chat locked: employer disabled messaging or invite-only — skip permanently
                if thread.get("chat_locked"):
                    lock_reason = thread["chat_locked"]
                    log_debug(f"LLM [{state.short}] {neg_id}: переписка недоступна — {lock_reason!r}")
                    self._add_log(state.short, state.color,
                        f"🤖 [{employer_short}] 🔒 переписка недоступна, пропуск", "warning", neg_id=neg_id)
                    state.llm_replied_msgs.add((neg_id, "locked"))  # permanent skip
                    upsert_interview(neg_id, acc=state.short, acc_color=state.color, chat_status="locked")
                    continue

                # Persist thread data to interviews DB
                upsert_interview(neg_id, acc=state.short, acc_color=state.color,
                                 employer=employer, vacancy_title=vacancy_title,
                                 employer_last_msg=employer_msg if employer_msg else None,
                                 needs_reply=bool(thread.get("needs_reply")))

                if not thread.get("needs_reply"):
                    log_debug(f"LLM [{state.short}] {neg_id}: ответ не нужен (последнее сообщение — от соискателя)")
                    upsert_interview(neg_id, acc=state.short, acc_color=state.color, chat_status="waiting_hr")
                    self._add_log(state.short, state.color, f"🤖 [{employer_short}] последнее сообщение наше, пропуск", "info", neg_id=neg_id)
                    continue
                last_msg_id = thread["last_msg_id"]
                key = (neg_id, last_msg_id)
                # Per-account dedup (prevents retries within same session)
                if key in state.llm_replied_msgs:
                    log_debug(f"LLM [{state.short}] {neg_id}: уже отвечали на msg {last_msg_id}")
                    self._add_log(state.short, state.color, f"🤖 [{employer_short}] уже отвечали в этой сессии, пропуск", "info", neg_id=neg_id)
                    continue
                # Temporary skip for transient failures (LLM API error, send network error)
                _skip_until = state._llm_temp_skip.get(key, 0)
                if time.time() < _skip_until:
                    mins = max(1, int((_skip_until - time.time()) / 60))
                    self._add_log(state.short, state.color,
                        f"🤖 [{employer_short}] повтор через ~{mins}м (ошибка в предыдущем цикле)", "info", neg_id=neg_id)
                    log_debug(f"LLM [{state.short}] {neg_id}: temp_skip до {_skip_until:.0f}")
                    continue
                # Global dedup by (cur_pid, neg_id, last_msg_id) — prevents double-send
                # when two accounts share the same HH user (same cur_pid)
                global_key = (cur_pid, neg_id, last_msg_id)
                with self._llm_sent_lock:
                    if global_key in self._llm_sent_global:
                        log_debug(f"LLM [{state.short}] {neg_id}: уже отправлено другим аккаунтом (pid={cur_pid})")
                        self._add_log(state.short, state.color, f"🤖 [{employer_short}] уже отправлено другим аккаунтом, пропуск", "info")
                        state.llm_replied_msgs.add(key)
                        continue

                progress = f"[{i+1}/{min(len(candidates),15)}]"
                self._add_log(state.short, state.color,
                    f"🤖 {progress} [{employer_short}]: «{employer_msg[:50]}»", "info", neg_id=neg_id)
                log_debug(f"LLM [{state.short}] {progress} {neg_id} ({employer_short}): загружаю историю чата")
                cover_letter = state.acc.get("letter", "") if CONFIG.llm_use_cover_letter else ""
                # Fetch resume for LLM context
                if CONFIG.llm_use_resume:
                    rh = state.acc.get("resume_hash", "")
                    _cached = rh and rh in _resume_cache and (time.time() - _resume_cache[rh][1] < _RESUME_CACHE_TTL)
                    resume_text = fetch_resume_text(state.acc)
                    if resume_text:
                        src = "кэш" if _cached else "загружено"
                        self._add_log(state.short, state.color,
                            f"🤖 📄 Резюме в контексте LLM ({src}, {len(resume_text)} симв.)", "info", neg_id=neg_id)
                    else:
                        self._add_log(state.short, state.color,
                            f"🤖 📄 Резюме не удалось загрузить — LLM работает без него", "warning", neg_id=neg_id)
                else:
                    resume_text = ""
                # Fetch full conversation history so LLM has full context
                full_history = _fetch_chat_history(state.acc, neg_id, max_messages=20)
                conversation = full_history if full_history else thread["messages"]

                # Detect robot-recruiter with button questions
                # Check last employer message for actions.text_buttons
                _last_emp_raw = None
                if full_history:
                    for msg_raw in reversed(full_history):
                        if msg_raw.get("sender") == "employer":
                            _last_emp_raw = msg_raw
                            break
                _raw_actions = (_last_emp_raw or {}).get("actions") or {}
                _text_buttons = _raw_actions.get("text_buttons", [])
                _is_bot_msg = (_last_emp_raw or {}).get("is_bot", False)
                if _text_buttons:
                    # Robot-recruiter with buttons — pick button answer instead of LLM
                    btn_text = _text_buttons[0].get("text", "ДА")
                    for b in _text_buttons:
                        t_lower = b.get("text", "").lower()
                        if t_lower in ("да", "yes", "согласен", "подтверждаю", "готов", "готова"):
                            btn_text = b["text"]
                            break
                    log_debug(f"LLM [{state.short}] {neg_id}: робот-рекрутер, кнопки={[b.get('text') for b in _text_buttons]}, отвечаю '{btn_text}'")
                    self._add_log(state.short, state.color,
                        f"🤖 [{employer_short}] 🤖 Робот → '{btn_text}'", "info", neg_id=neg_id)
                    upsert_interview(neg_id, acc=state.short, acc_color=state.color,
                                     employer=employer_short, vacancy_title=vacancy_title,
                                     chat_status="robot")
                    ok = send_negotiation_message(state.acc, neg_id, btn_text)
                    if ok and ok != "chat_not_found":
                        state.llm_replied_msgs.add(key)
                        replied += 1
                        ts = datetime.now().strftime("%H:%M")
                        self.llm_log.appendleft({
                            "time": ts, "acc": state.short, "color": state.color,
                            "employer": employer_short, "vacancy_title": vacancy_title,
                            "neg_id": neg_id, "employer_msg": employer_msg[:50],
                            "bot_reply": f"🤖 Кнопка: {btn_text}", "sent": True,
                        })
                    elif ok == "chat_not_found":
                        state._llm_no_chat.add(neg_id)
                        state.llm_replied_msgs.add(key)
                        log_debug(f"LLM [{state.short}] {neg_id}: робот-кнопка 409, чат закрыт — добавлен в _llm_no_chat")
                    elif not ok:
                        state._llm_temp_skip[key] = time.time() + 1800
                    continue

                # Если в истории нет реального сообщения от работодателя — не отвечаем.
                # Случай 1: только системные события ("Отклик на вакансию" и т.п.) — history пустая
                # Случай 2: history есть, но последнее сообщение от нас (уже ответили)
                has_employer_msg = any(m.get("sender") == "employer" for m in conversation)
                last_real_sender = conversation[-1].get("sender") if conversation else None
                if not has_employer_msg:
                    log_debug(f"LLM [{state.short}] {neg_id}: нет реальных сообщений работодателя (только системные), пропуск")
                    state.llm_replied_msgs.add(key)  # не повторять этот триггер
                    continue
                if last_real_sender == "applicant":
                    log_debug(f"LLM [{state.short}] {neg_id}: последнее реальное сообщение наше — уже ответили, пропуск")
                    state.llm_replied_msgs.add(key)
                    continue
                # In-a-row limit: count consecutive applicant messages at the end of conversation
                _consecutive_ours = 0
                for _cm in reversed(conversation):
                    if _cm.get("sender") == "applicant":
                        _consecutive_ours += 1
                    else:
                        break
                # Update tracking dict (reset when employer replied = _consecutive_ours is 0 or low)
                state._msg_consecutive[neg_id] = _consecutive_ours
                if _consecutive_ours >= 4:
                    log_debug(f"LLM [{state.short}] {neg_id}: in_a_row_limit: {_consecutive_ours} сообщений без ответа HR, пропуск")
                    self._add_log(state.short, state.color,
                        f"🤖 [{employer_short}] ⚠️ in_a_row_limit: {_consecutive_ours} сообщения без ответа HR, пропуск", "warning", neg_id=neg_id)
                    state.llm_replied_msgs.add(key)
                    continue
                log_debug(f"LLM [{state.short}] {neg_id}: история {len(conversation)} сообщений, резюме {len(resume_text)} симв., отправляю в LLM")
                self._add_log(state.short, state.color,
                    f"🤖 {progress} [{employer_short}]: история {len(conversation)} сообщ., жду LLM…", "info", neg_id=neg_id)
                reply_text = generate_llm_reply(conversation, thread.get("employer_name", ""), cover_letter, resume_text)
                if not reply_text:
                    self._add_log(state.short, state.color, f"🤖 [{employer_short}] LLM вернул пустой ответ, повтор через 30м", "warning", neg_id=neg_id)
                    log_debug(f"LLM [{state.short}] {neg_id}: пустой ответ от LLM, ставим temp_skip 30м")
                    state._llm_temp_skip[key] = time.time() + 1800  # retry in 30 min
                    continue
                log_debug(f"LLM [{state.short}] {neg_id}: ответ получен ({len(reply_text)} симв.), отправляю")

                ts = datetime.now().strftime("%d.%m %H:%M")

                if CONFIG.llm_auto_send:
                    # Re-check global dedup right before sending (atomic reserve)
                    with self._llm_sent_lock:
                        if global_key in self._llm_sent_global:
                            log_debug(f"LLM [{state.short}] {neg_id}: другой поток уже отправил (pid={cur_pid}), пропуск")
                            self._add_log(state.short, state.color, f"🤖 [{employer_short}] другой аккаунт уже отправил, пропуск", "info")
                            state.llm_replied_msgs.add(key)
                            continue
                        # Reserve the slot before sending so concurrent threads see it
                        self._llm_sent_global.add(global_key)
                    self._add_log(state.short, state.color,
                        f"🤖 [{employer_short}] отправляю: «{reply_text[:60]}»", "info", neg_id=neg_id)
                    log_debug(f"LLM [{state.short}] {neg_id}: отправляю сообщение в chatik")
                    ok = send_negotiation_message(state.acc, neg_id, reply_text, topic_id=thread.get("topic_id", ""))
                    if ok == "chat_not_found":
                        with self._llm_sent_lock:
                            self._llm_sent_global.discard(global_key)
                        state.llm_replied_msgs.add(key)
                        state._llm_no_chat.add(neg_id)  # permanent: this neg_id returns 409
                        upsert_interview(neg_id, acc=state.short, acc_color=state.color,
                                         employer=employer, vacancy_title=vacancy_title,
                                         chat_not_found=True)  # persist to survive restarts
                        self._add_log(state.short, state.color,
                            f"🤖 [{employer_short}] 🔒 переписка закрыта (409), пропуск", "warning", neg_id=neg_id)
                        continue
                    if ok:
                        state.llm_replied_msgs.add(key)
                        state._msg_consecutive[neg_id] = state._msg_consecutive.get(neg_id, 0) + 1
                        replied += 1
                        # Don't mark_read — let unread stay so we catch follow-up HR messages
                        upsert_interview(neg_id, acc=state.short, acc_color=state.color,
                                         llm_reply=reply_text, llm_sent=True)
                        self._add_log(state.short, state.color,
                            f"🤖 Авто-ответ → {employer}: {reply_text[:60]}…", "success", neg_id=neg_id)
                        self.llm_log.appendleft({
                            "time": ts, "acc": state.short, "color": state.color,
                            "employer": employer, "vacancy_title": vacancy_title,
                            "neg_id": neg_id, "employer_msg": employer_msg,
                            "bot_reply": reply_text, "sent": True,
                        })
                    else:
                        # Release the reserved global slot so another account can retry
                        with self._llm_sent_lock:
                            self._llm_sent_global.discard(global_key)
                        # Use temp_skip (30 min) instead of permanent mark — send error may be transient
                        state._llm_temp_skip[key] = time.time() + 1800  # retry in 30 min
                        upsert_interview(neg_id, acc=state.short, acc_color=state.color,
                                         llm_reply=reply_text, llm_sent=False)
                        self._add_log(state.short, state.color,
                            f"🤖 Черновик (ошибка отправки, повтор ~30м) → {employer}: {reply_text[:60]}…", "warning", neg_id=neg_id)
                        self.llm_log.appendleft({
                            "time": ts, "acc": state.short, "color": state.color,
                            "employer": employer, "vacancy_title": vacancy_title,
                            "neg_id": neg_id, "employer_msg": employer_msg,
                            "bot_reply": reply_text, "sent": False,
                        })
                else:
                    state.llm_replied_msgs.add(key)
                    upsert_interview(neg_id, acc=state.short, acc_color=state.color,
                                     llm_reply=reply_text, llm_sent=False)
                    self._add_log(state.short, state.color,
                        f"🤖 Черновик [{employer}]: {reply_text[:80]}…", "info", neg_id=neg_id)
                    self.llm_log.appendleft({
                        "time": ts, "acc": state.short, "color": state.color,
                        "employer": employer, "vacancy_title": vacancy_title,
                        "neg_id": neg_id, "employer_msg": employer_msg,
                        "bot_reply": reply_text, "sent": False,
                    })

                time.sleep(3)  # rate limit between messages
            except Exception as e:
                log_debug(f"_process_llm_replies {neg_id}: {e}")
                # Release any reserved global dedup slot for this neg_id that may have been
                # reserved before the exception occurred but not yet cleaned up
                try:
                    with self._llm_sent_lock:
                        to_remove = {gk for gk in self._llm_sent_global if gk[1] == neg_id}
                        self._llm_sent_global -= to_remove
                except Exception:
                    pass

        state.llm_replied_count += replied
        if replied:
            state.llm_status = f"✅ {replied} ответов отправлено"
            log_debug(f"LLM auto-reply [{state.short}]: {replied} ответов отправлено")
        elif candidates:
            state.llm_status = f"⏳ {len(candidates)} чатов, 0 отправлено"

    def _fetch_hh_stats_worker(self, idx: int, state: AccountState) -> None:
        """Thread worker for HH stats polling"""
        try:
            self._fetch_hh_stats_worker_inner(idx, state)
        except Exception as e:
            log_debug(f"STATS WORKER CRASHED [{state.short}]: {e}")
            import traceback
            log_debug(traceback.format_exc())

    def _fetch_hh_stats_worker_inner(self, idx: int, state: AccountState) -> None:
        while not self._stop_event.is_set():
            # Wait only during global pause — LLM/stats should work even with daily limit pause
            while self.paused and not self._stop_event.is_set() and not getattr(state, '_deleted', False):
                time.sleep(2)
            if self._stop_event.is_set() or getattr(state, '_deleted', False):
                break

            state.hh_stats_loading = True
            try:
                # Negotiations stats
                stats = fetch_hh_negotiations_stats(state.acc)
                if stats.get("auth_error"):
                    state.cookies_expired = True
                    self._add_log(
                        state.short, state.color,
                        "⚠️ Куки протухли! (HH stats) Обновите куки.", "error",
                    )
                    state.hh_stats_loading = False
                    # Don't overwrite real stats with zeroes on auth failure
                    self._stop_event.wait(max(CONFIG.llm_check_interval * 60, 120))
                    continue
                old_interviews = state.hh_interviews
                state.hh_interviews = stats["interview"]
                state.hh_interviews_recent = stats["recent_interview"]
                state.hh_viewed = stats["viewed"]
                state.hh_not_viewed = stats["not_viewed"]
                state.hh_discards = stats["discard"]
                state.hh_interviews_list = stats["interviews_list"]
                state.hh_interview_neg_ids = stats.get("neg_ids", [])
                state.hh_unread_by_employer = stats.get("unread_by_employer", 0)

                # Persist interviews to DB (neg_id → employer from interviews_list text)
                for neg_id in state.hh_interview_neg_ids:
                    upsert_interview(neg_id, acc=state.short, acc_color=state.color)
                # Try to enrich with employer/vacancy from interviews_list if counts match
                if len(state.hh_interview_neg_ids) == len(stats["interviews_list"]):
                    for neg_id, item in zip(state.hh_interview_neg_ids, stats["interviews_list"]):
                        parts = item.get("text", "").rsplit(" ", 1)
                        upsert_interview(neg_id, acc=state.short, acc_color=state.color,
                                         vacancy_title=item.get("text", ""))

                # Possible offers
                offers = fetch_hh_possible_offers(state.acc)
                state.hh_possible_offers = offers

                # Resume statistics (views, shows, invitations, touch timer)
                rs = fetch_resume_stats(state.acc)
                state.resume_views_7d = rs["views"]
                state.resume_views_new = rs["views_new"]
                state.resume_shows_7d = rs["shows"]
                state.resume_invitations_7d = rs["invitations"]
                state.resume_invitations_new = rs["invitations_new"]
                state.resume_next_touch_seconds = rs["next_touch_seconds"]
                state.resume_free_touches = rs["free_touches"]
                state.resume_global_invitations = rs["global_invitations"]
                state.resume_new_invitations_total = rs["new_invitations_total"]

                # Resume view history
                state.resume_view_history = fetch_resume_view_history(state.acc, limit=100)

                state.hh_stats_updated = datetime.now()

                if old_interviews > 0 and stats["interview"] > old_interviews:
                    new_count = stats["interview"] - old_interviews
                    self._add_log(
                        state.short, state.color,
                        f"🎯 НОВОЕ ПРИГЛАШЕНИЕ! (+{new_count} интервью)",
                        "success",
                    )

                log_debug(
                    f"HH stats {state.short}: {stats['interview']} интервью, "
                    f"{rs['views']} просмотров резюме, {rs['new_invitations_total']} новых инвайтов"
                )

                # LLM auto-reply (skip if paused)
                if self.paused or state.paused:
                    log_debug(f"LLM [{state.short}]: пропуск — на паузе")
                    state.hh_stats_loading = False
                    time.sleep(max(CONFIG.llm_check_interval * 60, 120))
                    continue

                _has_llm = CONFIG.llm_api_key or any(
                    p.get("api_key") for p in (CONFIG.llm_profiles or []) if p.get("enabled", True)
                )
                _neg_count = len(state.hh_interview_neg_ids)
                if not CONFIG.llm_enabled:
                    log_debug(f"LLM [{state.short}]: пропуск — глобально выключено")
                elif not _has_llm:
                    self._add_log(state.short, state.color, "🤖 LLM: нет API ключа ни в одном профиле", "warning")
                elif not state.llm_enabled:
                    log_debug(f"LLM [{state.short}]: пропуск — выключено для аккаунта")
                else:
                    if _neg_count:
                        self._add_log(state.short, state.color, f"🤖 LLM: проверяю {_neg_count} переговоров…", "info")
                    else:
                        self._add_log(state.short, state.color, "🤖 LLM: нет переговоров в статусе Интервью, проверяю чаты…", "info")
                    self._process_llm_replies(state)
            except Exception as e:
                log_debug(f"HH stats fetch error ({state.short}): {e}")
            finally:
                state.hh_stats_loading = False

            time.sleep(max(CONFIG.llm_check_interval * 60, 120))


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(title="HH Bot Dashboard")
manager = ConnectionManager()
bot = BotManager()

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def startup():
    load_accounts()
    bot.start()
    asyncio.create_task(broadcast_loop())


@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_json()
            cmd = data.get("type", "")

            if cmd == "pause_toggle":
                bot.toggle_pause()
            elif cmd == "account_pause":
                try:
                    idx = int(data.get("idx", -1))
                except (ValueError, TypeError):
                    continue
                bot.toggle_account_pause(idx)
            elif cmd == "account_llm":
                try:
                    idx = int(data.get("idx", -1))
                except (ValueError, TypeError):
                    continue
                bot.toggle_account_llm(idx)
            elif cmd == "account_oauth":
                try:
                    idx = int(data.get("idx", -1))
                except (ValueError, TypeError):
                    continue
                bot.toggle_account_oauth(idx)
            elif cmd == "set_config":
                key = data.get("key")
                value = data.get("value")
                if key == "allowed_schedules" and isinstance(value, list):
                    CONFIG.allowed_schedules = [s for s in value if isinstance(s, str)]
                    save_config()
                    bot._add_log("", "", f"⚙️ Формат работы: {CONFIG.allowed_schedules or 'все'}", "info")
                elif key == "auto_apply_tests":
                    CONFIG.auto_apply_tests = bool(value)
                    save_config()
                    bot._add_log("", "", f"⚙️ Авто-тесты: {'ВКЛ' if CONFIG.auto_apply_tests else 'ВЫКЛ'}", "info")
                elif key and key in _CONFIG_KEYS:
                    old_val = getattr(CONFIG, key)
                    try:
                        setattr(CONFIG, key, type(old_val)(value))
                        save_config()
                        bot._add_log("", "", f"⚙️ {key} = {value}", "info")
                    except Exception as e:
                        log_debug(f"set_config error: {e}")
            elif cmd == "set_questionnaire":
                templates = data.get("templates")
                default = data.get("default_answer")
                if isinstance(templates, list):
                    CONFIG.questionnaire_templates = templates
                if isinstance(default, str):
                    CONFIG.questionnaire_default_answer = default
                save_config()
                bot._add_log("", "", f"📝 Шаблоны опроса обновлены ({len(CONFIG.questionnaire_templates)} шт.)", "info")
            elif cmd == "set_letter_templates":
                templates = data.get("templates")
                if isinstance(templates, list):
                    CONFIG.letter_templates = templates
                    save_config()
                    bot._add_log("", "", f"✉️ Шаблоны писем обновлены ({len(templates)} шт.)", "info")
            elif cmd == "set_url_pool":
                pool = data.get("urls")
                if isinstance(pool, list):
                    normalized = []
                    for u in pool:
                        entry = _url_entry(u)
                        if entry["url"]:
                            normalized.append(entry)
                    CONFIG.url_pool = normalized
                    save_config()
                    bot._add_log("", "", f"🔗 Пул URL обновлён ({len(CONFIG.url_pool)} шт.)", "info")
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


@app.post("/api/pause")
async def api_pause():
    bot.toggle_pause()
    return {"paused": bot.paused}


@app.post("/api/account/{idx}/pause")
async def api_account_pause(idx: int):
    bot.toggle_account_pause(idx)
    if 0 <= idx < len(bot.account_states):
        paused = bot.account_states[idx].paused
    else:
        temp_idx = idx - len(bot.account_states)
        s = bot.temp_states.get(temp_idx)
        paused = s.paused if s else False
    return {"paused": paused}


@app.post("/api/account/{idx}/llm_toggle")
async def api_account_llm_toggle(idx: int):
    bot.toggle_account_llm(idx)
    if 0 <= idx < len(bot.account_states):
        enabled = bot.account_states[idx].llm_enabled
    else:
        temp_idx = idx - len(bot.account_states)
        s = bot.temp_states.get(temp_idx)
        enabled = s.llm_enabled if s else True
    return {"llm_enabled": enabled}


class ConfigUpdate(BaseModel):
    key: str
    value: float


@app.post("/api/settings")
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


@app.get("/api/sessions")
async def api_sessions():
    """Список браузерных сессий без cookies."""
    base_idx = len(bot.account_states)
    return [
        {
            "idx": base_idx + i,
            "name": s.get("name", f"Браузер #{i+1}"),
            "short": s.get("short", ""),
            "resume_hash": s.get("resume_hash", ""),
            "all_resumes": s.get("all_resumes", []),
            "letter": s.get("letter", ""),
            "temp": True,
            "bot_active": s.get("bot_active", False),
        }
        for i, s in enumerate(bot.temp_sessions)
    ]


@app.get("/api/debug/session/{idx}")
async def api_debug_session(idx: int):
    """Показать SSR структуру для браузерной сессии (для отладки resume_hash)."""
    temp_idx = idx - len(bot.account_states)
    if temp_idx < 0 or temp_idx >= len(bot.temp_sessions):
        return {"error": "session not found"}
    ts = bot.temp_sessions[temp_idx]
    raw_line = ts.get("_raw_cookie_line", "")
    if not raw_line:
        # Восстановить raw_line из cookies dict
        raw_line = "; ".join(f"{k}={v}" for k, v in ts.get("cookies", {}).items())
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": raw_line,
    }
    loop = asyncio.get_event_loop()
    def _fetch():
        r = requests.get("https://hh.ru/applicant/resumes", headers=headers, verify=False, timeout=15)
        ssr = parse_hh_lux_ssr(r.text)
        # Показываем только верхние ключи и примеры
        preview = {}
        for k, v in ssr.items():
            if isinstance(v, list) and v:
                preview[k] = [v[0]] if len(v) > 0 else []
            elif isinstance(v, dict):
                preview[k] = {kk: vv for kk, vv in list(v.items())[:5]}
            else:
                preview[k] = v
        return {"status": r.status_code, "ssr_keys": list(ssr.keys()), "ssr_preview": preview}
    result = await loop.run_in_executor(None, _fetch)
    return result


@app.get("/api/debug")
async def api_debug():
    snap = bot.get_state_snapshot()
    return {
        "temp_sessions_count": len(bot.temp_sessions),
        "temp_sessions": [
            {k: v for k, v in s.items() if k != "cookies"}
            for s in bot.temp_sessions
        ],
        "accounts_in_snapshot": [
            {"idx": a["idx"], "name": a["name"], "temp": a.get("temp", False)}
            for a in snap["accounts"]
        ],
    }


@app.get("/api/debug/neg_ids/{idx}")
async def api_debug_neg_ids(idx: int):
    """Принудительно вызвать fetch_hh_negotiations_stats для аккаунта и вернуть neg_ids + sample hrefs."""
    if idx < len(bot.account_states):
        state = bot.account_states[idx]
    elif idx - len(bot.account_states) in bot.temp_states:
        state = bot.temp_states[idx - len(bot.account_states)]
    else:
        return {"error": "account not found"}

    acc = state.acc
    cookies = acc["cookies"]
    headers_req = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    import asyncio
    resp = await asyncio.get_event_loop().run_in_executor(
        None, lambda: requests.get(
            "https://hh.ru/applicant/negotiations?filter=all&state=INTERVIEW&page=0",
            cookies=cookies, headers=headers_req, timeout=15,
        )
    )
    body = resp.text
    parts = re.split(r'data-qa="negotiations-item"', body)
    first_item_html = parts[1][:3000] if len(parts) > 1 else "NO ITEMS FOUND"
    all_numbers = re.findall(r'\b\d{6,}\b', body[:80000])[:30]
    data_attrs = re.findall(r'data-[\w-]+="\d{4,}"', body[:80000])[:20]
    neg_ids_from_json = re.findall(r'"chatId"\s*:\s*(\d+)', body)
    # Also try case-insensitive and other ID field names
    chat_ids_any = re.findall(r'"(?:chatId|chat_id|topicId|topic_id|negotiationId|id)"\s*:\s*(\d{8,})', body)
    # Look for __INITIAL_STATE__ or similar embedded JSON
    initial_state_match = re.search(r'window\.__(?:INITIAL_STATE|REDUX_STATE|DATA)__\s*=\s*(\{.*?\});', body[:200000], re.DOTALL)
    initial_state_keys = []
    if initial_state_match:
        try:
            import json as _json
            _data = _json.loads(initial_state_match.group(1))
            initial_state_keys = list(_data.keys())[:20]
        except Exception:
            initial_state_keys = ["parse_error"]
    # Look for any script tags with large JSON
    script_jsons = re.findall(r'<script[^>]*>\s*(?:var|const|window\.\w+)\s*=\s*(\{[^<]{100,})', body[:200000])
    script_json_keys = []
    for sj in script_jsons[:3]:
        try:
            import json as _json
            _d = _json.loads(sj)
            script_json_keys.append(list(_d.keys())[:10])
        except Exception:
            script_json_keys.append(["parse_error", sj[:50]])
    return {
        "status_code": resp.status_code,
        "items_count": len(parts) - 1,
        "first_item_html": first_item_html,
        "all_long_numbers_in_page": all_numbers,
        "data_attrs_with_numbers": data_attrs,
        "chatid_from_json": neg_ids_from_json[:20],
        "any_id_fields_8plus_digits": chat_ids_any[:20],
        "initial_state_keys": initial_state_keys,
        "script_json_keys": script_json_keys,
    }


@app.get("/api/debug/thread/{idx}/{chat_id}")
async def api_debug_thread(idx: int, chat_id: str):
    """Test fetch_negotiation_thread for a given chatId using account idx."""
    if idx < len(bot.account_states):
        state = bot.account_states[idx]
    elif idx - len(bot.account_states) in bot.temp_states:
        state = bot.temp_states[idx - len(bot.account_states)]
    else:
        return {"error": "account not found"}
    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: fetch_negotiation_thread(state.acc, chat_id)
    )
    return result


@app.get("/api/debug/thread_raw/{idx}/{chat_id}")
async def api_debug_thread_raw(idx: int, chat_id: str):
    """Return raw JSON structure from /chat/messages?chatId=... for debugging."""
    if idx < len(bot.account_states):
        state = bot.account_states[idx]
    elif idx - len(bot.account_states) in bot.temp_states:
        state = bot.temp_states[idx - len(bot.account_states)]
    else:
        return {"error": "account not found"}
    acc = state.acc
    import asyncio
    def _fetch():
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, */*",
            "Referer": "https://hh.ru/applicant/negotiations",
        }
        resp = requests.get(
            f"https://hh.ru/chat/messages?chatId={chat_id}",
            cookies=acc["cookies"], headers=headers, timeout=15,
        )
        try:
            data = resp.json()
        except Exception:
            return {"status": resp.status_code, "raw_text": resp.text[:3000]}
        chats_data = data.get("chats", {})
        chats_obj = chats_data.get("chats") or {}
        items = chats_obj.get("items", [])
        display_info = chats_data.get("chatsDisplayInfo", {})
        return {
            "status": resp.status_code,
            "top_keys": list(data.keys()),
            "pagination": {
                "page": chats_obj.get("page"),
                "perPage": chats_obj.get("perPage"),
                "pages": chats_obj.get("pages"),
                "found": chats_obj.get("found"),
                "hasNextPage": chats_obj.get("hasNextPage"),
                "nextFrom": chats_obj.get("nextFrom"),
            },
            "items_count": len(items),
            "item_ids": [str(i.get("id", "?")) for i in items[:10]],
            "item_keys_sample": list(items[0].keys()) if items else [],
            "display_info_sample_keys": list(display_info.keys())[:10],
            "first_item_full": items[0] if items else None,
        }
    return await asyncio.get_event_loop().run_in_executor(None, _fetch)


@app.get("/api/applied")
async def api_applied(limit: int = 300):
    return get_applied_list(limit)


@app.get("/api/tests")
async def api_tests(limit: int = 300):
    return get_test_list(limit)


@app.get("/api/interviews")
async def api_interviews(acc: str = "", limit: int = 2000, status: str = ""):
    return get_interviews_list(acc=acc, limit=limit, status=status)


@app.get("/api/vacancies")
async def api_vacancies(limit: int = 3000):
    return get_vacancy_db(limit)


@app.delete("/api/vacancy/{vacancy_id}")
async def api_vacancy_delete(vacancy_id: str, account: str = ""):
    """Удалить вакансию из applied и/или test кэша."""
    _load_cache()
    removed = []
    with _cache_lock:
        if account:
            # Удалить только для конкретного аккаунта
            if account in _cache_applied and vacancy_id in _cache_applied[account]:
                del _cache_applied[account][vacancy_id]
                removed.append(f"applied:{account}")
        else:
            # Удалить из всех аккаунтов
            for acc_name in list(_cache_applied.keys()):
                if vacancy_id in _cache_applied[acc_name]:
                    del _cache_applied[acc_name][vacancy_id]
                    removed.append(f"applied:{acc_name}")
        if vacancy_id in _cache_tests:
            del _cache_tests[vacancy_id]
            removed.append("test")
    if "applied" in " ".join(removed):
        threading.Thread(target=_save_applied_async, daemon=True).start()
    if "test" in " ".join(removed):
        threading.Thread(target=_save_tests_async, daemon=True).start()
    return {"ok": True, "removed": removed}


@app.get("/api/negotiations/{idx}")
async def api_negotiations(idx: int):
    s = None
    if 0 <= idx < len(bot.account_states):
        s = bot.account_states[idx]
    else:
        temp_idx = idx - len(bot.account_states)
        s = bot.temp_states.get(temp_idx)
    if s:
        return {
            "interviews": s.hh_interviews,
            "viewed": s.hh_viewed,
            "not_viewed": s.hh_not_viewed,
            "discards": s.hh_discards,
            "interviews_list": s.hh_interviews_list,
            "possible_offers": s.hh_possible_offers,
            "updated": s.hh_stats_updated.isoformat() if s.hh_stats_updated else None,
        }
    return {"error": "Invalid idx"}


@app.post("/api/account/{idx}/resume_touch")
async def api_resume_touch(idx: int):
    bot.trigger_resume_touch(idx)
    return {"ok": True}


@app.post("/api/account/{idx}/resume_touch_toggle")
async def api_resume_touch_toggle(idx: int):
    enabled = bot.toggle_resume_touch(idx)
    return {"ok": True, "enabled": enabled}


@app.post("/api/account/{idx}/set_urls")
async def api_set_urls(idx: int, request: Request):
    """Обновить список поисковых URL аккаунта и индивидуальную глубину поиска."""
    body = await request.json()
    urls = [u.strip() for u in body.get("urls", []) if u.strip()]
    # url_pages: {url: pages} — индивидуальная глубина, 0/None = использовать глобальное
    url_pages = {}
    for k, v in body.get("url_pages", {}).items():
        try:
            url_pages[k] = int(v) if v else 0
        except (ValueError, TypeError):
            pass
    if 0 <= idx < len(bot.account_states):
        bot.account_states[idx].acc["urls"] = urls
        bot.account_states[idx].acc["url_pages"] = url_pages
        bot.account_states[idx].total_urls = len(urls)
        if 0 <= idx < len(accounts_data):
            accounts_data[idx]["urls"] = urls
            accounts_data[idx]["url_pages"] = url_pages
            save_accounts()
        return {"ok": True, "count": len(urls)}
    return {"ok": False, "error": "Аккаунт не найден"}


@app.post("/api/account/{idx}/set_letter")
async def api_set_letter(idx: int, request: Request):
    """Обновить письмо аккаунта в памяти."""
    body = await request.json()
    letter = body.get("letter", "")
    if 0 <= idx < len(bot.account_states):
        bot.account_states[idx].acc["letter"] = letter
        if 0 <= idx < len(accounts_data):
            accounts_data[idx]["letter"] = letter
            save_accounts()
        return {"ok": True}
    temp_idx = idx - len(bot.account_states)
    if 0 <= temp_idx < len(bot.temp_sessions):
        bot.temp_sessions[temp_idx]["letter"] = letter
        if temp_idx in bot.temp_states:
            bot.temp_states[temp_idx].acc["letter"] = letter
        save_browser_sessions(bot.temp_sessions)
        return {"ok": True}
    return {"ok": False, "error": "Аккаунт не найден"}


@app.post("/api/account/{idx}/update_cookies")
async def api_update_cookies(idx: int, body: dict):
    """Обновить куки аккаунта в памяти без перезапуска."""
    raw = body.get("cookies", "").strip()
    if not raw:
        return {"ok": False, "error": "Строка cookies пустая"}

    cookies, raw_line = _parse_cookies_str(raw)

    if not cookies:
        return {"ok": False, "error": "Не удалось распознать cookies — вставьте cURL или строку cookie: ..."}
    if "hhtoken" not in cookies:
        return {"ok": False, "error": "Не найден hhtoken"}
    if "_xsrf" not in cookies:
        return {"ok": False, "error": "Не найден _xsrf"}

    # Обновляем в account_states (основной аккаунт)
    if 0 <= idx < len(bot.account_states):
        state = bot.account_states[idx]
        auth_cookies = {k: v for k, v in cookies.items() if k in _AUTH_COOKIE_KEYS}
        state.acc["cookies"] = auth_cookies
        state.acc["_raw_cookie_line"] = raw_line
        state.cookies_expired = False  # сбрасываем флаг протухших кук
        # Также обновляем в accounts_data чтобы новые воркеры тоже получили свежие куки
        if 0 <= idx < len(accounts_data):
            accounts_data[idx]["cookies"] = auth_cookies
            save_accounts()
        log_debug(f"update_cookies [{state.name}]: обновлены куки ({len(auth_cookies)} ключей)")
        return {"ok": True, "name": state.name, "keys": list(auth_cookies.keys())}

    # Обновляем temp сессию
    temp_idx = idx - len(bot.account_states)
    if 0 <= temp_idx < len(bot.temp_sessions):
        auth_cookies = {k: v for k, v in cookies.items() if k in _AUTH_COOKIE_KEYS}
        bot.temp_sessions[temp_idx]["cookies"] = auth_cookies
        bot.temp_sessions[temp_idx]["_raw_cookie_line"] = raw_line
        if temp_idx in bot.temp_states:
            bot.temp_states[temp_idx].acc["cookies"] = auth_cookies
            bot.temp_states[temp_idx].cookies_expired = False  # сбрасываем флаг
        save_browser_sessions(bot.temp_sessions)
        name = bot.temp_sessions[temp_idx].get("name", f"Браузер #{temp_idx+1}")
        log_debug(f"update_cookies [temp {temp_idx}] {name}: обновлены куки ({len(auth_cookies)} ключей)")
        return {"ok": True, "name": name, "keys": list(auth_cookies.keys())}

    return {"ok": False, "error": "Аккаунт не найден"}


@app.post("/api/account/{idx}/profile")
async def api_account_profile(idx: int, request: Request):
    """Обновить профиль основного аккаунта (name, short, color, resume_hash)."""
    body = await request.json()
    if not (0 <= idx < len(accounts_data)):
        return {"ok": False, "error": "Аккаунт не найден"}
    acc = accounts_data[idx]
    for field in ("name", "short", "color", "resume_hash"):
        if field in body and isinstance(body[field], str) and body[field].strip():
            acc[field] = body[field].strip()
    if 0 <= idx < len(bot.account_states):
        state = bot.account_states[idx]
        state.name = acc.get("name", state.name)
        state.short = acc.get("short", state.short)
        state.color = acc.get("color", state.color)
    save_accounts()
    return {"ok": True}


@app.post("/api/accounts/add")
async def api_account_add(request: Request):
    """Добавить новый основной аккаунт."""
    body = await request.json()
    name = body.get("name", "").strip()
    short = body.get("short", "").strip()
    color = body.get("color", "cyan").strip()
    resume_hash = body.get("resume_hash", "").strip()
    cookies_str = body.get("cookies", "").strip()
    letter = body.get("letter", "").strip()

    if not name or not resume_hash or not cookies_str:
        return {"ok": False, "error": "Требуются: name, resume_hash, cookies"}

    cookies, _ = _parse_cookies_str(cookies_str)
    if not cookies or "hhtoken" not in cookies:
        return {"ok": False, "error": "Не удалось распознать cookies (нужен hhtoken)"}

    auth_cookies = {k: v for k, v in cookies.items() if k in _AUTH_COOKIE_KEYS}
    acc = {
        "name": name,
        "short": short or (name.split()[0] if name.split() else name),
        "color": color,
        "resume_hash": resume_hash,
        "letter": letter,
        "cookies": auth_cookies,
        "urls": [],
    }
    accounts_data.append(acc)
    save_accounts()

    # Запускаем воркеры для нового аккаунта
    state = AccountState(acc)
    bot.account_states.append(state)
    new_idx = len(bot.account_states) - 1
    for target in (bot._run_account_worker, bot._fetch_hh_stats_worker):
        threading.Thread(target=target, args=(new_idx, state), daemon=True).start()

    return {"ok": True, "idx": new_idx, "name": name}


@app.delete("/api/account/{idx}/delete")
async def api_account_delete(idx: int):
    """Удалить основной аккаунт."""
    if not (0 <= idx < len(accounts_data)):
        return {"ok": False, "error": "Аккаунт не найден"}

    # Останавливаем воркер
    if 0 <= idx < len(bot.account_states):
        bot.account_states[idx]._deleted = True

    name = accounts_data[idx].get("name", f"#{idx}")

    # Pop both lists atomically, account_states first to keep indices in sync
    if 0 <= idx < len(bot.account_states):
        bot.account_states.pop(idx)
    accounts_data.pop(idx)

    save_accounts()
    bot._add_log("", "", f"🗑️ Аккаунт удалён: {name}", "info")
    return {"ok": True}


@app.post("/api/account/{idx}/apply_tests")
async def api_apply_tests(idx: int):
    """Переключить флаг apply_tests для аккаунта/сессии."""
    base = len(bot.account_states)
    if idx < base:
        state = bot.account_states[idx]
        state.apply_tests = not state.apply_tests
        accounts_data[idx]["apply_tests"] = state.apply_tests
        save_accounts()
        return {"ok": True, "apply_tests": state.apply_tests}
    ti = idx - base
    state = bot.temp_states.get(ti)
    if state:
        state.apply_tests = not state.apply_tests
        if 0 <= ti < len(bot.temp_sessions):
            bot.temp_sessions[ti]["apply_tests"] = state.apply_tests
            save_browser_sessions(bot.temp_sessions)
        return {"ok": True, "apply_tests": state.apply_tests}
    return {"ok": False, "error": "Аккаунт не найден"}


@app.get("/api/raw/config")
async def api_raw_config_get():
    """Вернуть текущий config как объект."""
    cfg = {k: getattr(CONFIG, k) for k in _CONFIG_KEYS}
    cfg["questionnaire_templates"] = CONFIG.questionnaire_templates
    cfg["letter_templates"] = CONFIG.letter_templates
    cfg["url_pool"] = CONFIG.url_pool
    return cfg


@app.post("/api/raw/config")
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


@app.get("/api/raw/accounts")
async def api_raw_accounts_get():
    """Вернуть accounts без значений cookies (только ключи)."""
    safe = []
    for acc in accounts_data:
        a = {k: v for k, v in acc.items() if k != "cookies"}
        a["cookies"] = {k: "***" for k in acc.get("cookies", {})}
        safe.append(a)
    return safe


@app.post("/api/raw/accounts")
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


@app.get("/api/account/{idx}/resume_text")
async def api_resume_text(idx: int):
    """Получить и вернуть текстовое представление резюме (для проверки)."""
    s = None
    if 0 <= idx < len(bot.account_states):
        s = bot.account_states[idx]
    else:
        temp_idx = idx - len(bot.account_states)
        s = bot.temp_states.get(temp_idx)
    if not s:
        return {"ok": False, "error": "Invalid idx"}
    # Force refresh (clear cache for this hash so we fetch fresh)
    rh = s.acc.get("resume_hash", "")
    _resume_cache.pop(rh, None)
    text = await asyncio.get_event_loop().run_in_executor(None, fetch_resume_text, s.acc)
    return {"ok": True, "resume_hash": rh, "length": len(text), "text": text}


@app.get("/api/account/{idx}/resume_views")
async def api_resume_views(idx: int):
    """История просмотров резюме для аккаунта"""
    s = None
    if 0 <= idx < len(bot.account_states):
        s = bot.account_states[idx]
    else:
        temp_idx = idx - len(bot.account_states)
        s = bot.temp_states.get(temp_idx)
    if s:
        # если кэш ещё пустой — фетчим прямо сейчас
        if not s.resume_view_history:
            loop = asyncio.get_event_loop()
            s.resume_view_history = await loop.run_in_executor(
                None, fetch_resume_view_history, s.acc, 100
            )
        if not s.resume_views_7d:
            loop = asyncio.get_event_loop()
            rs = await loop.run_in_executor(None, fetch_resume_stats, s.acc)
            s.resume_views_7d = rs["views"]
            s.resume_views_new = rs["views_new"]
            s.resume_shows_7d = rs["shows"]
            s.resume_invitations_7d = rs["invitations"]
            s.resume_invitations_new = rs["invitations_new"]
            s.resume_next_touch_seconds = rs["next_touch_seconds"]
            s.resume_free_touches = rs["free_touches"]
            s.resume_global_invitations = rs["global_invitations"]
            s.resume_new_invitations_total = rs["new_invitations_total"]
        return {
            "history": s.resume_view_history,
            "stats": {
                "views_7d": s.resume_views_7d,
                "views_new": s.resume_views_new,
                "shows_7d": s.resume_shows_7d,
                "invitations_7d": s.resume_invitations_7d,
                "invitations_new": s.resume_invitations_new,
                "global_invitations": s.resume_global_invitations,
                "new_invitations_total": s.resume_new_invitations_total,
                "next_touch_seconds": s.resume_next_touch_seconds,
                "free_touches": s.resume_free_touches,
            }
        }
    return {"error": "Invalid idx"}


def _analyze_resume(acc: dict, extra_terms: list = None) -> dict:
    """Аудит резюме — что видит HR, что не заполнено, рекомендации."""
    resume_hash = acc.get("resume_hash", "")
    if not resume_hash:
        return {"error": "Нет resume_hash"}
    cookies = acc.get("cookies", {})
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    try:
        # 1. Fetch resume page SSR
        r = requests.get(
            f"https://hh.ru/resume/{resume_hash}",
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies, verify=False, timeout=15,
        )
        if r.status_code != 200 or _is_login_page(r.text):
            return {"error": "auth_error"}
        ssr = parse_hh_lux_ssr(r.text)
        resume = None
        for key in ("applicantResume", "resume", "resumeView"):
            val = ssr.get(key)
            if isinstance(val, dict) and val and not val.get("forbidden"):
                resume = val
                break
        if not resume:
            return {"error": "Не удалось получить данные резюме"}

        attrs = resume.get("_attributes", {})
        field_statuses = resume.get("fieldStatuses", {})

        # Helper
        def _str(val):
            if isinstance(val, list) and val:
                v = val[0]
                if isinstance(v, dict):
                    return v.get("string", v.get("text", v.get("name", str(v))))
                return str(v)
            return ""

        # 2. Fetch stats from resumes page
        r2 = requests.get(
            "https://hh.ru/applicant/resumes",
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies, verify=False, timeout=15,
        )
        stats_data = {}
        if r2.status_code == 200:
            ssr2 = parse_hh_lux_ssr(r2.text)
            all_stats = ssr2.get("applicantResumesStatistics", {}).get("resumes", {})
            rid = str(attrs.get("id", ""))
            stats_data = all_stats.get(rid, {}).get("statistics", {})

        # 3. Build analysis
        job_status_raw = resume.get("jobSearchStatus", [])
        job_status = ""
        if job_status_raw and isinstance(job_status_raw, list):
            job_status = (job_status_raw[0].get("jobSearchStatus", {}).get("name", "") if isinstance(job_status_raw[0], dict) else "")

        title = _str(resume.get("title"))
        roles = [r.get("text", "") for r in resume.get("professionalRole", []) if isinstance(r, dict)]
        skills = [s.get("string", "") or s.get("name", "") for s in resume.get("keySkills", []) if isinstance(s, dict)]
        salary = resume.get("salary", [])
        salary_str = ""
        if salary and isinstance(salary[0], dict):
            amt = salary[0].get("amount") or salary[0].get("string")
            salary_str = str(amt) if amt else ""

        work_schedule = [_str([s]) for s in resume.get("workSchedule", [])]
        work_formats = [_str([s]) for s in resume.get("workFormats", [])]
        employment = [_str([s]) for s in resume.get("employment", [])]

        # Green fields = fields that could improve but not filled
        green = field_statuses.get("greenFields", [])
        red = field_statuses.get("redFields", [])

        # Recommendations
        issues = []
        if job_status == "not_looking_for_job":
            issues.append({"level": "critical", "text": "Статус «Не ищу работу» — HR пропускают такие резюме", "fix": "Поменять на «Активно ищу» или «Рассматриваю предложения»"})
        elif job_status == "looking_for_offers":
            issues.append({"level": "info", "text": "Статус «Рассматриваю предложения» — OK, но «Активно ищу» даёт больше показов"})

        percent = attrs.get("percent", 0)
        if percent and percent < 80:
            issues.append({"level": "high", "text": f"Заполненность {percent}% — HR видят это, нужно 80%+", "fix": "Заполнить пустые поля"})

        status = attrs.get("status", "")
        if status != "published":
            issues.append({"level": "critical", "text": f"Статус резюме: {status} (не опубликовано)", "fix": "Опубликовать резюме на hh.ru"})

        if not attrs.get("canPublishOrUpdate"):
            issues.append({"level": "high", "text": "Резюме нельзя обновить/опубликовать", "fix": "Проверить ограничения в настройках hh.ru"})

        if "photo" in green:
            issues.append({"level": "high", "text": "Нет фото — HR часто фильтруют «только с фото»", "fix": "Добавить деловое фото"})

        if "salary" in green or not salary_str:
            issues.append({"level": "medium", "text": "Не указана зарплата — HR не может фильтровать по ожиданиям", "fix": "Указать желаемую зарплату"})

        if "skills" in green:
            issues.append({"level": "medium", "text": "Не заполнено «О себе» — теряете очки в поиске", "fix": "Написать 3-5 предложений о себе"})

        if "email" in green:
            issues.append({"level": "low", "text": "Не указан email", "fix": "Добавить email для связи"})

        if "recommendation" in green:
            issues.append({"level": "low", "text": "Нет рекомендаций", "fix": "Попросить коллегу/руководителя оставить рекомендацию"})

        if "certificate" in green:
            issues.append({"level": "low", "text": "Нет сертификатов", "fix": "Добавить профильные сертификаты (курсы, экзамены)"})

        search_shows = (stats_data.get("searchShows") or {}).get("count", 0)
        views = (stats_data.get("views") or {}).get("count", 0)
        views_new = (stats_data.get("views") or {}).get("countNew", 0)
        invitations = (stats_data.get("invitations") or {}).get("count", 0)
        inv_new = (stats_data.get("invitations") or {}).get("countNew", 0)

        if search_shows < 5:
            issues.append({"level": "high", "text": f"Только {search_shows} показов в поиске за 7 дней — резюме плохо ранжируется", "fix": "Обновить резюме, поменять статус на «Активно ищу», поднять резюме"})

        _SCHEDULE_MAP = {"full_day": "Полный день", "remote": "Удалённая", "flexible": "Гибкий", "shift": "Сменный", "flyInFlyOut": "Вахта"}
        _FORMAT_MAP = {"ON_SITE": "Офис", "REMOTE": "Удалёнка", "HYBRID": "Гибрид", "FIELD_WORK": "Разъезды"}
        _EMPLOY_MAP = {"full": "Полная", "part": "Частичная", "project": "Проектная", "volunteer": "Волонтёрство", "probation": "Стажировка"}
        _STATUS_MAP = {
            "not_looking_for_job": "Не ищу работу",
            "looking_for_offers": "Рассматриваю предложения",
            "actively_searching": "Активно ищу работу",
            "has_job_offer": "Есть оффер",
            "accepted_job_offer": "Принял оффер",
        }

        # 4. Market analysis — competitor data and vacancy supply/demand
        market = {}
        try:
            # Use first role or first part of title for search
            search_term = (roles[0] if roles else title.split(",")[0].strip()) if title else ""
            encoded_title = urllib.parse.quote(search_term)
            # Fetch resume clusters (competitor counts)
            r_clusters = requests.get(
                f"https://hh.ru/shards/search/resume/clusters?text={encoded_title}&area=1",
                headers={"User-Agent": ua, "Accept": "application/json"},
                cookies=cookies, verify=False, timeout=10,
            )
            clusters_data = {}
            active_seekers = 0
            experience_dist = []
            top_competitor_skills = []
            if r_clusters.status_code == 200:
                try:
                    cj = r_clusters.json()
                    clusters = cj.get("clusters", {})
                    # clusters is a dict: {"experience": {"groups": {"noExperience": {"count": N, "title": "..."}, ...}}, ...}
                    exp_groups = clusters.get("experience", {}).get("groups", {})
                    for gid, gdata in exp_groups.items():
                        experience_dist.append({
                            "id": gid,
                            "name": gdata.get("title", gid),
                            "count": gdata.get("count", 0),
                        })
                    # Active seekers from job_search_status cluster
                    js_groups = clusters.get("job_search_status", {}).get("groups", {})
                    for gid, gdata in js_groups.items():
                        if "актив" in (gdata.get("title", "")).lower() or gid == "active_search":
                            active_seekers = gdata.get("count", 0)
                            break
                    # Top skills
                    skill_groups = clusters.get("skill", {}).get("groups", {})
                    sorted_skills = sorted(skill_groups.items(), key=lambda x: x[1].get("count", 0), reverse=True)
                    for gid, gdata in sorted_skills[:20]:
                        top_competitor_skills.append({
                            "name": gdata.get("title", gid),
                            "count": gdata.get("count", 0),
                        })
                except (json.JSONDecodeError, KeyError):
                    pass

            # Fetch vacancy count for this title
            vacancy_count = 0
            try:
                r_vac = requests.get(
                    f"https://hh.ru/search/vacancy?text={encoded_title}&area=1",
                    headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
                    cookies=cookies, verify=False, timeout=10,
                )
                if r_vac.status_code == 200:
                    vac_ssr = parse_hh_lux_ssr(r_vac.text)
                    # Try multiple SSR keys
                    sc = vac_ssr.get("searchCounts", {})
                    if isinstance(sc, dict) and sc.get("value"):
                        vacancy_count = int(sc["value"])
                    if not vacancy_count:
                        for ssr_key in ("totalVacancies", "vacancyCount"):
                            v = vac_ssr.get(ssr_key)
                            if v:
                                vacancy_count = int(v)
                                break
                    if not vacancy_count:
                        vsr = vac_ssr.get("vacancySearchResult", {})
                        vacancy_count = int(vsr.get("found", 0) or vsr.get("total", 0) or 0)
                    if not vacancy_count:
                        # Fallback: parse from HTML
                        for pattern in [
                            r'data-qa="vacancies-total-found"[^>]*>\s*([\d\s\xa0]+)',
                            r'"found"\s*:\s*(\d+)',
                            r'Найден\w*\s+([\d\s\xa0]+)\s+ваканс',
                        ]:
                            m_found = re.search(pattern, r_vac.text)
                            if m_found:
                                vacancy_count = int(m_found.group(1).replace(" ", "").replace("\xa0", ""))
                                break
            except Exception:
                pass

            supply_demand = round(active_seekers / vacancy_count, 2) if vacancy_count > 0 else 0

            market = {
                "search_term": search_term,
                "vacancy_count": vacancy_count,
                "active_seekers": active_seekers,
                "supply_demand_ratio": supply_demand,
                "experience_distribution": experience_dist,
                "top_competitor_skills": top_competitor_skills,
            }
        except Exception as e:
            log_debug(f"_analyze_resume market analysis error: {e}")

        # 5. Field weight analysis — exact recipe for 100%
        weight_analysis = []
        _FIELD_LABELS = {
            "experience": "Опыт работы", "keySkills": "Ключевые навыки", "advancedKeySkills": "Навыки (расширенные)",
            "primaryEducation": "Образование", "title": "Должность", "professionalRole": "Проф. роль",
            "area": "Город", "phone": "Телефон", "language": "Языки", "firstName": "Имя", "lastName": "Фамилия",
            "photo": "Фото", "salary": "Зарплата", "email": "Email", "recommendation": "Рекомендации",
            "personalSite": "Сайт/портфолио", "additionalEducation": "Курсы", "attestationEducation": "Экзамены",
            "middleName": "Отчество", "portfolio": "Портфолио", "metro": "Метро", "skills": "О себе",
            "certificate": "Сертификаты", "driverLicenseTypes": "Водительские права", "hasVehicle": "Автомобиль",
        }
        conds = resume.get("_conditions", {})
        total_weight = 0
        filled_weight = 0
        for field, info in conds.items():
            if not isinstance(info, dict):
                continue
            w = info.get("weight", 0)
            st = info.get("status", "")
            if w > 0:
                total_weight += w
                is_filled = st not in ("green", "inactive", "")  # green = optional unfilled, ok = filled
                if is_filled:
                    filled_weight += w
                weight_analysis.append({
                    "field": field,
                    "label": _FIELD_LABELS.get(field, field),
                    "weight": w,
                    "filled": is_filled,
                    "status": st,
                })
        # Sort: unfilled high-weight first
        weight_analysis.sort(key=lambda x: (-1 if not x["filled"] else 1, -x["weight"]))

        # 6. Supply/demand across multiple search terms
        supply_demand_comparison = []
        try:
            terms = set()
            # Title parts
            if title:
                for part in title.split(","):
                    t = part.strip()
                    if t and len(t) > 3:
                        terms.add(t)
            # Roles
            for r in roles:
                if r:
                    terms.add(r)
            # Common QA variants
            if any("тестир" in t.lower() or "qa" in t.lower() for t in terms):
                terms.update(["автоматизация тестирования", "QA engineer"])
            # User-provided extra terms
            if extra_terms:
                terms.update(extra_terms)

            for term in list(terms)[:10]:
                try:
                    enc = urllib.parse.quote(term)
                    r_vc = requests.get(
                        f"https://hh.ru/search/vacancy?text={enc}&area=1",
                        headers={"User-Agent": ua, "Accept": "text/html"},
                        cookies=cookies, verify=False, timeout=10,
                    )
                    vc = 0
                    if r_vc.status_code == 200:
                        vc_ssr = parse_hh_lux_ssr(r_vc.text)
                        sc = vc_ssr.get("searchCounts", {})
                        if isinstance(sc, dict) and sc.get("value"):
                            vc = int(sc["value"])
                        if not vc:
                            m_f = re.search(r'"found"\s*:\s*(\d+)', r_vc.text[:5000])
                            if m_f:
                                vc = int(m_f.group(1))
                    supply_demand_comparison.append({
                        "term": term,
                        "vacancies": vc,
                        "ratio": round(active_seekers / vc, 1) if vc > 0 else 0,
                    })
                except Exception:
                    pass
            supply_demand_comparison.sort(key=lambda x: x.get("ratio") or 9999)
        except Exception as e:
            log_debug(f"_analyze_resume supply/demand comparison error: {e}")

        # 7. HR activity analysis — applicantEmployerManagersActivity from negotiations SSR
        hr_activity = {"active_count": 0, "slow_count": 0, "dead_count": 0}
        try:
            r_neg = requests.get(
                "https://hh.ru/applicant/negotiations",
                headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
                cookies=cookies, verify=False, timeout=15,
            )
            if r_neg.status_code == 200 and not _is_login_page(r_neg.text):
                neg_ssr = parse_hh_lux_ssr(r_neg.text)
                managers = neg_ssr.get("applicantEmployerManagersActivity", [])
                if isinstance(managers, list):
                    for mgr in managers:
                        if not isinstance(mgr, dict):
                            continue
                        inactive_min = mgr.get("@inactiveMinutes", mgr.get("inactiveMinutes", 0))
                        if not isinstance(inactive_min, (int, float)):
                            try:
                                inactive_min = int(inactive_min)
                            except (ValueError, TypeError):
                                inactive_min = 0
                        inactive_days = inactive_min / 1440.0
                        if inactive_days < 3:
                            hr_activity["active_count"] += 1
                        elif inactive_days <= 7:
                            hr_activity["slow_count"] += 1
                        else:
                            hr_activity["dead_count"] += 1
        except Exception as e:
            log_debug(f"_analyze_resume hr_activity error: {e}")

        return {
            "ok": True,
            "name": f"{_str(resume.get('firstName'))} {_str(resume.get('lastName'))}".strip(),
            "title": title,
            "roles": roles,
            "skills": skills[:25],
            "total_experience_months": int(_str(resume.get("totalExperience")) or 0),
            "education_level": _str(resume.get("educationLevel")),
            "percent": percent,
            "status": status,
            "job_search_status": job_status,
            "job_search_status_label": _STATUS_MAP.get(job_status, job_status),
            "salary": salary_str,
            "work_schedule": [_SCHEDULE_MAP.get(s, s) for s in work_schedule],
            "work_formats": [_FORMAT_MAP.get(s, s) for s in work_formats],
            "employment": [_EMPLOY_MAP.get(s, s) for s in employment],
            "has_photo": "photo" not in green,
            "area": _str(resume.get("area")),
            "stats_7d": {
                "search_shows": search_shows,
                "views": views,
                "views_new": views_new,
                "invitations": invitations,
                "invitations_new": inv_new,
            },
            "issues": issues,
            "green_fields": green,
            "market": market,
            "weight_analysis": weight_analysis,
            "filled_weight": filled_weight,
            "total_weight": total_weight,
            "supply_demand_comparison": supply_demand_comparison,
            "hr_activity": hr_activity,
        }
    except Exception as e:
        log_debug(f"_analyze_resume error: {e}")
        return {"error": str(e)}


@app.post("/api/account/{idx}/oauth_token")
async def api_oauth_token(idx: int):
    """Get/refresh OAuth token for account."""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"ok": False, "error": "Invalid idx"}
    loop = asyncio.get_event_loop()
    token = await loop.run_in_executor(None, _obtain_oauth_token, acc)
    if token:
        rh = acc.get("resume_hash", "")
        with _oauth_lock:
            info = _oauth_tokens.get(rh, {})
        return {
            "ok": True,
            "token_prefix": token[:20] + "...",
            "expires_in": int(info.get("expires_at", 0) - time.time()),
            "has_refresh": bool(info.get("refresh_token")),
        }
    return {"ok": False, "error": "Failed to obtain token"}


@app.get("/api/account/{idx}/oauth_status")
async def api_oauth_status(idx: int):
    """Check OAuth token status."""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"error": "Invalid idx"}
    rh = acc.get("resume_hash", "")
    with _oauth_lock:
        info = _oauth_tokens.get(rh, {})
    if info:
        remaining = int(info.get("expires_at", 0) - time.time())
        return {
            "has_token": True,
            "token_prefix": info.get("access_token", "")[:20] + "...",
            "expires_in_hours": round(remaining / 3600, 1),
            "has_refresh": bool(info.get("refresh_token")),
        }
    return {"has_token": False}


@app.post("/api/account/{idx}/oauth_touch")
async def api_oauth_touch(idx: int):
    """Touch/publish resume via OAuth API (no captcha)."""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"ok": False, "error": "Invalid idx"}
    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(None, _oauth_touch_resume, acc)
    return {"ok": ok, "message": msg}


@app.get("/api/account/{idx}/test_llm_questionnaire")
async def api_test_llm_questionnaire(idx: int, vacancy_id: str = ""):
    """Test LLM questionnaire answering without submitting."""
    if not vacancy_id:
        return {"error": "vacancy_id required"}
    acc = bot._get_apply_acc(idx)
    if not acc:
        return {"error": "Invalid idx"}
    def _do():
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        r = requests.get(
            f"https://hh.ru/applicant/vacancy_response?vacancyId={vacancy_id}&withoutTest=no",
            headers={"User-Agent": ua, "Accept": "text/html"},
            cookies=acc.get("cookies", {}), verify=False, timeout=15)
        rich = _parse_questionnaire_rich(r.text)
        resume_text = fetch_resume_text(acc) if CONFIG.llm_use_resume else ""
        answers = generate_llm_questionnaire_answers(rich, f"Vacancy {vacancy_id}", "", resume_text)
        result = []
        for q in rich:
            llm_ans = answers.get(q["field"], "")
            result.append({
                "field": q["field"], "type": q["type"], "text": q["text"],
                "options": q.get("options", []),
                "llm_answer": llm_ans,
                "template_answer": get_questionnaire_answer(q["text"]),
            })
        return {"questions": result, "llm_answered": len(answers), "total": len(rich),
                "llm_enabled": CONFIG.llm_enabled, "profiles": len(CONFIG.llm_profiles or [])}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _do)


@app.get("/api/account/{idx}/resume_audit")
async def api_resume_audit(idx: int, extra_terms: str = ""):
    """Аудит резюме — анализ видимости для HR."""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"error": "Invalid idx"}
    extra = [t.strip() for t in (extra_terms or "").split(",") if t.strip()] if extra_terms else []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _analyze_resume, acc, extra)


@app.get("/api/account/{idx}/hot_leads")
async def api_hot_leads(idx: int):
    """Possible job offers — горячие лиды, работодатели готовые пригласить."""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"error": "Invalid idx"}
    try:
        r = requests.get(
            "https://hh.ru/shards/applicant/negotiations/possible_job_offers",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "X-Xsrftoken": acc.get("cookies", {}).get("_xsrf", ""),
                "Referer": "https://hh.ru/applicant/negotiations",
            },
            cookies=acc.get("cookies", {}), verify=False, timeout=15,
        )
        if r.status_code != 200:
            return {"offers": [], "error": f"HTTP {r.status_code}"}
        d = r.json()
        offers = []
        for o in d.get("possibleJobOffers", []):
            offers.append({
                "employer": o.get("name", "?"),
                "employer_id": o.get("employerId"),
                "vacancies": o.get("vacancyNames", []),
                "vacancy_id": o.get("vacancyId", ""),
                "has_invitation": o.get("hasInvitationTopic", False),
                "topic_ids": o.get("topicIds", []),
            })
        return {"offers": offers, "total": len(offers)}
    except Exception as e:
        return {"offers": [], "error": str(e)}


@app.get("/api/hr_contacts")
async def api_hr_contacts():
    """Return collected HR contact info from vacancy pre-checks."""
    return {"contacts": list(bot.hr_contacts), "total": len(bot.hr_contacts)}


@app.get("/api/account/{idx}/remindable")
async def api_remindable(idx: int):
    """Return negotiations where responseReminderState.allowed is True (can send reminder)."""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"error": "Invalid idx"}
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    try:
        r = requests.get(
            "https://hh.ru/applicant/negotiations",
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
            cookies=acc.get("cookies", {}), verify=False, timeout=15,
        )
        if r.status_code != 200 or _is_login_page(r.text):
            return {"error": "auth_error", "remindable": []}
        ssr = parse_hh_lux_ssr(r.text)
        topic_list = ssr.get("topicList", [])
        remindable = []
        for topic in (topic_list if isinstance(topic_list, list) else []):
            if not isinstance(topic, dict):
                continue
            rrs = topic.get("responseReminderState", {})
            if isinstance(rrs, dict) and rrs.get("allowed"):
                # Extract employer / vacancy info
                employer = ""
                vacancy = ""
                chat_id = topic.get("chatId", "")
                topic_id = topic.get("topicId", "")
                # Try to get display info
                v_info = topic.get("vacancy", {})
                if isinstance(v_info, dict):
                    vacancy = v_info.get("name", "")
                    emp = v_info.get("company", v_info.get("employer", {}))
                    if isinstance(emp, dict):
                        employer = emp.get("name", "")
                    elif isinstance(emp, str):
                        employer = emp
                remindable.append({
                    "chat_id": str(chat_id),
                    "topic_id": str(topic_id),
                    "employer": employer,
                    "vacancy": vacancy,
                })
        return {"remindable": remindable, "total": len(remindable)}
    except Exception as e:
        return {"error": str(e), "remindable": []}


@app.post("/api/account/{idx}/clone_resume")
async def api_clone_resume(idx: int, request: Request):
    """Clone resume and optionally set title. Body: {title?: "new title"}"""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"ok": False, "error": "Invalid idx"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_title = body.get("title", "")
    resume_hash = acc.get("resume_hash", "")
    if not resume_hash:
        return {"ok": False, "error": "No resume_hash"}
    xsrf = acc.get("cookies", {}).get("_xsrf", "")
    try:
        r = requests.post(
            "https://hh.ru/applicant/resumes/clone",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "X-Xsrftoken": xsrf,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://hh.ru",
                "Referer": "https://hh.ru/applicant/resumes",
            },
            cookies=acc.get("cookies", {}),
            data=f"resume={resume_hash}&_xsrf={xsrf}",
            verify=False, timeout=15,
        )
        if r.status_code == 200:
            d = r.json()
            new_url = d.get("url", "")
            # Extract new hash from URL like /profile/resume/?resume=HASH
            new_hash = ""
            m = re.search(r'resume=([a-f0-9]+)', new_url)
            if m:
                new_hash = m.group(1)
            if not new_hash:
                return {"ok": True, "new_hash": "", "message": "Склонировано, но hash не получен"}

            # Read original resume to copy all fields
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            r_orig = requests.get(f"https://hh.ru/resume/{resume_hash}",
                headers={"User-Agent": ua, "Accept": "text/html"},
                cookies=acc.get("cookies", {}), verify=False, timeout=15)
            orig_data = {}
            m_ssr = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>([\s\S]*?)</template>', r_orig.text)
            if m_ssr:
                orig_data = json.loads(m_ssr.group(1)).get("applicantResume", {})

            # Build fields to copy
            fields = {}
            if new_title:
                fields["title"] = [{"string": new_title}]
            for copy_field in ("experience", "primaryEducation", "skills", "employment",
                               "workSchedule", "workFormats", "businessTripReadiness",
                               "relocation", "travelTime"):
                val = orig_data.get(copy_field, [])
                if val:
                    fields[copy_field] = val
            # Add salary + remote if not in original
            if not orig_data.get("salary"):
                fields["salary"] = [{"amount": 100000, "currency": "RUR"}]
            if not any(s.get("string") == "remote" for s in orig_data.get("workSchedule", [])):
                ws = list(orig_data.get("workSchedule", []))
                ws.append({"string": "remote"})
                fields["workSchedule"] = ws
            if not any(s.get("string") == "REMOTE" for s in orig_data.get("workFormats", [])):
                wf = list(orig_data.get("workFormats", []))
                wf.append({"string": "REMOTE"})
                fields["workFormats"] = wf

            # Apply all fields to clone
            edited_count = 0
            for field_name, field_data in fields.items():
                res = _edit_resume_field(acc, new_hash, {field_name: field_data})
                if res.get("ok"):
                    edited_count += 1

            return {
                "ok": True,
                "new_hash": new_hash,
                "edit_url": f"https://hh.ru/resume/edit/{new_hash}/position",
                "fields_copied": edited_count,
                "message": f"Полный клон создан! {edited_count} полей скопировано. Осталось опубликовать на hh.ru",
            }
        else:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _edit_resume_field(acc: dict, resume_hash: str, fields: dict) -> dict:
    """Edit resume fields via POST /applicant/resume/edit. Returns {ok, error}."""
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    xsrf = acc.get("cookies", {}).get("_xsrf", "")
    try:
        # Warm up session (DDoS Guard needs a GET first)
        s = requests.Session()
        s.verify = False
        try:
            s.get("https://hh.ru/applicant/resumes", headers={"User-Agent": ua, "Accept": "text/html"},
                  cookies=acc.get("cookies", {}), timeout=10)
            # POST edit
            r = s.post(
            f"https://hh.ru/applicant/resume/edit?resume={resume_hash}&hhtmSource=resume_partial_edit",
            headers={
                "User-Agent": ua,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Xsrftoken": xsrf,
                "Origin": "https://hh.ru",
                "Referer": f"https://hh.ru/resume/edit/{resume_hash}/position",
            },
            cookies=acc.get("cookies", {}),
            json=fields,
            timeout=15,
        )
            if r.status_code in (200, 204):
                return {"ok": True}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        finally:
            s.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/account/{idx}/edit_resume")
async def api_edit_resume(idx: int, request: Request):
    """Edit resume fields via API. Body: {resume_hash, title, salary, skills, professionalRole}"""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"ok": False, "error": "Invalid idx"}
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}

    resume_hash = body.get("resume_hash") or acc.get("resume_hash", "")
    if not resume_hash:
        return {"ok": False, "error": "No resume_hash"}

    # Build fields dict in HH format
    fields = {}
    if "title" in body and body["title"]:
        fields["title"] = [{"string": body["title"]}]
    if "salary" in body:
        try:
            sal = int(body["salary"])
            if sal > 0:
                fields["salary"] = [{"amount": sal, "currency": body.get("currency", "RUR")}]
            else:
                fields["salary"] = []
        except (ValueError, TypeError):
            pass
    if "skills" in body and body["skills"]:
        fields["skills"] = [{"string": body["skills"]}]
    if "professionalRole" in body:
        try:
            fields["professionalRole"] = [{"string": int(body["professionalRole"])}]
        except (ValueError, TypeError):
            pass

    if not fields:
        return {"ok": False, "error": "No fields to update"}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _edit_resume_field, acc, resume_hash, fields)
    return result


@app.get("/api/account/{idx}/all_resumes")
async def api_all_resumes(idx: int):
    """List all resumes for this account (including clones). Uses HTML page for full data."""
    acc = bot._get_apply_acc(idx)
    if acc is None:
        return {"error": "Invalid idx"}
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    try:
        # Use HTML page — shards API returns truncated data for clones
        r = requests.get(
            "https://hh.ru/applicant/resumes",
            headers={"User-Agent": ua, "Accept": "text/html", "Referer": "https://hh.ru/"},
            cookies=acc.get("cookies", {}), verify=False, timeout=15,
        )
        if r.status_code != 200:
            return {"resumes": [], "error": f"HTTP {r.status_code}"}
        ssr = parse_hh_lux_ssr(r.text)
        ssr_resumes = ssr.get("applicantResumes", [])
        stats = ssr.get("applicantResumesStatistics", {}).get("resumes", {})

        resumes = []
        for res in ssr_resumes:
            attrs = res.get("_attributes", {})
            rid = str(attrs.get("id", ""))
            rhash = attrs.get("hash", "")
            title_list = res.get("title", [])
            title = title_list[0].get("string", "") if title_list and isinstance(title_list[0], dict) else ""
            # Get percent from full page if shards returns 0
            percent = attrs.get("percent", 0)
            # Stats
            rs = stats.get(rid, {}).get("statistics", {})
            resumes.append({
                "hash": rhash,
                "title": title or "(без заголовка)",
                "status": attrs.get("status", ""),
                "percent": percent,
                "is_searchable": attrs.get("isSearchable", False),
                "can_publish": attrs.get("canPublishOrUpdate", False),
                "updated": attrs.get("updated"),
                "skills_count": len(res.get("keySkills", [])),
                "experience_count": len(res.get("experience", [])),
                "views_7d": (rs.get("views") or {}).get("count", 0),
                "shows_7d": (rs.get("searchShows") or {}).get("count", 0),
                "edit_url": f"https://hh.ru/resume/edit/{rhash}/position",
            })
        return {"resumes": resumes, "total": len(resumes)}
    except Exception as e:
        return {"resumes": [], "error": str(e)}


async def _fetch_questionnaire_data(acc: dict, vid: str) -> dict:
    """
    Получает форму опросника и возвращает список вопросов с полями.
    НЕ отправляет отклик.
    """
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": f"https://hh.ru/vacancy/{vid}",
    }
    url_form = f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}&withoutTest=no"
    async with aiohttp.ClientSession(cookies=acc["cookies"], connector=connector, headers=headers) as session:
        async with session.get(url_form, timeout=aiohttp.ClientTimeout(total=15)) as r:
            html = await r.text()
            if r.status in (401, 403) or _is_login_page(html):
                return {"questions": [], "hidden": {}, "error": "auth"}

    hidden = dict(re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html))
    hidden.update(dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]+type="hidden"[^>]+value="([^"]*)"', html)))

    # Тексты вопросов
    q_blocks = re.findall(
        r'data-qa="task-question">(.*?)(?=data-qa="task-question"|</(?:div|section|form)>)',
        html, re.DOTALL
    )
    q_texts = []
    for b in q_blocks:
        c = re.sub(r'<[^>]+>', ' ', b)
        c = re.sub(r'&quot;', '"', re.sub(r'&ndash;', '–', re.sub(r'&nbsp;', ' ', c)))
        c = re.sub(r'\s+', ' ', c).strip()
        q_texts.append(c)

    questions = []
    q_idx = 0

    # Textarea
    for name in re.findall(r'<textarea[^>]+name="(task_\d+_text)"', html):
        q_text = q_texts[q_idx] if q_idx < len(q_texts) else ""
        suggested = get_questionnaire_answer(q_text)
        questions.append({"field": name, "type": "textarea", "text": q_text,
                          "options": [], "suggested": suggested})
        q_idx += 1

    # Radio
    radio_groups: dict = {}
    radio_order: list = []
    for inp in re.findall(r'<input[^>]+type="radio"[^>]+>', html, re.I):
        nm = re.search(r'name="([^"]+)"', inp)
        vl = re.search(r'value="([^"]+)"', inp)
        if nm and vl and re.match(r'task_\d+', nm.group(1)):
            n, v = nm.group(1), vl.group(1)
            if n not in radio_groups:
                radio_groups[n] = []
                radio_order.append(n)
            radio_groups[n].append(v)

    # Labels для radio — ищем label рядом с каждым input
    label_map: dict = {}
    for inp_with_id in re.findall(r'<input[^>]+type="radio"[^>]+id="([^"]+)"[^>]*>', html, re.I):
        label_m = re.search(rf'<label[^>]+for="{re.escape(inp_with_id)}"[^>]*>(.*?)</label>', html, re.DOTALL)
        if label_m:
            lbl = re.sub(r'<[^>]+>', '', label_m.group(1)).strip()
            label_map[inp_with_id] = lbl
    # Fallback: по порядку "да"/"нет" если labels не найдены
    default_labels = ["да", "нет"]

    for name in radio_order:
        vals = radio_groups[name]
        q_text = q_texts[q_idx] if q_idx < len(q_texts) else ""
        options = []
        for i, v in enumerate(vals):
            lbl = label_map.get(v, default_labels[i] if i < len(default_labels) else v)
            options.append({"value": v, "label": lbl})
        if not vals:
            q_idx += 1
            continue
        tmpl = get_questionnaire_answer(q_text).lower()
        chosen = vals[0]
        if any(w in tmpl for w in ("нет", "no", "не готов", "не готова", "не могу")):
            chosen = vals[1] if len(vals) > 1 else vals[0]
        questions.append({"field": name, "type": "radio", "text": q_text,
                          "options": options, "suggested": chosen})
        q_idx += 1

    # Checkbox
    checkbox_groups: dict = {}
    checkbox_order: list = []
    for inp in re.findall(r'<input[^>]+type="checkbox"[^>]+>', html, re.I):
        nm = re.search(r'name="([^"]+)"', inp)
        vl = re.search(r'value="([^"]+)"', inp)
        if nm and vl and re.match(r'task_\d+', nm.group(1)):
            n, v = nm.group(1), vl.group(1)
            if n not in checkbox_groups:
                checkbox_groups[n] = []
                checkbox_order.append(n)
            checkbox_groups[n].append(v)
    for name in checkbox_order:
        if name in radio_groups:
            continue  # skip if already handled as radio
        vals = checkbox_groups[name]
        q_text = q_texts[q_idx] if q_idx < len(q_texts) else ""
        q_idx += 1
        options = [{"value": v, "label": v} for v in vals]
        questions.append({"field": name, "type": "checkbox", "text": q_text,
                          "options": options, "suggested": vals[0] if vals else ""})

    return {"questions": questions, "hidden": hidden, "url_form": url_form}


# Auth cookies нужные для откликов (без трекинговых с + и =)
_AUTH_COOKIE_KEYS = {
    "hhtoken", "_xsrf", "hhul", "crypted_id", "iap.uid",
    "hhrole", "regions", "GMT", "hhuid", "crypted_hhuid",
}


def _parse_cookies_str(raw: str) -> tuple:
    """
    Парсит cURL-запрос, 'Cookie: ...' или просто 'key=val; key2=val2'.
    Возвращает (cookies_dict, raw_cookie_line).
    """
    raw = raw.strip()
    raw_line = ""

    # Decode Unicode escapes (\u0021 → !, etc.)
    raw = raw.encode().decode('unicode_escape', errors='replace') if '\\u00' in raw else raw

    if raw.startswith("curl "):
        # cURL: ищем -H 'cookie: ...' или -H "cookie: ..."  (multiline OK)
        m = re.search(r"-H\s+['\"](?:C|c)ookie:\s*([^'\"]+)['\"]", raw, re.DOTALL)
        if not m:
            # Chrome uses -b flag: -b $'key=val; ...' or -b 'key=val; ...'
            m = re.search(r"-b\s+\$?['\"]([^'\"]+)['\"]", raw, re.DOTALL)
        if not m:
            # Try --cookie flag
            m = re.search(r"--cookie\s+['\"]([^'\"]+)['\"]", raw, re.DOTALL)
        if m:
            raw_line = m.group(1).strip()
        else:
            return {}, ""
    elif raw.lower().startswith("cookie:"):
        raw_line = raw[7:].strip()
    else:
        raw_line = raw

    cookies: dict = {}
    for part in raw_line.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    return cookies, raw_line


def _validate_and_profile(raw_cookie_line: str) -> dict:
    """
    Синхронно проверяет сессию и вытаскивает профиль из SSR.
    Передаёт Cookie как сырую строку заголовка — без перекодирования requests.
    Возвращает {"ok": bool, "name": str, "resume_hash": str, "error": str}.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://hh.ru/",
        "Cookie": raw_cookie_line,  # сырая строка, без URL-кодирования
    }
    try:
        r = requests.get(
            "https://hh.ru/applicant/resumes",
            headers=headers,
            verify=False,
            timeout=15,
            allow_redirects=True,
        )
    except Exception as e:
        return {"ok": False, "error": f"Ошибка сети: {e}"}

    if r.status_code != 200:
        hint = " — возможно, сессия устарела или нужно войти заново" if r.status_code in (401, 403) else ""
        return {"ok": False, "error": f"Сессия невалидна: HTTP {r.status_code}{hint}"}

    ssr = parse_hh_lux_ssr(r.text)

    # Имя — account.firstName + account.lastName
    name = ""
    for path in [
        lambda s: f"{s.get('account',{}).get('firstName','')} {s.get('account',{}).get('lastName','')}".strip(),
        lambda s: f"{s.get('hhidAccount',{}).get('firstName','')} {s.get('hhidAccount',{}).get('lastName','')}".strip(),
        lambda s: s.get("currentUser", {}).get("fullName", ""),
    ]:
        try:
            v = path(ssr)
            if v:
                name = v
                break
        except Exception:
            pass

    # Все резюме из списка applicantResumes
    all_resumes = []
    for res in ssr.get("applicantResumes", []):
        h = (
            res.get("_attributes", {}).get("hash", "") or
            res.get("resume", {}).get("hash", "") or ""
        )
        title = (
            res.get("_attributes", {}).get("title", "") or
            res.get("title", "") or
            res.get("resume", {}).get("title", "") or ""
        )
        if h:
            all_resumes.append({"hash": h, "title": title or "Резюме"})

    # latestResumeHash как приоритетный выбор
    latest = ssr.get("latestResumeHash", "")
    if latest:
        resume_hash = latest
        # Убеждаемся что latestResumeHash есть в списке
        if not any(r["hash"] == latest for r in all_resumes):
            all_resumes.insert(0, {"hash": latest, "title": "Резюме"})
    elif all_resumes:
        resume_hash = all_resumes[0]["hash"]
    else:
        resume_hash = ""

    return {"ok": True, "name": name or "Браузер", "resume_hash": resume_hash, "all_resumes": all_resumes}


@app.post("/api/session/add")
async def api_session_add(body: dict):
    """Добавить временную сессию из браузера по строке cookies."""
    cookie_str = body.get("cookies", "").strip()
    if not cookie_str:
        return {"status": "error", "message": "Строка cookies пустая"}

    cookies, raw_cookie_line = _parse_cookies_str(cookie_str)

    if not cookies or not raw_cookie_line:
        return {"status": "error", "message": "Не удалось распознать cookies — вставьте cURL целиком или строку Cookie: ..."}
    if "hhtoken" not in cookies:
        return {"status": "error", "message": "Не найден hhtoken — вставьте полный cURL (правая кнопка на запросе → Copy as cURL)"}
    if "_xsrf" not in cookies:
        return {"status": "error", "message": "Не найден _xsrf"}

    loop = asyncio.get_event_loop()
    profile = await loop.run_in_executor(None, _validate_and_profile, raw_cookie_line)

    if not profile["ok"]:
        return {"status": "error", "message": profile["error"]}

    # Имя: из формы → из SSR → "Браузер"
    display_name = body.get("name", "").strip() or profile["name"]
    all_resumes = profile.get("all_resumes", [])

    # Пользователь может передать resume_hash при множественном выборе
    selected_hash = body.get("resume_hash", "").strip()
    if selected_hash and any(r["hash"] == selected_hash for r in all_resumes):
        resume_hash = selected_hash
    else:
        resume_hash = profile["resume_hash"]

    # Письмо: из формы → из совпадающего аккаунта по resume_hash → пусто
    letter = body.get("letter", "").strip()
    if not letter:
        for acc in accounts_data:
            if acc.get("resume_hash") == resume_hash:
                letter = acc.get("letter", "")
                break

    # Храним только auth-куки (без трекинговых с + / = в значениях)
    auth_cookies = {k: v for k, v in cookies.items() if k in _AUTH_COOKIE_KEYS}

    idx_in_temp = len(bot.temp_sessions)
    temp_acc = {
        "name": f"{display_name} (🌐)",
        "short": f"🌐{display_name.split()[0] if display_name.split() else display_name}",
        "color": "yellow",
        "resume_hash": resume_hash,
        "all_resumes": all_resumes,
        "letter": letter,
        "cookies": auth_cookies,
        "urls": [],
    }
    bot.temp_sessions.append(temp_acc)
    save_browser_sessions(bot.temp_sessions)

    return {
        "status": "ok",
        "message": f"Сессия добавлена: {temp_acc['name']}",
        "idx": len(bot.account_states) + idx_in_temp,
        "name": temp_acc["name"],
        "resume_hash": resume_hash,
    }


@app.patch("/api/session/{idx}")
async def api_session_patch(idx: int, body: dict):
    temp_idx = idx - len(bot.account_states)
    if 0 <= temp_idx < len(bot.temp_sessions):
        ts = bot.temp_sessions[temp_idx]
        if "letter" in body:
            ts["letter"] = body["letter"]
            # Обновляем живой AccountState если сессия запущена
            if temp_idx in bot.temp_states:
                bot.temp_states[temp_idx].acc["letter"] = body["letter"]
        if "resume_hash" in body:
            new_hash = body["resume_hash"]
            ts["resume_hash"] = new_hash
            if temp_idx in bot.temp_states:
                state = bot.temp_states[temp_idx]
                state.acc["resume_hash"] = new_hash
                state.acc["urls"] = bot._build_session_urls(new_hash)
        save_browser_sessions(bot.temp_sessions)
        return {"status": "ok"}
    return {"status": "error", "message": "Не найдено"}


@app.post("/api/session/{idx}/activate")
async def api_session_activate(idx: int):
    """Запустить браузерную сессию как бот-аккаунт."""
    temp_idx = idx - len(bot.account_states)
    if temp_idx < 0 or temp_idx >= len(bot.temp_sessions):
        return {"status": "error", "message": "Не найдено"}
    ts = bot.temp_sessions[temp_idx]
    if not ts.get("resume_hash"):
        return {"status": "error", "message": "Сначала найдите резюме (нажмите 🔄)"}
    ok = bot.activate_session(temp_idx)
    if ok:
        return {"status": "ok", "message": f"Сессия {ts['name']} запущена как бот"}
    return {"status": "error", "message": "Не удалось запустить"}


@app.post("/api/session/{idx}/refresh")
async def api_session_refresh(idx: int):
    """Перепрофилировать сессию: обновить имя и resume_hash из HH."""
    temp_idx = idx - len(bot.account_states)
    if temp_idx < 0 or temp_idx >= len(bot.temp_sessions):
        return {"status": "error", "message": "Не найдено"}
    ts = bot.temp_sessions[temp_idx]
    raw_line = ts.get("_raw_cookie_line", "") or "; ".join(f"{k}={v}" for k, v in ts.get("cookies", {}).items())
    loop = asyncio.get_event_loop()
    profile = await loop.run_in_executor(None, _validate_and_profile, raw_line)
    if not profile["ok"]:
        return {"status": "error", "message": profile.get("error", "Ошибка")}
    if profile["resume_hash"]:
        bot.temp_sessions[temp_idx]["resume_hash"] = profile["resume_hash"]
    if profile.get("all_resumes"):
        bot.temp_sessions[temp_idx]["all_resumes"] = profile["all_resumes"]
    if profile["name"] and profile["name"] != "Браузер":
        old_name = ts.get("name", "")
        # Сохраняем эмодзи суффикс если есть
        suffix = " (🌐)" if "(🌐)" in old_name else ""
        bot.temp_sessions[temp_idx]["name"] = profile["name"] + suffix
    save_browser_sessions(bot.temp_sessions)
    return {"status": "ok", "resume_hash": profile["resume_hash"], "name": profile["name"]}


@app.delete("/api/session/{idx}")
async def api_session_delete(idx: int):
    temp_idx = idx - len(bot.account_states)
    if 0 <= temp_idx < len(bot.temp_sessions):
        removed = bot.temp_sessions.pop(temp_idx)
        # Stop worker thread if active
        if temp_idx in bot.temp_states:
            bot.temp_states[temp_idx]._deleted = True
        # Remap temp_states keys because temp_sessions list shifted after pop
        new_temp_states = {}
        for old_i, state in bot.temp_states.items():
            if old_i == temp_idx:
                continue  # deleted — skip
            new_i = old_i - 1 if old_i > temp_idx else old_i
            new_temp_states[new_i] = state
        bot.temp_states = new_temp_states
        save_browser_sessions(bot.temp_sessions)
        return {"status": "ok", "message": f"Сессия удалена: {removed.get('name')}"}
    return {"status": "error", "message": "Не найдено"}


@app.post("/api/session/{idx}/profile")
async def api_session_profile(idx: int, request: Request):
    """Обновить профиль браузерной сессии (name, short, color, resume_hash)."""
    temp_idx = idx - len(bot.account_states)
    if not (0 <= temp_idx < len(bot.temp_sessions)):
        return {"ok": False, "error": "Сессия не найдена"}
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}
    ts = bot.temp_sessions[temp_idx]
    for field in ("name", "short", "color", "resume_hash"):
        if field in body and isinstance(body[field], str) and body[field].strip():
            ts[field] = body[field].strip()
    if temp_idx in bot.temp_states:
        state = bot.temp_states[temp_idx]
        state.name = ts.get("name", state.name)
        state.short = ts.get("short", state.short)
        state.color = ts.get("color", state.color)
        state.acc.update({k: ts[k] for k in ("name", "short", "color", "resume_hash") if k in ts})
    save_browser_sessions(bot.temp_sessions)
    return {"ok": True}


@app.post("/api/apply/check")
async def api_apply_check(body: dict):
    """
    Шаг 1: проверяет вакансию — можно ли откликнуться, требует ли опрос.
    Возвращает статус и данные формы если test-required.
    """
    acc_idx = int(body.get("account_idx", 0))
    raw = body.get("vacancy_id", "").strip()
    # Извлекаем ID из URL или чистого числа
    m = re.search(r'/vacancy/(\d+)', raw) or re.match(r'^(\d+)$', raw)
    if not m:
        return {"status": "error", "message": "Не удалось определить ID вакансии"}
    vid = m.group(1)

    acc = bot._get_apply_acc(acc_idx)
    if acc is None:
        return {"status": "error", "message": "Неверный аккаунт"}

    custom_letter = body.get("letter", "").strip()
    if custom_letter:
        acc["letter"] = custom_letter

    # Пробуем через popup
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        async with aiohttp.ClientSession(
            cookies=acc["cookies"],
            connector=aiohttp.TCPConnector(ssl=ssl_ctx),
            headers=get_headers(acc.get("cookies", {}).get("_xsrf", ""))
        ) as session:
            data = aiohttp.FormData()
            for k, v in [("resume_hash", acc["resume_hash"]), ("vacancy_id", vid),
                         ("letter", acc["letter"]), ("lux", "true"), ("ignore_postponed", "true")]:
                data.add_field(k, v)
            async with session.post(
                "https://hh.ru/applicant/vacancy_response/popup",
                data=data, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                txt = await r.text()
                status_code = r.status

        # Auth check
        if status_code in (401, 403) or (status_code == 200 and _is_login_page(txt)):
            return {"status": "error", "vacancy_id": vid, "message": "⚠️ Куки протухли — обновите в настройках"}

        # Разбираем ответ
        if status_code == 200:
            info = {}
            if "shortVacancy" in txt:
                try:
                    p = json.loads(txt)
                    info = {
                        "title": glom(p, "responseStatus.shortVacancy.name", default=""),
                        "company": glom(p, "responseStatus.shortVacancy.company.name", default=""),
                    }
                except Exception:
                    pass
            return {"status": "sent", "vacancy_id": vid, **info,
                    "message": "Отклик уже отправлен (без опроса)"}

        if "negotiations-limit-exceeded" in txt:
            return {"status": "limit", "vacancy_id": vid, "message": "Достигнут дневной лимит откликов"}

        if "alreadyApplied" in txt:
            return {"status": "already", "vacancy_id": vid, "message": "Уже откликались на эту вакансию"}

        if "test-required" in txt:
            qdata = await _fetch_questionnaire_data(acc, vid)
            return {
                "status": "test_required",
                "vacancy_id": vid,
                "questions": qdata["questions"],
                "letter": acc["letter"],
                "message": f"Вакансия требует опрос ({len(qdata['questions'])} вопросов)",
            }

        return {"status": "error", "vacancy_id": vid, "message": f"HTTP {status_code}: {txt[:100]}"}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/apply/submit")
async def api_apply_submit(body: dict):
    """
    Шаг 2: отправляет отклик с заполненными ответами на опрос.
    """
    acc_idx = int(body.get("account_idx", 0))
    vid = str(body.get("vacancy_id", "")).strip()
    letter = body.get("letter", "")
    user_answers = body.get("answers", {})  # {field_name: value}

    acc = bot._get_apply_acc(acc_idx)
    if acc is None:
        return {"status": "error", "message": "Неверный аккаунт"}
    if letter:
        acc = {**acc, "letter": letter}

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    url_form = f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}&withoutTest=no"

    try:
        async with aiohttp.ClientSession(
            cookies=acc["cookies"],
            connector=aiohttp.TCPConnector(ssl=ssl_ctx),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                     "Accept": "text/html,*/*", "Referer": f"https://hh.ru/vacancy/{vid}"}
        ) as session:
            # Свежая форма (нужны актуальные uidPk/guid/startTime/_xsrf)
            async with session.get(url_form, timeout=aiohttp.ClientTimeout(total=15)) as r:
                html = await r.text()
                if r.status in (401, 403) or _is_login_page(html):
                    return {"status": "error", "message": "⚠️ Куки протухли — обновите в настройках"}

            hidden = dict(re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html))
            hidden.update(dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]+type="hidden"[^>]+value="([^"]*)"', html)))

            form = aiohttp.FormData()
            form.add_field("resume_hash", acc["resume_hash"])
            form.add_field("vacancy_id", vid)
            form.add_field("letter", acc["letter"])
            form.add_field("lux", "true")
            for name in ("_xsrf", "uidPk", "guid", "startTime", "testRequired"):
                if name in hidden:
                    form.add_field(name, hidden[name])
            for name, value in user_answers.items():
                form.add_field(name, str(value))

            async with session.post(
                url_form,
                headers={"X-Xsrftoken": acc.get("cookies", {}).get("_xsrf", ""), "Referer": url_form},
                data=form,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=False,
            ) as r2:
                status = r2.status
                location = r2.headers.get("location", "")

        if status in (302, 303):
            if "negotiations-limit-exceeded" in location:
                return {"status": "limit", "message": "Достигнут лимит откликов"}
            if "withoutTest=no" in location or f"vacancyId={vid}" in location:
                return {"status": "error", "message": "Форма не принята — возможно не все вопросы заполнены"}
            # Успех
            state = bot._get_apply_state(acc_idx)
            if state:
                state.sent += 1
                state.questionnaire_sent += 1
            add_applied(acc["name"], vid)
            short = state.short if state else acc.get("name", "?")
            color = state.color if state else ""
            bot._add_log(short, color, f"📝 Ручной отклик (опрос): {vid}", "success")
            return {"status": "sent", "message": "Отклик успешно отправлен ✅"}

        return {"status": "error", "message": f"HTTP {status}"}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/llm_profiles")
async def api_llm_profiles(request: Request):
    """Save LLM multi-profile configuration."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}
    profiles = body.get("profiles")
    mode = body.get("mode", "fallback")
    if isinstance(profiles, list):
        # Preserve existing api_key if incoming profile sends empty string
        # (happens when page reloads and key fields are blank for security reasons)
        old_by_idx = {i: p for i, p in enumerate(CONFIG.llm_profiles or [])}
        for i, p in enumerate(profiles):
            if not p.get("api_key") and old_by_idx.get(i, {}).get("api_key"):
                p["api_key"] = old_by_idx[i]["api_key"]
        CONFIG.llm_profiles = profiles
        # Keep legacy fields in sync with first profile for backward compat
        if profiles:
            first = profiles[0]
            if first.get("api_key"):
                CONFIG.llm_api_key = first["api_key"]
            if first.get("base_url"):
                CONFIG.llm_base_url = first["base_url"]
            if first.get("model"):
                CONFIG.llm_model = first["model"]
    if mode in ("fallback", "roundrobin"):
        CONFIG.llm_profile_mode = mode
    save_config()
    return {"ok": True}


@app.post("/api/llm_toggle")
async def api_llm_toggle():
    """Toggle global LLM auto-reply on/off instantly."""
    CONFIG.llm_enabled = not CONFIG.llm_enabled
    save_config()
    bot._add_log("", "", f"🤖 LLM авто-ответы {'включены' if CONFIG.llm_enabled else 'выключены'}", "success" if CONFIG.llm_enabled else "warning")
    return {"llm_enabled": CONFIG.llm_enabled}


@app.post("/api/llm_config")
async def api_llm_config(request: Request):
    """Save LLM configuration."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}
    if "api_key" in body and str(body["api_key"]).strip():
        CONFIG.llm_api_key = str(body["api_key"]).strip()
    if "base_url" in body:
        CONFIG.llm_base_url = str(body["base_url"]).strip()
    if "model" in body:
        CONFIG.llm_model = str(body["model"]).strip()
    if "system_prompt" in body:
        CONFIG.llm_system_prompt = str(body["system_prompt"]).strip()
    if "enabled" in body:
        CONFIG.llm_enabled = bool(body["enabled"])
    if "auto_send" in body:
        CONFIG.llm_auto_send = bool(body["auto_send"])
    if "use_cover_letter" in body:
        CONFIG.llm_use_cover_letter = bool(body["use_cover_letter"])
    if "use_resume" in body:
        CONFIG.llm_use_resume = bool(body["use_resume"])
    # Sync first profile for backward compat if profiles exist
    if CONFIG.llm_profiles and CONFIG.llm_api_key:
        first = CONFIG.llm_profiles[0]
        if not first.get("api_key") or "api_key" in body:
            first["api_key"] = CONFIG.llm_api_key
        if not first.get("base_url") or "base_url" in body:
            first["base_url"] = CONFIG.llm_base_url
        if not first.get("model") or "model" in body:
            first["model"] = CONFIG.llm_model
    save_config()
    return {"ok": True}


# Модели которые стоит исключить из чат-списка
_LLM_EXCLUDE_KEYWORDS = ("embed", "whisper", "tts", "dall", "moderation", "search", "realtime", "transcri")

def _is_chat_model(model_id: str) -> bool:
    mid = model_id.lower()
    return not any(k in mid for k in _LLM_EXCLUDE_KEYWORDS)

def _detect_base_url(api_key: str) -> str:
    """Угадать base_url по формату ключа."""
    if api_key.startswith("gsk_"):
        return "https://api.groq.com/openai/v1"
    if api_key.startswith("sk-or-"):
        return "https://openrouter.ai/api/v1"
    if api_key.startswith("sk-proj-"):
        return "https://api.openai.com/v1"
    # DeepSeek ключи короче ~35 символов и начинаются с sk-
    if api_key.startswith("sk-") and len(api_key) < 45:
        return "https://api.deepseek.com"
    return "https://api.openai.com/v1"

@app.post("/api/llm_run_now")
async def api_llm_run_now():
    """Принудительно запустить LLM авто-ответы для всех аккаунтов прямо сейчас (в фоне)."""
    import asyncio, threading
    def _run():
        states = list(bot.account_states) + list(bot.temp_states.values())
        for state in states:
            try:
                bot._process_llm_replies(state)
            except Exception as e:
                log_debug(f"llm_run_now {state.short}: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "accounts": len(bot.account_states) + len(bot.temp_states)}


@app.post("/api/llm_reset_replied")
async def api_llm_reset_replied():
    """Сбросить историю отправленных LLM-ответов для всех аккаунтов.
    Позволяет боту повторно обработать чаты, помеченные как 'уже отвечали'.
    """
    all_states = list(bot.account_states) + list(bot.temp_states.values())
    cleared = []
    for state in all_states:
        n_replied = len(state.llm_replied_msgs)
        n_skip = len(state._llm_temp_skip)
        n_no_chat = len(state._llm_no_chat)
        state.llm_replied_msgs.clear()
        state._llm_temp_skip.clear()
        state._llm_no_chat.clear()
        cleared.append({"acc": state.short, "replied_cleared": n_replied, "skip_cleared": n_skip, "no_chat_cleared": n_no_chat})
    with bot._llm_sent_lock:
        n_global = len(bot._llm_sent_global)
        bot._llm_sent_global.clear()
    bot._add_log("system", "green", f"🤖 История LLM-ответов сброшена для {len(cleared)} аккаунтов + {n_global} глобальных записей", "success")
    return {"ok": True, "cleared": cleared, "global_cleared": n_global}


@app.post("/api/llm_detect")
async def api_llm_detect(request: Request):
    """Определить провайдера по ключу и получить список доступных моделей."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}
    api_key = str(body.get("api_key", "")).strip()
    base_url = str(body.get("base_url", "")).strip()
    if not api_key:
        return {"ok": False, "error": "Нет ключа"}
    if not base_url:
        base_url = _detect_base_url(api_key)
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=12, verify=False,
        )
        if resp.status_code != 200:
            return {"ok": False, "base_url": base_url, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        raw_models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        chat_models = [m for m in raw_models if _is_chat_model(m)]
        # Сортируем: сначала более новые (обычно содержат большую цифру или "latest")
        chat_models.sort(key=lambda m: (
            "latest" in m,
            any(x in m for x in ("gpt-4", "claude", "llama-3", "deepseek", "gemini")),
        ), reverse=True)
        return {"ok": True, "base_url": base_url, "models": chat_models}
    except Exception as e:
        return {"ok": False, "base_url": base_url, "error": str(e)}


@app.post("/api/account/{idx}/decline_discards")
async def api_decline_discards(idx: int):
    """Авто-отклонение дискардов в переговорах"""
    if 0 <= idx < len(bot.account_states):
        acc = bot.account_states[idx].acc

        def do_decline():
            return auto_decline_discards(acc)

        count = await asyncio.get_event_loop().run_in_executor(None, do_decline)
        bot._add_log(
            bot.account_states[idx].short,
            bot.account_states[idx].color,
            f"🗑️ Отклонено дискардов: {count}",
            "info",
        )
        return {"declined": count}
    return {"error": "Invalid idx"}


# ============================================================
# BROADCAST LOOP
# ============================================================

async def broadcast_loop():
    while True:
        try:
            if manager.active:
                snapshot = bot.get_state_snapshot()
                await manager.broadcast(snapshot)
        except Exception as e:
            log_debug(f"broadcast_loop error: {e}")
        await asyncio.sleep(0.3)


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
