import { AetherFilePreviewModal } from "./file-preview-modal.ts";

if (!customElements.get("aether-file-preview-modal")) {
  customElements.define("aether-file-preview-modal", AetherFilePreviewModal);
}
