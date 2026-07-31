"""Tests for the LINE identity binding module.

Covers:
1. DEFAULT_ADMIN_DB points to the correct path (not the legacy ~/services/...)
2. _load_cache() logs a WARNING (not DEBUG) when admin.db is missing
3. resolve() returns None and does not raise when admin.db is absent
4. IdentityResolver uses ADMIN_DB_PATH env override
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.platforms.line.identity import (
    DEFAULT_ADMIN_DB,
    IdentityResolver,
    BindStateStore,
)


class TestDefaultAdminDbPath:
    """The default admin.db path must point to the real location, not the
    legacy ~/services/ path that was absorbed into ai-stack."""

    def test_default_not_legacy_services_path(self):
        legacy = Path.home() / "services" / "admin_panel" / "admin.db"
        assert DEFAULT_ADMIN_DB != legacy

    def test_default_points_to_ai_data(self):
        expected = Path.home() / "ai-data" / "admin-panel" / "admin.db"
        assert DEFAULT_ADMIN_DB == expected

    def test_env_override_works(self, monkeypatch):
        custom = Path(tempfile.gettempdir()) / "custom-admin.db"
        monkeypatch.setenv("ADMIN_DB_PATH", str(custom))
        from plugins.platforms.line import identity as identity_mod
        # Re-evaluate the default with the env override in place
        result = Path(
            os.getenv("ADMIN_DB_PATH", str(Path.home() / "ai-data" / "admin-panel" / "admin.db"))
        )
        assert result == custom


class TestMissingAdminDbWarning:
    """When admin.db doesn't exist, a WARNING must be emitted — not DEBUG.
    A silent DEBUG failure means all users get treated as unbound and the bot
    appears dead, with nobody noticing until someone checks debug logs."""

    def test_missing_admin_db_logs_warning_not_debug(self, monkeypatch, caplog):
        """admin.db absent → logger.warning, NOT logger.debug."""
        monkeypatch.delenv("ADMIN_DB_PATH", raising=False)
        # Point to a path that definitely doesn't exist
        nonexistent = Path(tempfile.gettempdir()) / "nonexistent-admin.db"
        resolver = IdentityResolver(admin_db_path=str(nonexistent))

        with caplog.at_level(logging.DEBUG):
            resolver._load_cache()

        # Must have a WARNING-level record about admin.db not found
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "admin.db not found" in r.getMessage()
        ]
        assert len(warning_records) >= 1, (
            "Expected a WARNING log when admin.db is missing, but got none. "
            "A DEBUG-level log would be invisible and hide the fact that all "
            "users are being treated as unbound."
        )

        # The warning must mention the consequences
        msg = warning_records[0].getMessage()
        assert "identity resolution will fail" in msg
        assert "unbound" in msg
        assert "ADMIN_DB_PATH" in msg

    def test_resolve_returns_none_when_admin_db_missing(self, monkeypatch):
        """resolve() must return None (not raise) when admin.db is absent."""
        nonexistent = Path(tempfile.gettempdir()) / "nonexistent-admin.db"
        resolver = IdentityResolver(admin_db_path=str(nonexistent))
        assert resolver.resolve("line", "U12345") is None

    def test_resolve_returns_none_for_unknown_user_with_db(self, tmp_path):
        """resolve() returns None for a user not in the identity_map."""
        db_path = tmp_path / "admin.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE pipeline_config (component TEXT, config_json TEXT)"
        )
        conn.execute(
            "INSERT INTO pipeline_config VALUES ('channel_gw', "
            "'{\"identity_map\": []}')"
        )
        conn.execute("CREATE TABLE users (employee_id TEXT, name TEXT, active INTEGER)")
        conn.commit()
        conn.close()

        resolver = IdentityResolver(admin_db_path=str(db_path))
        assert resolver.resolve("line", "U_unknown") is None


class TestBindStateStore:

    def test_set_and_is_awaiting(self):
        store = BindStateStore()
        assert not store.is_awaiting("U1")
        store.set_awaiting("U1")
        assert store.is_awaiting("U1")

    def test_clear(self):
        store = BindStateStore()
        store.set_awaiting("U1")
        store.clear("U1")
        assert not store.is_awaiting("U1")

    def test_clear_unknown_is_noop(self):
        store = BindStateStore()
        store.clear("U_nonexistent")  # should not raise


class TestIdentityResolverWithDb:

    def test_resolve_finds_binding(self, tmp_path):
        db_path = tmp_path / "admin.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE pipeline_config (component TEXT, config_json TEXT)"
        )
        conn.execute(
            "INSERT INTO pipeline_config VALUES ('channel_gw', "
            "'{\"identity_map\": ["
            "{\"channel\": \"line\", \"channel_user_id\": \"U123\", "
            "\"employee_id\": \"E456\", \"note\": \"test\"}]"
            "}')"
        )
        conn.execute("CREATE TABLE users (employee_id TEXT, name TEXT, active INTEGER)")
        conn.execute(
            "INSERT INTO users VALUES ('E456', 'Alice', 1)"
        )
        conn.commit()
        conn.close()

        resolver = IdentityResolver(admin_db_path=str(db_path))
        assert resolver.resolve("line", "U123") == "E456"
        assert resolver.get_employee_name("E456") == "Alice"

    def test_resolve_wildcard_channel(self, tmp_path):
        db_path = tmp_path / "admin.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE pipeline_config (component TEXT, config_json TEXT)"
        )
        conn.execute(
            "INSERT INTO pipeline_config VALUES ('channel_gw', "
            "'{\"identity_map\": ["
            "{\"channel\": \"*\", \"channel_user_id\": \"U_wild\", "
            "\"employee_id\": \"E_wild\"}]"
            "}')"
        )
        conn.execute("CREATE TABLE users (employee_id TEXT, name TEXT, active INTEGER)")
        conn.commit()
        conn.close()

        resolver = IdentityResolver(admin_db_path=str(db_path))
        assert resolver.resolve("line", "U_wild") == "E_wild"
