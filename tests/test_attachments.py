"""Shared-file tests — the attachment processor and document extraction.
FakeProvider + FakeEventStore; no network, no DB. (Pattern:
test_screenvision.py.)"""

from __future__ import annotations

import base64
import io
import zipfile

from fakes import FakeEventStore, FakeMediaStore, FakeProvider, FakeRegistry

from aether.connectors.attachments import (
    AttachmentProcessor,
    AttachmentResult,
    attachments_note,
    extract_doc_parts,
    extract_doc_text,
    senses_text,
)
from aether.connectors.base import Attachment, InboundMessage, classify_attachment
from aether.llm.base import ProviderError
from aether.llm.types import AudioBlock, ImageBlock, Turn, VideoBlock
from aether.media.frames import WatchOutcome

MINIMAL_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT /F1 24 Tf 100 700 Td (hello world) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
trailer
<< /Root 1 0 R >>
startxref
0
%%EOF
"""


def _message(*attachments: Attachment, text: str = "") -> InboundMessage:
    return InboundMessage(
        surface="telegram",
        handle="@vedant",
        text=text,
        chat_ref="42",
        attachments=tuple(attachments),
    )


def _image() -> Attachment:
    # a real PNG magic — the store sniffs, so a truncated signature would
    # store as a download-only blob, not an image
    return Attachment("image", "cat.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"fake")


def _voice_note() -> Attachment:
    # a real Ogg magic for the same reason
    return Attachment("audio", "voice.ogg", "audio/ogg", b"OggS" + b"fake")


def _video_clip() -> Attachment:
    return Attachment("video", "clip.mp4", "video/mp4", b"\x00\x00\x00\x18ftypisom" + b"fake")


class FakeTranscriber:
    """The GroqTranscriber double: scripted words, or None when it can't."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.calls: list[tuple[bytes, str, str]] = []

    async def transcribe(self, data: bytes, filename: str, media_type: str = "") -> str | None:
        self.calls.append((data, filename, media_type))
        return self.answer


class RefusingProvider(FakeProvider):
    """A provider whose wire can't carry the medium — the call-time
    discovery the ladder runs on."""

    async def complete(self, system, messages, tools) -> Turn:
        raise ProviderError("this wire carries text and images only")


class RoleRegistry:
    """ProviderRegistry double that answers per role — the ladder tests
    need a video model that isn't also the image model."""

    def __init__(self, **roles) -> None:
        self._roles = roles

    def for_role(self, role: str):
        return self._roles.get(role)


class FakeFrameWatcher:
    """FrameWatcher double: one scripted outcome, every call recorded."""

    def __init__(self, outcome: WatchOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[bytes, str, str]] = []

    async def watch(self, video: bytes, media_type: str, label: str) -> WatchOutcome:
        self.calls.append((video, media_type, label))
        return self.outcome


