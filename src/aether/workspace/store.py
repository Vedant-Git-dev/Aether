"""Aether's workspace: a small set of human-editable Markdown files that
define who Aether is, how it behaves, durable facts about the user, and
daily working notes — separate from (and much smaller than) the Postgres
event memory, which stays the source of truth for observed history.

    IDENTITY.md  — name, vibe, presentation: who Aether is
    SOUL.md      — personality, tone, behavioral boundaries (never permissions)
    AGENTS.md    — how Aether should use this workspace
    USER.md      — stable user preferences (active until superseded) and the
                   profile of who the user is (confirmed once, corrected by
                   superseding)
    MEMORY.md    — curated durable facts and decisions
    BOOTSTRAP.md — present only until first-run setup is done
    memory/YYYY-MM-DD.md — daily working notes, promoted into MEMORY.md
                           when something turns out to matter

Every file is one sealed row in `workspace_files`: the name is a keyed
blind token plus a sealed display copy, the content is sealed with AAD
bound to the row id, and decryption happens in memory only. There is no
readable copy on disk. `import_from_disk` moves an old file-backed
workspace into the table once and removes the files after each row
commits.

Every entry carries provenance in an inline HTML comment —
`<!-- id:... source:... at:... status:... -->` — so a written fact never
silently turns into an unattributed permanent one, and a preference can be
superseded instead of piling up as a contradiction.
"""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import asyncpg
from rapidfuzz import fuzz

from ..memory.crypto import Cipher, CryptoError
from ..memory.storage import (
    delete_workspace_file,
    upsert_workspace_file,
    workspace_row,
    workspace_rows,
)

log = logging.getLogger("aether.workspace")

# a compact, curated block — much smaller than the event-memory budget,
# because MEMORY.md is meant to stay small by construction
_CONTEXT_BUDGET_CHARS = 6_000

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ENTRY_RE = re.compile(r"^- (?P<text>.*) <!-- (?P<meta>[^<>]*?) -->\s*$")
_META_RE = re.compile(r"(\w+):(\S+)")
_KNOWN_META = {"id", "source", "at", "status", "confidence"}

VALID_KINDS = ("preference", "fact", "daily", "profile")

# key=value assignments and well-known token shapes — "Secrets/API keys must
# never be written into memory files" is enforced here, not just documented.
_SECRET_RE = re.compile(
    r"(api[_-]?key|secret|password|access[_-]?token|bearer)\s*[:=]\s*\S{6,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}",
    re.IGNORECASE,
)


# every encrypted-at-rest blob starts with this prefix, so a file's shape
# on disk is unambiguous: prefixed bytes decrypt, anything else is plain
# text — waiting for the one-time boot migration, or dropped in by hand
_ENC_MAGIC = b"aether-workspace\x01"


def _aad(name: str) -> str:
    """One file's AES-GCM binding to its own name — a ciphertext can't be
    swapped between files undetected, the same discipline as the
    database's table:column:record binding."""
    return f"workspace:file:{name}"


class SecretRejected(ValueError):
    """A write was refused because it looked like a secret or API key."""


class WorkspaceWriteError(RuntimeError):
    """A workspace file could not be written (disk full, permissions,
    the directory vanished underneath us). Never a reason to crash the
    turn that triggered it — the caller reports it in plain words."""


_EDITABLE_KEYS = ("identity", "soul", "agents", "user", "memory")
_VALID_SOURCES = ("agent", "user")


@dataclass(frozen=True)
class WorkspaceEntry:
    id: str
    text: str
    source: str
    at: str
    status: str
    confidence: float | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceHit:
    file: str
    id: str
    text: str
    score: float


_IDENTITY_DEFAULT = """\
# IDENTITY.md

Who Aether is, not what the user wants from it.

- Name: Aether
- Creature: Autonomous AI agent
- Vibe: Calm, observant, decisive
- Emoji: ◈
"""

