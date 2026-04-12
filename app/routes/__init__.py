"""
FastAPI app creation and route registration.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# Singleton bot/manager are created in app.instances so every router module
# can import them without pulling in the package __init__ (avoids circular imports).
from app.instances import bot, manager  # re-exported for back-compat

app = FastAPI(title="HH Bot Dashboard")

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")

# -- Register routers (imported after app is created) --
from app.routes.core import router as core_router          # noqa: E402
from app.routes.accounts import router as accounts_router  # noqa: E402
from app.routes.sessions import router as sessions_router  # noqa: E402
from app.routes.data import router as data_router          # noqa: E402
from app.routes.apply import router as apply_router        # noqa: E402
from app.routes.settings import router as settings_router  # noqa: E402
from app.routes.llm import router as llm_router            # noqa: E402
from app.routes.debug import router as debug_router        # noqa: E402

app.include_router(core_router)
app.include_router(accounts_router)
app.include_router(sessions_router)
app.include_router(data_router)
app.include_router(apply_router)
app.include_router(settings_router)
app.include_router(llm_router)
app.include_router(debug_router)
