"""Workspace store tests: sealed rows, async reads, no plaintext on disk."""

from __future__ import annotations

from datetime import date

import pytest

from aether.memory.crypto import Cipher, generate_key_b64
from aether.workspace import SecretRejected, Workspace


def _cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


def _ws(db) -> Workspace:
    return Workspace(db, cipher=_cipher())


async def _corrupt(ws: Workspace, name: str) -> None:
    """Write bytes that cannot decrypt, the way a rotated key leaves a row."""
    row = await ws._row(name)
    assert row is not None
    await ws._pool.execute(
        "UPDATE workspace_files SET content_enc = $1 WHERE id = $2", b"not ciphertext", row["id"]
    )


# ---------------------------------------------------------------------------
# scaffold / bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_ensure_scaffold_creates_every_fixed_file_and_reports_first_run(db) -> None:
    ws = _ws(db)
    assert await ws.ensure_scaffold() is True
    for name, key in (
        ("IDENTITY.md", "identity"),
        ("SOUL.md", "soul"),
        ("AGENTS.md", "agents"),
        ("USER.md", "user"),
        ("MEMORY.md", "memory"),
        ("BOOTSTRAP.md", "bootstrap"),
    ):
        assert await ws.read(key), name
    assert await ws.needs_bootstrap() is True