_SOUL_DEFAULT = """\
# SOUL.md

Personality, communication style, and behavioral boundaries. Change
them through the control center, or by asking Aether — it is read on
every turn.

- Communicate plainly and concisely; expand only when asked for detail.
- When uncertain, say so rather than guessing with confidence.
- Treat the user's information as private: never volunteer it to a
  channel or person it didn't come from.
- This file shapes tone and judgment calls only. It never grants or
  withholds permission to act — that is the authorization policy's job
  alone, and nothing written here can override it.
"""

_AGENTS_DEFAULT = """\
# AGENTS.md

Workspace operating instructions — how Aether uses these files, not what
it says or who it is.

- Use workspace_remember(kind="preference") for something the user
  consistently wants, and kind="fact" for a durable decision or fact
  likely to matter across sessions. Use kind="daily" for context worth
  keeping only for today. Use kind="profile" for a durable fact about
  who the user is — anything you inferred yourself lands as pending and
  waits for their yes; only what they stated directly is confirmed.
- A new preference that contradicts an old one should supersede it
  (workspace_remember's `supersedes`), not sit alongside it.
- Most things belong in ordinary event memory, not here — write to this
  workspace sparingly, for what should survive and guide future turns.
- From time to time, look at today's daily notes and promote anything
  that turned out to matter into MEMORY.md with workspace_promote; let
  the rest age out unpromoted.
- Never write a secret, password, or API key into any workspace file.
"""

_USER_DEFAULT = """\
# USER.md

Stable preferences the user has stated, each active until superseded.
"""

_MEMORY_DEFAULT = """\
# MEMORY.md

Curated long-term memory: durable facts, decisions, and relationships.
Keep this compact — if it's not likely to matter in a month, it probably
belongs in daily memory instead.
"""

_BOOTSTRAP_DEFAULT = """\
# BOOTSTRAP.md

This workspace hasn't been introduced to its user yet. Next time you are
talking with them, weave a few light questions into the conversation —
don't block on this or turn it into a form:

1. What should they call you, and does the default identity (IDENTITY.md)
   suit them, or would they like it changed?
2. Anything about tone or style they want (SOUL.md)?
3. Any standing preferences worth remembering now (USER.md)?

Write what you learn with workspace_remember, change IDENTITY.md/SOUL.md
through the control center if asked, then call workspace_finish_bootstrap.
If they ask you to just get on with a task first, do the task — this can
wait for a natural opening.
"""

_DEFAULTS = {
    "IDENTITY.md": _IDENTITY_DEFAULT,
    "SOUL.md": _SOUL_DEFAULT,
    "AGENTS.md": _AGENTS_DEFAULT,
    "USER.md": _USER_DEFAULT,
    "MEMORY.md": _MEMORY_DEFAULT,
}

# context_block() order: IDENTITY -> SOUL -> AGENTS -> USER -> MEMORY, then
# BOOTSTRAP.md if first-run setup is still pending
_CONTEXT_FILES = [
    ("IDENTITY.md", "Identity"),
    ("SOUL.md", "Soul"),
    ("AGENTS.md", "Agent instructions"),
    ("USER.md", "User preferences"),
    ("MEMORY.md", "Long-term memory"),
]

_FILE_BY_KEY = {
    "identity": "IDENTITY.md",
    "soul": "SOUL.md",
    "agents": "AGENTS.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
    "bootstrap": "BOOTSTRAP.md",
}


def looks_like_secret(text: str) -> bool:
    return bool(_SECRET_RE.search(text))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_id() -> str:
    return secrets.token_hex(4)


