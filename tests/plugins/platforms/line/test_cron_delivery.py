"""Tests for plugins/platforms/line/cron_delivery.py.

These tests exercise the F3 daily group summary cron delivery module
without touching any real LINE API or real database.  All external
dependencies (HTTP, SQLite) are mocked or use temp files.
"""

import json
import sqlite3
import urllib.error
import http.client
from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, call

import pytest

from plugins.platforms.line.cron_delivery import (
    EVENT_TYPE_ORDER,
    ExtractedEvent,
    LinePushClient,
    LinePushError,
    LiteLLMSummaryComposer,
    SummaryPushLog,
    SummaryRunResult,
    _compose_message,
    _split_for_line,
    _strip_markdown,
    _text_message,
    cron_script_main,
    events_for_local_day,
    get_tracked_groups,
    run_f3_daily_summary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, frozen_now):
    """Create a temp line_ingestion SQLite DB with schema + sample data.

    Uses ``frozen_now`` for all timestamps so that date-filtered queries
    (which depend on the system timezone via ``astimezone()``) find the
    sample events deterministically regardless of the host TZ.
    """

    db_path = tmp_path / "line_ingestion.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE raw_line_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            message_id TEXT NOT NULL UNIQUE,
            source_timestamp_ms INTEGER NOT NULL,
            text TEXT NOT NULL,
            received_at REAL NOT NULL
        );
        CREATE INDEX idx_raw_line_messages_group_ts
            ON raw_line_messages(group_id, source_timestamp_ms);

        CREATE TABLE extracted_events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            actors TEXT NOT NULL,
            due_date TEXT,
            source_channel TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            source_ts INTEGER NOT NULL,
            context_quote TEXT NOT NULL,
            confidence REAL NOT NULL,
            status TEXT NOT NULL,
            needs_review INTEGER NOT NULL,
            message_id TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX idx_extracted_events_source
            ON extracted_events(source_ref, source_ts);

        CREATE TABLE line_summary_pushes (
            source_ref TEXT NOT NULL,
            local_day TEXT NOT NULL,
            target_group_id TEXT NOT NULL,
            pushed_at REAL NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (source_ref, local_day)
        );
        """
    )

    # Insert sample raw messages for 3 groups.  All timestamps are anchored
    # to ``frozen_now`` so date-filtered queries find them regardless of host TZ.
    now_ms = int(frozen_now.timestamp() * 1000)
    sample_messages = [
        ("Cgroup1", "Uuser1", "msg1", now_ms - 3600000, "Project kickoff scheduled"),
        ("Cgroup1", "Uuser2", "msg2", now_ms - 1800000, "Budget approved"),
        ("Cgroup2", "Uuser3", "msg3", now_ms - 7200000, "Deadline extended to Friday"),
        ("Cgroup3", "Uuser4", "msg4", now_ms - 5400000, "No events here"),
    ]
    conn.executemany(
        """
        INSERT INTO raw_line_messages
            (group_id, user_id, message_id, source_timestamp_ms, text, received_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(g, u, m, ts, t, now_ms / 1000) for g, u, m, ts, t in sample_messages],
    )

    # Insert sample extracted events for group1 and group2.
    sample_events = [
        ("evt1", "decision", "Project kickoff decided", '["Alice"]', None,
         "line", "Cgroup1", now_ms - 3600000, "Project kickoff scheduled",
         0.95, "confirmed", 0, "msg1"),
        ("evt2", "commitment", "Budget approved", '["Bob"]', None,
         "line", "Cgroup1", now_ms - 1800000, "Budget approved",
         0.88, "confirmed", 0, "msg2"),
        ("evt3", "deadline", "Deadline extended", '["Charlie"]', "2026-07-31",
         "line", "Cgroup2", now_ms - 7200000, "Deadline extended to Friday",
         0.92, "confirmed", 0, "msg3"),
    ]
    conn.executemany(
        """
        INSERT INTO extracted_events
            (id, event_type, summary, actors, due_date, source_channel,
             source_ref, source_ts, context_quote, confidence, status,
             needs_review, message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        sample_events,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def frozen_now():
    """A fixed datetime for deterministic local_day tests."""

    return datetime(2026, 7, 31, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# get_tracked_groups
# ---------------------------------------------------------------------------

class TestGetTrackedGroups:
    def test_returns_distinct_groups(self, tmp_db):
        groups = get_tracked_groups(tmp_db)
        assert sorted(groups) == ["Cgroup1", "Cgroup2", "Cgroup3"]

    def test_empty_db_returns_empty_list(self, tmp_path):
        db_path = tmp_path / "nonexistent.sqlite3"
        assert get_tracked_groups(db_path) == []

    def test_db_without_table_returns_empty_list(self, tmp_path):
        db_path = tmp_path / "empty.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.close()
        assert get_tracked_groups(db_path) == []

    def test_filters_empty_group_ids(self, tmp_path):
        db_path = tmp_path / "test.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE raw_line_messages (
                id INTEGER PRIMARY KEY,
                group_id TEXT,
                user_id TEXT,
                message_id TEXT,
                source_timestamp_ms INTEGER,
                text TEXT,
                received_at REAL
            );
            """
        )
        conn.executemany(
            "INSERT INTO raw_line_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "Cvalid", "U1", "m1", 1000, "text", 1.0),
                (2, "", "U2", "m2", 2000, "text", 2.0),
                (3, None, "U3", "m3", 3000, "text", 3.0),
            ],
        )
        conn.commit()
        conn.close()
        groups = get_tracked_groups(db_path)
        assert groups == ["Cvalid"]


