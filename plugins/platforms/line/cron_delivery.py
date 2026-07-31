"""F3 daily group summary cron delivery for the LINE platform.

This module replaces the channel_gw ``POST /internal/report`` path for F3
daily group summaries.  It reads the dynamically-tracked group list from
``line_ingestion``'s SQLite database, generates a Traditional-Chinese daily
summary per group via LiteLLM, and pushes each summary to the corresponding
LINE group via the LINE Messaging API Push endpoint.

Design goals
------------
* **No new scheduler.**  Triggered by a hermes-agent cron job whose
  ``script`` calls :func:`run_f3_daily_summary`.  Scheduling itself is
  handled by the native ``cron`` system.
* **Dynamic multi-group targets.**  The group list is read from the
  ``raw_line_messages`` table at run time — new groups are picked up
  automatically without config changes.
* **Failures are discoverable.**  Every push result (success, 429, or
  error) is logged via the ``hermes.line_cron_delivery`` logger and
  returned in the run result dict so the cron job output surfaces it.
* **Idempotent.**  The ``line_summary_pushes`` table deduplicates per
  ``(source_ref, local_day)`` so a retried run does not double-push.
* **429 graceful degradation.**  When the LINE free-tier push quota is
  exhausted, the affected group is skipped with a clear warning — no
  crash, no retry loop.
* **No heavy dependencies.**  Only stdlib (sqlite3, urllib, json, logging,
  dataclasses).  No aiohttp or line-bot-sdk required.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

logger = logging.getLogger("hermes.line_cron_delivery")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_PER_BUBBLE_CHARS = 5000
LINE_SAFE_BUBBLE_CHARS = 4500
LINE_MAX_MESSAGES_PER_CALL = 5
LINE_PUSH_TIMEOUT_SECONDS = 30.0

# Event statuses that count as "confirmed" for summary inclusion.
SUMMARY_STATUSES = {"extracted", "confirmed"}

# Event type ordering for summary composition (decisions first).
EVENT_TYPE_ORDER = {
    "decision": 0,
    "deadline": 1,
    "commitment": 2,
    "requirement": 3,
    "solution": 4,
}

# Markdown tokens to strip before sending text to LINE.
_MARKDOWN_TOKENS_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)

# HTTP status that indicates the LINE push quota is exhausted.
HTTP_TOO_MANY_REQUESTS = 429


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtractedEvent:
    """A validated structured event extracted from LINE group messages."""

    id: str
    event_type: str
    summary: str
    actors: List[str]
    due_date: Optional[str]
    source_channel: str
    source_ref: str
    source_ts: int
    context_quote: str
    confidence: float
    status: str
    needs_review: bool
    message_id: str


@dataclass(frozen=True)
class SummaryRunResult:
    """Counters from one daily summary delivery run."""

    groups_checked: int = 0
    groups_pushed: int = 0
    groups_skipped_empty: int = 0
    groups_skipped_already_pushed: int = 0
    groups_failed: int = 0
    groups_skipped_429: int = 0
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Group tracking — reads the dynamic group list from line_ingestion's DB
# ---------------------------------------------------------------------------

def get_tracked_groups(db_path: Path) -> List[str]:
    """Return the distinct list of group IDs currently being ingested.

    Reads from the ``raw_line_messages`` table in the line_ingestion SQLite
    database.  New groups appear automatically as soon as their first
    message is ingested — no config change required.

    Args:
        db_path: Path to the ``line_ingestion.sqlite3`` database.

    Returns:
        Sorted list of group IDs (strings).  Empty list if the database
        or table does not exist.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        logger.warning("line_ingestion DB not found at %s", db_path)
        return []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT group_id
                FROM raw_line_messages
                WHERE group_id IS NOT NULL AND group_id != ''
                ORDER BY group_id
                """
            ).fetchall()
    except sqlite3.Error as exc:
        logger.error("Failed to read tracked groups from %s: %s", db_path, exc)
        return []

    groups = [row[0] for row in rows]
    logger.info("Found %d tracked groups in line_ingestion DB", len(groups))
    return groups


# ---------------------------------------------------------------------------
# Push log — deduplicates per (group, day)
# ---------------------------------------------------------------------------

class SummaryPushLog:
    """Persist daily summary push delivery state in the line_ingestion DB.

    Uses the existing ``line_summary_pushes`` table so that retries
    (cron job re-runs, process crashes) do not double-push to the same
    group on the same day.
    """

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def init(self) -> None:
        """Create the push-log table if it does not already exist."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.database_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS line_summary_pushes (
                    source_ref TEXT NOT NULL,
                    local_day TEXT NOT NULL,
                    target_group_id TEXT NOT NULL,
                    pushed_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    PRIMARY KEY (source_ref, local_day)
                )
                """
            )

    def already_pushed(self, source_ref: str, local_day: date) -> bool:
        """Return whether a group already received today's summary."""

        with sqlite3.connect(str(self.database_path)) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM line_summary_pushes
                WHERE source_ref = ? AND local_day = ?
                """,
                (source_ref, local_day.isoformat()),
            ).fetchone()
        return row is not None

    def record_push(
        self,
        source_ref: str,
        local_day: date,
        target_group_id: str,
    ) -> None:
        """Record one successful push for the group and day."""

        with sqlite3.connect(str(self.database_path)) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO line_summary_pushes
                    (source_ref, local_day, target_group_id)
                VALUES (?, ?, ?)
                """,
                (source_ref, local_day.isoformat(), target_group_id),
            )


