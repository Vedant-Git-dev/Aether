import { QuestionPage } from "./question-page.ts";

if (!customElements.get("aether-question-page")) {
  customElements.define("aether-question-page", QuestionPage);
}