# ---------------------------------------------------------------------------
# SummaryPushLog
# ---------------------------------------------------------------------------

class TestSummaryPushLog:
    def test_init_creates_table(self, tmp_path):
        db_path = tmp_path / "pushlog.sqlite3"
        log = SummaryPushLog(db_path)
        log.init()
        conn = sqlite3.connect(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        assert ("line_summary_pushes",) in tables

    def test_record_and_check(self, tmp_path):
        db_path = tmp_path / "pushlog.sqlite3"
        log = SummaryPushLog(db_path)
        log.init()

        today = date(2026, 7, 31)
        assert not log.already_pushed("Cgroup1", today)

        log.record_push("Cgroup1", today, "Cgroup1")
        assert log.already_pushed("Cgroup1", today)

        # Different day should not be marked.
        assert not log.already_pushed("Cgroup1", date(2026, 7, 30))

    def test_idempotent_record(self, tmp_path):
        db_path = tmp_path / "pushlog.sqlite3"
        log = SummaryPushLog(db_path)
        log.init()

        today = date(2026, 7, 31)
        log.record_push("Cgroup1", today, "Cgroup1")
        log.record_push("Cgroup1", today, "Cgroup1")  # Should not raise.

        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM line_summary_pushes WHERE source_ref = ? AND local_day = ?",
            ("Cgroup1", today.isoformat()),
        ).fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# events_for_local_day
# ---------------------------------------------------------------------------

class TestEventsForLocalDay:
    def test_returns_events_for_group(self, tmp_db, frozen_now):
        events = events_for_local_day(
            tmp_db, "Cgroup1", frozen_now.date(),
            {"extracted", "confirmed"}, timezone.utc,
        )
        assert len(events) == 2
        types = [e.event_type for e in events]
        assert "decision" in types
        assert "commitment" in types

    def test_empty_for_group_with_no_events(self, tmp_db, frozen_now):
        events = events_for_local_day(
            tmp_db, "Cgroup3", frozen_now.date(),
            {"extracted", "confirmed"}, timezone.utc,
        )
        assert events == []

    def test_empty_statuses_returns_empty(self, tmp_db, frozen_now):
        events = events_for_local_day(
            tmp_db, "Cgroup1", frozen_now.date(),
            set(), timezone.utc,
        )
        assert events == []

    def test_nonexistent_db_returns_empty(self, tmp_path, frozen_now):
        events = events_for_local_day(
            tmp_path / "nonexistent.sqlite3", "Cgroup1", frozen_now.date(),
            {"extracted", "confirmed"}, timezone.utc,
        )
        assert events == []

    def test_events_sorted_by_type_then_ts(self, tmp_db, frozen_now):
        events = events_for_local_day(
            tmp_db, "Cgroup1", frozen_now.date(),
            {"extracted", "confirmed"}, timezone.utc,
        )
        # Decision (order 0) should come before commitment (order 2).
        assert events[0].event_type == "decision"
        assert events[1].event_type == "commitment"

    def test_extracted_event_fields_parsed(self, tmp_db, frozen_now):
        events = events_for_local_day(
            tmp_db, "Cgroup1", frozen_now.date(),
            {"extracted", "confirmed"}, timezone.utc,
        )
        evt = events[0]
        assert evt.id == "evt1"
        assert evt.event_type == "decision"
        assert evt.summary == "Project kickoff decided"
        assert evt.actors == ["Alice"]
        assert evt.due_date is None
        assert evt.source_ref == "Cgroup1"
        assert evt.status == "confirmed"
        assert evt.needs_review is False