# ---------------------------------------------------------------------------
# Event extraction store — reads extracted events for a group on a given day
# ---------------------------------------------------------------------------

def events_for_local_day(
    db_path: Path,
    group_id: str,
    local_day: date,
    statuses: Set[str],
    tz: timezone,
) -> List[ExtractedEvent]:
    """Return same-day extracted events for one group, filtered by status.

    Replicates the logic from ``line_ingestion.extraction_store.ExtractionStore.
    events_for_local_day`` but as a standalone function so this module has
    no import dependency on the ``line_ingestion`` package.
    """
    start = datetime.combine(local_day, time.min, tzinfo=tz)
    end = datetime.combine(local_day, time.max, tzinfo=tz)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    if not statuses:
        return []

    if not db_path.exists():
        return []

    placeholders = ",".join("?" for _ in statuses)
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT id, event_type, summary, actors, due_date, source_channel,
                       source_ref, source_ts, context_quote, confidence, status,
                       needs_review, message_id
                FROM extracted_events
                WHERE source_ref = ?
                  AND source_ts BETWEEN ? AND ?
                  AND status IN ({placeholders})
                ORDER BY source_ts, id
                """,
                (group_id, start_ms, end_ms, *sorted(statuses)),
            ).fetchall()
    except sqlite3.Error:
        return []

    events = []
    for row in rows:
        events.append(
            ExtractedEvent(
                id=str(row["id"]),
                event_type=str(row["event_type"]),
                summary=str(row["summary"]),
                actors=json.loads(str(row["actors"])) if row["actors"] else [],
                due_date=row["due_date"],
                source_channel=str(row["source_channel"]),
                source_ref=str(row["source_ref"]),
                source_ts=int(row["source_ts"]),
                context_quote=str(row["context_quote"]),
                confidence=float(row["confidence"]),
                status=str(row["status"]),
                needs_review=bool(row["needs_review"]),
                message_id=str(row["message_id"]),
            )
        )
    return events


# ---------------------------------------------------------------------------
# LINE Push API client
# ---------------------------------------------------------------------------

class LinePushError(Exception):
    """Raised when a LINE Push API call fails."""

    def __init__(self, status_code: int, body: str, retry_after: Optional[str] = None):
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after
        super().__init__(f"LINE push {status_code}: {body[:200]}")


class LinePushClient:
    """Minimal LINE Messaging API push client using stdlib urllib.

    No aiohttp or line-bot-sdk dependency — only ``urllib.request``.
    """

    def __init__(
        self,
        channel_access_token: str,
        endpoint: str = LINE_PUSH_URL,
        timeout_seconds: float = LINE_PUSH_TIMEOUT_SECONDS,
    ):
        self._token = channel_access_token
        self._endpoint = endpoint
        self._timeout = timeout_seconds

    def push_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        """Push a text message to a LINE chat (group, room, or user).

        Returns ``{"success": True}`` on HTTP 200.
        Raises :class:`LinePushError` on non-200 responses.
        """
        chunks = _split_for_line(text)
        if not chunks:
            return {"success": True}
        messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]
        payload = {"to": chat_id, "messages": messages}
        return self._post(payload)

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(
            self._endpoint,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
                if response.status >= 400:
                    raise LinePushError(
                        response.status, body,
                        response.headers.get("Retry-After"),
                    )
                if not body:
                    return {"success": True}
                decoded = json.loads(body)
                if not isinstance(decoded, dict):
                    raise LinePushError(500, f"Non-dict response: {body[:200]}")
                return decoded
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise LinePushError(
                exc.code, body,
                exc.headers.get("Retry-After"),
            )


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _text_message(text: str) -> Dict[str, Any]:
    """Build a LINE text message object, capped to per-bubble max."""
    if len(text) > LINE_PER_BUBBLE_CHARS:
        text = text[: LINE_PER_BUBBLE_CHARS - 1] + "…"
    return {"type": "text", "text": text}


def _split_for_line(text: str, max_chars: int = LINE_SAFE_BUBBLE_CHARS) -> List[str]:
    """Split text into LINE-sized bubbles, preferring paragraph/line breaks."""

    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining and len(chunks) < LINE_MAX_MESSAGES_PER_CALL:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            remaining = ""
            break
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        if chunks:
            tail = chunks[-1]
            if len(tail) > max_chars - 1:
                tail = tail[: max_chars - 1]
            chunks[-1] = tail.rstrip() + "…"
        else:
            chunks.append(remaining[: max_chars - 1] + "…")
    return chunks


def _strip_markdown(text: str) -> str:
    """Remove Markdown that LINE can't render, preserving URLs."""

    if not text:
        return text

    def _unfence(m: re.Match) -> str:
        return m.group(1).rstrip("\n")

    text = _MD_CODE_BLOCK_RE.sub(_unfence, text)
    normalized = _MARKDOWN_TOKENS_RE.sub("", text.strip())
    for token in ("**", "__", "#", "`"):
        normalized = normalized.replace(token, "")
    return normalized.strip()


