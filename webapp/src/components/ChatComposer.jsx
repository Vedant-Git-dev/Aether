import { useEffect, useRef, useState } from "react";
import { Button, Card } from "./ui.jsx";

const RECEIPT_TIMEOUT_MS = 8000;
const RETRY_LIMIT_MS = 8000;

export default function ChatComposer() {
  const [token, setToken] = useState("");
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [status, setStatus] = useState("");
  const outgoing = useRef(null);

  useEffect(() => {
    const suspend = () => {
      outgoing.current?.();
      outgoing.current = null;
      setToken("");
      setText("");
      setPending(false);
      setStatus("");
    };
    const onVisibility = () => { if (document.hidden) suspend(); };
    window.addEventListener("pagehide", suspend);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", suspend);
      document.removeEventListener("visibilitychange", onVisibility);
      outgoing.current?.();
    };
  }, []);

  const submit = (event) => {
    event.preventDefault();
    if (!token.trim() || !text.trim() || outgoing.current) return;
    const frame = { id: crypto.randomUUID(), text };
    const url = new URL("/ws", location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    let closed = false;
    let socket;
    let timer;
    let delay = 250;

    const close = () => {
      closed = true;
      clearTimeout(timer);
      socket?.close();
    };

    const settle = (message) => {
      close();
      outgoing.current = null;
      setPending(false);
      setStatus(message);
    };

    const connect = () => {
      if (closed) return;
      const attempt = new WebSocket(url);
      socket = attempt;
      timer = setTimeout(() => attempt.close(), RECEIPT_TIMEOUT_MS);
      attempt.onopen = () => {
        if (closed) return;
        attempt.send(JSON.stringify({ token: token.trim() }));
        attempt.send(JSON.stringify(frame));
      };
      attempt.onmessage = ({ data }) => {
        if (closed || socket !== attempt) return;
        let receipt;
        try {
          receipt = JSON.parse(data);
        } catch {
          return;
        }
        if (receipt?.type === "receipt" && receipt.id === frame.id) {
          setText("");
          settle("Message received.");
        }
      };
      attempt.onclose = ({ code }) => {
        if (closed || socket !== attempt) return;
        clearTimeout(timer);
        if (code === 1008) {
          setToken("");
          setText("");
          settle("The panel session ended. Delivery could not be confirmed.");
          return;
        }
        setStatus("Connection interrupted. Retrying this message.");
        timer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, RETRY_LIMIT_MS);
      };
    };

    outgoing.current = close;
    setPending(true);
    setStatus("Waiting for a receipt.");
    connect();
  };

  return (
    <Card className="mb-4 p-4">
      <details>
        <summary className="cursor-pointer font-medium">Send a chat message</summary>
        <form className="mt-3 flex max-w-lg flex-col gap-3" autoComplete="off" onSubmit={submit}>
          <label className="flex flex-col gap-1">
            Panel session token
            <input className="rounded border px-3 py-2" type="password" autoComplete="off" value={token} disabled={pending} onChange={(e) => setToken(e.target.value)} required />
          </label>
          <label className="flex flex-col gap-1">
            Message
            <textarea className="rounded border px-3 py-2" value={text} disabled={pending} onChange={(e) => setText(e.target.value)} required />
          </label>
          <Button className="self-start" type="submit" disabled={pending}>Send message</Button>
          {status && <p role="status">{status}</p>}
        </form>
      </details>
    </Card>
  );
}
