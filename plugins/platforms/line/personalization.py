"""
LINE platform personalization hook: soul/skill injection.

Once the LINE user_id is resolved to an employee_id (via IdentityResolver),
this module loads the employee's soul (department persona) and skill
(department/personal skill settings) from admin.db and injects them into
the ``pre_llm_call`` hook context.

Mirrors channel_gw's ``_load_soul()`` and ``_load_skill()`` SQL semantics
exactly (channel_gw/main.py:414-443, 466-503).  admin.db is treated as
**read-only** — we never write to it or alter its schema.

Design notes
------------

* **Hook, not core edit.**  Soul/skill are injected via hermes-agent's
  ``pre_llm_call`` plugin hook, which returns ``{"context": "..."}``.
  Hermes injects this into the *user message* (never the system prompt)
  to preserve prompt-cache stability across turns.  This is the same
  mechanism ``hermes-hooks/intention_router.py`` uses.

* **Platform-scoped.**  The hook callback is registered globally on the
  plugin manager, but short-circuits immediately when
  ``platform != "line"`` — zero cost for other platforms.

* **Cached.**  Both the IdentityResolver (identity_map) and the soul/skill
  data use a 60-second TTL cache, so a burst of messages from the same
  employee hits admin.db at most once per minute.

* **Safe-degrades.**  Every failure path returns ``None`` (no context
  injected) rather than raising.  A missing admin.db, an unbound user, or
  a transient DB error never breaks the conversation.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Defaults (mirror channel_gw/main.py) ────────────────────────────────

DEFAULT_ADMIN_DB = Path(
    os.getenv("ADMIN_DB_PATH", str(Path.home() / "services" / "admin_panel" / "admin.db"))
)

# Cache TTL — balance freshness vs. DB load.  Matches IdentityResolver's
# default cache_ttl (60 s).  Soul/skill data changes rarely (admin panel
# edits), so a minute of staleness is acceptable.
DEFAULT_CACHE_TTL = 60


# ── Cache for soul/skill data ────────────────────────────────────────────

class SoulSkillCache:
    """TTL cache for soul/skill data keyed by employee_id.

    Prevents a DB hit on every message — a burst of 10 messages from the
    same employee within 60 seconds results in a single admin.db query.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_CACHE_TTL) -> None:
        self._store: Dict[str, Tuple[str, str, float]] = {}
        self._ttl = ttl_seconds

    def get(self, employee_id: str) -> Optional[Tuple[str, str]]:
        """Return ``(soul, skill)`` if cached and fresh, else ``None``."""
        entry = self._store.get(employee_id)
        if entry is None:
            return None
        soul, skill, ts = entry
        if time.time() - ts > self._ttl:
            del self._store[employee_id]
            return None
        return soul, skill

    def set(self, employee_id: str, soul: str, skill: str) -> None:
        self._store[employee_id] = (soul, skill, time.time())

    def invalidate(self, employee_id: str) -> None:
        self._store.pop(employee_id, None)

    def clear(self) -> None:
        self._store.clear()


# ── Module-level singletons ──────────────────────────────────────────────

_cache = SoulSkillCache()
_admin_db = DEFAULT_ADMIN_DB

# IdentityResolver singleton — ensures the identity_map cache (also 60 s
# TTL) is reused across hook invocations.  Without this, every
# pre_llm_call would create a fresh resolver and reload the identity_map
# from admin.db.
_identity_resolver: Optional[object] = None


def _get_identity_resolver():
    """Return the cached IdentityResolver singleton."""
    global _identity_resolver
    if _identity_resolver is None:
        from plugins.platforms.line.identity import IdentityResolver
        _identity_resolver = IdentityResolver()
    return _identity_resolver


# ── Soul/skill loaders (mirror channel_gw _load_soul / _load_skill) ──────

def _load_soul(employee_id: str, admin_db: Path) -> str:
    """Load ``base_soul`` + dept ``soul_overlay`` for the given employee.

    Mirrors channel_gw's ``_load_soul`` (channel_gw/main.py:414-443)::

        SELECT department_id FROM users WHERE employee_id=? AND active=1
        SELECT value FROM system_settings WHERE key='base_soul'
        SELECT soul_overlay FROM departments WHERE id=? AND active=1

    Returns ``""`` on any error (safe degradation).
    """
    try:
        conn = sqlite3.connect(str(admin_db))
        dept_row = conn.execute(
            "SELECT department_id FROM users WHERE employee_id=? AND active=1",
            (employee_id,),
        ).fetchone()
        department_id = dept_row[0] if dept_row and dept_row[0] else None

        base_row = conn.execute(
            "SELECT value FROM system_settings WHERE key='base_soul'"
        ).fetchone()
        base_soul = (base_row[0] or "").strip() if base_row else ""

        dept_overlay = ""
        if department_id:
            overlay_row = conn.execute(
                "SELECT soul_overlay FROM departments WHERE id=? AND active=1",
                (department_id,),
            ).fetchone()
            if overlay_row:
                dept_overlay = (overlay_row[0] or "").strip()
        conn.close()

        parts = [p for p in [base_soul, dept_overlay] if p]
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning("LINE personalization: _load_soul failed (employee=%s): %s", employee_id, e)
        return ""