# ---------------------------------------------------------------------------
# Summary composition via LiteLLM
# ---------------------------------------------------------------------------

class SummaryComposer(Protocol):
    """Compose a Traditional-Chinese summary from extracted events."""

    def compose(self, events: List[ExtractedEvent], local_day: date) -> str:
        """Return summary text."""


class LiteLLMSummaryComposer:
    """Generate a Traditional-Chinese daily summary via LiteLLM.

    Replicates the prompt and call structure from
    ``line_ingestion.summary_service.LiteLLMSummaryComposer``.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def compose(self, events: List[ExtractedEvent], local_day: date) -> str:
        """Generate a Traditional-Chinese daily summary via LiteLLM."""

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是組織記憶助理。請用繁體中文整理 LINE 群組當日事件摘要，"
                        "聚焦決策、期限、承諾、需求與解法，保持精簡且不要虛構。"
                        "輸出必須是適合 LINE text message 的純文字；可以使用 emoji 與換行，"
                        "條列請用 1. 或 ・。禁止使用 Markdown 語法或標記，"
                        "包含 **、__、#、反引號、引用符號與表格。"
                    ),
                },
                {
                    "role": "user",
                    "content": self._event_prompt(events, local_day),
                },
            ],
            "temperature": 0,
        }
        response = self._post_json("/v1/chat/completions", payload)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("LiteLLM response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LiteLLM response content is empty")
        return _strip_markdown(content)

    def _event_prompt(self, events: List[ExtractedEvent], local_day: date) -> str:
        lines = [f"日期: {local_day.isoformat()}", "事件:"]
        for event in events:
            actors = "、".join(event.actors) if event.actors else "未標示"
            due_date = event.due_date or "無"
            happened_at = datetime.fromtimestamp(event.source_ts / 1000).strftime(
                "%H:%M"
            )
            lines.append(
                "- "
                f"type={event.event_type}; time={happened_at}; actors={actors}; "
                f"due_date={due_date}; status={event.status}; summary={event.summary}; "
                f"quote={event.context_quote}"
            )
        lines.append("請輸出適合直接推播到 LINE 群組的每日摘要。")
        return "\n".join(lines)

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read().decode("utf-8")
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise ValueError("LiteLLM response is not a JSON object")
        return decoded


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_f3_daily_summary(
    db_path: Path,
    line_channel_access_token: str,
    litellm_base_url: str,
    litellm_model: str,
    *,
    litellm_api_key: str = "",
    skip_empty_day: bool = True,
    disclosure_text: str = "本訊息由 AI 助理自動產生。",
    now: Optional[datetime] = None,
) -> SummaryRunResult:
    """Run one daily summary delivery pass for all tracked LINE groups.

    This is the main entry point called by a hermes-agent cron job.
    It:

    1. Reads the dynamic group list from ``line_ingestion``'s SQLite DB.
    2. For each group, reads today's extracted events.
    3. Composes a Traditional-Chinese summary via LiteLLM.
    4. Pushes the summary to the group via the LINE Push API.
    5. Handles 429 (quota exhausted) gracefully — logs and skips.
    6. Records successful pushes for idempotency.

    Args:
        db_path: Path to the ``line_ingestion.sqlite3`` database.
        line_channel_access_token: LINE channel access token for Push API.
        litellm_base_url: LiteLLM base URL (e.g. ``http://localhost:4000``).
        litellm_model: LiteLLM model name for summary composition.
        litellm_api_key: Optional LiteLLM API key.
        skip_empty_day: If True, skip groups with no extracted events.
        disclosure_text: Footer text appended to each summary.
        now: Optional override for the current time (for testing).

    Returns:
        :class:`SummaryRunResult` with per-group counters and error list.
    """
    current = now.astimezone() if now is not None else datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    local_day = current.date()
    local_tz = current.tzinfo or timezone.utc

    db_path = Path(db_path)
    push_log = SummaryPushLog(db_path)
    push_log.init()

    composer = LiteLLMSummaryComposer(
        litellm_base_url, litellm_model, api_key=litellm_api_key
    )
    pusher = LinePushClient(line_channel_access_token)

    groups = get_tracked_groups(db_path)
    if not groups:
        logger.warning("No tracked groups found — nothing to summarize")
        return SummaryRunResult(
            groups_checked=0,
            errors=["No tracked groups found in line_ingestion DB"],
        )

    pushed = 0
    skipped_empty = 0
    skipped_already = 0
    failed = 0
    skipped_429 = 0
    errors: List[str] = []

    for group_id in groups:
        if push_log.already_pushed(group_id, local_day):
            skipped_already += 1
            logger.info(
                "Group %s already pushed today (%s) — skipping",
                group_id, local_day.isoformat(),
            )
            continue

        events = events_for_local_day(
            db_path, group_id, local_day, SUMMARY_STATUSES, local_tz
        )
        if not events and skip_empty_day:
            skipped_empty += 1
            logger.info(
                "Group %s has no extracted events for %s — skipping",
                group_id, local_day.isoformat(),
            )
            continue

        try:
            message = _compose_message(composer, events, local_day, disclosure_text)
        except Exception as exc:
            failed += 1
            msg = f"Group {group_id}: summary composition failed: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
            continue

        try:
            pusher.push_text(group_id, message)
            push_log.record_push(group_id, local_day, group_id)
            pushed += 1
            logger.info(
                "Group %s: summary pushed successfully (%d events)",
                group_id, len(events),
            )
        except LinePushError as exc:
            if exc.status_code == HTTP_TOO_MANY_REQUESTS:
                skipped_429 += 1
                msg = (
                    f"Group {group_id}: LINE push quota exhausted (429) — "
                    f"skipping. Retry-After: {exc.retry_after}"
                )
                logger.warning(msg)
                errors.append(msg)
            else:
                failed += 1
                msg = f"Group {group_id}: LINE push failed ({exc.status_code}): {exc.body[:200]}"
                logger.error(msg)
                errors.append(msg)
        except Exception as exc:
            failed += 1
            msg = f"Group {group_id}: unexpected push error: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)

    result = SummaryRunResult(
        groups_checked=len(groups),
        groups_pushed=pushed,
        groups_skipped_empty=skipped_empty,
        groups_skipped_already_pushed=skipped_already,
        groups_failed=failed,
        groups_skipped_429=skipped_429,
        errors=errors,
    )
    logger.info("F3 daily summary run completed: %s", result)
    return result


def _compose_message(
    composer: SummaryComposer,
    events: List[ExtractedEvent],
    local_day: date,
    disclosure_text: str,
) -> str:
    """Compose the full LINE message: summary + disclosure footer."""

    if events:
        sorted_events = sorted(
            events,
            key=lambda event: (
                EVENT_TYPE_ORDER.get(event.event_type, 99),
                event.source_ts,
                event.id,
            ),
        )
        summary = composer.compose(sorted_events, local_day)
    else:
        summary = f"{local_day.isoformat()} 今日沒有已萃取或確認的事件。"
    return f"{summary.strip()}\n\n{disclosure_text.strip()}"


# ---------------------------------------------------------------------------
# Cron job script entry point
# ---------------------------------------------------------------------------

def cron_script_main() -> int:
    """Run the F3 daily summary as a cron job script.

    Reads configuration from environment variables (matching the
    ``line_ingestion`` config convention):

    - ``LINE_INGESTION_DB`` — path to the SQLite database.
    - ``LINE_CHANNEL_ACCESS_TOKEN`` — LINE Push API token.
    - ``LINE_SUMMARY_LITELLM_BASE_URL`` — LiteLLM base URL.
    - ``LINE_SUMMARY_LITELLM_MODEL`` — LiteLLM model name.
    - ``LINE_SUMMARY_LITELLM_API_KEY`` — optional LiteLLM API key.
    - ``LINE_SUMMARY_SKIP_EMPTY_DAY`` — ``true``/``false`` (default ``true``).
    - ``LINE_SUMMARY_DISCLOSURE_TEXT`` — footer text.
    """
    import os
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_path = Path(
        os.environ.get("LINE_INGESTION_DB", "data/line_ingestion.sqlite3")
    )
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    litellm_url = os.environ.get(
        "LINE_SUMMARY_LITELLM_BASE_URL", "http://localhost:4000"
    )
    litellm_model = os.environ.get(
        "LINE_SUMMARY_LITELLM_MODEL", "qwen36-27b"
    )
    litellm_api_key = os.environ.get("LINE_SUMMARY_LITELLM_API_KEY", "")
    skip_empty = os.environ.get(
        "LINE_SUMMARY_SKIP_EMPTY_DAY", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    disclosure = os.environ.get(
        "LINE_SUMMARY_DISCLOSURE_TEXT", "本訊息由 AI 助理自動產生。"
    )

    if not token:
        print("ERROR: LINE_CHANNEL_ACCESS_TOKEN is required", file=sys.stderr)
        return 1

    result = run_f3_daily_summary(
        db_path=db_path,
        line_channel_access_token=token,
        litellm_base_url=litellm_url,
        litellm_model=litellm_model,
        litellm_api_key=litellm_api_key,
        skip_empty_day=skip_empty,
        disclosure_text=disclosure,
    )

    # Print a human-readable summary for the cron job output log.
    print(
        f"F3 daily summary: checked={result.groups_checked} "
        f"pushed={result.groups_pushed} "
        f"skipped_empty={result.groups_skipped_empty} "
        f"skipped_already={result.groups_skipped_already_pushed} "
        f"failed={result.groups_failed} "
        f"skipped_429={result.groups_skipped_429}"
    )
    if result.errors:
        print("Errors:")
        for err in result.errors:
            print(f"  - {err}")

    # Non-zero exit if any group failed (not 429 — that's expected when quota is exhausted).
    return 1 if result.groups_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(cron_script_main())