# ---------------------------------------------------------------------------
# LinePushClient
# ---------------------------------------------------------------------------

class TestLinePushClient:
    def test_push_text_success(self):
        client = LinePushClient("test-token")
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = client.push_text("Cgroup1", "Hello world")

        assert result == {"ok": True}

    def test_push_text_empty_content(self):
        client = LinePushClient("test-token")
        result = client.push_text("Cgroup1", "")
        assert result == {"success": True}

    def test_push_text_429_raises(self):
        client = LinePushClient("test-token")
        headers = http.client.HTTPMessage()
        headers.add_header("Retry-After", "3600")
        mock_error = urllib.error.HTTPError(
            url="http://test", code=429, msg="Too Many Requests",
            hdrs=headers,
            fp=Mock(),
        )
        mock_error.read = Mock(return_value=b'{"message": "quota exceeded"}')

        with patch("urllib.request.urlopen", side_effect=mock_error):
            with pytest.raises(LinePushError) as exc_info:
                client.push_text("Cgroup1", "Hello")

        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after == "3600"

    def test_push_text_500_raises(self):
        client = LinePushClient("test-token")
        mock_error = urllib.error.HTTPError(
            url="http://test", code=500, msg="Internal Server Error",
            hdrs=http.client.HTTPMessage(),
            fp=Mock(),
        )
        mock_error.read = Mock(return_value=b'{"message": "server error"}')

        with patch("urllib.request.urlopen", side_effect=mock_error):
            with pytest.raises(LinePushError) as exc_info:
                client.push_text("Cgroup1", "Hello")

        assert exc_info.value.status_code == 500

    def test_push_text_long_content_chunks(self):
        client = LinePushClient("test-token")
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        long_text = "A" * 10000
        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            client.push_text("Cgroup1", long_text)

        # Verify the request was made with chunked messages.
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        assert len(body["messages"]) <= 5
        assert body["to"] == "Cgroup1"


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

class TestSplitForLine:
    def test_short_text_single_chunk(self):
        result = _split_for_line("Hello")
        assert result == ["Hello"]

    def test_empty_text(self):
        assert _split_for_line("") == []

    def test_long_text_multiple_chunks(self):
        text = "A" * 10000
        chunks = _split_for_line(text)
        assert len(chunks) > 1
        assert all(len(c) <= 5000 for c in chunks)

    def test_truncation_with_ellipsis(self):
        text = "A" * 30000
        chunks = _split_for_line(text)
        assert len(chunks) == 5
        assert chunks[-1].endswith("…")


class TestStripMarkdown:
    def test_strips_bold(self):
        assert _strip_markdown("**bold**") == "bold"

    def test_strips_headings(self):
        assert _strip_markdown("# Heading") == "Heading"

    def test_strips_code_blocks(self):
        assert _strip_markdown("```python\ncode\n```") == "code"

    def test_strips_inline_code(self):
        assert _strip_markdown("`code`") == "code"

    def test_preserves_urls(self):
        text = "[label](http://example.com)"
        result = _strip_markdown(text)
        assert "http://example.com" in result
        assert "label" in result

    def test_empty(self):
        assert _strip_markdown("") == ""
        assert _strip_markdown(None) is None


