"""The deterministic notification path.

An event arrives, code matches it against the armed alerts, and a match
enqueues a sealed candidate. From there the decision to speak is code: quiet
hours hold a low-urgency candidate, a digest-style alert batches, a recent
"told you" folds into the next batch instead of pinging twice, the daily cap
suppresses, and the outbox records sent or failed with a retry time. No
unprompted model turn has to notice anything for a notification to land.

Wording is rendered by code from the candidate's structured fields through
the message catalog. The event's raw text never reaches the sentence, so
content injected into a message cannot become Aether's own words.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import AppConfig
from ..memory.timewindow import (
    in_quiet_window,
    local_stamp,
    parse_quiet_hours,
    zone,
)
from ..message_catalog import catalog
from ..origin import TurnOrigin
from ..outbound import is_deliverable
from ..tools.result import ToolOutcome
from .notifications import FAILED, HELD, QUEUED, AlertNotifications, Notification
from .sources import manifest, nearest_alternative
from .spec import (
    DIGEST,
    IMMEDIATE,
    Alert,
    AlertEvent,
    AlertSpec,
    alert_matches,
    event_fields,
    has_criteria,
    normalize_spec,
)
from .store import AlertStore, where_due

log = logging.getLogger("aether.alerts")

Notify = Callable[..., Awaitable[dict[str, str] | None]]
ActionRunner = Callable[[Alert, Any], Awaitable[bool]]

# urgency (0-3) on the 0-10 scale the interruption budget already reads
_SALIENCE = {0: 2.0, 1: 4.0, 2: 8.0, 3: 10.0}


def salience_for(urgency: int) -> float:
    return _SALIENCE.get(max(0, min(3, int(urgency))), 2.0)


class AlertEngine:
    """Matching, deciding and delivering — the whole path, one seam."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: AlertStore,
        notifications: AlertNotifications,
        scheduler: Any = None,
        timezone: str = "UTC",
        clock: Callable[[], datetime] | None = None,
        locale: str = "en",
    ) -> None:
        self._config = config  # THE live object; every gate is read per call
        self._store = store
        self._notifications = notifications
        self._scheduler = scheduler
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locale = locale
        self._notify: Notify | None = None
        self._run_action: ActionRunner | None = None
        self._wording: Any = None
        self._active: list[Alert] = []
        self._loaded = False

    # -- lifecycle -----------------------------------------------------------

    def attach_notify(self, notify: Notify) -> None:
        """Join the loop's interruption budget after boot builds it."""
        self._notify = notify

    def attach_action_runner(self, run: ActionRunner) -> None:
        """Join the loop's executor — a folded routine still passes the gate."""
        self._run_action = run

    def attach_wording(self, wording: Any) -> None:
        """Join the phrasing seam. Absent or failing, the catalog speaks."""
        self._wording = wording

    async def refresh(self) -> None:
        self._active = await self._store.list_active()
        self._loaded = True

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.refresh()

    # -- matching ------------------------------------------------------------

    async def evaluate_event(self, event: Any) -> list[str]:
        """Match one ingested event and queue one candidate per match.

        Returns the titles that matched, so the event's sealed meta can say
        why it mattered. A match never sends words from here; delivery is the
        queue's own job, so a later unprompted turn is not the delivery
        mechanism.
        """
        if not self._config.alerts.enabled or getattr(event, "noise", False):
            return []
        await self._ensure_loaded()
        matched = [
            alert
            for alert in self._active
            if event.id > alert.last_event_id and alert_matches(alert.spec, event)
        ]
        await self._advance(event.id)
        titles: list[str] = []
        for alert in matched:
            await self._notifications.enqueue(
                event_fields(alert.id, event),
                style=alert.spec.style,
            )
            titles.append(alert.title or self.describe(alert.spec))
            if alert.spec.action:
                await self._fire_action(alert, event)
        return titles

    async def _advance(self, event_id: int) -> None:
        """Move every alert's watermark past this event, once, in one
        statement — matching and non-matching alike."""
        stale = [alert for alert in self._active if alert.last_event_id < event_id]
        if not stale:
            return
        await self._store.mark_run([alert.id for alert in stale], event_id)
        for alert in stale:
            alert.last_event_id = event_id

    async def _fire_action(self, alert: Alert, event: Any) -> None:
        if self._run_action is None:
            return
        now = self._clock()
        cooldown = self._config.alerts.action_cooldown_seconds
        if (
            cooldown > 0
            and alert.last_run_at is not None
            and now < alert.last_run_at + timedelta(seconds=cooldown)
        ):
            return
        await self._store.mark_action_fired(alert.id)
        alert.last_run_at = now
        try:
            ran = await self._run_action(alert, event)
        except Exception:
            log.exception("alert %s action failed — continuing", alert.id)
            return
        # the outcome ping is code-rendered, like every other alert sentence;
        # a parked call already has its approval card, so nothing is said here
        if ran and self._notify is not None:
            await self._notify(
                await self._words(self._action_facts(alert, event)) or self.action_note(alert)
            )

    # -- delivery ------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> int:
        """Deliver everything that is due: reminder moments, held batches,
        and failed sends whose retry time arrived. Returns the messages sent."""
        now = now or self._clock()
        await self._ensure_loaded()
        await self._queue_due_reminders(now)
        return await self._deliver_pending(now)

    async def _queue_due_reminders(self, now: datetime) -> None:
        for alert in self._active:
            if alert.status != "active" or not where_due(alert, now):
                continue
            event = AlertEvent(
                alert_id=alert.id,
                event_id=0,
                source="reminder",
                kind="reminder",
                subject=alert.spec.title,
                summary=alert.spec.title,
                occurred_at=now.isoformat(),
            )
            await self._notifications.enqueue(
                event,
                style=IMMEDIATE,
                key=f"reminder:{alert.id}:{alert.spec.remind_at}",
            )

    async def _deliver_pending(self, now: datetime) -> int:
        if self._notify is None:
            return 0
        rows = await self._notifications.pending(now)
        if not rows:
            return 0
        grouped: dict[int, list[Notification]] = {}
        for row in rows:
            grouped.setdefault(row.alert_id, []).append(row)

        cap = self._config.alerts.daily_cap
        day_start, day_end = self._local_day(now)
        used = await self._notifications.sent_between(day_start, day_end)
        sent = 0
        quiet = parse_quiet_hours(self._config.agent.quiet_hours)

        for alert_id, group in grouped.items():
            alert = await self._store.get(alert_id)
            if alert is None or alert.status != "active":
                await self._notifications.mark_suppressed([row.id for row in group], "alert_gone")
                continue
            urgent = max(row.urgent for row in group)
            salience = salience_for(urgent)

            if used >= cap:
                await self._notifications.mark_suppressed([row.id for row in group], "daily_cap")
                continue

            if (
                quiet is not None
                and in_quiet_window(quiet, now.astimezone(zone(self._timezone)))
                and salience < self._config.agent.quiet_urgent_salience
            ):
                await self._notifications.mark_held(
                    [row.id for row in group], self._quiet_end(now, quiet)
                )
                continue

            wait_until = await self._batch_until(alert, group, now)
            if wait_until is not None:
                await self._notifications.mark_held([row.id for row in group], wait_until)
                continue

            digest = alert.spec.style == DIGEST or len(group) > 1
            text = await self._words(self._facts(alert, group, digest=digest)) or self._render(
                alert, group, digest=digest
            )
            refs = await self._notify(text, urgent=salience, channel=alert.spec.channel)
            ids = [row.id for row in group]
            if refs:
                await self._notifications.mark_sent(ids)
                await self._store.mark_notified([alert.id])
                if alert.spec.is_reminder:
                    await self._store.complete(alert.id)
                used += len(group)
                sent += 1
                continue
            # nothing landed: retry while the budget allows, then stop
            attempts = max(row.attempts for row in group)
            if attempts + 1 >= self._config.alerts.max_attempts:
                await self._notifications.mark_suppressed(ids, "no_surface")
            else:
                await self._notifications.mark_failed(
                    ids,
                    next_try_at=now + timedelta(minutes=self._config.alerts.retry_minutes),
                )
        return sent

    async def _batch_until(
        self, alert: Alert, group: list[Notification], now: datetime
    ) -> datetime | None:
        """When this batch should wait — a digest window, or the tail of a
        recent immediate note for the same alert. None means send now."""
        if alert.spec.style == DIGEST:
            oldest = min(row.created_at for row in group)
            ready = oldest + timedelta(minutes=self._config.alerts.digest_minutes)
            if now < ready:
                return ready
        last = await self._notifications.last_sent(alert.id)
        if last is not None:
            ready = last + timedelta(hours=self._config.alerts.tell_window_hours)
            if now < ready:
                return ready
        return None

    def _render(self, alert: Alert, group: list[Notification], *, digest: bool) -> str:
        book = catalog(self._locale)
        if alert.spec.is_reminder:
            return book.render(
                "alerts.reminder_note",
                title=alert.title or book.render("alerts.fallback_reminder"),
            )
        title = alert.title or book.render("alerts.fallback_alert")
        if digest:
            lines = [self._detail(row.event) for row in group[:20]]
            return (
                book.render("alerts.digest", title=title, count=len(group))
                + "\n"
                + "\n".join(lines)
            )
        return book.render("alerts.note", title=title, detail=self._detail(group[0].event))

    def _detail(self, event: AlertEvent) -> str:
        source = self._source_label(event.source)
        text = (
            event.subject
            or event.summary
            or event.kind
            or catalog(self._locale).render("alerts.fallback_detail")
        )
        if event.sender:
            return catalog(self._locale).render(
                "alerts.detail_from", source=source, sender=event.sender, detail=text
            )
        return catalog(self._locale).render("alerts.detail", source=source, detail=text)

    def _source_label(self, source: str) -> str:
        scope = manifest().by_key(source)
        if scope is not None:
            return scope.label
        return (source or "Aether").replace("_", " ").capitalize()

    # -- wording -------------------------------------------------------------

    async def _words(self, facts: dict[str, Any]) -> str | None:
        """The model's phrasing, or None when it is absent or unusable.

        Only the words ride this seam. The engine has already decided to
        send; a failure here falls back to the catalog sentence and never
        changes the decision.
        """
        if self._wording is None:
            return None
        try:
            words = await self._wording.compose(facts)
        except Exception:
            log.warning("alert wording failed — using the catalog sentence", exc_info=True)
            return None
        # a blank or placeholder answer is not words a person can read; the
        # catalog sentence takes over rather than sending nothing useful
        return words.strip() if is_deliverable(words) else None

    def _facts(self, alert: Alert, group: list[Notification], *, digest: bool) -> dict[str, Any]:
        """The sealed-down ground truth a phrasing pass is allowed to see."""
        book = catalog(self._locale)
        facts: dict[str, Any] = {
            "kind": "reminder" if alert.spec.is_reminder else "alert",
            "title": alert.title
            or book.render(
                "alerts.fallback_reminder" if alert.spec.is_reminder else "alerts.fallback_alert"
            ),
            "digest": digest,
            "count": len(group),
        }
        if alert.spec.is_reminder:
            facts["when"] = self.when(alert.spec)
            return facts
        facts["source"] = self._source_label(group[0].event.source)
        facts["items"] = [
            {
                "sender": row.event.sender,
                "subject": row.event.subject,
                "summary": row.event.summary,
            }
            for row in group[:20]
        ]
        return facts

    def _action_facts(self, alert: Alert, event: Any) -> dict[str, Any]:
        fields = event_fields(alert.id, event)
        return {
            "kind": "action",
            "title": alert.title or catalog(self._locale).render("alerts.fallback_alert"),
            "source": self._source_label(fields.source),
            "items": [
                {
                    "sender": fields.sender,
                    "subject": fields.subject,
                    "summary": fields.summary,
                }
            ],
            "count": 1,
            "digest": False,
        }

    # -- rendering for tools and the panel ------------------------------------

    def describe(self, spec: AlertSpec) -> str:
        """A condition in plain words, assembled from catalog pieces."""
        book = catalog(self._locale)
        parts: list[str] = []
        scope = manifest().by_key(spec.scope)
        if spec.scope and (scope is not None or spec.scope != "*"):
            parts.append(
                book.render(
                    "alerts.condition_scope",
                    label=scope.label if scope is not None else spec.scope,
                )
            )
        if spec.event:
            parts.append(
                book.render(
                    "alerts.condition_event",
                    event=manifest().label_for_kind(spec.event),
                )
            )
        if spec.sender:
            parts.append(book.render("alerts.condition_sender", sender=spec.sender))
        if spec.contains:
            parts.append(book.render("alerts.condition_contains", words=spec.contains))
        elif spec.description:
            parts.append(book.render("alerts.condition_contains", words=spec.description))
        if not parts:
            return book.render("alerts.condition_scope", label=book.render("alerts.anything"))
        return ", ".join(parts)

    def when(self, spec: AlertSpec) -> str:
        moment = self._coerce_moment(spec.remind_at)
        if moment is None:
            return spec.remind_at
        return local_stamp(moment, self._timezone)

    def confirmation(self, action: str, alert: Alert) -> str:
        """The sentence the stored record earns — never a claim about a
        write that did not happen."""
        book = catalog(self._locale)
        title = alert.title or (
            book.render("alerts.fallback_reminder")
            if alert.spec.is_reminder
            else book.render("alerts.fallback_alert")
        )
        if action == "created":
            if alert.spec.is_reminder:
                return book.render("alerts.set_reminder", title=title, when=self.when(alert.spec))
            return book.render("alerts.set", title=title, condition=self.describe(alert.spec))
        if action == "updated":
            if alert.spec.is_reminder:
                return book.render(
                    "alerts.updated_reminder", title=title, when=self.when(alert.spec)
                )
            return book.render("alerts.updated", title=title, condition=self.describe(alert.spec))
        if action == "paused":
            return book.render("alerts.paused", title=title)
        if action == "resumed":
            return book.render("alerts.resumed", title=title)
        return book.render("alerts.removed", title=title)

    def refusal(self, key: str, **params: Any) -> str:
        return catalog(self._locale).render(key, **params)

    def action_note(self, alert: Alert) -> str:
        """The sentence a folded action's success earns — code, not model."""
        book = catalog(self._locale)
        return book.render(
            "alerts.action_note",
            title=alert.title or book.render("alerts.fallback_alert"),
        )

    async def get(self, alert_id: int) -> Alert | None:
        return await self._store.get(alert_id)

    async def sections(self, limit: int = 30) -> str:
        """The list surface: three typed sections, ids included for the
        model, plain words for everything else."""
        alerts: list[Alert] = []
        reminders: list[Alert] = []
        for alert in await self._store.list(limit):
            if alert.status != "active":
                continue
            (reminders if alert.spec.is_reminder else alerts).append(alert)
        book = catalog(self._locale)
        blocks: list[str] = []
        if alerts:
            lines = [
                f"#{alert.id} "
                + book.render(
                    "alerts.list_line",
                    title=alert.title or "Alert",
                    condition=self.describe(alert.spec),
                )
                for alert in alerts
            ]
            blocks.append(book.render("alerts.list_alerts") + "\n" + "\n".join(lines))
        if reminders:
            lines = [
                f"#{alert.id} "
                + book.render(
                    "alerts.list_reminder_line",
                    title=alert.title or "Reminder",
                    when=self.when(alert.spec),
                )
                for alert in reminders
            ]
            blocks.append(book.render("alerts.list_reminders") + "\n" + "\n".join(lines))
        scheduled = await self._scheduled_lines()
        if scheduled:
            blocks.append(book.render("alerts.list_scheduled") + "\n" + "\n".join(scheduled))
        return "\n\n".join(blocks) or book.render("alerts.list_empty")

    async def _scheduled_lines(self) -> list[str]:
        if self._scheduler is None:
            return []
        try:
            rows = await self._scheduler.list(limit=30)
        except Exception:
            log.warning("could not read scheduled actions", exc_info=True)
            return []
        lines = [
            f"#{row.id} {row.label} — {local_stamp(row.run_at, self._timezone)}"
            for row in rows
            if getattr(row, "status", "") == "pending"
        ]
        return lines

    # -- mutations -----------------------------------------------------------

    def prepare(self, spec: AlertSpec) -> AlertSpec | ToolOutcome:
        """Validate a spec against the manifest and the clock.

        Returns the normalized spec, or the catalog sentence explaining why
        it cannot be armed — an unsupported scope with the nearest working
        alternative, a reminder with no usable moment, or an alert with no
        condition.
        """
        spec = normalize_spec(spec)
        if spec.is_reminder:
            spec = replace(spec, scope="", event="", sender="", contains="", description="")
            moment = self._coerce_moment(spec.remind_at)
            if moment is None:
                return ToolOutcome.refused(self.refusal("alerts.reminder_needs_time"))
            spec = replace(spec, remind_at=moment.isoformat())
            return spec
        if spec.scope:
            scope = manifest().by_key(spec.scope)
            if scope is None:
                return ToolOutcome.refused(self.refusal("alerts.scope_unknown", scope=spec.scope))
            if not scope.supports_events:
                text = self.refusal("alerts.scope_unsupported", label=scope.label)
                alternative = nearest_alternative(spec.scope)
                if alternative is not None:
                    text += self.refusal("alerts.scope_alternative", label=alternative.label)
                else:
                    text += self.refusal("alerts.scope_no_alternative", label=scope.label)
                return ToolOutcome.refused(text)
        if not has_criteria(spec):
            return ToolOutcome.refused(self.refusal("alerts.needs_condition"))
        return spec

    async def create(self, spec: AlertSpec, origin: TurnOrigin) -> ToolOutcome:
        ready = self.prepare(spec)
        if isinstance(ready, ToolOutcome):
            return ready
        made = await self._store.create(spec=ready, origin=origin)
        if made is None:
            return ToolOutcome.refused(self.refusal("alerts.refused"))
        await self.refresh()
        return ToolOutcome.ok(self.confirmation("created", made))

    async def update(self, alert_id: int, spec: AlertSpec, origin: TurnOrigin) -> ToolOutcome:
        if await self._store.get(alert_id) is None:
            return ToolOutcome.refused(self.refusal("alerts.not_found"))
        ready = self.prepare(spec)
        if isinstance(ready, ToolOutcome):
            return ready
        updated = await self._store.update(alert_id, spec=ready, origin=origin)
        if updated is None:
            return ToolOutcome.refused(self.refusal("alerts.refused"))
        await self.refresh()
        return ToolOutcome.ok(self.confirmation("updated", updated))

    async def set_enabled(self, alert_id: int, enabled: bool, origin: TurnOrigin) -> ToolOutcome:
        if await self._store.get(alert_id) is None:
            return ToolOutcome.refused(self.refusal("alerts.not_found"))
        changed = await self._store.set_enabled(alert_id, enabled, origin=origin)
        if changed is None:
            return ToolOutcome.refused(self.refusal("alerts.refused"))
        await self.refresh()
        return ToolOutcome.ok(self.confirmation("resumed" if enabled else "paused", changed))

    async def remove(self, alert_id: int, origin: TurnOrigin) -> ToolOutcome:
        existing = await self._store.get(alert_id)
        if existing is None:
            return ToolOutcome.refused(self.refusal("alerts.not_found"))
        removed = await self._store.delete(alert_id, origin=origin)
        if not removed:
            return ToolOutcome.refused(self.refusal("alerts.refused"))
        await self.refresh()
        return ToolOutcome.ok(self.confirmation("removed", existing))

    # -- clock helpers -------------------------------------------------------

    def _local_day(self, now: datetime) -> tuple[datetime, datetime]:
        local = now.astimezone(zone(self._timezone))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start.astimezone(UTC), end.astimezone(UTC)

    def _coerce_moment(self, value: str) -> datetime | None:
        raw = (value or "").strip()
        if not raw:
            return None
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone(self._timezone))
        return moment.astimezone(UTC)

    def _quiet_end(self, now: datetime, window: tuple[int, int]) -> datetime:
        local = now.astimezone(zone(self._timezone))
        hour, minute = divmod(window[1], 60)
        end = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if end <= local:
            end += timedelta(days=1)
        return end.astimezone(UTC)


def notification_statuses() -> tuple[str, ...]:
    """The vocabulary the panel renders — no free text in the column."""
    return (QUEUED, HELD, FAILED)
