"""
FastAPI app creation and route registration.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.manager import BotManager
from app.websocket import ConnectionManager

# -- App & singleton instances --

app = FastAPI(title="HH Bot Dashboard")
manager = ConnectionManager()
bot = BotManager()

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")

# -- Register routes --
# Import AFTER app/bot/manager are created so the module can reference them.
from app.routes.api import router as api_router  # noqa: E402

app.include_router(api_router)
