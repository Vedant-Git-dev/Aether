"""The media-carrying park flow, unit: a parked call that references a
stored file marks the approval row and shows the file itself with the ask.

test_agent_loop.py owns the loop's general tests (and stays untouched);
this file only adds the media-out park seam Build F introduced — the
indexed reference the eviction query needs, and the bytes shown rather
than described.
"""

from __future__ import annotations

from fakes import FakeMediaStore
from test_agent_loop import LoopKit

from aether.agent.loop import SurfaceFanout
from aether.connectors.base import Attachment
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


async def test_a_media_carrying_park_marks_the_row_and_shows_the_file() -> None:
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

    assert kit.executed == []  # parked, not run
    # the row carries the indexed reference — what keeps the blob safe
    # from eviction while the question waits
    assert kit.approvals.media_ids == [kept.media_id]
    # and the ask itself showed the file, not words about it
    assert surface.approvals[0]["attachment"] == Attachment(
        "video", "laptop.mp4", "video/mp4", _MP4
    )
    assert "held for approval" in result.content


async def test_a_plain_park_neither_marks_the_row_nor_passes_media() -> None:
    kit, surface = _wired(FakeMediaStore())

    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )

    # no media anywhere: the reference is absent (None, not a row value),
    # and the surface saw no kwarg at all
    assert kit.approvals.media_ids == [None]
    assert set(surface.approvals[0]) == {"approval_id", "text"}


async def test_a_parked_call_whose_blob_is_gone_asks_with_words_only() -> None:
    """Eviction between the ask and the parking never raises — the id is
    still marked (it is what was asked about), but the words go alone."""
    store = FakeMediaStore()
    kept = await store.put(_MP4, filename="laptop.mp4", origin="telegram")
    store.rows.pop(kept.media_id)
    kit, surface = _wired(store)

    await kit.loop._execute(
        ToolCall(
            id="t1",
            name="mail__send_message",
            arguments={"to": "a@b.c", "media_id": kept.media_id},
        )
    )

    assert kit.approvals.media_ids == [kept.media_id]
    assert set(surface.approvals[0]) == {"approval_id", "text"}


async def test_a_junk_media_id_is_ignored_rather_than_marked() -> None:
    """Only a real integer id marks the row — booleans (a bool is an int
    in Python), zero, negatives, and strings never reach the store. Each
    call parks a distinct request, so the loop's own duplicate-pending
    dedup (False == 0 under Python value equality) stays out of the way."""
    kit, _ = _wired(FakeMediaStore())
    for bad in (True, False, 0, -1, "3", None):
        await kit.loop._execute(
            ToolCall(
                id=f"t{bad!r}",
                name="mail__send_message",
                arguments={"to": f"a{bad!r}.c", "media_id": bad},
            )
        )
    assert kit.approvals.media_ids == [None] * 6
