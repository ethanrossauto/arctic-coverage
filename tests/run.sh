#!/usr/bin/env bash
# Every check this project can run, in one command.
#
# 🔑 THE POINT IS THAT CI CALLS THIS AND NOTHING ELSE. The workflow file installs
# dependencies and runs this script, so a red build is reproducible locally with one command
# instead of by reading yaml, and no check can exist that only ever runs on a GitHub runner.
# A test nobody can run before pushing is a test nobody runs.
#
#     bash tests/run.sh              everything that needs no database
#     RUN_E2E=1 bash tests/run.sh    also the browser suite (see the note below)
#
# ⚠️ THIS DOES NOT GATE THE DEPLOY. Vercel ships this site from its own git integration, which
# runs independently of Actions, so a red check here does not stop a deploy. Making it gate is
# a Vercel setting and a separate decision. Do not describe this as protecting the site.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

failed=()
run() {
  local name="$1"; shift
  printf '\n\033[1m── %s\033[0m\n' "$name"
  if "$@"; then
    printf '\033[32m✓ %s\033[0m\n' "$name"
  else
    printf '\033[31m✗ %s\033[0m\n' "$name"
    failed+=("$name")
  fi
}

# 🔒 NO DATABASE, NO API KEYS, NO NETWORK. Every group below runs against fixtures, which is
# what lets this suite run on a pull request from a fork with no secrets available. The moment
# a test needs a live database it belongs behind RUN_E2E, not here.
run "python lint"     "$PY" -m ruff check .
run "python types"    "$PY" -m mypy
run "python tests"    "$PY" -m pytest tests/ -q
run "eslint"          npx --no-install eslint .
run "typescript"      npx --no-install tsc --noEmit
run "build"           npm run build --silent
run "attribution"     bash checks/attribution.sh
run "dependencies"    bash checks/deps.sh

# ⚠️ THE BROWSER SUITE IS OPT-IN, AND SAYS SO RATHER THAN SKIPPING QUIETLY. It needs a live
# Postgres and both dev servers, so it cannot run on a fork's pull request. A suite that
# silently skips itself reads as a suite that passed.
if [ "${RUN_E2E:-0}" = "1" ]; then
  run "browser (e2e)" npx --no-install playwright test
else
  printf '\n\033[33m○ browser (e2e) not run\033[0m  needs a database and both dev servers: RUN_E2E=1 bash tests/run.sh\n'
fi

printf '\n'
if [ ${#failed[@]} -eq 0 ]; then
  printf '\033[32mall checks passed\033[0m\n'
  exit 0
fi
printf '\033[31m%d failed: %s\033[0m\n' "${#failed[@]}" "${failed[*]}"
exit 1