def _strip_provenance(text: str) -> str:
    """Removes only the real, appended provenance comment from a bullet
    entry — never a '<!-- ... -->'-shaped substring the entry's own text
    happens to contain. Reuses _ENTRY_RE so the two never disagree about
    where the text ends and the provenance comment begins."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        m = _ENTRY_RE.match(stripped)
        if m is None:
            out.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}- {m.group('text')}")
    return "\n".join(out)


def _format_entry(
    text: str,
    *,
    entry_id: str,
    source: str,
    at: str,
    status: str = "active",
    confidence: float | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    meta = [f"id:{entry_id}", f"source:{source}", f"at:{at}", f"status:{status}"]
    if confidence is not None:
        try:
            meta.append(f"confidence:{float(confidence):.2f}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"confidence must be a number, got {confidence!r}") from exc
    for key, value in (extra or {}).items():
        meta.append(f"{key}:{value}")
    return f"- {text.strip()} <!-- {' '.join(meta)} -->"


def _parse_entry(line: str) -> WorkspaceEntry | None:
    m = _ENTRY_RE.match(line.strip())
    if not m:
        return None
    meta = dict(_META_RE.findall(m.group("meta")))
    try:
        confidence = float(meta["confidence"]) if "confidence" in meta else None
    except ValueError:
        confidence = None
    extra = {k: v for k, v in meta.items() if k not in _KNOWN_META}
    return WorkspaceEntry(
        id=meta.get("id", ""),
        text=m.group("text"),
        source=meta.get("source", "agent"),
        at=meta.get("at", ""),
        status=meta.get("status", "active"),
        confidence=confidence,
        extra=extra,
    )


def _set_entry_status(
    content: str,
    entry_ids: set[str],
    status: str,
    from_statuses: frozenset[str] = frozenset({"active"}),
) -> str:
    """Rewrite matching entries in place with a new status — never deletes
    a line, so the log of what was ever written stays intact. Only entries
    currently in one of `from_statuses` flip, so an already-superseded line
    is never touched again."""
    out = []
    for line in content.splitlines():
        entry = _parse_entry(line)
        if entry is not None and entry.id in entry_ids and entry.status in from_statuses:
            out.append(
                _format_entry(
                    entry.text,
                    entry_id=entry.id,
                    source=entry.source,
                    at=entry.at,
                    status=status,
                    confidence=entry.confidence,
                    extra=entry.extra,
                )
            )
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _section_entries(content: str, heading: str) -> list[WorkspaceEntry]:
    """Parsed bullet entries under one `## {heading}` section only —
    USER.md holds both preferences and the profile, and one section's
    readers must never see the other's lines."""
    out: list[WorkspaceEntry] = []
    inside = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            inside = line == f"## {heading}"
            continue
        if not inside or not line.startswith("- "):
            continue
        entry = _parse_entry(line)
        out.append(
            entry
            if entry is not None
            else WorkspaceEntry(
                id="", text=line[2:].strip(), source="human", at="", status="active"
            )
        )
    return out


def _drop_pending(content: str) -> str:
    """Entries the user hasn't confirmed yet never reach the prompt as if
    they were facts — a pending line stays in the file (the tools can see
    and supersede it) but contributes nothing to context_block."""
    out = []
    for line in content.splitlines():
        entry = _parse_entry(line)
        if entry is not None and entry.status == "pending":
            continue
        out.append(line)
    return "\n".join(out)


def _append_under_heading(content: str, heading: str, line: str) -> str:
    """Insert `line` as the last bullet under `## {heading}`, creating the
    heading at the end of the file if it isn't there yet."""
    marker = f"## {heading}"
    lines = content.splitlines()
    if marker not in lines:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(marker)
        lines.append(line)
        return "\n".join(lines) + "\n"
    start = lines.index(marker)
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines[insert_at:insert_at] = [line]
    return "\n".join(lines) + "\n"