class TestTextMessage:
    def test_short_text(self):
        msg = _text_message("Hello")
        assert msg == {"type": "text", "text": "Hello"}

    def test_long_text_truncated(self):
        msg = _text_message("A" * 6000)
        assert len(msg["text"]) == 5000
        assert msg["text"].endswith("…")


# ---------------------------------------------------------------------------
# LiteLLMSummaryComposer
# ---------------------------------------------------------------------------

class TestLiteLLMSummaryComposer:
    def test_compose_success(self):
        composer = LiteLLMSummaryComposer(
            "http://localhost:4000", "test-model", api_key="key"
        )
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "choices": [{
                "message": {"content": "Summary text here"}
            }]
        }).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        events = [
            ExtractedEvent(
                id="evt1", event_type="decision", summary="Test decision",
                actors=["Alice"], due_date=None, source_channel="line",
                source_ref="Cgroup1", source_ts=1000000,
                context_quote="Test quote", confidence=0.9, status="confirmed",
                needs_review=False, message_id="msg1",
            ),
        ]

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = composer.compose(events, date(2026, 7, 31))

        assert result == "Summary text here"

    def test_compose_empty_response_raises(self):
        composer = LiteLLMSummaryComposer(
            "http://localhost:4000", "test-model"
        )
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "choices": [{
                "message": {"content": "   "}
            }]
        }).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            with pytest.raises(ValueError, match="empty"):
                composer.compose([], date(2026, 7, 31))

    def test_compose_no_choices_raises(self):
        composer = LiteLLMSummaryComposer(
            "http://localhost:4000", "test-model"
        )
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "choices": []
        }).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            with pytest.raises(ValueError, match="no choices"):
                composer.compose([], date(2026, 7, 31))


# ---------------------------------------------------------------------------
# _compose_message
# ---------------------------------------------------------------------------

class TestComposeMessage:
    def test_with_events(self):
        composer = Mock()
        composer.compose.return_value = "Daily summary"

        events = [
            ExtractedEvent(
                id="evt1", event_type="decision", summary="Test",
                actors=["Alice"], due_date=None, source_channel="line",
                source_ref="Cgroup1", source_ts=1000000,
                context_quote="Quote", confidence=0.9, status="confirmed",
                needs_review=False, message_id="msg1",
            ),
        ]

        result = _compose_message(composer, events, date(2026, 7, 31), "Footer")
        assert "Daily summary" in result
        assert "Footer" in result

    def test_without_events(self):
        composer = Mock()
        result = _compose_message(composer, [], date(2026, 7, 31), "Footer")
        assert "沒有已萃取或確認的事件" in result
        assert "Footer" in result

    def test_events_sorted_before_composition(self):
        composer = Mock()
        composer.compose.return_value = "sorted"

        events = [
            ExtractedEvent(
                id="evt2", event_type="commitment", summary="B",
                actors=[], due_date=None, source_channel="line",
                source_ref="Cgroup1", source_ts=2000000,
                context_quote="Q", confidence=0.9, status="confirmed",
                needs_review=False, message_id="msg2",
            ),
            ExtractedEvent(
                id="evt1", event_type="decision", summary="A",
                actors=[], due_date=None, source_channel="line",
                source_ref="Cgroup1", source_ts=1000000,
                context_quote="Q", confidence=0.9, status="confirmed",
                needs_review=False, message_id="msg1",
            ),
        ]

        _compose_message(composer, events, date(2026, 7, 31), "Footer")
        # Verify composer received events sorted by type order (decision=0 before commitment=2).
        called_events = composer.compose.call_args[0][0]
        assert called_events[0].event_type == "decision"
        assert called_events[1].event_type == "commitment"


# ---------------------------------------------------------------------------
# run_f3_daily_summary
# ---------------------------------------------------------------------------

