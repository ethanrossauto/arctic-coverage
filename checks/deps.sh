#!/usr/bin/env bash
# Do requirements.txt and pyproject.toml agree about the runtime dependencies?
#
#   bash checks/deps.sh
#
# Exit 0 = in step.  Exit 1 = STOP, or could not look.
#
# WHY THIS EXISTS. On 2026-08-07 a production deploy returned 500 on EVERY route because
# psycopg was listed in pyproject.toml and not in requirements.txt. Vercel installs from
# requirements.txt; the build reported success, resolved exactly the requirements.txt set,
# and the app then failed at import. Nothing caught it until a curl of the live site.
#
# The two files have different jobs and both have to be right:
#   requirements.txt   what the deployed function installs
#   pyproject.toml     the interpreter version, the Vercel entrypoint, local tooling
#
# 🔑 A COMMENT ASKING A HUMAN TO KEEP TWO LISTS IN STEP IS NOT A MECHANISM. Both files now
# carry that comment, and this script is what makes it true. Same disease as two allowlists.
#
# ⚠️ python-dotenv is EXPECTED to be in pyproject and absent from requirements: only
# scripts/ imports it, and the runtime bundle should not carry what it never calls. It is
# allowlisted below by name, so a second such divergence has to be declared rather than
# assumed.
set -uo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")
cd "$ROOT" || exit 1

REQ="requirements.txt"
PROJ="pyproject.toml"

for f in "$REQ" "$PROJ"; do
  [ -f "$f" ] || { echo "deps STOP: $f is missing, so nothing was compared."; exit 1; }
done

# Normalise to "name==version", lower-cased, extras stripped, comments and blanks dropped.
# ⚠️ The trailing comma matters. pyproject lists entries as `"name==1.2",` and stripping
# quotes and spaces still leaves the comma, so without this the two lists never match and
# every package reports as missing from both sides. That was the first version of this
# script, and it failed loudly rather than quietly, which is the only reason it was caught.
norm() { sed 's/#.*//' | tr -d ' "' | tr -d "'" | sed 's/\[[^]]*\]//' | sed 's/,*$//' \
         | grep -E '^[A-Za-z0-9._-]+==' | tr 'A-Z' 'a-z' | sort -u; }

REQ_LIST=$(norm < "$REQ")
# Only the [project] dependencies array, not the optional dev extras.
PROJ_LIST=$(awk '/^dependencies *= *\[/{f=1;next} f&&/^\]/{f=0} f' "$PROJ" | norm)

if [ -z "$REQ_LIST" ] || [ -z "$PROJ_LIST" ]; then
  echo "deps STOP: one of the two lists parsed as empty, so nothing was compared."
  echo "           That is 'could not look', not 'in step'."
  exit 1
fi

# Declared divergences. A package here may be in pyproject and absent from requirements.
RUNTIME_EXEMPT='^python-dotenv=='

MISSING_FROM_REQ=$(comm -23 <(printf '%s\n' "$PROJ_LIST") <(printf '%s\n' "$REQ_LIST") \
                   | grep -vE "$RUNTIME_EXEMPT" || true)
MISSING_FROM_PROJ=$(comm -13 <(printf '%s\n' "$PROJ_LIST") <(printf '%s\n' "$REQ_LIST") || true)

RC=0
if [ -n "$MISSING_FROM_REQ" ]; then
  echo "deps STOP: in pyproject.toml but NOT in requirements.txt, so the deploy will not install it:"
  printf '%s\n' "$MISSING_FROM_REQ" | sed 's/^/    /'
  echo "    (this is the exact shape of the 2026-08-07 outage)"
  RC=1
fi
if [ -n "$MISSING_FROM_PROJ" ]; then
  echo "deps STOP: in requirements.txt but NOT in pyproject.toml, so local tooling and the deploy disagree:"
  printf '%s\n' "$MISSING_FROM_PROJ" | sed 's/^/    /'
  RC=1
fi

if [ "$RC" -eq 0 ]; then
  N=$(printf '%s\n' "$REQ_LIST" | wc -l | tr -d ' ')
  echo "deps clear: checked $N pinned runtime dependency/ies, requirements.txt and pyproject.toml agree"
fi

exit "$RC"