class Workspace:
    """Database-backed: one sealed record per file in `workspace_files`.

    A file's name is a keyed blind token (equality lookup) plus a sealed
    display copy; its content is sealed with AAD bound to the row id. Reads
    decrypt in memory only. There is no readable copy on disk — the old
    file workspace is imported once and removed (see `import_from_disk`).
    """

    def __init__(self, pool: asyncpg.Pool, *, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher

    # -- lifecycle --------------------------------------------------------

    async def ensure_scaffold(self) -> bool:
        """Write the default files if the workspace is empty. Returns True
        the first time it is created — the caller's cue that first-run
        bootstrap applies. A workspace that already exists only backfills a
        fixed file a user happens to have deleted; it never resurrects a
        BOOTSTRAP.md the agent removed on purpose."""
        existing = await self._names()
        first_run = not existing
        for filename, default in _DEFAULTS.items():
            if filename not in existing:
                await self._put(filename, default)
        if first_run:
            await self._put("BOOTSTRAP.md", _BOOTSTRAP_DEFAULT)
        return first_run

    async def import_from_disk(self, root: str | Path) -> int:
        """One-time move of an old file-backed workspace into sealed rows.
        Reads plaintext and legacy encrypted files alike, writes each row,
        then removes the file only after the row is committed. A file that
        cannot be read is left exactly where it is. Returns how many files
        moved."""
        root = Path(root)
        if not root.is_dir():
            return 0
        names = [filename for filename, _ in _CONTEXT_FILES]
        if (root / "BOOTSTRAP.md").exists():
            names.append("BOOTSTRAP.md")
        memory_dir = root / "memory"
        if memory_dir.is_dir():
            names.extend(
                f"memory/{path.name}" for path in sorted(memory_dir.glob("*.md"), reverse=True)
            )
        moved = 0
        for name in names:
            path = root / name
            try:
                raw = path.read_bytes()
            except (FileNotFoundError, OSError):
                continue
            text = ""
            if raw.startswith(_ENC_MAGIC):
                try:
                    text = self._cipher.decrypt_text(raw[len(_ENC_MAGIC) :], aad=_aad(name))
                except CryptoError:
                    log.warning("workspace file %s would not decrypt — left on disk", name)
                    continue
            else:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    log.warning("workspace file %s is not text — left on disk", name)
                    continue
            await self._put(name, text)
            try:
                path.unlink()
            except OSError as exc:
                log.warning(
                    "workspace file %s moved to the database but not removed (%s)", name, exc
                )
            moved += 1
        # the directories are empty now; a non-empty one is left alone
        for directory in (root / "memory", root):
            with contextlib.suppress(OSError):
                directory.rmdir()
        return moved

    async def is_empty(self) -> bool:
        return not await self._names()

    async def export_files(self) -> dict[str, str]:
        """Every file's plaintext, for the passphrase-encrypted export."""
        return {name: await self._read_name(name) for name in await self._names(daily_cap=None)}

    async def import_files(self, files: dict[str, str]) -> int:
        """Write a decrypted export back. Only names this workspace owns
        are accepted, so an edited bundle cannot create arbitrary rows."""
        written = 0
        for name, content in files.items():
            self._check_import_name(name)
            if not isinstance(content, str):
                raise ValueError("every imported file must be text")
            if looks_like_secret(content):
                raise SecretRejected(
                    f"{name} looks like it contains a secret or API key — import refused"
                )
            await self._put(name, content)
            written += 1
        return written

    @staticmethod
    def _check_import_name(name: str) -> None:
        if name in set(_FILE_BY_KEY.values()) or name == "BOOTSTRAP.md":
            return
        prefix, _, suffix = name.partition("/")
        if prefix == "memory" and suffix.endswith(".md") and _DATE_RE.match(suffix[:-3]):
            return
        raise ValueError(f"the export contains an unknown file: {name!r}")

    async def needs_bootstrap(self) -> bool:
        return await self._row("BOOTSTRAP.md") is not None

    async def finish_bootstrap(self) -> bool:
        existed = await self.needs_bootstrap()
        if existed:
            await self._delete("BOOTSTRAP.md")
        return existed

    def today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    # -- reading ------------------------------------------------------------

    async def read(self, file: str, *, date_str: str | None = None) -> str:
        """Raw file content (provenance comments kept) — "" if missing or
        no longer decryptable. `file` is one of
        identity/soul/agents/user/memory/bootstrap/daily; `daily` needs
        `date_str` (YYYY-MM-DD), defaulting to today."""
        name = self._resolve(file, date_str)
        return await self._read_name(name) if name is not None else ""

    async def entries(self, file: str, *, date_str: str | None = None) -> list[WorkspaceEntry]:
        """Parsed bullet entries for one file, in order. A line with no
        provenance comment — something a human typed straight into the
        file — comes back with source="human" and no id, so generated and
        human-authored content stay visually distinguishable without
        polluting the Markdown with a tag humans have to write by hand."""
        out: list[WorkspaceEntry] = []
        for raw_line in (await self.read(file, date_str=date_str)).splitlines():
            line = raw_line.strip()
            if not line.startswith("- "):
                continue
            entry = _parse_entry(line)
            out.append(
                entry
                if entry is not None
                else WorkspaceEntry(
                    id="", text=line[2:].strip(), source="human", at="", status="active"
                )
            )
        return out

    async def context_block(self) -> str:
        """IDENTITY -> SOUL -> AGENTS -> USER -> MEMORY, provenance
        stripped, bounded to a compact budget — plus BOOTSTRAP.md while
        first-run setup is still pending. A missing or malformed file
        contributes nothing rather than breaking the turn."""
        sections: list[str] = []
        used = 0
        for filename, heading in _CONTEXT_FILES:
            body = _strip_provenance(_drop_pending(await self._read_name(filename))).strip()
            if not body:
                continue
            block = f"## {heading} ({filename})\n{body}"
            if used + len(block) > _CONTEXT_BUDGET_CHARS:
                break
            sections.append(block)
            used += len(block)
        bootstrap = (await self._read_name("BOOTSTRAP.md")).strip()
        if bootstrap:
            sections.append(f"## First-run setup (BOOTSTRAP.md)\n{bootstrap}")
        if not sections:
            return ""
        return "[workspace]\n" + "\n\n".join(sections)

    # -- writing ------------------------------------------------------------

    async def remember(
        self,
        text: str,
        kind: str,
        *,
        source: str = "agent",
        confidence: float | None = None,
        category: str | None = None,
        supersedes: list[str] | None = None,
        when: date | None = None,
    ) -> str:
        """Write one durable (preference/fact/profile) or temporary (daily)
        entry. Returns the new entry's id. A profile entry is confirmed
        when the user stated it themselves and pending when the agent
        inferred it — pending until confirm_profile (or a /profile yes)
        flips it. Raises SecretRejected if the text looks like an API key,
        password, or token, ValueError for bad input, and
        WorkspaceWriteError if the row couldn't be saved — never silently
        drops a write."""
        text = text.strip()
        if not text:
            raise ValueError("remember needs non-empty text")
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
        if source not in _VALID_SOURCES:
            raise ValueError(f"source must be one of {_VALID_SOURCES}, got {source!r}")
        if looks_like_secret(text):
            raise SecretRejected("that looks like a secret or API key — refusing to store it")

        entry_id = _new_id()
        status = ("confirmed" if source == "user" else "pending") if kind == "profile" else "active"
        line = _format_entry(
            text,
            entry_id=entry_id,
            source=source,
            at=_now_iso(),
            status=status,
            confidence=confidence,
        )

        if kind == "profile":
            content = await self._read_name("USER.md") or _USER_DEFAULT
            if supersedes:
                content = _set_entry_status(
                    content,
                    set(supersedes),
                    "superseded",
                    from_statuses=frozenset({"active", "pending"}),
                )
            content = _append_under_heading(content, "Profile", line)
            await self._put("USER.md", content)
        elif kind == "preference":
            content = await self._read_name("USER.md") or _USER_DEFAULT
            if supersedes:
                content = _set_entry_status(content, set(supersedes), "superseded")
            content = _append_under_heading(content, "Preferences", line)
            await self._put("USER.md", content)
        elif kind == "fact":
            content = await self._read_name("MEMORY.md") or _MEMORY_DEFAULT
            content = _append_under_heading(content, (category or "Notes").strip(), line)
            await self._put("MEMORY.md", content)
        else:  # daily
            day = when or datetime.now(UTC).date()
            name = f"memory/{day.isoformat()}.md"
            content = await self._read_name(name) or f"# {day.isoformat()}\n"
            content = content.rstrip("\n") + "\n" + line + "\n"
            await self._put(name, content)
        return entry_id

    # -- profile: durable facts about the user, pending until confirmed ----

    async def profile(self) -> list[WorkspaceEntry]:
        """The confirmed profile — who the user is, as they have signed off
        on it. Pending guesses and superseded corrections are excluded."""
        content = await self._read_name("USER.md")
        return [e for e in _section_entries(content, "Profile") if e.status == "confirmed"]

    async def pending_profile(self) -> list[WorkspaceEntry]:
        """Profile entries the agent inferred that are still waiting for
        one yes — the batch the confirmation message lists."""
        content = await self._read_name("USER.md")
        return [e for e in _section_entries(content, "Profile") if e.status == "pending"]

    async def confirm_profile(self, entry_ids: list[str]) -> int:
        """Flip pending profile entries to confirmed. Entries that were
        already confirmed, superseded, or unknown are left alone; returns
        how many actually flipped."""
        ids = {i for i in entry_ids if i}
        if not ids:
            return 0
        content = await self._read_name("USER.md")
        if not content:
            return 0
        targets = {e.id for e in _section_entries(content, "Profile") if e.status == "pending"}
        targets &= ids
        if not targets:
            return 0
        await self._put(
            "USER.md",
            _set_entry_status(content, targets, "confirmed", from_statuses=frozenset({"pending"})),
        )
        return len(targets)

    async def promote(self, date_str: str, entry_id: str, *, category: str | None = None) -> bool:
        """Copy one active daily entry into MEMORY.md, carrying its
        provenance forward, and mark the original 'promoted' (never
        deleted — the daily log stays an immutable record)."""
        self._check_date(date_str)
        daily_name = f"memory/{date_str}.md"
        content = await self._read_name(daily_name)
        if not content:
            return False
        found: WorkspaceEntry | None = None
        for line in content.splitlines():
            entry = _parse_entry(line)
            if entry is not None and entry.id == entry_id and entry.status == "active":
                found = entry
                break
        if found is None:
            return False
        await self._put(daily_name, _set_entry_status(content, {entry_id}, "promoted"))

        mem_content = await self._read_name("MEMORY.md") or _MEMORY_DEFAULT
        new_line = _format_entry(
            found.text,
            entry_id=_new_id(),
            source=found.source,
            at=_now_iso(),
            confidence=found.confidence,
            extra={"promoted_from": f"{date_str}#{entry_id}"},
        )
        mem_content = _append_under_heading(mem_content, (category or "Notes").strip(), new_line)
        await self._put("MEMORY.md", mem_content)
        return True

    async def write_raw(self, file: str, text: str) -> None:
        """Replace one of the five curated files wholesale — the human
        edit path (a control-center save, or the agent's workspace_rewrite
        tool for identity/soul only). Restricted to a fixed set of keys: a
        caller can never point this at an arbitrary row, and it refuses
        anything that looks like a secret, same as remember()."""
        key = file.strip().lower()
        if key not in _EDITABLE_KEYS:
            raise ValueError(f"file must be one of {_EDITABLE_KEYS}, got {file!r}")
        if looks_like_secret(text):
            raise SecretRejected("that looks like a secret or API key — refusing to store it")
        await self._put(_FILE_BY_KEY[key], text)

    # -- search -----------------------------------------------------------

    async def search(self, query: str, limit: int = 10) -> list[WorkspaceHit]:
        """Deterministic fuzzy keyword search across every workspace file —
        the fixed five, BOOTSTRAP.md if present, and daily notes (most
        recent first, capped so an old workspace can't make every search
        slow)."""
        query = query.strip().lower()
        if not query:
            return []
        scored: list[tuple[float, WorkspaceHit]] = []
        for name in await self._names():
            content = await self._read_name(name)
            if not content:
                continue
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                entry = _parse_entry(line)
                text = entry.text if entry is not None else line.lstrip("- ").strip()
                if not text:
                    continue
                score = fuzz.partial_ratio(query, text.lower())
                if score > 40:
                    scored.append(
                        (
                            score,
                            WorkspaceHit(
                                file=name, id=entry.id if entry else "", text=text, score=score
                            ),
                        )
                    )
        scored.sort(key=lambda t: t[0], reverse=True)
        return [hit for _, hit in scored[:limit]]

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _check_date(date_str: str) -> None:
        if not _DATE_RE.match(date_str):
            raise ValueError(f"date must look like YYYY-MM-DD, got {date_str!r}")

    def _resolve(self, file: str, date_str: str | None) -> str | None:
        key = file.strip().lower()
        if key == "daily":
            day = date_str or datetime.now(UTC).date().isoformat()
            self._check_date(day)
            return f"memory/{day}.md"
        return _FILE_BY_KEY.get(key)

    def _token(self, name: str) -> bytes:
        return self._cipher.blind_token("workspace", name)

    async def _row(self, name: str) -> asyncpg.Record | None:
        return await workspace_row(self._pool, self._token(name))

    async def _read_name(self, name: str) -> str:
        """One file's text, decrypted in memory. A missing row or one that
        no longer decrypts degrades to empty — the agent keeps running, it
        just sees less context."""
        row = await self._row(name)
        if row is None or not row["content_enc"]:
            return ""
        try:
            return self._cipher.decrypt_text(
                row["content_enc"], aad=f"workspace_files:content_enc:{row['id']}"
            )
        except CryptoError:
            log.warning("workspace file %r would not decrypt — treated as empty", name)
            return ""

    async def _put(self, name: str, content: str) -> None:
        """Upsert one file. Raises WorkspaceWriteError when the database
        refuses — never silently drops a write."""
        try:
            await upsert_workspace_file(
                self._pool,
                self._cipher,
                token=self._token(name),
                name=name,
                content=content,
            )
        except (asyncpg.PostgresError, CryptoError) as exc:
            log.warning("could not write workspace file %r (%s)", name, exc)
            raise WorkspaceWriteError(f"could not save {name}: {exc}") from exc

    async def _delete(self, name: str) -> None:
        await delete_workspace_file(self._pool, self._token(name))

    async def _names(self, *, daily_cap: int | None = 60) -> list[str]:
        """Every stored file name in display order: the fixed set,
        BOOTSTRAP.md if pending, then daily notes most recent first (capped
        for search), then anything else imported by hand."""
        rows = await workspace_rows(self._pool)
        names: list[str] = []
        for row in rows:
            try:
                names.append(
                    self._cipher.decrypt_text(
                        row["name_enc"], aad=f"workspace_files:name_enc:{row['id']}"
                    )
                )
            except CryptoError:
                log.warning("workspace row %s has an unreadable name — skipped", row["id"])
        present = set(names)
        ordered = [filename for filename, _ in _CONTEXT_FILES if filename in present]
        if "BOOTSTRAP.md" in present:
            ordered.append("BOOTSTRAP.md")
        dailies = sorted((n for n in present if n.startswith("memory/")), reverse=True)
        if daily_cap is not None:
            dailies = dailies[:daily_cap]
        ordered.extend(dailies)
        ordered.extend(sorted(present - set(ordered)))
        return ordered