class TestRunF3DailySummary:
    def test_successful_run(self, tmp_db, frozen_now):
        composer = Mock()
        composer.compose.return_value = "Test summary"

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher.push_text.return_value = {"ok": True}
            mock_pusher_cls.return_value = mock_pusher

            result = run_f3_daily_summary(
                db_path=tmp_db,
                line_channel_access_token="test-token",
                litellm_base_url="http://localhost:4000",
                litellm_model="test-model",
                now=frozen_now,
            )

        assert result.groups_checked == 3
        assert result.groups_pushed == 2  # group1 and group2 have events
        assert result.groups_skipped_empty == 1  # group3 has no events
        assert result.groups_failed == 0
        assert result.groups_skipped_already_pushed == 0
        assert result.errors == []

    def test_skips_already_pushed(self, tmp_db, frozen_now):
        # Pre-record a push for group1.
        log = SummaryPushLog(tmp_db)
        log.init()
        log.record_push("Cgroup1", frozen_now.date(), "Cgroup1")

        composer = Mock()
        composer.compose.return_value = "Test summary"

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher.push_text.return_value = {"ok": True}
            mock_pusher_cls.return_value = mock_pusher

            result = run_f3_daily_summary(
                db_path=tmp_db,
                line_channel_access_token="test-token",
                litellm_base_url="http://localhost:4000",
                litellm_model="test-model",
                now=frozen_now,
            )

        assert result.groups_skipped_already_pushed == 1
        assert result.groups_pushed == 1  # only group2

    def test_429_handling(self, tmp_db, frozen_now):
        composer = Mock()
        composer.compose.return_value = "Test summary"

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher.push_text.side_effect = [
                LinePushError(429, "quota exceeded", "3600"),
                {"ok": True},
            ]
            mock_pusher_cls.return_value = mock_pusher

            result = run_f3_daily_summary(
                db_path=tmp_db,
                line_channel_access_token="test-token",
                litellm_base_url="http://localhost:4000",
                litellm_model="test-model",
                now=frozen_now,
            )

        assert result.groups_skipped_429 == 1
        assert result.groups_pushed == 1
        assert len(result.errors) == 1
        assert "429" in result.errors[0]

    def test_composition_failure(self, tmp_db, frozen_now):
        composer = Mock()
        composer.compose.side_effect = ValueError("LLM failed")

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher_cls.return_value = mock_pusher

            result = run_f3_daily_summary(
                db_path=tmp_db,
                line_channel_access_token="test-token",
                litellm_base_url="http://localhost:4000",
                litellm_model="test-model",
                now=frozen_now,
            )

        assert result.groups_failed == 2  # both group1 and group2 fail
        assert len(result.errors) == 2
        assert all("composition failed" in e for e in result.errors)

    def test_push_failure(self, tmp_db, frozen_now):
        composer = Mock()
        composer.compose.return_value = "Test summary"

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher.push_text.side_effect = LinePushError(500, "server error")
            mock_pusher_cls.return_value = mock_pusher

            result = run_f3_daily_summary(
                db_path=tmp_db,
                line_channel_access_token="test-token",
                litellm_base_url="http://localhost:4000",
                litellm_model="test-model",
                now=frozen_now,
            )

        assert result.groups_failed == 2
        assert len(result.errors) == 2
        assert all("push failed" in e for e in result.errors)

    def test_empty_db(self, tmp_path, frozen_now):
        result = run_f3_daily_summary(
            db_path=tmp_path / "nonexistent.sqlite3",
            line_channel_access_token="test-token",
            litellm_base_url="http://localhost:4000",
            litellm_model="test-model",
            now=frozen_now,
        )

        assert result.groups_checked == 0
        assert len(result.errors) == 1
        assert "No tracked groups" in result.errors[0]

    def test_skip_empty_day_false(self, tmp_db, frozen_now):
        composer = Mock()
        composer.compose.return_value = "No events today"

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher.push_text.return_value = {"ok": True}
            mock_pusher_cls.return_value = mock_pusher

            result = run_f3_daily_summary(
                db_path=tmp_db,
                line_channel_access_token="test-token",
                litellm_base_url="http://localhost:4000",
                litellm_model="test-model",
                skip_empty_day=False,
                now=frozen_now,
            )

        # group3 has no events but should still be pushed.
        assert result.groups_pushed == 3
        assert result.groups_skipped_empty == 0

    def test_does_not_record_push_on_failure(self, tmp_db, frozen_now):
        composer = Mock()
        composer.compose.return_value = "Test summary"

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher.push_text.side_effect = LinePushError(500, "server error")
            mock_pusher_cls.return_value = mock_pusher

            result = run_f3_daily_summary(
                db_path=tmp_db,
                line_channel_access_token="test-token",
                litellm_base_url="http://localhost:4000",
                litellm_model="test-model",
                now=frozen_now,
            )

        # Verify no push was recorded.
        log = SummaryPushLog(tmp_db)
        assert not log.already_pushed("Cgroup1", frozen_now.date())
        assert not log.already_pushed("Cgroup2", frozen_now.date())

    def test_does_not_record_push_on_429(self, tmp_db, frozen_now):
        composer = Mock()
        composer.compose.return_value = "Test summary"

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher.push_text.side_effect = LinePushError(429, "quota", "3600")
            mock_pusher_cls.return_value = mock_pusher

            result = run_f3_daily_summary(
                db_path=tmp_db,
                line_channel_access_token="test-token",
                litellm_base_url="http://localhost:4000",
                litellm_model="test-model",
                now=frozen_now,
            )

        # Verify no push was recorded (429 is not a success).
        log = SummaryPushLog(tmp_db)
        assert not log.already_pushed("Cgroup1", frozen_now.date())
        assert not log.already_pushed("Cgroup2", frozen_now.date())


