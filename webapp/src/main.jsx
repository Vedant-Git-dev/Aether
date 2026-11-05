import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// the interface face and the figures face, self-hosted so the panel works
// with no network. Latin only — this UI is English.
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "./index.css";
import App from "./App.jsx";
import { DataProvider } from "./store.jsx";
import { ToastProvider } from "./components/Toasts.jsx";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ToastProvider>
      <DataProvider>
        <App />
      </DataProvider>
    </ToastProvider>
  </StrictMode>
);