def _zip(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in parts.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestClassify:
    def test_mime_says_image_first(self) -> None:
        assert classify_attachment("no_extension", "image/jpeg") == "image"

    def test_filename_extension_decides_without_a_mime(self) -> None:
        assert classify_attachment("shot.webp", "") == "image"
        assert classify_attachment("report.pdf", "") == "doc"

    def test_everything_else_is_a_doc(self) -> None:
        assert classify_attachment("archive.zip", "application/zip") == "doc"

    def test_mime_says_audio_and_video_too(self) -> None:
        assert classify_attachment("no_extension", "audio/ogg") == "audio"
        assert classify_attachment("no_extension", "video/mp4") == "video"

    def test_filename_extensions_decide_audio_and_video(self) -> None:
        assert classify_attachment("song.mp3", "") == "audio"
        assert classify_attachment("voice-note.opus", "") == "audio"
        assert classify_attachment("clip.mp4", "") == "video"
        assert classify_attachment("screen-capture.webm", "") == "video"


class TestImages:
    async def test_an_image_is_described_and_stored(self) -> None:
        provider = FakeProvider(
            [
                Turn(
                    text='{"description": "a ginger cat on a windowsill", '
                    '"people": [], "text_visible": ["MILK 2%"], "notable": []}'
                )
            ]
        )
        processor = AttachmentProcessor(FakeRegistry(provider), FakeEventStore())
        results = await processor.process(_message(_image(), text="is this our cat?"))

        assert results[0].blind is False
        assert results[0].ingested is not None and results[0].ingested.stored is True
        record = processor._events.ingested[0]
        assert record["kind"] == "shared_image"
        assert record["source"] == "telegram"
        payload = record["payload"]
        assert payload["description"] == "a ginger cat on a windowsill"
        assert payload["text_visible"] == ["MILK 2%"]
        assert payload["caption"] == "is this our cat?"
        assert payload["filename"] == "cat.png"
        assert "people" not in payload  # empty values dropped
        sender = record["sender"]
        assert sender.platform == "telegram" and sender.handle == "@vedant"

        # the vision turn carried the actual image, and no tools
        system, messages, tools = provider.calls[0]
        assert tools == []
        assert "is this our cat?" in messages[0].text
        assert any(
            isinstance(b, ImageBlock) and b.media_type == "image/png" for b in messages[0].blocks
        )

    async def test_with_a_store_the_image_bytes_are_kept(self) -> None:
        provider = FakeProvider([Turn(text='{"description": "a cat"}')])
        media = FakeMediaStore()
        processor = AttachmentProcessor(FakeRegistry(provider), FakeEventStore(), media=media)
        await processor.process(_message(_image()))

        payload = processor._events.ingested[0]["payload"]
        record = await media.get(payload["media_id"])
        assert record is not None
        assert (
            record.data == b"\x89PNG\r\n\x1a\n" + b"fake"
        )  # the bytes round-trip, not just the description
        assert record.media_type == "image/png"  # sniffed, never the declared MIME

    async def test_a_doc_keeps_its_bytes_too(self) -> None:
        media = FakeMediaStore()
        processor = AttachmentProcessor(FakeRegistry(None), FakeEventStore(), media=media)
        attachment = Attachment("doc", "notes.txt", "text/plain", b"remember the milk")
        await processor.process(_message(attachment))

        payload = processor._events.ingested[0]["payload"]
        record = await media.get(payload["media_id"])
        assert record is not None
        assert record.data == b"remember the milk"

    async def test_a_full_store_degrades_to_text_only(self) -> None:
        provider = FakeProvider([Turn(text='{"description": "a cat"}')])
        media = FakeMediaStore()
        media.full = True
        processor = AttachmentProcessor(FakeRegistry(provider), FakeEventStore(), media=media)
        results = await processor.process(_message(_image()))

        assert results[0].blind is False  # still seen, still remembered
        assert "media_id" not in processor._events.ingested[0]["payload"]

    async def test_a_blind_image_still_stores_its_bytes(self) -> None:
        media = FakeMediaStore()
        processor = AttachmentProcessor(FakeRegistry(None), FakeEventStore(), media=media)
        results = await processor.process(_message(_image(), text="my new bike"))

        assert results[0].blind is True  # the plain note still reads
        record = processor._events.ingested[0]
        assert record["kind"] == "shared_image"
        payload = record["payload"]
        kept = await media.get(payload["media_id"])
        assert kept is not None and kept.data == b"\x89PNG\r\n\x1a\n" + b"fake"
        assert "not seen yet" in payload["seen_note"]
        assert payload["caption"] == "my new bike"

    async def test_no_vision_provider_is_blind_never_crashes(self) -> None:
        processor = AttachmentProcessor(FakeRegistry(None), FakeEventStore())
        results = await processor.process(_message(_image(), text="look"))
        assert results == [AttachmentResult(ingested=None, blind=True)]
        assert processor._events.ingested == []

    async def test_a_blind_provider_is_blind_too(self) -> None:
        provider = FakeProvider([Turn(text="{}")])
        provider.supports_vision = False
        processor = AttachmentProcessor(FakeRegistry(provider), FakeEventStore())
        results = await processor.process(_message(_image()))
        assert results[0].blind is True
        assert provider.calls == []

    async def test_an_unseen_image_says_it_was_already_told(self) -> None:
        # the seen_note exists so the agent doesn't re-explain a failure the
        # connector already announced — "already told in chat" is the load-
        # bearing phrase
        media = FakeMediaStore()
        processor = AttachmentProcessor(FakeRegistry(None), FakeEventStore(), media=media)
        await processor.process(_message(_image(), text="what is this?"))
        payload = processor._events.ingested[0]["payload"]
        assert payload["seen_note"] == (
            "not seen yet — no vision model is set; the user was already "
            "told this in chat, so answer what they actually asked without "
            "re-explaining the failure"
        )


class TestSenses:
    """senses_text — the capabilities block riding every system prompt, so
    the model never guesses its senses from raw config values (where
    'base' reads as nothing being set)."""

    def test_every_role_resolved_names_its_model(self) -> None:
        text = senses_text(FakeRegistry(FakeProvider([])), voice=False, frames=False)
        assert text.startswith("Your senses right now, resolved from the running settings:")
        assert "- images: you read them (model fake-model)" in text
        assert "- audio: you listen to it directly (model fake-model)" in text
        assert "- video: you watch it directly (model fake-model)" in text
        assert "never claim a sense you don't have" in text

    def test_nothing_set_says_so_per_sense(self) -> None:
        text = senses_text(RoleRegistry(), voice=False, frames=False)
        assert "- images: you cannot read them — nothing is set for them" in text
        assert "- audio: you cannot hear it — nothing is set for it" in text
        assert "- video: you cannot watch it — nothing is set for it" in text

    def test_a_blind_image_provider_is_not_an_eye(self) -> None:
        provider = FakeProvider([])
        provider.supports_vision = False
        text = senses_text(RoleRegistry(image=provider), voice=False, frames=False)
        assert "- images: you cannot read them — nothing is set for them" in text

    def test_voice_transcription_is_a_ladder_rung_not_a_model(self) -> None:
        text = senses_text(RoleRegistry(), voice=True, frames=False)
        assert "- audio: no model listens, but voice notes are transcribed for you" in text

    def test_frames_reach_the_agent_as_images(self) -> None:
        # image set, video not — frames are the honest in-between rung
        text = senses_text(RoleRegistry(image=FakeProvider([])), voice=False, frames=True)
        assert (
            "- video: no model watches it, but frames pulled from a shared "
            "video reach you as images (model fake-model)" in text
        )

    def test_frames_do_not_count_without_an_eye(self) -> None:
        text = senses_text(RoleRegistry(), voice=True, frames=True)
        assert "- video: you cannot watch it — nothing is set for it" in text

    def test_the_processor_reads_its_own_senses(self) -> None:
        processor = AttachmentProcessor(
            FakeRegistry(FakeProvider([])),
            FakeEventStore(),
            frames=FakeFrameWatcher(WatchOutcome(frames={})),
        )
        assert processor.capabilities_note() == senses_text(
            processor._providers, voice=False, frames=True
        )

    async def test_non_json_vision_turn_stores_raw_text(self) -> None:
        processor = AttachmentProcessor(
            FakeRegistry(FakeProvider([Turn(text="A handwritten note, no JSON")])),
            FakeEventStore(),
        )
        results = await processor.process(_message(_image()))
        assert results[0].blind is False
        assert processor._events.ingested[0]["payload"]["description"] == (
            "A handwritten note, no JSON"
        )

    def test_the_note_is_the_plain_words_once_per_burst(self) -> None:
        one = attachments_note([AttachmentResult(None, blind=True)])
        assert one == (
            "I can't see this image yet — no vision model is set. Set one with "
            "/config (llm.image_model), or pick a main model that can read "
            "images, and I'll see what you send."
        )
        two = attachments_note(
            [AttachmentResult(None, blind=True), AttachmentResult(None, blind=True)]
        )
        assert "these images" in two
        assert attachments_note([AttachmentResult(None), AttachmentResult(None)]) == ""

    def test_per_file_notes_ride_alongside_the_blind_note(self) -> None:
        both = attachments_note(
            [AttachmentResult(None, blind=True), AttachmentResult(None, note="voice words")]
        )
        assert "can't see" in both and "voice words" in both
        assert attachments_note([AttachmentResult(None, note="just this")]) == "just this"


class TestDocs:
    async def test_a_text_file_is_stored_with_its_content(self) -> None:
        processor = AttachmentProcessor(FakeRegistry(None), FakeEventStore())
        attachment = Attachment("doc", "notes.txt", "text/plain", b"remember the milk")
        results = await processor.process(_message(attachment))

        assert results[0].ingested is not None and results[0].ingested.stored is True
        record = processor._events.ingested[0]
        assert record["kind"] == "shared_doc"
        payload = record["payload"]
        assert payload["text"] == "remember the milk"
        assert payload["filename"] == "notes.txt"
        assert payload["chat_ref"] == "42"

    async def test_doc_text_is_capped(self) -> None:
        processor = AttachmentProcessor(FakeRegistry(None), FakeEventStore())
        big = Attachment("doc", "big.txt", "text/plain", b"x" * 100_000)
        await processor.process(_message(big))
        assert len(processor._events.ingested[0]["payload"]["text"]) == 20_000

    async def test_an_unextractable_file_still_leaves_a_row(self) -> None:
        processor = AttachmentProcessor(FakeRegistry(None), FakeEventStore())
        attachment = Attachment("doc", "archive.zip", "application/zip", b"not really a zip")
        await processor.process(_message(attachment))
        payload = processor._events.ingested[0]["payload"]
        assert payload["extract_note"] == "no readable text could be extracted"
        assert "text" not in payload


class TestAudio:
    async def test_a_voice_note_is_transcribed_into_a_shared_audio_event(self) -> None:
        transcriber = FakeTranscriber("the porch light is still on")
        media = FakeMediaStore()
        processor = AttachmentProcessor(
            FakeRegistry(None), FakeEventStore(), media=media, voice=transcriber
        )
        note = _voice_note()
        results = await processor.process(_message(note, text="listen to this"))

        assert results[0].ingested is not None and results[0].ingested.stored is True
        assert results[0].note == ""  # read plainly — nothing to apologize for
        record = processor._events.ingested[0]
        assert record["kind"] == "shared_audio"
        payload = record["payload"]
        assert payload["text"] == "the porch light is still on"
        assert payload["caption"] == "listen to this"
        kept = await media.get(payload["media_id"])
        assert kept is not None and kept.data == note.data
        # the transcription heard the real bytes, with their real names
        assert transcriber.calls == [(note.data, "voice.ogg", "audio/ogg")]

    async def test_an_audio_model_listens_directly(self) -> None:
        provider = FakeProvider(
            [
                Turn(
                    text=(
                        '{"description": "a two-minute ramble about the porch light", '
                        '"text_spoken": ["the porch light is still on"]}'
                    )
                )
            ]
        )
        processor = AttachmentProcessor(RoleRegistry(audio=provider), FakeEventStore())
        note = _voice_note()
        results = await processor.process(_message(note, text="listen to this"))

        record = processor._events.ingested[0]
        assert record["kind"] == "shared_audio"
        payload = record["payload"]
        assert payload["description"] == "a two-minute ramble about the porch light"
        assert payload["text_spoken"] == ["the porch light is still on"]
        # listened, not transcribed — and no apology rides along
        assert "text" not in payload
        assert "transcribe_note" not in payload
        assert results[0].note == ""
        # the audio turn carried the real bytes
        assert any(isinstance(b, AudioBlock) for b in provider.calls[0][1][0].blocks)

    async def test_a_refused_audio_note_falls_back_to_transcription(self) -> None:
        transcriber = FakeTranscriber("the porch light is still on")
        processor = AttachmentProcessor(
            RoleRegistry(audio=RefusingProvider([])), FakeEventStore(), voice=transcriber
        )
        results = await processor.process(_message(_voice_note()))

        payload = processor._events.ingested[0]["payload"]
        assert payload["text"] == "the porch light is still on"
        assert "transcribe_note" not in payload
        assert results[0].note == ""
        # the fallback actually ran
        assert transcriber.calls

    async def test_a_refused_note_without_a_transcriber_names_both_gaps(self) -> None:
        processor = AttachmentProcessor(RoleRegistry(audio=RefusingProvider([])), FakeEventStore())
        results = await processor.process(_message(_voice_note()))

        payload = processor._events.ingested[0]["payload"]
        assert payload["transcribe_note"] == (
            "not understood — the configured audio model couldn't take it "
            "and no transcription service is set"
        )
        assert "text" not in payload
        # the fact rides the payload for the agent to say — nothing canned
        assert results[0].note == ""

    async def test_without_a_transcriber_the_note_is_kept_with_the_plain_fact(self) -> None:
        media = FakeMediaStore()
        processor = AttachmentProcessor(RoleRegistry(), FakeEventStore(), media=media)
        note = _voice_note()
        results = await processor.process(_message(note))

        assert results[0].ingested is not None and results[0].ingested.stored is True
        payload = processor._events.ingested[0]["payload"]
        assert payload["transcribe_note"] == (
            "not understood — no audio model or transcription service is set"
        )
        assert "text" not in payload
        kept = await media.get(payload["media_id"])
        assert kept is not None and kept.data == note.data
        # no canned chat note — the agent speaks from the payload
        assert results[0].note == ""
        assert attachments_note(results) == ""

    async def test_a_failed_transcription_degrades_the_same_way(self) -> None:
        transcriber = FakeTranscriber(None)
        processor = AttachmentProcessor(RoleRegistry(), FakeEventStore(), voice=transcriber)
        results = await processor.process(_message(_voice_note()))
        payload = processor._events.ingested[0]["payload"]
        assert payload["transcribe_note"] == "not understood — no transcription service answered"
        assert results[0].note == ""


class TestVideo:
    async def test_a_video_model_watches_directly(self) -> None:
        provider = FakeProvider([Turn(text='{"description": "a dog on skates"}')])
        processor = AttachmentProcessor(RoleRegistry(video=provider), FakeEventStore())
        clip = _video_clip()
        results = await processor.process(_message(clip, text="the dog on skates"))

        record = processor._events.ingested[0]
        assert record["kind"] == "shared_video"
        payload = record["payload"]
        assert payload["description"] == "a dog on skates"
        # watched, not apologized for
        assert "understand_note" not in payload
        assert results[0].note == ""
        # the video turn carried the real bytes
        assert any(isinstance(b, VideoBlock) for b in provider.calls[0][1][0].blocks)

    async def test_a_refused_video_falls_through_to_frames(self) -> None:
        image = FakeProvider([Turn(text='{"description": "a dog rolling past"}')])
        watcher = FakeFrameWatcher(
            WatchOutcome(frames={"frame_00.jpg": b"jpg bytes"}, duration_seconds=9.5)
        )
        processor = AttachmentProcessor(
            RoleRegistry(video=RefusingProvider([]), image=image),
            FakeEventStore(),
            frames=watcher,
        )
        clip = _video_clip()
        results = await processor.process(_message(clip))

        payload = processor._events.ingested[0]["payload"]
        assert payload["description"] == "a dog rolling past"
        assert payload["understand_note"] == (
            "understood from evenly spaced frames, not watched directly"
        )
        assert payload["duration"] == 9.5
        assert results[0].note == ""
        # the frames rung actually ran, with the real bytes
        assert watcher.calls == [(clip.data, "video/mp4", "clip.mp4")]

    async def test_frames_ride_as_images_in_frame_order(self) -> None:
        image = FakeProvider([Turn(text='{"description": "two frames"}')])
        watcher = FakeFrameWatcher(
            WatchOutcome(frames={"frame_01.jpg": b"two", "frame_00.jpg": b"one"})
        )
        processor = AttachmentProcessor(RoleRegistry(image=image), FakeEventStore(), frames=watcher)
        await processor.process(_message(_video_clip()))

        blocks = [b for b in image.calls[0][1][0].blocks if isinstance(b, ImageBlock)]
        assert [b.data_b64 for b in blocks] == [
            base64.b64encode(b"one").decode("ascii"),
            base64.b64encode(b"two").decode("ascii"),
        ]

    async def test_a_refused_video_with_no_image_model_names_both_gaps(self) -> None:
        processor = AttachmentProcessor(RoleRegistry(video=RefusingProvider([])), FakeEventStore())
        results = await processor.process(_message(_video_clip()))

        payload = processor._events.ingested[0]["payload"]
        assert payload["understand_note"] == (
            "kept, but not understood — the configured video model couldn't "
            "take it, and no image model is set to read frames"
        )
        assert "description" not in payload
        assert results[0].note == ""

    async def test_a_spent_budget_says_what_happened(self) -> None:
        image = FakeProvider([])
        watcher = FakeFrameWatcher(
            WatchOutcome(
                frames={}, duration_seconds=None, reason="today's video job budget is spent"
            )
        )
        processor = AttachmentProcessor(RoleRegistry(image=image), FakeEventStore(), frames=watcher)
        results = await processor.process(_message(_video_clip()))

        payload = processor._events.ingested[0]["payload"]
        assert payload["understand_note"] == (
            "kept, but not understood — today's video job budget is spent"
        )
        # the image model never fired — the budget stopped the rung first
        assert image.calls == []
        assert results[0].note == ""

    async def test_a_shared_video_is_kept_with_the_plain_fact(self) -> None:
        media = FakeMediaStore()
        processor = AttachmentProcessor(RoleRegistry(), FakeEventStore(), media=media)
        clip = _video_clip()
        results = await processor.process(_message(clip, text="the dog on skates"))

        assert results[0].ingested is not None and results[0].ingested.stored is True
        record = processor._events.ingested[0]
        assert record["kind"] == "shared_video"
        payload = record["payload"]
        assert payload["understand_note"] == (
            "kept, but not understood — no video model or image model is set"
        )
        assert payload["caption"] == "the dog on skates"
        kept = await media.get(payload["media_id"])
        assert kept is not None and kept.data == clip.data
        # no canned chat note — the agent says it from the payload
        assert results[0].note == ""
        assert attachments_note(results) == ""


class TestExtraction:
    def test_plain_text_and_family(self) -> None:
        assert extract_doc_text("a.txt", "text/plain", b"hello\nworld") == "hello\nworld"
        assert extract_doc_text("a.csv", "", b"a,b") == "a,b"
        assert extract_doc_text("a.md", "text/markdown", b"# hi") == "# hi"

    def test_pdf(self) -> None:
        text = extract_doc_text("letter.pdf", "application/pdf", MINIMAL_PDF)
        assert "hello world" in text

    def test_pdf_missing_dependency_is_distinguishable_and_actionable(
        self, monkeypatch, caplog
    ) -> None:
        """A missing pypdf must not look like a corrupt file: it's logged
        loudly (ERROR, naming the fix) instead of blending into the quiet
        per-file WARNING every other extraction failure gets."""
        import sys

        monkeypatch.setitem(sys.modules, "pypdf", None)  # simulates "not installed"
        with caplog.at_level("ERROR", logger="aether.connectors.attachments"):
            text = extract_doc_text("letter.pdf", "application/pdf", MINIMAL_PDF)
        assert text == ""
        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, "missing pypdf must log at ERROR, not just the usual WARNING"
        assert "pypdf" in error_records[0].message
        assert "install" in error_records[0].message.lower()

    def test_pdf_unexpected_parsing_errors_still_degrade_quietly(self, caplog) -> None:
        """A genuine parsing failure (not a missing dependency) must keep
        its existing, quieter treatment — only the dependency case gets the
        louder log level."""
        with caplog.at_level("WARNING", logger="aether.connectors.attachments"):
            text = extract_doc_text("letter.pdf", "application/pdf", b"not a pdf at all")
        assert text == ""
        assert not any(r.levelname == "ERROR" for r in caplog.records)
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_pdf_encrypted_or_unreadable_extracts_to_nothing_never_raises(self) -> None:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(MINIMAL_PDF))
        writer = PdfWriter()
        writer.append(reader)
        writer.encrypt(user_password="secret", owner_password="secret-owner")
        buf = io.BytesIO()
        writer.write(buf)

        assert extract_doc_text("locked.pdf", "application/pdf", buf.getvalue()) == ""

    def test_docx(self) -> None:
        xml = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body>"
            b"<w:p><w:r><w:t>First </w:t></w:r><w:r><w:t>paragraph</w:t></w:r></w:p>"
            b"<w:p><w:r><w:t>Second</w:t></w:r></w:p>"
            b"</w:body></w:document>"
        )
        docx = _zip({"word/document.xml": xml})
        assert extract_doc_text("letter.docx", "", docx) == "First paragraph\nSecond"

    def test_xlsx(self) -> None:
        strings = (
            b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b"<si><t>Name</t></si><si><t>Sam</t></si></sst>"
        )
        sheet = (
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b"<sheetData><row><c><v>42</v></c></row></sheetData></worksheet>"
        )
        xlsx = _zip({"xl/sharedStrings.xml": strings, "xl/worksheets/sheet1.xml": sheet})
        text = extract_doc_text("books.xlsx", "", xlsx)
        assert "Name" in text and "Sam" in text and "42" in text

    def test_pptx(self) -> None:
        slide1 = (
            b'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            b'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            b"<p:cSld><p:spTree><p:sp><p:txBody>"
            b"<a:p><a:r><a:t>Roadmap</a:t></a:r></a:p>"
            b"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        slide2 = (
            b'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            b'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            b"<p:cSld><p:spTree><p:sp><p:txBody>"
            b"<a:p><a:r><a:t>Q3 plan</a:t></a:r></a:p>"
            b"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        pptx = _zip({"ppt/slides/slide2.xml": slide2, "ppt/slides/slide1.xml": slide1})
        assert extract_doc_text("deck.pptx", "", pptx) == "Roadmap\nQ3 plan"

    def test_corrupt_files_extract_to_nothing_never_raise(self) -> None:
        garbage = b"\x00\x01garbage\xff\xfe"
        for name in ("a.pdf", "a.docx", "a.xlsx", "a.pptx", "a.zip", ""):
            assert extract_doc_text(name, "", garbage) == ""
        assert extract_doc_text("a.txt", "", garbage) != ""  # plain text survives anything


class TestZipBombProtection:
    """docx/xlsx/pptx are zips of XML — MAX_ATTACHMENT_BYTES only bounds the
    compressed upload, so these guard the decompressed side: a small
    crafted archive must never be allowed to blow up memory/CPU."""

    def _bomb_zip(self, member: str, size: int) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(member, b"A" * size)  # deflates to a tiny compressed size
        return buf.getvalue()

    def test_oversized_member_is_rejected_before_full_decompression(self) -> None:
        from aether.connectors.attachments import MAX_ZIP_ENTRY_BYTES

        bomb = self._bomb_zip("word/document.xml", MAX_ZIP_ENTRY_BYTES + 1024)
        assert extract_doc_text("letter.docx", "", bomb) == ""

    def test_many_small_members_are_capped_by_the_total_budget(self) -> None:
        from aether.connectors.attachments import MAX_ZIP_ENTRY_BYTES, MAX_ZIP_TOTAL_BYTES

        # each member is under the per-entry cap, but enough of them blow
        # past the combined-document cap
        per_member = MAX_ZIP_ENTRY_BYTES // 2
        count = (MAX_ZIP_TOTAL_BYTES // per_member) + 2
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/sharedStrings.xml", b"<sst/>")
            for i in range(count):
                zf.writestr(f"xl/worksheets/sheet{i}.xml", b"A" * per_member)
        assert extract_doc_text("book.xlsx", "", buf.getvalue()) == ""

    def test_too_many_archive_entries_is_rejected(self) -> None:
        from aether.connectors.attachments import MAX_ZIP_ENTRIES

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(MAX_ZIP_ENTRIES + 1):
                zf.writestr(f"xl/worksheets/sheet{i}.xml", b"<x/>")
        assert extract_doc_text("book.xlsx", "", buf.getvalue()) == ""

    def test_xml_doctype_and_entity_declarations_are_rejected(self) -> None:
        """The 'billion laughs' shape: a tiny entity declaration that
        expands to an enormous parse — refused outright, not expanded."""
        xml = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p><w:r><w:t>&lol;</w:t></w:r></w:p></w:body></w:document>"
        )
        docx = _zip({"word/document.xml": xml})
        assert extract_doc_text("letter.docx", "", docx) == ""

    def test_normal_documents_are_unaffected_by_the_new_caps(self) -> None:
        """The bounded reader must still produce identical output for an
        ordinary, well-formed document — these are the same fixtures
        TestExtraction uses for docx/xlsx/pptx."""
        xml = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>"
        )
        docx = _zip({"word/document.xml": xml})
        assert extract_doc_text("letter.docx", "", docx) == "Hello"


