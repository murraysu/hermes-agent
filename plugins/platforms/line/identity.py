"""
LINE identity binding for the Hermes Agent LINE plugin.

Mirrors channel_gw's IdentityResolver (``channel_gw/identity.py``):

* **Resolve** — reads the ``identity_map`` from admin.db's
  ``pipeline_config`` table (``component='channel_gw'``), exactly as
  channel_gw does.  admin.db is treated as **read-only** — we never
  write to it or alter its schema.

* **Bind** — writes new bindings through the Admin Panel ``POST
  /api/bind`` endpoint, which appends to the same
  ``pipeline_config.config_json.identity_map`` array that channel_gw
  uses.  This is the *same write point* channel_gw uses, so there is
  no data fragmentation.

* **Bind state** (``awaiting_nickname``) — kept in a lightweight
  in-memory store with TTL, replacing channel_gw's Redis dependency.
  The state is ephemeral by design (channel_gw used a 10-minute TTL).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Defaults (mirror channel_gw/identity.py) ──────────────────────────

# The old default ~/services/admin_panel/admin.db is the legacy path from when
# admin_panel ran as a standalone service under ~/services/. That service has
# since been absorbed into the ai-stack monorepo (~/ai-stack/code/<svc>), and
# the real admin.db now lives at ~/ai-data/admin-panel/admin.db. Using the
# stale path would silently break identity resolution for every user.
# ADMIN_DB_PATH env var can still override for custom deployments.
DEFAULT_ADMIN_DB = Path(
    os.getenv("ADMIN_DB_PATH", str(Path.home() / "ai-data" / "admin-panel" / "admin.db"))
)
DEFAULT_ADMIN_PANEL_URL = os.getenv("ADMIN_PANEL_URL", "http://host.docker.internal:8888")

BIND_STATE_TTL = 600  # 10 minutes — matches channel_gw's BIND_STATE_TTL


# ── Bind-state store ───────────────────────────────────────────────────

class BindStateStore:
    """Lightweight in-memory store for the ``awaiting_nickname`` state.

    Replaces channel_gw's Redis-based ``bind:{channel}:{user_id}`` key.
    TTL prevents stale state from accumulating if a user starts but
    never completes the binding flow.
    """

    def __init__(self, ttl_seconds: int = BIND_STATE_TTL) -> None:
        self._store: Dict[str, float] = {}
        self._ttl = ttl_seconds

    def is_awaiting(self, user_id: str) -> bool:
        self._prune()
        return user_id in self._store

    def set_awaiting(self, user_id: str) -> None:
        self._store[user_id] = time.time() + self._ttl

    def clear(self, user_id: str) -> None:
        self._store.pop(user_id, None)

    def _prune(self) -> None:
        now = time.time()
        self._store = {uid: exp for uid, exp in self._store.items() if now < exp}


# ── Identity resolver ──────────────────────────────────────────────────

class IdentityResolver:
    """Resolve LINE user IDs to employee IDs via admin.db.

    Reads the ``identity_map`` from ``pipeline_config`` where
    ``component='channel_gw'`` — the *same* table and field channel_gw
    reads.  admin.db is opened read-only.

    New bindings are written through the Admin Panel ``/api/bind`` API
    (which writes to the same ``pipeline_config.config_json`` field),
    never directly to admin.db.
    """

    def __init__(
        self,
        admin_db_path: Optional[str] = None,
        admin_panel_url: Optional[str] = None,
        cache_ttl: int = 60,
    ) -> None:
        self._admin_db = Path(
            admin_db_path
            or os.getenv("ADMIN_DB_PATH", str(DEFAULT_ADMIN_DB))
        )
        self._admin_panel_url = (
            admin_panel_url
            or os.getenv("ADMIN_PANEL_URL", DEFAULT_ADMIN_PANEL_URL)
        ).rstrip("/")
        self._cache: Dict[Tuple[str, str], str] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl = cache_ttl
        self._employee_names: Dict[str, str] = {}

    # ── Cache management ────────────────────────────────────────────

    def _load_cache(self) -> None:
        """Load identity_map from admin.db (read-only)."""
        now = time.time()
        if now - self._cache_ts < self._cache_ttl and self._cache:
            return

        if not self._admin_db.exists():
            logger.warning(
                "LINE identity: admin.db not found at %s — "
                "identity resolution will fail and all users will be treated "
                "as unbound (every 1:1 message will be intercepted by the "
                "binding prompt). Set ADMIN_DB_PATH to the correct location "
                "and mount admin.db into the container (read-only).",
                self._admin_db,
            )
            return

        try:
            conn = sqlite3.connect(str(self._admin_db))
            cursor = conn.execute(
                "SELECT config_json FROM pipeline_config "
                "WHERE component='channel_gw'"
            )
            row = cursor.fetchone()
            if row:
                cfg = json.loads(row[0])
                new_cache: Dict[Tuple[str, str], str] = {}
                for entry in cfg.get("identity_map", []):
                    ch = entry.get("channel", "line")
                    cuid = entry.get("channel_user_id", "")
                    eid = entry.get("employee_id", "")
                    if cuid and eid:
                        new_cache[(ch, cuid)] = eid
                self._cache = new_cache
                logger.debug(
                    "LINE identity: cache reloaded — %d mappings",
                    len(self._cache),
                )

            # Employee names for display (users table, active only)
            cursor2 = conn.execute(
                "SELECT employee_id, name FROM users WHERE active=1"
            )
            self._employee_names = {r[0]: r[1] for r in cursor2.fetchall()}

            conn.close()
            self._cache_ts = now
        except Exception as e:
            logger.error("LINE identity: failed to load cache from admin.db: %s", e)

    # ── Public API ──────────────────────────────────────────────────

    def resolve(self, channel: str, channel_user_id: str) -> Optional[str]:
        """Resolve a channel user ID to employee_id.

        Returns ``None`` if no binding exists.  Mirrors
        channel_gw's ``identity.resolve`` — checks both the
        specific channel and the ``*`` wildcard.
        """
        self._load_cache()
        eid = self._cache.get((channel, channel_user_id)) or self._cache.get(
            ("*", channel_user_id)
        )
        if not eid:
            logger.debug(
                "LINE identity: no binding for channel=%s, user_id=%s",
                channel,
                channel_user_id,
            )
        return eid

    def get_employee_name(self, employee_id: str) -> str:
        """Get display name for an employee (from users table)."""
        self._load_cache()
        return self._employee_names.get(employee_id, employee_id)

    async def bind(self, channel: str, channel_user_id: str, nickname: str) -> dict:
        """Bind a channel user to an employee via the Admin Panel API.

        Calls ``POST {ADMIN_PANEL_URL}/api/bind`` with
        ``{"channel", "channel_user_id", "nickname"}`` — the *same*
        endpoint channel_gw uses.  On success the cache is invalidated
        so the next ``resolve()`` picks up the new binding.

        Returns the API response dict (same shape as channel_gw's
        ``identity.bind`` return value).
        """
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0, trust_env=True) as client:
                resp = await client.post(
                    f"{self._admin_panel_url}/api/bind",
                    json={
                        "channel": channel,
                        "channel_user_id": channel_user_id,
                        "nickname": nickname,
                    },
                )
                result = resp.json()
                if result.get("ok"):
                    self._cache_ts = 0  # force cache reload
                    logger.info(
                        "LINE identity: bind success — "
                        "channel=%s, user_id=%s, nickname=%s",
                        channel,
                        channel_user_id,
                        nickname,
                    )
                else:
                    logger.warning(
                        "LINE identity: bind failed — "
                        "channel=%s, user_id=%s, nickname=%s, code=%s",
                        channel,
                        channel_user_id,
                        nickname,
                        result.get("code", ""),
                    )
                return result
        except Exception as e:
            logger.error("LINE identity: bind API call failed: %s", e)
            return {
                "ok": False,
                "message": "綁定服務暫時不可用，請稍後再試。",
            }
