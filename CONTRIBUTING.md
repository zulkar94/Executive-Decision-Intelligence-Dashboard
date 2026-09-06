# Contributing

## Setup

```bash
cp .env.example .env
make install
make test
```

A devcontainer is included: open the repository in a Codespace or in VS Code with the Dev
Containers extension and it installs everything and forwards port 8000.

## Before opening a pull request

```bash
make lint type test security
```

All four must pass. CI runs the same commands plus `pip-audit`, gitleaks, Trivy, CodeQL,
and the end-to-end smoke script, on Python 3.11 and 3.12, with coverage gated at 85%.

## Standards

**Tests are part of the change.** New analytics need a unit test against a hand-checkable
case; new endpoints need a test for the happy path and for the authorisation boundary.

**Security findings get fixed, not suppressed.** `# nosec` is not accepted in
`app/security/`. If a scanner is genuinely wrong, restructure the code so it is
unambiguous — renaming a variable is usually enough, and leaves the next reader better off.

**Keep the analytics package pure.** `app/analytics/` must not import FastAPI, SQLAlchemy,
or `app.config`. It takes arrays and returns dataclasses. Anything else belongs in a router
or in `app/matrix.py`.

**Comments explain the non-obvious.** Why a constant column maps to 0.5, why the lockout
counter commits before raising, why coverage needs the greenlet concurrency setting. Not
what the line does.

**Frontend stays dependency-free.** No npm, no bundler, no CDN. If something needs a
library, it probably needs to move to the backend.

## Adding an MCDA method

1. Implement it in `app/analytics/mcda.py` returning `(scores, contributions)`.
2. Register it in `evaluate` and extend the `Method` literal in `app/routers/analytics.py`
   and the `RankingOut` schema.
3. Add a unit test asserting it agrees with the others on a dominant option, and a bounds
   test if the scores have a natural range.

## Commits

Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`). Keep the
subject under 72 characters and explain *why* in the body when it is not obvious.