# ---------------------------------------------------------------------------
# cron_script_main
# ---------------------------------------------------------------------------

class TestCronScriptMain:
    def test_missing_token_returns_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("LINE_INGESTION_DB", str(tmp_path / "db.sqlite3"))

        exit_code = cron_script_main()
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "LINE_CHANNEL_ACCESS_TOKEN is required" in captured.err

    def test_successful_run(self, tmp_path, monkeypatch, frozen_now):
        db_path = tmp_path / "line_ingestion.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE raw_line_messages (
                id INTEGER PRIMARY KEY,
                group_id TEXT,
                user_id TEXT,
                message_id TEXT,
                source_timestamp_ms INTEGER,
                text TEXT,
                received_at REAL
            );
            CREATE TABLE extracted_events (
                id TEXT PRIMARY KEY,
                event_type TEXT,
                summary TEXT,
                actors TEXT,
                due_date TEXT,
                source_channel TEXT,
                source_ref TEXT,
                source_ts INTEGER,
                context_quote TEXT,
                confidence REAL,
                status TEXT,
                needs_review INTEGER,
                message_id TEXT
            );
            CREATE TABLE line_summary_pushes (
                source_ref TEXT,
                local_day TEXT,
                target_group_id TEXT,
                PRIMARY KEY (source_ref, local_day)
            );
            """
        )
        # Insert a raw message so get_tracked_groups returns a group.
        now_ms = int(frozen_now.timestamp() * 1000)
        conn.execute(
            """
            INSERT INTO raw_line_messages
                (group_id, user_id, message_id, source_timestamp_ms, text, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Cgroup1", "Uuser1", "msg1", now_ms, "test", now_ms / 1000),
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
        monkeypatch.setenv("LINE_INGESTION_DB", str(db_path))
        monkeypatch.setenv("LINE_SUMMARY_LITELLM_BASE_URL", "http://localhost:4000")
        monkeypatch.setenv("LINE_SUMMARY_LITELLM_MODEL", "test-model")

        composer = Mock()
        composer.compose.return_value = "Summary"

        with patch(
            "plugins.platforms.line.cron_delivery.LiteLLMSummaryComposer",
            return_value=composer,
        ), patch(
            "plugins.platforms.line.cron_delivery.LinePushClient"
        ) as mock_pusher_cls:
            mock_pusher = Mock()
            mock_pusher.push_text.return_value = {"ok": True}
            mock_pusher_cls.return_value = mock_pusher

            exit_code = cron_script_main()

        assert exit_code == 0
