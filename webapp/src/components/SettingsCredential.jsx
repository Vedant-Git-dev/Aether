// One paste field: the value lives in this component only until Store
// consumes it — it is never rendered back and never kept after.
import { useState } from "react";
import { Button } from "./ui.jsx";
import { QuietInput } from "./SettingsFrame.jsx";

export default function CredentialInput({ cred, onSave }) {
  const [value, setValue] = useState("");

  const submit = () => {
    const next = value.trim();
    if (!next) return;
    onSave(cred, next);
    setValue("");
  };

  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <QuietInput
        data-testid={`settings-cred-${cred.name}`}
        type="password"
        placeholder="paste a value"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        className="quiet-field-edge w-52"
      />
      <Button
        kind="neutral"
        data-testid={`settings-cred-save-${cred.name}`}
        disabled={!value.trim()}
        onClick={submit}
        className="text-[12px]"
      >
        Store
      </Button>
    </div>
  );
}
