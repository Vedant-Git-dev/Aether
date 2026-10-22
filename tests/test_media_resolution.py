"""The media-resolution seam, unit: which stored file a share names.

The observed symptoms this file pins: a junk or guessed media id that
silently clamped to blob 1 — the oldest file — so "send it back"
delivered a different, older image; a .docx request answered with an old
photo captioned as a video; and no way for the model to resolve "the
video" at all, so every id was a guess.

test_agent_loop.py owns the loop's general tests (and stays untouched);
test_native_tools.py owns the per-tool basics. This file covers only
what resolution added: strict ids, the find_media listing, the kind
check, and the memory chunk that anchors a shared image to its id.
"""

from __future__ import annotations

from fakes import FakeMediaStore, FakeProvider, FakeRegistry
from test_native_tools import NativeKit

from aether.memory.vectors import _chunk_text

_PNG = b"\x89PNG\r\n\x1a\n" + b"pixel"
_MP4 = b"\x00\x00\x00\x18ftypisom" + b"laptop-tour"
_MP3 = b"ID3" + b"x" * 64


class MediaSurface:
    """A media-aware surface double: records the note and the file it rode
    with — the default fake connector sends text only, so the real
    handler's sends need one that speaks the attachment kwarg."""

    def __init__(self) -> None:
        self.name = "media"
        self.sent: list[tuple[str, object | None]] = []

    async def send_to_user(self, text: str, *, attachment=None) -> str:
        self.sent.append((text, attachment))
        return "m1"


async def _store_with_files() -> FakeMediaStore:
    store = FakeMediaStore()
    await store.put(_PNG, filename="photo.jpg", origin="telegram")
    await store.put(_MP4, filename="laptop.mp4", origin="telegram")
    await store.put(_MP3, filename="voice.mp3", origin="telegram")
    await store.put(b"quarterly numbers, plain text", filename="notes.txt", origin="telegram")
    return store


async def test_a_junk_media_id_is_refused_never_clamped_to_the_oldest_file() -> None:
    """Zero, negatives, booleans, and strings used to clamp to blob 1 —
    the oldest stored file — so a guess delivered SOMETHING, honestly
    labeled as the file the user asked for. Now every junk id is a
    refusal that names the way out, and nothing is sent."""
    surface = MediaSurface()
    kit = NativeKit(media=await _store_with_files(), connector=surface)

    for junk in (None, "", 0, -1, True, "1", "x", 1.5):
        out = await kit.run("share_media", {"media_id": junk, "text": "here"})
        assert "needs media_id" in out, junk
        assert "find_media" in out, junk
    assert surface.sent == []  # the oldest file (blob 1) never went out


async def test_find_media_lists_ids_kinds_and_names_newest_first() -> None:
    """The resolver: every stored file with its id, kind, name, size, and
    origin — newest first, so 'the video I just sent' is a fact the model
    reads, not a number it guesses."""
    kit = NativeKit(media=await _store_with_files())

    out = await kit.run("find_media", {})
    assert out.startswith("Stored files, newest first:")
    assert out.index("media id 4") < out.index("media id 3")  # newest leads
    assert "'laptop.mp4'" in out and "a video" in out
    assert "'photo.jpg'" in out and "an image" in out
    assert "'voice.mp3'" in out and "an audio" in out
    assert "'notes.txt'" in out and "a document" in out
    assert "from telegram" in out
    assert "never guess" in out  # the one-question rule rides along

    videos = await kit.run("find_media", {"kind": "video"})
    assert "laptop.mp4" in videos and "photo.jpg" not in videos

    matched = await kit.run("find_media", {"match": "laptop"})
    assert "laptop.mp4" in matched and "photo.jpg" not in matched

    assert "kind must be one of" in await kit.run("find_media", {"kind": "clip"})
    assert "No stored file matches" in await kit.run("find_media", {"match": "nothing here"})
    assert "limit must be a number" in await kit.run("find_media", {"limit": "many"})


async def test_share_media_refuses_a_kind_mismatch_in_plain_words() -> None:
    """A photo can never answer 'the video': when the model passes the
    kind the user named, a sniffed mismatch refuses the send — the wrong
    file stays stored, and the user hears a question, not a lie."""
    surface = MediaSurface()
    store = await _store_with_files()
    kit = NativeKit(media=store, connector=surface)
    image_id = 1
    video_id = 2

    out = await kit.run("share_media", {"media_id": image_id, "text": "here", "kind": "video"})
    assert "that media id is an image, not a video" in out
    assert "find_media" in out
    assert surface.sent == []

    assert "kind must be one of" in await kit.run(
        "share_media", {"media_id": video_id, "text": "here", "kind": "clip"}
    )

    # the match sends: the file rides with the note, its stored name kept
    out = await kit.run("share_media", {"media_id": video_id, "text": "the video", "kind": "video"})
    assert out == "Sent to the user's chat surfaces."
    assert [(text, att.filename) for text, att in surface.sent] == [("the video", "laptop.mp4")]


async def test_the_guessing_id_refusals_reach_every_media_tool() -> None:
    """look_at_media and edit_video refuse a junk id exactly like the
    share does — one strict rule, not three different parses."""
    store = await _store_with_files()
    kit = NativeKit(media=store, providers=FakeRegistry(FakeProvider([])))

    out = await kit.run("look_at_media", {"media_id": "1", "question": "what is it?"})
    assert "look_at_media needs media_id" in out and "find_media" in out

    class _NeverEditor:
        async def edit(self, media_id, operation, params):  # pragma: no cover
            raise AssertionError("a junk id must never reach the editor")

    kit = NativeKit(editor=_NeverEditor())
    out = await kit.run("edit_video", {"media_id": 0, "operation": "trim"})
    assert "edit_video needs media_id" in out and "find_media" in out


def test_a_shared_image_chunk_names_its_file_and_media_id() -> None:
    """The memory of a shared image used to hold only the description — a
    later 'send it back' found the words but not the file, and guessed an
    id. The chunk now leads with the file's own facts: name and id."""
    chunks = _chunk_text(
        "telegram",
        "shared_image",
        {
            "media_id": 3,
            "filename": "photo.jpg",
            "description": "a ginger cat asleep",
        },
    )
    assert chunks == [("", "shared file: photo.jpg (media id 3); a ginger cat asleep")]

    # no id stored (the pre-media days, or a full store): the chunk still
    # carries the description alone rather than inventing an anchor
    assert _chunk_text("telegram", "shared_image", {"description": "a cat"}) == [("", "a cat")]