def _load_skill(employee_id: str, admin_db: Path) -> str:
    """Load global + dept + user skills for the given employee.

    Mirrors channel_gw's ``_load_skill`` (channel_gw/main.py:466-503)::

        SELECT content FROM skills WHERE scope='global' AND active=1
        SELECT s.content FROM skills s
          JOIN department_skills ds ON ds.skill_id=s.id
          WHERE ds.department_id=? AND s.active=1
        SELECT s.content FROM skills s
          JOIN user_skills us ON us.skill_id=s.id
          WHERE us.employee_id=? AND s.active=1

    Join order is **global → dept → user** (same as channel_gw).
    Returns ``""`` on any error (safe degradation).
    """
    try:
        conn = sqlite3.connect(str(admin_db))
        dept_row = conn.execute(
            "SELECT department_id FROM users WHERE employee_id=? AND active=1",
            (employee_id,),
        ).fetchone()
        department_id = dept_row[0] if dept_row and dept_row[0] else None

        global_rows = conn.execute(
            "SELECT content FROM skills WHERE scope='global' AND active=1 ORDER BY sort_order ASC"
        ).fetchall()

        dept_rows = []
        if department_id:
            dept_rows = conn.execute(
                """SELECT s.content FROM skills s
                   JOIN department_skills ds ON ds.skill_id=s.id
                   WHERE ds.department_id=? AND s.active=1
                   ORDER BY s.sort_order ASC""",
                (department_id,),
            ).fetchall()

        user_rows = conn.execute(
            """SELECT s.content FROM skills s
               JOIN user_skills us ON us.skill_id=s.id
               WHERE us.employee_id=? AND s.active=1
               ORDER BY s.id ASC""",
            (employee_id,),
        ).fetchall()

        conn.close()
        parts = [row[0].strip() for row in global_rows + dept_rows + user_rows if row[0].strip()]
        return "\n\n---\n\n".join(parts)
    except Exception as e:
        logger.warning("LINE personalization: _load_skill failed (employee=%s): %s", employee_id, e)
        return ""


# ── Cached loader ────────────────────────────────────────────────────────

def _load_soul_skill(employee_id: str) -> Tuple[str, str]:
    """Load ``(soul, skill)`` for the given employee, with caching.

    Returns ``("", "")`` if the employee_id is empty or admin.db is
    unavailable.
    """
    if not employee_id:
        return "", ""

    cached = _cache.get(employee_id)
    if cached is not None:
        return cached

    if not _admin_db.exists():
        logger.debug(
            "LINE personalization: admin.db not found at %s — "
            "soul/skill injection will be skipped",
            _admin_db,
        )
        return "", ""

    soul = _load_soul(employee_id, _admin_db)
    skill = _load_skill(employee_id, _admin_db)
    _cache.set(employee_id, soul, skill)
    return soul, skill


# ── pre_llm_call hook ────────────────────────────────────────────────────

def pre_llm_call_hook(**kwargs) -> Optional[Dict[str, str]]:
    """LINE platform ``pre_llm_call`` hook.

    Extracts the LINE user_id from the hook payload's ``sender_id``,
    resolves it to an employee_id via :class:`IdentityResolver`, then
    loads the employee's soul/skill from admin.db and returns it as
    hook context.

    The returned ``{"context": "..."}`` is injected into the *user
    message* by hermes-agent's ``pre_llm_call`` hook system (NOT the
    system prompt), preserving prompt-cache stability across turns.
    This mirrors the pattern used by ``hermes-hooks/intention_router.py``.

    **Safe-degradation** — returns ``None`` (no-op) when:

    * Platform is not ``"line"``
    * ``sender_id`` is empty
    * Identity resolution fails (unbound user)
    * admin.db is unavailable
    * Any DB error occurs

    No exception from this hook can break the agent loop —
    ``invoke_hook`` wraps every callback in try/except.
    """
    platform = kwargs.get("platform", "")
    if platform != "line":
        return None

    sender_id = kwargs.get("sender_id", "") or ""
    if not sender_id:
        return None

    # Resolve LINE user_id → employee_id via the shared IdentityResolver.
    # The IdentityResolver caches the identity_map from admin.db with a
    # 60-second TTL, so this is cheap after the first call.
    try:
        resolver = _get_identity_resolver()
        employee_id = resolver.resolve("line", sender_id)
    except Exception as e:
        logger.debug("LINE personalization: identity resolution failed: %s", e)
        return None

    if not employee_id:
        # Unbound user — soul/skill can't be loaded.  This is expected
        # for users who haven't completed the binding flow.
        return None

    soul, skill = _load_soul_skill(employee_id)

    parts = [p for p in (soul, skill) if p]
    if not parts:
        return None

    context = "\n\n".join(parts)
    logger.debug(
        "LINE personalization: injected soul/skill for employee=%s "
        "(soul=%d chars, skill=%d chars)",
        employee_id, len(soul), len(skill),
    )
    return {"context": context}
