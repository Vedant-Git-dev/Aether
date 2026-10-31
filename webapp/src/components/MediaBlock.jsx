// One stored file under an activity row, rendered by what the server says
// it is: the sniffed type from the response decides the element, so a lying
// declared type in the event can never pick the renderer. A missing blob
// degrades to the plain note, never a broken element; anything the server
// won't render inline (a document, an archive) shows nothing here — the
// row's words already say what it is.
import { useEffect, useState } from "react";
import { fetchMedia } from "../lib/api.js";

export default function MediaBlock({ mediaId }) {
  const [shown, setShown] = useState(null); // { url, type } | "gone"

  useEffect(() => {
    let alive = true;
    let url = null;
    fetchMedia(`/api/media/${mediaId}`)
      .then((blob) => {
        if (!alive) return;
        url = URL.createObjectURL(blob);
        setShown({ url, type: blob.type });
      })
      .catch(() => {
        if (alive) setShown("gone");
      });
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [mediaId]);

  if (!shown) return null;
  if (shown === "gone") {
    return (
      <div data-testid="media-gone" className="mt-1 text-xs text-ink-3">
        (this file is no longer stored)
      </div>
    );
  }
  const { url, type } = shown;
  // only the sniffed, server-allowlisted types keep their MIME on the way
  // out — anything else arrives as octet-stream and renders nothing
  if (type.startsWith("image/")) {
    return (
      <img
        data-testid="media-image"
        src={url}
        alt=""
        className="mt-1.5 max-h-56 rounded-[8px] border border-[var(--aether-hairline)]"
      />
    );
  }
  if (type === "video/mp4" || type === "video/webm") {
    return (
      <video
        data-testid="media-video"
        controls
        src={url}
        className="mt-1.5 max-h-64 w-full max-w-md rounded-[8px] border border-[var(--aether-hairline)]"
      />
    );
  }
  if (type === "audio/mpeg" || type === "audio/ogg") {
    return (
      <audio
        data-testid="media-audio"
        controls
        src={url}
        className="mt-1.5 w-full max-w-md"
      />
    );
  }
  return null;
}
