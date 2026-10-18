"""Media — Aether's eyes and hands for everything that is not text.

- ``store`` — the encrypted blob store: shared images, voice notes,
  videos, and generated files become one AES-GCM blob per row, deduped
  by a keyed content tag, kept under a live-read quota with
  least-recently-use eviction that never touches anything a pending
  approval or an undelivered result still needs.
"""

from .store import MediaRecord, MediaRef, MediaStore, MediaStoreFull, sniff_media_type

__all__ = ["MediaRecord", "MediaRef", "MediaStore", "MediaStoreFull", "sniff_media_type"]
