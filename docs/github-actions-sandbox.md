# The batch queue (GitHub Actions)

Aether can run patient, non-sensitive background jobs in **your own code
repository**, using GitHub Actions' workflow queue as a batch runner. This
page is the whole setup: two files committed to the repo, one key pasted
into Aether, one switch in the settings.

## What this is — and what it is not

- **Batch only.** A job waits in the repository's queue, starts when a
  runner is free, and takes minutes — so only work that can wait
  (`latency_class: background`) and may be seen by the repository's readers
  (`sensitivity: ordinary`) is ever sent here. Sensitive, interactive, or
  unlabeled work is refused by the adapter itself and runs on a dedicated
  sandbox instead.
- **Off by default.** Nothing is dispatched until you switch the batch
  queue on and paste the key.
- **Metered.** Every job's wall-clock time is counted against a monthly
  minutes cap you set (default 1500, below the 2000 free minutes GitHub
  bills). At 80% Aether warns you; at 100% it stops sending jobs there
  until the month turns.
- **One job, one run, no leftovers.** Each job is one workflow run; the
  run is deleted after Aether collects the outcome, so the repository
  doesn't accumulate history.
- **Not a sandbox.** The queue's machines are shared GitHub
  infrastructure. Anything that needs isolation or secrecy never goes
  here — the routing ladder guarantees it in code, not in wording.

## What crosses into the repository

No Aether secret is ever committed or dispatched. The only things a run
receives are three strings:

- `session` — which job this is (a short id, for the run's name),
- `bundle` — a one-time URL: the runner pulls the job's files from it,
  exactly once,
- `result` — a one-time URL: the runner posts the job's exit code and
  output to it, exactly once.

Both URLs contain a random 32-byte token, die on first use, and expire
within half an hour regardless. A replayed or leaked URL is simply dead.
Even the run's own logs are safe to keep — but Aether deletes the run
after collecting anyway.

## Setup

### 1. Commit the two files

`.github/workflows/aether-batch.yml`:

```yaml
name: Aether batch job

on:
  workflow_dispatch:
    inputs:
      session:
        description: which job this is
        required: true
      bundle:
        description: one-time URL to pull the job's files from
        required: true
      result:
        description: one-time URL to push the job's outcome to
        required: true

permissions: {}

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Run the job
        env:
          AETHER_BUNDLE_URL: ${{ inputs.bundle }}
          AETHER_RESULT_URL: ${{ inputs.result }}
        run: python .github/scripts/aether_runner.py
```

`.github/scripts/aether_runner.py`:

```python
#!/usr/bin/env python3
"""The batch runner: pull the job's files, run them, post the outcome.

Everything it touches is a one-time URL made for this very run — the
bundle dies the moment it is read, the result URL dies the moment this
posts to it, and both expire within half an hour no matter what. No
Aether secret lives in this repository.
"""

import json
import os
import pathlib
import subprocess
import sys
import urllib.request

BUNDLE_URL = os.environ["AETHER_BUNDLE_URL"]
RESULT_URL = os.environ["AETHER_RESULT_URL"]

# the job's own limit is enforced by Aether's clock, not here; this cap
# only keeps a hung script from holding the runner for the full step
JOB_TIMEOUT_SECONDS = 20 * 60


def main() -> int:
    with urllib.request.urlopen(BUNDLE_URL, timeout=120) as resp:
        bundle = json.load(resp)

    work = pathlib.Path("aether-job")
    work.mkdir(exist_ok=True)
    for name, content in bundle["files"].items():
        # names are flat by contract — a path would be refused before dispatch
        (work / name).write_text(content, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, bundle["script_name"]],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=JOB_TIMEOUT_SECONDS,
    )

    # if the bundle pull above failed, this script dies before posting —
    # on purpose: a spent bundle means another run already had this job,
    # and posting here would overwrite its real outcome
    payload = json.dumps(
        {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-1_000_000:],
            "stderr": proc.stderr[-1_000_000:],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        RESULT_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
```

### 2. Make a key for Aether

Create a **fine-grained personal access token** (GitHub settings ->
Developer settings -> Fine-grained tokens):

- Repository access: **only the repository** holding the workflow above.
- Permissions: **Actions -> Read and write** (nothing else — not contents,
  not secrets, not administration).

Paste it into Aether's settings page as the **batch queue key**.

### 3. Point Aether at the queue

In the settings, switch the batch queue on and fill in:

- the repository (`owner/name`),
- the workflow file name (`aether-batch.yml`),
- the branch it dispatches on (`main`, unless yours differs),
- Aether's own public address (already set for the panel — the runner
  uses it to reach the transfer routes),
- the monthly minutes cap.

## Failure shapes, plainly

- If the queue never takes the job (a dispatch that didn't land), Aether
  tries once more and then falls back to another provider — nothing ran.
- Once the queue has accepted the job, the outcome is always real: the
  posted exit code, or "the job ran past its time limit and was stopped"
  if it never came back. A started job is never re-run elsewhere.
- A run that finished without posting (network died mid-report) reads as
  a failed job, not a silent success.

## The honest gray zone

Running your own short scripts through the Actions queue is shaped like
CI and billed as CI, but it is not what the queue was built for — the
Additional Product Terms frown on using it as a general compute service.
Aether treats it accordingly: off by default, capped by a monthly meter
that fails closed, batch-only, and every job deleted after collect.
Keep the cap honest, keep the jobs short, and turn it off if you stop
needing it.