class TestStructuredParts:
    """extract_doc_parts: the (location, text) form the vector memory
    indexes — pages and slides carry the numbering the file itself has,
    everything else is one whole part."""

    def test_pdf_parts_are_per_page(self) -> None:
        parts = extract_doc_parts("letter.pdf", "application/pdf", MINIMAL_PDF)
        assert parts, "the minimal pdf has at least one page"
        assert parts[0][0] == "page 1"
        assert "hello world" in parts[0][1]

    def test_pptx_parts_are_per_slide_in_deck_order(self) -> None:
        slide = (
            b'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            b'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            b"<p:cSld><p:spTree><p:sp><p:txBody>"
            b"<a:p><a:r><a:t>Roadmap</a:t></a:r></a:p>"
            b"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        # slide10 would sort before slide2 lexicographically — numeric
        # order is what the deck actually shows
        pptx = _zip(
            {
                "ppt/slides/slide10.xml": slide,
                "ppt/slides/slide2.xml": slide,
                "ppt/slides/slide1.xml": slide,
            }
        )
        parts = extract_doc_parts("deck.pptx", "", pptx)
        assert [loc for loc, _ in parts] == ["slide 1", "slide 2", "slide 10"]
        assert all("Roadmap" in text for _, text in parts)

    def test_plain_text_is_one_whole_part(self) -> None:
        assert extract_doc_parts("a.txt", "text/plain", b"hello\nworld") == [("", "hello\nworld")]
        assert extract_doc_parts("a.csv", "", b"a,b") == [("", "a,b")]

    def test_unreadable_file_has_no_parts(self) -> None:
        garbage = b"\x00\x01garbage\xff\xfe"
        assert extract_doc_parts("a.pdf", "", garbage) == []
        assert extract_doc_parts("a.docx", "", garbage) == []