@pytest.mark.integration
async def test_ensure_scaffold_is_idempotent_and_never_overwrites(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.write_raw("identity", "# IDENTITY.md\n\n- Name: Renamed\n")
    assert await ws.ensure_scaffold() is False  # not a first run the second time
    assert "Renamed" in await ws.read("identity")


@pytest.mark.integration
async def test_ensure_scaffold_never_resurrects_a_finished_bootstrap(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    assert await ws.finish_bootstrap() is True
    await ws.ensure_scaffold()  # a later boot, same workspace directory
    assert await ws.needs_bootstrap() is False


@pytest.mark.integration
async def test_ensure_scaffold_backfills_a_file_the_user_deleted(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws._delete("SOUL.md")
    await ws.ensure_scaffold()
    assert "SOUL.md" in await ws.read("soul")


@pytest.mark.integration
async def test_finish_bootstrap_is_false_when_nothing_was_pending(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.finish_bootstrap()
    assert await ws.finish_bootstrap() is False


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_read_is_empty_before_scaffolding(db) -> None:
    ws = _ws(db)
    assert await ws.read("identity") == ""
    assert await ws.read("daily") == ""


@pytest.mark.integration
async def test_read_unknown_file_key_is_empty(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    assert await ws.read("nonsense") == ""


@pytest.mark.integration
async def test_read_daily_defaults_to_today(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    entry_id = await ws.remember("shipped the release", "daily")
    today = date.today().isoformat()
    assert await ws.read("daily") == await ws.read("daily", date_str=today)
    assert entry_id in await ws.read("daily")


@pytest.mark.integration
async def test_daily_date_must_look_like_an_iso_date(db) -> None:
    """A path-traversal attempt in the date must fail closed, not be
    silently joined onto the workspace root."""
    ws = _ws(db)
    await ws.ensure_scaffold()
    with pytest.raises(ValueError):
        await ws.read("daily", date_str="../../etc/passwd")
    with pytest.raises(ValueError):
        await ws.promote("not-a-date", "abc123")


# ---------------------------------------------------------------------------
# context_block — the IDENTITY -> SOUL -> AGENTS -> USER -> MEMORY order
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_context_block_orders_sections_and_strips_provenance(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("prefers short replies", "preference")
    block = await ws.context_block()
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


@pytest.mark.integration
async def test_context_block_drops_bootstrap_once_finished(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.finish_bootstrap()
    assert "First-run setup" not in await ws.context_block()


@pytest.mark.integration
async def test_context_block_is_empty_before_scaffolding(db) -> None:
    assert await _ws(db).context_block() == ""


@pytest.mark.integration
async def test_context_block_preserves_text_containing_html_comment_markers(db) -> None:
    """A literal '<!--' inside the user's own text must survive into the
    model's actual per-turn context — only the real, appended provenance
    comment gets stripped."""
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("explain what <!-- means in HTML", "preference")
    block = await ws.context_block()
    assert "explain what <!-- means in HTML" in block
    assert "status:active" not in block  # the real provenance is still gone


@pytest.mark.integration
async def test_context_block_preserves_text_with_multiple_comment_blocks(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    text = "see <!-- note a --> and also <!-- note b --> for details"
    await ws.remember(text, "preference")
    block = await ws.context_block()
    assert text in block
    assert "status:active" not in block


# ---------------------------------------------------------------------------
# remember — preferences, facts, daily notes
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_remember_rejects_bad_kind_and_empty_text(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    with pytest.raises(ValueError):
        await ws.remember("text", "not-a-kind")
    with pytest.raises(ValueError):
        await ws.remember("   ", "fact")


@pytest.mark.integration
async def test_remember_fact_goes_under_a_category_heading(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("the user works on Aether", "fact", category="Projects")
    memory = await ws.read("memory")
    assert "## Projects" in memory
    assert "the user works on Aether" in memory


@pytest.mark.integration
async def test_remember_fact_defaults_to_notes_category(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("approvals default to 24h", "fact")
    assert "## Notes" in await ws.read("memory")


@pytest.mark.integration
async def test_remember_preference_supersedes_an_older_one(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    old_id = await ws.remember("prefers email for urgent things", "preference")
    await ws.remember("prefers telegram for urgent things", "preference", supersedes=[old_id])
    user = await ws.read("user")
    assert "status:superseded" in [line for line in user.splitlines() if old_id in line][0]
    assert "status:active" in [line for line in user.splitlines() if "telegram" in line][0]


@pytest.mark.integration
async def test_remember_daily_creates_one_file_per_day(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("note one", "daily", when=date(2026, 1, 1))
    await ws.remember("note two", "daily", when=date(2026, 1, 2))
    assert "note one" in await ws.read("daily", date_str="2026-01-01")
    assert "note two" in await ws.read("daily", date_str="2026-01-02")
    assert "note one" not in await ws.read("daily", date_str="2026-01-02")


@pytest.mark.integration
async def test_remember_refuses_things_that_look_like_secrets(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    for text in (
        "api_key: sk-abcdefghijklmnopqrstuvwxyz0123456789",
        "AWS key is AKIAABCDEFGHIJKLMNOP",
        "github token ghp_abcdefghijklmnopqrstuvwxyz012345",
        "password: hunter2345",
    ):
        with pytest.raises(SecretRejected):
            await ws.remember(text, "fact")
    assert "## Notes" not in await ws.read("memory")


@pytest.mark.integration
async def test_remember_each_entry_gets_a_unique_id(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    first = await ws.remember("fact one", "fact")
    second = await ws.remember("fact two", "fact")
    assert first != second


@pytest.mark.integration
async def test_remember_preserves_text_containing_html_comment_markers(db) -> None:
    """A literal '<!--' inside the user's own text must not be mistaken for
    the start of the provenance comment — the non-greedy version of this
    regex used to truncate the stored text right after it."""
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("explain what <!-- means in HTML", "fact")
    entry = (await ws.entries("memory"))[0]
    assert entry.text == "explain what <!-- means in HTML"


@pytest.mark.integration
async def test_remember_preserves_text_containing_multiple_comment_blocks(db) -> None:
    """More than one '<!-- ... -->'-shaped substring in the user's own text
    must not confuse which block is the real, appended provenance comment —
    the greedy text match has to land on the last one, not the first."""
    ws = _ws(db)
    await ws.ensure_scaffold()
    text = "see <!-- note a --> and also <!-- note b --> for details"
    await ws.remember(text, "fact")
    entry = (await ws.entries("memory"))[0]
    assert entry.text == text
    assert entry.id  # the real provenance comment was still parsed out correctly
    assert entry.source == "agent"


@pytest.mark.integration
async def test_remember_rejects_non_numeric_confidence(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    with pytest.raises(ValueError):
        # the bad type is the point — remember() must refuse it at runtime
        await ws.remember("x", "fact", confidence=["not", "a", "number"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# promote — daily -> long-term memory, with provenance
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_promote_moves_an_entry_and_keeps_the_original_on_record(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    today = date.today().isoformat()
    entry_id = await ws.remember("the deploy finally went out", "daily")

    assert await ws.promote(today, entry_id) is True
    assert "the deploy finally went out" in await ws.read("memory")
    assert f"promoted_from:{today}#{entry_id}" in await ws.read("memory")

    daily = await ws.read("daily")
    assert "status:promoted" in [line for line in daily.splitlines() if entry_id in line][0]


@pytest.mark.integration
async def test_promote_is_false_for_a_missing_date_or_entry(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    assert await ws.promote("2026-01-01", "doesnotexist") is False
    entry_id = await ws.remember("something", "daily")
    assert await ws.promote("2026-01-01", entry_id) is False  # right id, wrong day


@pytest.mark.integration
async def test_promote_twice_is_false_the_second_time(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    today = date.today().isoformat()
    entry_id = await ws.remember("only promotable once", "daily")
    assert await ws.promote(today, entry_id) is True
    assert await ws.promote(today, entry_id) is False  # already marked promoted, no longer active


@pytest.mark.integration
async def test_promote_carries_the_confidence_forward(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    today = date.today().isoformat()
    entry_id = await ws.remember("might ship Friday", "daily", confidence=0.6)
    assert await ws.promote(today, entry_id) is True
    promoted = next(e for e in await ws.entries("memory") if e.text == "might ship Friday")
    assert promoted.confidence == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_finds_entries_across_files(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("prefers concise notifications for low-priority events", "preference")
    await ws.remember("the user is currently working on Aether", "fact", category="Projects")

    hits = await ws.search("concise notifications")
    assert any("concise notifications" in h.text for h in hits)
    assert all(h.score > 40 for h in hits)


@pytest.mark.integration
async def test_search_empty_query_returns_nothing(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    assert await ws.search("") == []


@pytest.mark.integration
async def test_search_respects_the_limit(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    for i in range(5):
        await ws.remember(f"note number {i} about coffee", "fact")
    assert len(await ws.search("coffee", limit=2)) == 2


@pytest.mark.integration
async def test_search_tolerates_a_malformed_confidence_value(db) -> None:
    """A human hand-editing a workspace file can leave a non-numeric
    confidence token behind — search must skip over it, not crash."""
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("note about coffee", "fact")
    content = await ws.read("memory")
    await ws._put("MEMORY.md", content.replace("-->", "confidence:n/a -->", 1))
    hits = await ws.search("coffee")  # must not raise
    assert any("coffee" in h.text for h in hits)


# ---------------------------------------------------------------------------
# entries() — structured, human- vs machine-authored
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_entries_distinguishes_human_from_machine_authored_lines(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("prefers short replies", "preference", source="agent")
    # a human typing straight into the file writes a plain bullet, no comment
    await ws._put("USER.md", await ws.read("user") + "- always use dark mode\n")

    entries = await ws.entries("user")
    by_text = {e.text: e for e in entries}
    assert by_text["prefers short replies"].source == "agent"
    assert by_text["prefers short replies"].id != ""
    assert by_text["always use dark mode"].source == "human"
    assert by_text["always use dark mode"].id == ""


@pytest.mark.integration
async def test_entries_records_user_origin_separately_from_agent(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("told me directly: call her Jo", "fact", source="user")
    await ws.remember("inferred: probably a morning person", "fact", source="agent")
    sources = {e.text: e.source for e in await ws.entries("memory")}
    assert sources["told me directly: call her Jo"] == "user"
    assert sources["inferred: probably a morning person"] == "agent"


@pytest.mark.integration
async def test_remember_rejects_an_unknown_source(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    with pytest.raises(ValueError):
        await ws.remember("x", "fact", source="system")


@pytest.mark.integration
async def test_remember_confidence_is_retained_not_rounded_away(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("might prefer mornings", "fact", confidence=0.35)
    entry = (await ws.entries("memory"))[0]
    assert entry.confidence == pytest.approx(0.35)


@pytest.mark.integration
async def test_entries_tolerates_a_malformed_confidence_value(db) -> None:
    """A corrupted confidence token must degrade to confidence=None rather
    than raising and taking down the whole entries() call."""
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("some fact", "fact")
    content = await ws.read("memory")
    await ws._put("MEMORY.md", content.replace("-->", "confidence:n/a -->", 1))
    entries = await ws.entries("memory")  # must not raise
    entry = next(e for e in entries if e.text == "some fact")
    assert entry.confidence is None


# ---------------------------------------------------------------------------
# write_raw — the human/control-center edit path
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_write_raw_replaces_one_curated_file(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.write_raw("soul", "# SOUL.md\n\n- Be terse.\n")
    assert await ws.read("soul") == "# SOUL.md\n\n- Be terse.\n"


@pytest.mark.integration
async def test_write_raw_rejects_files_outside_the_fixed_set(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    for bad in ("bootstrap", "daily", "../../etc/passwd", "nonsense"):
        with pytest.raises(ValueError):
            await ws.write_raw(bad, "text")


@pytest.mark.integration
async def test_write_raw_refuses_secrets(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    before = await ws.read("memory")
    with pytest.raises(SecretRejected):
        await ws.write_raw("memory", "api_key: sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert await ws.read("memory") == before  # the rejected write never landed


# ---------------------------------------------------------------------------
# graceful degradation — malformed / unreadable files never crash a read
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_read_degrades_to_empty_on_undecodable_bytes(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await _corrupt(ws, "MEMORY.md")
    assert await ws.read("memory") == ""


@pytest.mark.integration
async def test_context_block_skips_an_undecodable_file_instead_of_raising(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await _corrupt(ws, "SOUL.md")
    block = await ws.context_block()  # must not raise
    assert "## Soul" not in block
    assert "## Identity" in block  # the rest of the workspace still loads


@pytest.mark.integration
async def test_search_skips_an_undecodable_file_instead_of_raising(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await _corrupt(ws, "AGENTS.md")
    await ws.remember("findable fact about coffee", "fact")
    assert any("coffee" in h.text for h in await ws.search("coffee"))


# ---------------------------------------------------------------------------
# sealed at rest — ciphertext only in the table, decrypted in memory
# ---------------------------------------------------------------------------


def _enc(db) -> Workspace:
    return Workspace(db, cipher=_cipher())


@pytest.mark.integration
async def test_stored_files_are_not_readable_text_in_the_database(db) -> None:
    ws = _enc(db)
    await ws.ensure_scaffold()
    await ws.remember("wants plain language, never emoji", "preference")
    await ws.remember("the ferry leaves at nine", "daily")
    rows = await db.fetch("SELECT name_enc, content_enc FROM workspace_files")
    for row in rows:
        for needle in (b"plain language", b"ferry", b"IDENTITY.md"):
            assert needle not in row["name_enc"]
            assert needle not in row["content_enc"]
        assert row["name_enc"] and row["content_enc"]  # real ciphertext, not an empty row
    # but the store itself reads everything back
    assert "plain language" in await ws.read("user")
    assert "ferry" in await ws.read("daily")


@pytest.mark.integration
async def test_every_workspace_path_round_trips_through_the_cipher(db) -> None:
    ws = _enc(db)
    await ws.ensure_scaffold()
    await ws.finish_bootstrap()
    pref = await ws.remember("summaries first, detail on request", "preference")
    daily = await ws.remember("shipped the batch adapter", "daily", when=date(2026, 10, 17))
    await ws.remember("a durable decision", "fact", category="Decisions")

    assert "summaries first" in await ws.read("user")
    assert "batch adapter" in await ws.read("daily", date_str="2026-10-17")
    assert "durable decision" in await ws.read("memory")
    assert any(e.id == pref for e in await ws.entries("user"))

    assert await ws.promote("2026-10-17", daily)
    assert "batch adapter" in await ws.read("memory")
    assert "summaries first" in await ws.context_block()
    assert any("summaries first" in hit.text for hit in await ws.search("summaries"))

    await ws.write_raw("soul", "# SOUL.md\n\n- rewritten by hand through the panel\n")
    assert "rewritten by hand" in await ws.read("soul")


@pytest.mark.integration
async def test_a_plaintext_workspace_is_imported_and_removed_once(db, tmp_path) -> None:
    # a pre-upgrade workspace: plain files on disk, never touched by a cipher
    root = tmp_path / "ws"
    (root / "memory").mkdir(parents=True)
    (root / "USER.md").write_text(
        "# USER.md\n\n- an old preference from before encryption\n", encoding="utf-8"
    )
    (root / "memory" / "2026-01-01.md").write_text(
        "# 2026-01-01\n\n- an old daily note\n", encoding="utf-8"
    )
    ws = Workspace(db, cipher=_cipher())
    assert await ws.import_from_disk(root) == 2
    assert "an old preference from before encryption" in await ws.read("user")
    assert "an old daily note" in await ws.read("daily", date_str="2026-01-01")
    # no readable copy was left behind
    assert not root.exists()
    # a second boot finds rows already present and imports nothing
    assert await ws.is_empty() is False
    assert await ws.import_from_disk(root) == 0


@pytest.mark.integration
async def test_an_imported_legacy_encrypted_file_moves_its_text(db, tmp_path) -> None:
    from aether.workspace.store import _ENC_MAGIC, _aad

    root = tmp_path / "ws"
    root.mkdir()
    cipher = _cipher()
    (root / "USER.md").write_bytes(
        _ENC_MAGIC
        + cipher.encrypt_text("# USER.md\n\n- sealed before the move\n", aad=_aad("USER.md"))
    )
    ws = Workspace(db, cipher=cipher)
    assert await ws.import_from_disk(root) == 1
    assert "sealed before the move" in await ws.read("user")
    assert not root.exists()


@pytest.mark.integration
async def test_a_row_whose_key_is_gone_degrades_to_empty_never_crashes(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await _corrupt(ws, "IDENTITY.md")
    assert await ws.read("identity") == ""  # not text, not garbage — simply empty
    assert "## Identity" not in await ws.context_block()
    assert await ws.needs_bootstrap() is True  # existence checks don't decrypt


# ---------------------------------------------------------------------------
# the profile — durable facts about the user, pending until confirmed
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_profile_line_the_user_stated_lands_confirmed(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("lives in Lisbon", "profile", source="user")
    entries = await ws.entries("user")
    profile = [e for e in entries if e.text == "lives in Lisbon"]
    assert len(profile) == 1
    assert profile[0].status == "confirmed"
    assert profile[0].source == "user"
    assert "## Profile" in await ws.read("user")  # its own section, beside preferences
    assert [e.text for e in await ws.profile()] == ["lives in Lisbon"]


@pytest.mark.integration
async def test_a_profile_entry_the_agent_inferred_lands_pending(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("works with marine electronics", "profile", source="agent")
    assert [e.text for e in await ws.pending_profile()] == ["works with marine electronics"]
    assert await ws.profile() == []  # nothing confirmed until the user says yes


@pytest.mark.integration
async def test_confirm_profile_flips_pending_to_confirmed_exactly_once(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    first = await ws.remember("a morning person", "profile", source="agent")
    second = await ws.remember("prefers voice notes", "profile", source="agent")
    confirmed = [e.id for e in await ws.pending_profile()]
    assert await ws.confirm_profile([first, second, "unknown-id"]) == 2
    assert [e.text for e in await ws.profile()] == ["a morning person", "prefers voice notes"]
    assert await ws.pending_profile() == []
    assert await ws.confirm_profile(confirmed) == 0  # a second yes changes nothing


@pytest.mark.integration
async def test_a_correction_supersedes_the_pending_entry_it_replaces(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    wrong = await ws.remember("lives in Madrid", "profile", source="agent")
    await ws.remember("lives in Porto", "profile", source="user", supersedes=[wrong])
    assert await ws.pending_profile() == []  # the guess it replaced is gone from the wait
    assert [e.text for e in await ws.profile()] == ["lives in Porto"]
    statuses = {e.text: e.status for e in await ws.entries("user")}
    assert statuses["lives in Madrid"] == "superseded"  # but never deleted


@pytest.mark.integration
async def test_pending_entries_never_reach_the_prompt_as_facts(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("an early riser", "profile", source="agent")
    await ws.remember("hates bullet points", "preference")
    block = await ws.context_block()
    assert "an early riser" not in block  # a guess is not the user's fact yet
    assert "hates bullet points" in block  # preferences are unaffected
    await ws.confirm_profile([e.id for e in await ws.pending_profile()])
    assert "an early riser" in await ws.context_block()  # confirmed, it counts


@pytest.mark.integration
async def test_the_profile_section_stays_apart_from_preferences(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.remember("prefers short replies", "preference")
    await ws.remember("a night owl", "profile", source="user")
    assert [e.text for e in await ws.profile()] == ["a night owl"]
    # and the plain entries() view still shows both — the file is one file
    texts = {e.text for e in await ws.entries("user")}
    assert texts == {"prefers short replies", "a night owl"}


@pytest.mark.integration
async def test_profile_rejects_secrets_like_every_other_kind(db) -> None:
    ws = _ws(db)
    await ws.ensure_scaffold()
    with pytest.raises(SecretRejected):
        await ws.remember("my key sk-abcdefghijklmnopqrstuvwxyz012345", "profile", source="user")
    assert await ws.profile() == [] and await ws.pending_profile() == []


# ---------------------------------------------------------------------------
# passphrase export / import
# ---------------------------------------------------------------------------


def test_export_bundle_round_trips_and_refuses_the_wrong_passphrase() -> None:
    from aether.memory.crypto import CryptoError
    from aether.workspace.transfer import export_bundle, import_bundle

    files = {"USER.md": "# USER.md\n\n- prefers short replies\n", "memory/2026-01-01.md": "x"}
    bundle = export_bundle(files, "correct horse battery staple")
    assert import_bundle(bundle, "correct horse battery staple") == files
    with pytest.raises(CryptoError):
        import_bundle(bundle, "wrong passphrase")
    with pytest.raises(CryptoError):
        export_bundle(files, "")


def test_export_bundle_does_not_carry_plaintext() -> None:
    from aether.workspace.transfer import export_bundle

    bundle = export_bundle({"USER.md": "a very private preference"}, "passphrase")
    assert b"very private preference" not in bundle


@pytest.mark.integration
async def test_export_files_and_import_files_move_the_workspace(db) -> None:
    source = _ws(db)
    await source.ensure_scaffold()
    await source.remember("prefers concise notifications", "preference")
    files = await source.export_files()
    assert any("prefers concise notifications" in text for text in files.values())

    await db.execute("TRUNCATE workspace_files RESTART IDENTITY")
    target = _ws(db)
    await target.ensure_scaffold()
    assert "prefers concise notifications" not in await target.read("user")
    assert await target.import_files(files) == len(files)
    assert "prefers concise notifications" in await target.read("user")


@pytest.mark.integration
async def test_import_files_refuses_a_name_outside_the_workspace(db) -> None:
    ws = _ws(db)
    with pytest.raises(ValueError):
        await ws.import_files({"../../etc/passwd": "x"})
    with pytest.raises(ValueError):
        await ws.import_files({"memory/not-a-date.md": "x"})
    with pytest.raises(SecretRejected):
        await ws.import_files({"USER.md": "api_key: sk-abcdefghijklmnopqrstuvwxyz012345"})


@pytest.mark.integration
async def test_a_trusted_import_restores_a_bundle_that_trips_the_secret_scan(db) -> None:
    # a legacy file can legitimately contain something that looks like a key;
    # an owner restoring their own export must not be locked out of it
    ws = _ws(db)
    await ws.ensure_scaffold()
    await ws.import_files({"USER.md": "api_key: sk-abcdefghijklmnopqrstuvwxyz012345"}, trusted=True)
    files = await ws.export_files()
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" in files["USER.md"]

    await db.execute("TRUNCATE workspace_files RESTART IDENTITY")
    restored = _ws(db)
    assert await restored.import_files(files, trusted=True) == len(files)
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" in await restored.read("user")
