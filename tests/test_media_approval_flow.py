"""The media-carrying park seam, unit: a call that names a stored file is
refused before any approval row exists — the user is never asked to
approve an action that cannot run exactly as offered.

test_agent_loop.py owns the loop's general tests (and stays untouched);
this file only covers the preflight Build F's media-marking park evolved
into: a missing or unreadable file can't run (nothing to attach), and a
present one can't run either — the connected actions take parameters,
not bytes, so "sent" must never be said with the file left behind.
"""

from __future__ import annotations

from fakes import FakeMediaStore
from test_agent_loop import LoopKit

from aether.agent.loop import SurfaceFanout
from aether.llm.types import ToolCall

_MP4 = b"\x00\x00\x00\x18ftypisom" + b"laptop-tour"


class MediaSurface:
    """A media-aware surface double: records every kwarg the ask carried,
    so the tests can see exactly when a file rode along."""

    def __init__(self) -> None:
        self.name = "media"
        self.approvals: list[dict] = []

    async def present_approval(self, approval_id: int, text: str, **kwargs) -> None:
        self.approvals.append({"approval_id": approval_id, "text": text, **kwargs})


def _wired(store=None) -> tuple:
    """A kit with the media store and a media-aware surface in place of
    the default fanout — the loop's own seams, set from outside so
    test_agent_loop.py's harness never changes."""
    kit = LoopKit(None)
    kit.loop._media = store
    surface = MediaSurface()
    kit.loop._surfaces = SurfaceFanout([surface])
    kit.add_tool("mail__send_message")
    return kit, surface


async def test_a_present_file_refuses_the_park_with_a_plain_reason() -> None:
    """The bytes are there and the action still can't run as offered —
    the connected tools take parameters, not bytes. No row, no ask, and
    the reason tells the model exactly what to do instead."""
    store = FakeMediaStore()
    kept = await store.put(_MP4, filename="laptop.mp4", origin="telegram")
    kit, surface = _wired(store)

    result = await kit.loop._execute(
        ToolCall(
            id="t1",
            name="mail__send_message",
            arguments={"to": "a@b.c", "media_id": kept.media_id},
        )
    )

    assert kit.executed == []  # refused before anything ran
    assert kit.approvals.media_ids == []  # no row was ever parked
    assert surface.approvals == []  # and the user was never asked
    assert "can't yet be attached" in result.content
    assert "media_id" in result.content  # the way out is named


async def test_a_missing_file_refuses_the_park_with_a_plain_reason() -> None:
    """Eviction between arrival and proposal never asks the user to
    approve a send with nothing attached — the model is told the file is
    gone and to ask for it again."""
    store = FakeMediaStore()
    kept = await store.put(_MP4, filename="laptop.mp4", origin="telegram")
    store.rows.pop(kept.media_id)
    kit, surface = _wired(store)

    result = await kit.loop._execute(
        ToolCall(
            id="t1",
            name="mail__send_message",
            arguments={"to": "a@b.c", "media_id": kept.media_id},
        )
    )

    assert kit.approvals.media_ids == []
    assert surface.approvals == []
    assert "no longer stored" in result.content
    assert "send the file again" in result.content


async def test_a_plain_park_neither_refuses_nor_passes_media() -> None:
    kit, surface = _wired(FakeMediaStore())

    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )

    # no media anywhere: the reference is absent (None, not a row value),
    # and the surface saw no kwarg at all
    assert kit.approvals.media_ids == [None]
    assert set(surface.approvals[0]) == {"approval_id", "text"}


async def test_a_parked_ask_shows_the_action_own_fields() -> None:
    """The card is the ask itself: the action as a question, then the
    request's own fields in their own order — the gate's reason is wiring
    the user never reads."""
    kit, surface = _wired(FakeMediaStore())

    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )

    assert surface.approvals[0]["text"] == "Send the email?\n\nTo: a@b.c\nBody: hi"


async def test_a_junk_media_id_is_ignored_rather_than_refused() -> None:
    """Only a real integer id names a file — booleans (a bool is an int
    in Python), zero, negatives, and strings never reach the store, so
    the call parks like any plain one. Each call parks a distinct
    request, so the loop's own duplicate-pending dedup (False == 0 under
    Python value equality) stays out of the way."""
    kit, surface = _wired(FakeMediaStore())
    for bad in (True, False, 0, -1, "3", None):
        await kit.loop._execute(
            ToolCall(
                id=f"t{bad!r}",
                name="mail__send_message",
                arguments={"to": f"a{bad!r}.c", "media_id": bad},
            )
        )
    assert kit.approvals.media_ids == [None] * 6
    assert len(surface.approvals) == 6
