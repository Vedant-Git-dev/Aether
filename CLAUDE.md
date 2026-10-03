# CLAUDE.md

## Testing Requirements

- Before considering any task complete, always run the complete test suite using `pytest`.
- Do not finalize the task until all tests have been executed.
- If tests fail, investigate and fix relevant issues before considering the task complete.
- Report the test command and its result in the final response.

## IMP Instructions

- always fetch and rebase/pull from git upstream before starting your task
- follow the structure and patterns, **NO** unnecessary one pattern in one file and different in other
- Keep things minimal and simple, doesnt mean that solve temporary, complete in uncomplex way where possible
- always keep branches synced before/after commiting, make sure to not get merge conflicts and other git issues

## CLAUDE.md Git Policy

- This `CLAUDE.md` file must **never be committed to Git**.
- Do **not** add `CLAUDE.md` to `.gitignore`.
- Do not modify `.gitignore` to exclude this file.
- Before creating or committing changes, ensure `CLAUDE.md` is not included in the commit.
- The file should remain available locally for Claude's instructions without becoming part of the repository's Git history.
- make sure not to commit, first ask and tell what you did, your commit and summary
