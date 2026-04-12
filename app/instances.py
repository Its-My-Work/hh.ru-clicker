"""
Singleton instances of BotManager and ConnectionManager.

Imported by app/routes/__init__.py to wire up the app, and by every
router module so handlers can reference `bot` and `manager` without
creating circular imports through `app.routes`.
"""

from app.manager import BotManager
from app.websocket import ConnectionManager

bot = BotManager()
manager = ConnectionManager()
