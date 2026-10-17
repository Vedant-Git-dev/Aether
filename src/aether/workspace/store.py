"""Aether's workspace: a small set of human-editable Markdown files that
define who Aether is, how it behaves, durable facts about the user, and
daily working notes — separate from (and much smaller than) the Postgres
event memory, which stays the source of truth for observed history.

    IDENTITY.md  — name, vibe, presentation: who Aether is
    SOUL.md      — personality, tone, behavioral boundaries (never permissions)
    AGENTS.md    — how Aether should use this workspace
    USER.md      — stable user preferences (active until superseded)
    MEMORY.md    — curated durable facts and decisions
    BOOTSTRAP.md — present only until first-run setup is done
    memory/YYYY-MM-DD.md — daily working notes, promoted into MEMORY.md
                           when something turns out to matter

At rest these files are encrypted with the same AES-256-GCM key as the
database (a deployment can turn that off). The trade is honest: raw disk
editing is gone — edits go through the control center or the workspace
tools, which decrypt in memory — and a file whose key no longer exists
reads as empty, never as text. File names and sizes stay visible
metadata, the same trade the database rows already make.

Every entry carries provenance in an inline HTML comment —
`<!-- id:... source:... at:... status:... -->` — so a written fact never
silently turns into an unattributed permanent one, and a preference can be
superseded instead of piling up as a contradiction.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from rapidfuzz import fuzz

from ..memory.crypto import Cipher, CryptoError

log = logging.getLogger("aether.workspace")

# a compact, curated block — much smaller than the event-memory budget,
# because MEMORY.md is meant to stay small by construction
_CONTEXT_BUDGET_CHARS = 6_000

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ENTRY_RE = re.compile(r"^- (?P<text>.*) <!-- (?P<meta>[^<>]*?) -->\s*$")
_META_RE = re.compile(r"(\w+):(\S+)")
_KNOWN_META = {"id", "source", "at", "status", "confidence"}

VALID_KINDS = ("preference", "fact", "daily")

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
  keeping only for today.
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


def _set_entry_status(content: str, entry_ids: set[str], status: str) -> str:
    """Rewrite matching active entries in place with a new status — never
    deletes a line, so the log of what was ever written stays intact."""
    out = []
    for line in content.splitlines():
        entry = _parse_entry(line)
        if entry is not None and entry.id in entry_ids and entry.status == "active":
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
    """Filesystem-backed. All reads/writes are plain, synchronous file I/O
    — the files are small and local, same spirit as config.py reading
    config.yaml straight off disk. With a cipher, every file sits on disk
    as an AES-256-GCM blob and is decrypted only in memory, so
    context_block and every workspace tool work unchanged."""

    def __init__(self, root: str | Path, *, cipher: Cipher | None = None) -> None:
        self.root = Path(root)
        # no cipher = the deployment's off switch: plain text files again
        self.cipher = cipher

    # -- lifecycle --------------------------------------------------------

    def ensure_scaffold(self) -> bool:
        """Create the workspace directory and default files if missing.
        Returns True the first time this workspace is created — the
        caller's cue that first-run bootstrap applies. A workspace that
        already exists only backfills a fixed file a user happens to have
        deleted; it never resurrects a BOOTSTRAP.md the agent removed on
        purpose. Raises WorkspaceWriteError if the directory itself can't
        be created (permissions, a full disk) — the caller decides whether
        that's fatal; the workspace is an enhancement, not core plumbing."""
        first_run = not self.root.exists()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "memory").mkdir(exist_ok=True)
            for filename, default in _DEFAULTS.items():
                path = self.root / filename
                if not path.exists():
                    self._write(path, filename, default)
            if first_run:
                bootstrap = self.root / "BOOTSTRAP.md"
                if not bootstrap.exists():
                    self._write(bootstrap, "BOOTSTRAP.md", _BOOTSTRAP_DEFAULT)
        except OSError as exc:
            raise WorkspaceWriteError(
                f"could not set up the workspace at {self.root}: {exc}"
            ) from exc
        return first_run

    def migrate_plaintext(self) -> int:
        """The one-time boot step for a workspace that just gained
        encryption: rewrite every plaintext file encrypted, in place.
        Already-encrypted files are left alone, and a file that can't be
        rewritten stays plaintext and readable — nothing is ever destroyed.
        Returns how many files were converted."""
        if self.cipher is None:
            return 0
        converted = 0
        for name in self._file_names(daily_cap=None):
            path = self.root / name
            try:
                raw = path.read_bytes()
            except (FileNotFoundError, OSError):
                continue
            if raw.startswith(_ENC_MAGIC):
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue  # binary junk — the read path already treats it as empty
            try:
                self._write(path, name, text)
            except WorkspaceWriteError:
                continue
            converted += 1
            log.info("workspace file %s rewritten encrypted at rest", name)
        return converted

    def needs_bootstrap(self) -> bool:
        return (self.root / "BOOTSTRAP.md").exists()

    def finish_bootstrap(self) -> bool:
        path = self.root / "BOOTSTRAP.md"
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise WorkspaceWriteError(f"could not remove BOOTSTRAP.md: {exc}") from exc
        return True

    def today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    # -- reading ------------------------------------------------------------

    def read(self, file: str, *, date_str: str | None = None) -> str:
        """Raw file content (provenance comments kept) — "" if missing,
        unreadable, or malformed. `file` is one of
        identity/soul/agents/user/memory/bootstrap/daily; `daily` needs
        `date_str` (YYYY-MM-DD), defaulting to today."""
        path = self._resolve(file, date_str)
        return self._read(path[0], path[1]) if path is not None else ""

    def entries(self, file: str, *, date_str: str | None = None) -> list[WorkspaceEntry]:
        """Parsed bullet entries for one file, in order. A line with no
        provenance comment — something a human typed straight into the
        file — comes back with source="human" and no id, so generated and
        human-authored content stay visually distinguishable without
        polluting the Markdown with a tag humans have to write by hand."""
        out: list[WorkspaceEntry] = []
        for raw_line in self.read(file, date_str=date_str).splitlines():
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

    def context_block(self) -> str:
        """IDENTITY -> SOUL -> AGENTS -> USER -> MEMORY, provenance
        stripped, bounded to a compact budget — plus BOOTSTRAP.md while
        first-run setup is still pending. A missing or malformed file
        contributes nothing rather than breaking the turn."""
        sections: list[str] = []
        used = 0
        for filename, heading in _CONTEXT_FILES:
            body = _strip_provenance(self._read(self.root / filename, filename)).strip()
            if not body:
                continue
            block = f"## {heading} ({filename})\n{body}"
            if used + len(block) > _CONTEXT_BUDGET_CHARS:
                break
            sections.append(block)
            used += len(block)
        bootstrap = self._read(self.root / "BOOTSTRAP.md", "BOOTSTRAP.md").strip()
        if bootstrap:
            sections.append(f"## First-run setup (BOOTSTRAP.md)\n{bootstrap}")
        if not sections:
            return ""
        return "[workspace]\n" + "\n\n".join(sections)

    # -- writing ------------------------------------------------------------

    def remember(
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
        """Write one durable (preference/fact) or temporary (daily) entry.
        Returns the new entry's id. Raises SecretRejected if the text looks
        like an API key, password, or token, ValueError for bad input, and
        WorkspaceWriteError if the file couldn't be saved — never silently
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
        line = _format_entry(
            text, entry_id=entry_id, source=source, at=_now_iso(), confidence=confidence
        )

        if kind == "preference":
            path = self.root / "USER.md"
            content = self._read(path, "USER.md") or _USER_DEFAULT
            if supersedes:
                content = _set_entry_status(content, set(supersedes), "superseded")
            content = _append_under_heading(content, "Preferences", line)
            self._write(path, "USER.md", content)
        elif kind == "fact":
            path = self.root / "MEMORY.md"
            content = self._read(path, "MEMORY.md") or _MEMORY_DEFAULT
            content = _append_under_heading(content, (category or "Notes").strip(), line)
            self._write(path, "MEMORY.md", content)
        else:  # daily
            day = when or datetime.now(UTC).date()
            name = f"memory/{day.isoformat()}.md"
            path = self._daily_path(day.isoformat())
            try:
                path.parent.mkdir(exist_ok=True)
            except OSError as exc:
                raise WorkspaceWriteError(f"could not create {path.parent}: {exc}") from exc
            content = self._read(path, name) or f"# {day.isoformat()}\n"
            content = content.rstrip("\n") + "\n" + line + "\n"
            self._write(path, name, content)
        return entry_id

    def promote(self, date_str: str, entry_id: str, *, category: str | None = None) -> bool:
        """Copy one active daily entry into MEMORY.md, carrying its
        provenance forward, and mark the original 'promoted' (never
        deleted — the daily log stays an immutable record)."""
        daily_path = self._daily_path(date_str)
        daily_name = f"memory/{date_str}.md"
        content = self._read(daily_path, daily_name)
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
        self._write(daily_path, daily_name, _set_entry_status(content, {entry_id}, "promoted"))

        memory_path = self.root / "MEMORY.md"
        mem_content = self._read(memory_path, "MEMORY.md") or _MEMORY_DEFAULT
        new_line = _format_entry(
            found.text,
            entry_id=_new_id(),
            source=found.source,
            at=_now_iso(),
            confidence=found.confidence,
            extra={"promoted_from": f"{date_str}#{entry_id}"},
        )
        mem_content = _append_under_heading(mem_content, (category or "Notes").strip(), new_line)
        self._write(memory_path, "MEMORY.md", mem_content)
        return True

    def write_raw(self, file: str, text: str) -> None:
        """Replace one of the five curated files wholesale — the human
        edit path (a control-center save, or the agent's workspace_rewrite
        tool for identity/soul only). Restricted to a fixed set of keys: a
        caller can never point this at an arbitrary path, and it refuses
        anything that looks like a secret, same as remember()."""
        key = file.strip().lower()
        if key not in _EDITABLE_KEYS:
            raise ValueError(f"file must be one of {_EDITABLE_KEYS}, got {file!r}")
        if looks_like_secret(text):
            raise SecretRejected("that looks like a secret or API key — refusing to store it")
        self._write(self.root / _FILE_BY_KEY[key], _FILE_BY_KEY[key], text)

    # -- search -----------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[WorkspaceHit]:
        """Deterministic fuzzy keyword search across every workspace file —
        the fixed five, BOOTSTRAP.md if present, and daily notes (most
        recent first, capped so an old workspace can't make every search
        slow)."""
        query = query.strip().lower()
        if not query:
            return []
        scored: list[tuple[float, WorkspaceHit]] = []
        for relpath, content in self._iter_files():
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
                                file=relpath, id=entry.id if entry else "", text=text, score=score
                            ),
                        )
                    )
        scored.sort(key=lambda t: t[0], reverse=True)
        return [hit for _, hit in scored[:limit]]

    # -- internals ----------------------------------------------------------

    def _read(self, path: Path, name: str) -> str:
        """One file's text, decrypting when this workspace encrypts at
        rest. Never raises: a missing file, an unreadable one, or one that
        no longer decrypts (a rotated-away key) degrades to empty — the
        agent keeps running, it just sees less context."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            log.warning("workspace file %s unreadable (%s) — treated as empty", path, exc)
            return ""
        if self.cipher is not None and raw.startswith(_ENC_MAGIC):
            try:
                return self.cipher.decrypt_text(raw[len(_ENC_MAGIC) :], _aad(name))
            except CryptoError as exc:
                log.warning(
                    "workspace file %s would not decrypt (%s) — treated as empty", path, exc
                )
                return ""
        # no cipher, or plaintext bytes — a hand-written file still reads,
        # and stays readable until the store's next write encrypts it
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            log.warning("workspace file %s is not text (%s) — treated as empty", path, exc)
            return ""

    def _write(self, path: Path, name: str, content: str) -> None:
        """One file, sealed as an AES-256-GCM blob when a cipher is set.
        Raises WorkspaceWriteError when the disk refuses — never silently
        drops a write."""
        data = (
            _ENC_MAGIC + self.cipher.encrypt_text(content, _aad(name))
            if self.cipher is not None
            else content.encode("utf-8")
        )
        try:
            path.write_bytes(data)
        except OSError as exc:
            log.warning("could not write workspace file %s (%s)", path, exc)
            raise WorkspaceWriteError(f"could not write {path.name}: {exc}") from exc

    def _daily_path(self, date_str: str) -> Path:
        if not _DATE_RE.match(date_str):
            raise ValueError(f"date must look like YYYY-MM-DD, got {date_str!r}")
        return self.root / "memory" / f"{date_str}.md"

    def _resolve(self, file: str, date_str: str | None) -> tuple[Path, str] | None:
        key = file.strip().lower()
        if key == "daily":
            day = date_str or datetime.now(UTC).date().isoformat()
            return self._daily_path(day), f"memory/{day}.md"
        filename = _FILE_BY_KEY.get(key)
        return (self.root / filename, filename) if filename else None

    def _file_names(self, *, daily_cap: int | None = 60) -> list[str]:
        """Every workspace file's root-relative name: the fixed set, a
        pending BOOTSTRAP.md, and the daily notes (most recent first).
        Search caps the dailies so an old workspace can't make it slow;
        the one-time migration passes None — it converts every file."""
        names = [filename for filename, _ in _CONTEXT_FILES]
        if (self.root / "BOOTSTRAP.md").exists():
            names.append("BOOTSTRAP.md")
        daily_dir = self.root / "memory"
        if daily_dir.is_dir():
            dailies = sorted(daily_dir.glob("*.md"), reverse=True)
            if daily_cap is not None:
                dailies = dailies[:daily_cap]
            names.extend(f"memory/{path.name}" for path in dailies)
        return names

    def _iter_files(self):
        for name in self._file_names():
            content = self._read(self.root / name, name)
            if content:
                yield name, content
