import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
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
