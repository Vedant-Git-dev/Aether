/**
 * Help examples shown by the Browser CLI root command.
 */
/** Core Browser CLI examples for lifecycle and inspection commands. */
export const browserCoreExamples = [
  "aether browser status",
  "aether browser start",
  "aether browser start --headless",
  "aether browser stop",
  "aether browser tabs",
  "aether browser open https://example.com",
  "aether browser focus abcd1234",
  "aether browser close abcd1234",
  "aether browser screenshot",
  "aether browser screenshot --full-page",
  "aether browser screenshot --ref 12",
  "aether browser snapshot",
  "aether browser snapshot --format aria --limit 200",
  "aether browser snapshot --efficient",
  "aether browser snapshot --labels",
];

/** Browser CLI examples for interaction/action commands. */
export const browserActionExamples = [
  "aether browser navigate https://example.com",
  "aether browser resize 1280 720",
  "aether browser click 12 --double",
  "aether browser click-coords 120 340",
  'aether browser type 23 "hello" --submit',
  "aether browser press Enter",
  "aether browser hover 44",
  "aether browser drag 10 11",
  "aether browser select 9 OptionA OptionB",
  "aether browser upload /tmp/aether/uploads/file.pdf",
  "aether browser upload media://inbound/file.pdf",
  'aether browser fill --fields \'[{"ref":"1","value":"Ada"}]\'',
  "aether browser dialog --accept",
  'aether browser wait --text "Done"',
  "aether browser evaluate --fn '(el) => el.textContent' --ref 7",
  "aether browser evaluate --fn 'const title = document.title; return title;'",
  "aether browser console --level error",
  "aether browser pdf",
  "aether browser batch --actions-file plan.json",
  'aether browser batch --actions \'[{"kind":"wait","timeMs":500},{"kind":"click","ref":"12"},{"kind":"type","ref":"23","text":"hello"}]\'',
  "aether browser batch --actions-file plan.json --continue",
];
