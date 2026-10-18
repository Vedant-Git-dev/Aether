"""Workspace store tests — plain filesystem I/O, no DB and no event loop
needed, so these run as ordinary sync tests against tmp_path."""

from __future__ import annotations

from datetime import date

import pytest

from aether.memory.crypto import Cipher
from aether.workspace import SecretRejected, Workspace, WorkspaceWriteError


def _ws(tmp_path) -> Workspace:
    return Workspace(tmp_path / "ws")


# ---------------------------------------------------------------------------
# scaffold / bootstrap
# ---------------------------------------------------------------------------


def test_ensure_scaffold_creates_every_fixed_file_and_reports_first_run(tmp_path) -> None:
    ws = _ws(tmp_path)
    assert ws.ensure_scaffold() is True
    for name in ("IDENTITY.md", "SOUL.md", "AGENTS.md", "USER.md", "MEMORY.md", "BOOTSTRAP.md"):
        assert (ws.root / name).exists(), name
    assert (ws.root / "memory").is_dir()
    assert ws.needs_bootstrap() is True


def test_ensure_scaffold_is_idempotent_and_never_overwrites(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    (ws.root / "IDENTITY.md").write_text("# IDENTITY.md\n\n- Name: Renamed\n", encoding="utf-8")
    assert ws.ensure_scaffold() is False  # not a first run the second time
    assert "Renamed" in ws.read("identity")


def test_ensure_scaffold_never_resurrects_a_finished_bootstrap(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    assert ws.finish_bootstrap() is True
    ws.ensure_scaffold()  # a later boot, same workspace directory
    assert ws.needs_bootstrap() is False


def test_ensure_scaffold_backfills_a_file_the_user_deleted(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    (ws.root / "SOUL.md").unlink()
    ws.ensure_scaffold()
    assert (ws.root / "SOUL.md").exists()


def test_finish_bootstrap_is_false_when_nothing_was_pending(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.finish_bootstrap()
    assert ws.finish_bootstrap() is False


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def test_read_is_empty_before_scaffolding(tmp_path) -> None:
    ws = _ws(tmp_path)
    assert ws.read("identity") == ""
    assert ws.read("daily") == ""


def test_read_unknown_file_key_is_empty(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    assert ws.read("nonsense") == ""


def test_read_daily_defaults_to_today(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    entry_id = ws.remember("shipped the release", "daily")
    today = date.today().isoformat()
    assert ws.read("daily") == ws.read("daily", date_str=today)
    assert entry_id in ws.read("daily")


def test_daily_date_must_look_like_an_iso_date(tmp_path) -> None:
    """A path-traversal attempt in the date must fail closed, not be
    silently joined onto the workspace root."""
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    with pytest.raises(ValueError):
        ws.read("daily", date_str="../../etc/passwd")
    with pytest.raises(ValueError):
        ws.promote("not-a-date", "abc123")


# ---------------------------------------------------------------------------
# context_block — the IDENTITY -> SOUL -> AGENTS -> USER -> MEMORY order
# ---------------------------------------------------------------------------


def test_context_block_orders_sections_and_strips_provenance(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("prefers short replies", "preference")
    block = ws.context_block()
    assert block.startswith("[workspace]")
    # headings, not bare filenames — AGENTS.md's own body mentions
    # "MEMORY.md" in prose, which would otherwise match too early
    identity_at = block.index("## Identity")
    soul_at = block.index("## Soul")
    agents_at = block.index("## Agent instructions")
    user_at = block.index("## User preferences")
    memory_at = block.index("## Long-term memory")
    assert identity_at < soul_at < agents_at < user_at < memory_at
    assert "<!--" not in block  # provenance is for the agent's tools, not the prompt
    assert "prefers short replies" in block
    assert "First-run setup" in block  # BOOTSTRAP.md still present


def test_context_block_drops_bootstrap_once_finished(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.finish_bootstrap()
    assert "First-run setup" not in ws.context_block()


def test_context_block_is_empty_before_scaffolding(tmp_path) -> None:
    assert _ws(tmp_path).context_block() == ""


def test_context_block_preserves_text_containing_html_comment_markers(tmp_path) -> None:
    """A literal '<!--' inside the user's own text must survive into the
    model's actual per-turn context — only the real, appended provenance
    comment gets stripped."""
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("explain what <!-- means in HTML", "preference")
    block = ws.context_block()
    assert "explain what <!-- means in HTML" in block
    assert "status:active" not in block  # the real provenance is still gone


def test_context_block_preserves_text_with_multiple_comment_blocks(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    text = "see <!-- note a --> and also <!-- note b --> for details"
    ws.remember(text, "preference")
    block = ws.context_block()
    assert text in block
    assert "status:active" not in block


# ---------------------------------------------------------------------------
# remember — preferences, facts, daily notes
# ---------------------------------------------------------------------------


def test_remember_rejects_bad_kind_and_empty_text(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    with pytest.raises(ValueError):
        ws.remember("text", "not-a-kind")
    with pytest.raises(ValueError):
        ws.remember("   ", "fact")


def test_remember_fact_goes_under_a_category_heading(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("the user works on Aether", "fact", category="Projects")
    memory = ws.read("memory")
    assert "## Projects" in memory
    assert "the user works on Aether" in memory


def test_remember_fact_defaults_to_notes_category(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("approvals default to 24h", "fact")
    assert "## Notes" in ws.read("memory")


def test_remember_preference_supersedes_an_older_one(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    old_id = ws.remember("prefers email for urgent things", "preference")
    ws.remember("prefers telegram for urgent things", "preference", supersedes=[old_id])
    user = ws.read("user")
    assert "status:superseded" in [line for line in user.splitlines() if old_id in line][0]
    assert "status:active" in [line for line in user.splitlines() if "telegram" in line][0]


def test_remember_daily_creates_one_file_per_day(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("note one", "daily", when=date(2026, 1, 1))
    ws.remember("note two", "daily", when=date(2026, 1, 2))
    assert (ws.root / "memory" / "2026-01-01.md").exists()
    assert (ws.root / "memory" / "2026-01-02.md").exists()
    assert "note one" not in ws.read("daily", date_str="2026-01-02")


def test_remember_refuses_things_that_look_like_secrets(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    for text in (
        "api_key: sk-abcdefghijklmnopqrstuvwxyz0123456789",
        "AWS key is AKIAABCDEFGHIJKLMNOP",
        "github token ghp_abcdefghijklmnopqrstuvwxyz012345",
        "password: hunter2345",
    ):
        with pytest.raises(SecretRejected):
            ws.remember(text, "fact")
    assert "## Notes" not in ws.read("memory")


def test_remember_each_entry_gets_a_unique_id(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    first = ws.remember("fact one", "fact")
    second = ws.remember("fact two", "fact")
    assert first != second


def test_remember_preserves_text_containing_html_comment_markers(tmp_path) -> None:
    """A literal '<!--' inside the user's own text must not be mistaken for
    the start of the provenance comment — the non-greedy version of this
    regex used to truncate the stored text right after it."""
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("explain what <!-- means in HTML", "fact")
    entry = ws.entries("memory")[0]
    assert entry.text == "explain what <!-- means in HTML"


def test_remember_preserves_text_containing_multiple_comment_blocks(tmp_path) -> None:
    """More than one '<!-- ... -->'-shaped substring in the user's own text
    must not confuse which block is the real, appended provenance comment —
    the greedy text match has to land on the last one, not the first."""
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    text = "see <!-- note a --> and also <!-- note b --> for details"
    ws.remember(text, "fact")
    entry = ws.entries("memory")[0]
    assert entry.text == text
    assert entry.id  # the real provenance comment was still parsed out correctly
    assert entry.source == "agent"


def test_remember_rejects_non_numeric_confidence(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    with pytest.raises(ValueError):
        # the bad type is the point — remember() must refuse it at runtime
        ws.remember("x", "fact", confidence=["not", "a", "number"])  # type: ignore[arg-type]


def test_remember_daily_raises_workspace_write_error_when_memory_dir_is_blocked(tmp_path) -> None:
    """If the daily directory can't be created, remember() must raise a
    clean WorkspaceWriteError instead of a raw OSError."""
    ws = _ws(tmp_path)
    ws.root.mkdir(parents=True)
    (ws.root / "memory").write_text("i am a file, not a directory", encoding="utf-8")
    with pytest.raises(WorkspaceWriteError):
        ws.remember("note", "daily")


# ---------------------------------------------------------------------------
# promote — daily -> long-term memory, with provenance
# ---------------------------------------------------------------------------


def test_promote_moves_an_entry_and_keeps_the_original_on_record(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    today = date.today().isoformat()
    entry_id = ws.remember("the deploy finally went out", "daily")

    assert ws.promote(today, entry_id) is True
    assert "the deploy finally went out" in ws.read("memory")
    assert f"promoted_from:{today}#{entry_id}" in ws.read("memory")

    daily = ws.read("daily")
    assert "status:promoted" in [line for line in daily.splitlines() if entry_id in line][0]


def test_promote_is_false_for_a_missing_date_or_entry(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    assert ws.promote("2026-01-01", "doesnotexist") is False
    entry_id = ws.remember("something", "daily")
    assert ws.promote("2026-01-01", entry_id) is False  # right id, wrong day


def test_promote_twice_is_false_the_second_time(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    today = date.today().isoformat()
    entry_id = ws.remember("only promotable once", "daily")
    assert ws.promote(today, entry_id) is True
    assert ws.promote(today, entry_id) is False  # already marked promoted, no longer active


def test_promote_carries_the_confidence_forward(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    today = date.today().isoformat()
    entry_id = ws.remember("might ship Friday", "daily", confidence=0.6)
    assert ws.promote(today, entry_id) is True
    promoted = next(e for e in ws.entries("memory") if e.text == "might ship Friday")
    assert promoted.confidence == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_finds_entries_across_files(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("prefers concise notifications for low-priority events", "preference")
    ws.remember("the user is currently working on Aether", "fact", category="Projects")

    hits = ws.search("concise notifications")
    assert any("concise notifications" in h.text for h in hits)
    assert all(h.score > 40 for h in hits)


def test_search_empty_query_returns_nothing(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    assert ws.search("") == []


def test_search_respects_the_limit(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    for i in range(5):
        ws.remember(f"note number {i} about coffee", "fact")
    assert len(ws.search("coffee", limit=2)) == 2


def test_search_tolerates_a_malformed_confidence_value(tmp_path) -> None:
    """A human hand-editing a workspace file can leave a non-numeric
    confidence token behind — search must skip over it, not crash."""
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("note about coffee", "fact")
    memory_path = ws.root / "MEMORY.md"
    corrupted = memory_path.read_text(encoding="utf-8").replace("-->", "confidence:n/a -->", 1)
    memory_path.write_text(corrupted, encoding="utf-8")
    hits = ws.search("coffee")  # must not raise
    assert any("coffee" in h.text for h in hits)


# ---------------------------------------------------------------------------
# entries() — structured, human- vs machine-authored
# ---------------------------------------------------------------------------


def test_entries_distinguishes_human_from_machine_authored_lines(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("prefers short replies", "preference", source="agent")
    # a human typing straight into the file writes a plain bullet, no comment
    user_path = ws.root / "USER.md"
    user_path.write_text(
        user_path.read_text(encoding="utf-8") + "- always use dark mode\n", encoding="utf-8"
    )

    entries = ws.entries("user")
    by_text = {e.text: e for e in entries}
    assert by_text["prefers short replies"].source == "agent"
    assert by_text["prefers short replies"].id != ""
    assert by_text["always use dark mode"].source == "human"
    assert by_text["always use dark mode"].id == ""


def test_entries_records_user_origin_separately_from_agent(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("told me directly: call her Jo", "fact", source="user")
    ws.remember("inferred: probably a morning person", "fact", source="agent")
    sources = {e.text: e.source for e in ws.entries("memory")}
    assert sources["told me directly: call her Jo"] == "user"
    assert sources["inferred: probably a morning person"] == "agent"


def test_remember_rejects_an_unknown_source(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    with pytest.raises(ValueError):
        ws.remember("x", "fact", source="system")


def test_remember_confidence_is_retained_not_rounded_away(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("might prefer mornings", "fact", confidence=0.35)
    entry = ws.entries("memory")[0]
    assert entry.confidence == pytest.approx(0.35)


def test_entries_tolerates_a_malformed_confidence_value(tmp_path) -> None:
    """A corrupted confidence token must degrade to confidence=None rather
    than raising and taking down the whole entries() call."""
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("some fact", "fact")
    memory_path = ws.root / "MEMORY.md"
    corrupted = memory_path.read_text(encoding="utf-8").replace("-->", "confidence:n/a -->", 1)
    memory_path.write_text(corrupted, encoding="utf-8")
    entries = ws.entries("memory")  # must not raise
    entry = next(e for e in entries if e.text == "some fact")
    assert entry.confidence is None


# ---------------------------------------------------------------------------
# write_raw — the human/control-center edit path
# ---------------------------------------------------------------------------


def test_write_raw_replaces_one_curated_file(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.write_raw("soul", "# SOUL.md\n\n- Be terse.\n")
    assert ws.read("soul") == "# SOUL.md\n\n- Be terse.\n"


def test_write_raw_rejects_files_outside_the_fixed_set(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    for bad in ("bootstrap", "daily", "../../etc/passwd", "nonsense"):
        with pytest.raises(ValueError):
            ws.write_raw(bad, "text")
    # nothing was written anywhere outside the workspace
    assert not (tmp_path.parent / "passwd").exists()


def test_write_raw_refuses_secrets(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    before = ws.read("memory")
    with pytest.raises(SecretRejected):
        ws.write_raw("memory", "api_key: sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert ws.read("memory") == before  # the rejected write never landed


# ---------------------------------------------------------------------------
# graceful degradation — malformed / unreadable files never crash a read
# ---------------------------------------------------------------------------


def test_read_degrades_to_empty_on_undecodable_bytes(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    (ws.root / "MEMORY.md").write_bytes(b"\xff\xfe\x00bad utf-8 \xff")
    assert ws.read("memory") == ""


def test_context_block_skips_an_undecodable_file_instead_of_raising(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    (ws.root / "SOUL.md").write_bytes(b"\xff\xfe garbage \xff")
    block = ws.context_block()  # must not raise
    assert "## Soul" not in block
    assert "## Identity" in block  # the rest of the workspace still loads


def test_search_skips_an_undecodable_file_instead_of_raising(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    (ws.root / "AGENTS.md").write_bytes(b"\xff\xfe garbage \xff")
    ws.remember("findable fact about coffee", "fact")
    assert any("coffee" in h.text for h in ws.search("coffee"))


def test_ensure_scaffold_raises_a_workspace_write_error_when_root_is_unwritable(tmp_path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    ws = Workspace(blocker / "ws")  # can't mkdir a child of a plain file
    with pytest.raises(WorkspaceWriteError):
        ws.ensure_scaffold()


# ---------------------------------------------------------------------------
# encrypted at rest — AES-256-GCM blobs on disk, decrypted only in memory
# ---------------------------------------------------------------------------


def _enc(tmp_path) -> Workspace:
    return Workspace(tmp_path / "ws", cipher=Cipher(b"t" * 32))


def test_stored_files_are_not_readable_text_on_disk(tmp_path) -> None:
    ws = _enc(tmp_path)
    ws.ensure_scaffold()
    ws.remember("wants plain language, never emoji", "preference")
    ws.remember("the ferry leaves at nine", "daily")
    for path, needle in (
        (ws.root / "USER.md", b"plain language"),
        (ws.root / "memory" / f"{ws.today()}.md", b"ferry"),
        (ws.root / "IDENTITY.md", b"# IDENTITY.md"),
    ):
        raw = path.read_bytes()
        assert needle not in raw, needle  # nothing readable on disk
        assert raw.startswith(b"aether-workspace\x01")  # the encrypted-blob marker
    # but the store itself reads everything back
    assert "plain language" in ws.read("user")
    assert "ferry" in ws.read("daily")


def test_every_workspace_path_round_trips_through_the_cipher(tmp_path) -> None:
    ws = _enc(tmp_path)
    ws.ensure_scaffold()
    ws.finish_bootstrap()
    pref = ws.remember("summaries first, detail on request", "preference")
    daily = ws.remember("shipped the batch adapter", "daily", when=date(2026, 10, 17))
    ws.remember("a durable decision", "fact", category="Decisions")

    assert "summaries first" in ws.read("user")
    assert "batch adapter" in ws.read("daily", date_str="2026-10-17")
    assert "durable decision" in ws.read("memory")
    assert any(e.id == pref for e in ws.entries("user"))

    assert ws.promote("2026-10-17", daily)
    assert "batch adapter" in ws.read("memory")
    assert "summaries first" in ws.context_block()
    assert any("summaries first" in hit.text for hit in ws.search("summaries"))

    ws.write_raw("soul", "# SOUL.md\n\n- rewritten by hand through the panel\n")
    assert "rewritten by hand" in ws.read("soul")


def test_a_plaintext_workspace_is_migrated_exactly_once(tmp_path) -> None:
    # a pre-upgrade workspace: plain files on disk, never touched by a cipher
    root = tmp_path / "ws"
    (root / "memory").mkdir(parents=True)
    (root / "USER.md").write_text(
        "# USER.md\n\n- an old preference from before encryption\n", encoding="utf-8"
    )
    (root / "memory" / "2026-01-01.md").write_text(
        "# 2026-01-01\n\n- an old daily note\n", encoding="utf-8"
    )
    ws = Workspace(root, cipher=Cipher(b"t" * 32))
    assert ws.ensure_scaffold() is False  # not a first run — an upgrade boot

    converted = ws.migrate_plaintext()
    assert converted == 2  # USER.md and the daily — the backfilled defaults
    # were written encrypted by the scaffold itself
    assert b"old preference" not in (root / "USER.md").read_bytes()
    assert "an old preference from before encryption" in ws.read("user")
    assert "an old daily note" in ws.read("daily", date_str="2026-01-01")
    assert ws.migrate_plaintext() == 0  # already-encrypted files are left alone


def test_migration_never_touches_a_workspace_without_a_cipher(tmp_path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "USER.md").write_text("# USER.md\n\n- stays as it is\n", encoding="utf-8")
    ws = Workspace(root)
    assert ws.migrate_plaintext() == 0
    assert "stays as it is" in (root / "USER.md").read_text(encoding="utf-8")


def test_a_file_whose_key_is_gone_degrades_to_empty_never_crashes(tmp_path) -> None:
    # a workspace written under a key that has since rotated away
    Workspace(tmp_path / "ws", cipher=Cipher(b"j" * 32)).ensure_scaffold()
    ws = Workspace(tmp_path / "ws", cipher=Cipher(b"t" * 32))
    assert ws.read("identity") == ""  # not text, not garbage — simply empty
    assert "## Identity" not in ws.context_block()
    assert ws.needs_bootstrap() is True  # existence checks don't decrypt


def test_a_hand_written_plaintext_file_reads_until_the_store_rewrites_it(tmp_path) -> None:
    ws = _enc(tmp_path)
    ws.ensure_scaffold()
    (ws.root / "USER.md").write_text("# USER.md\n\n- dropped in by hand\n", encoding="utf-8")
    assert "dropped in by hand" in ws.read("user")
    ws.remember("a stored preference", "preference")
    raw = (ws.root / "USER.md").read_bytes()
    assert b"dropped in by hand" not in raw  # the rewrite sealed the whole file
    assert "dropped in by hand" in ws.read("user")  # and nothing was lost


# ---------------------------------------------------------------------------
# the profile — durable facts about the user, pending until confirmed
# ---------------------------------------------------------------------------


def test_a_profile_line_the_user_stated_lands_confirmed(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("lives in Lisbon", "profile", source="user")
    entries = ws.entries("user")
    profile = [e for e in entries if e.text == "lives in Lisbon"]
    assert len(profile) == 1
    assert profile[0].status == "confirmed"
    assert profile[0].source == "user"
    assert "## Profile" in ws.read("user")  # its own section, beside preferences
    assert [e.text for e in ws.profile()] == ["lives in Lisbon"]


def test_a_profile_entry_the_agent_inferred_lands_pending(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("works with marine electronics", "profile", source="agent")
    assert [e.text for e in ws.pending_profile()] == ["works with marine electronics"]
    assert ws.profile() == []  # nothing confirmed until the user says yes


def test_confirm_profile_flips_pending_to_confirmed_exactly_once(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    first = ws.remember("a morning person", "profile", source="agent")
    second = ws.remember("prefers voice notes", "profile", source="agent")
    confirmed = [e.id for e in ws.pending_profile()]
    assert ws.confirm_profile([first, second, "unknown-id"]) == 2
    assert [e.text for e in ws.profile()] == ["a morning person", "prefers voice notes"]
    assert ws.pending_profile() == []
    assert ws.confirm_profile(confirmed) == 0  # a second yes changes nothing


def test_a_correction_supersedes_the_pending_entry_it_replaces(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    wrong = ws.remember("lives in Madrid", "profile", source="agent")
    ws.remember("lives in Porto", "profile", source="user", supersedes=[wrong])
    assert ws.pending_profile() == []  # the guess it replaced is gone from the wait
    assert [e.text for e in ws.profile()] == ["lives in Porto"]
    statuses = {e.text: e.status for e in ws.entries("user")}
    assert statuses["lives in Madrid"] == "superseded"  # but never deleted


def test_pending_entries_never_reach_the_prompt_as_facts(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("an early riser", "profile", source="agent")
    ws.remember("hates bullet points", "preference")
    block = ws.context_block()
    assert "an early riser" not in block  # a guess is not the user's fact yet
    assert "hates bullet points" in block  # preferences are unaffected
    ws.confirm_profile([e.id for e in ws.pending_profile()])
    assert "an early riser" in ws.context_block()  # confirmed, it counts


def test_the_profile_section_stays_apart_from_preferences(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    ws.remember("prefers short replies", "preference")
    ws.remember("a night owl", "profile", source="user")
    assert [e.text for e in ws.profile()] == ["a night owl"]
    # and the plain entries() view still shows both — the file is one file
    texts = {e.text for e in ws.entries("user")}
    assert texts == {"prefers short replies", "a night owl"}


def test_profile_rejects_secrets_like_every_other_kind(tmp_path) -> None:
    ws = _ws(tmp_path)
    ws.ensure_scaffold()
    with pytest.raises(SecretRejected):
        ws.remember("my key sk-abcdefghijklmnopqrstuvwxyz012345", "profile", source="user")
    assert ws.profile() == [] and ws.pending_profile() == []
