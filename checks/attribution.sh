#!/usr/bin/env bash
# The attribution sweep. THIS PROJECT ONLY, which is why it lives here rather than in
# hygiene/bin/ with the universal checks.
#
#   bash checks/attribution.sh
#
# Exit 0 = clear.  Exit 1 = STOP, or could not look.
#
# WHAT IT IS FOR. This repo is public cloud with no private twin, so everything tracked
# publishes and nothing is path-filtered. The project's one hard content rule is that the
# repo must read as a personal project: no reference to the company it was written for, to
# the exercise that prompted it, or to requirements handed down by anyone.
#
# WHY IT IS A FILE AND NOT A HABIT. That rule was broken twice in one evening, and each
# time it was caught by a human noticing rather than by anything running:
#
#   1. Seven comments phrased values as "the mask the brief specifies" and similar. Each
#      one reconstructed a requirement and credited it to someone else.
#   2. A company PRODUCT NAME was lifted into 20 places, including a schema check
#      constraint, a public API parameter description, and every entity name of one kind.
#      It arrived via research notes and carried its origin with it.
#
# The second is the instructive one. The first form is easy to remember to avoid because
# it sounds like what it is. A product name looks like ordinary domain vocabulary at the
# point you type it, which is exactly why it needs a machine rather than an intention.
#
# 🔒 IT FAILS CLOSED. An empty file list is reported as "could not look" and exits 1,
# because a sweep that cannot tell "found nothing" from "looked at nothing" is not a check.
# It also prints the number of files it covered on every run, pass or fail, for the same
# reason.
set -uo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")
cd "$ROOT" || exit 1

# Two classes of pattern, separated because they fail differently.
#
# NAMES: proper nouns that point at one company, one product or one exercise. These can
# only get in by being copied from research, and there is never a reason for one to be
# here. Any hit is a stop.
NAMES='auranet|ice[ _-]?spike|dominion[ _-]?dynamics|nanook|nunalivut'
#
# ATTRIBUTION: phrasings that credit a requirement to someone else. The technical content
# these wrap is usually fine; it is the "someone told me to" that must go. Stated as a
# design decision instead, the same fact carries no author.
ATTRIB='\bthe brief\b|\bassessment\b|\btake[ -]?home\b|problem [0-9]|declared scenario'
ATTRIB="$ATTRIB"'|\bas required\b|\bper the spec\b|evaluation criteri|\bgrader\b'
ATTRIB="$ATTRIB"'|\bthe reviewer\b|\bthe client (asks|wants|requires)\b'

# 🔴 TRACKED **AND** UNTRACKED-BUT-NOT-IGNORED. This used to be tracked files only,
# reasoning that only tracked files publish. That is true and it was still the wrong
# scope, because it made the check blind at exactly the moment it is most needed.
#
# A brand new source file is untracked for the whole time it is being written, which is
# when its prose is fresh and most likely to have carried something across from research
# notes. The check would print "clear: checked 54 tracked file(s)" while a new file sat
# beside it unread, and "clear" is what you remember. It happened: a new module was
# written, this passed, and the file had never been looked at.
#
# `--others --exclude-standard` adds precisely the files that are on their way to being
# committed. Anything genuinely scratch belongs in /tmp or in .gitignore, and ignored
# files stay out of scope here because they cannot publish.
mapfile -d '' -t FILES < <(git ls-files -z --cached --others --exclude-standard 2>/dev/null)
COUNT=${#FILES[@]}

if [ "$COUNT" -eq 0 ]; then
  echo "attribution STOP: git ls-files returned nothing, so nothing was examined."
  echo "                  That is 'could not look', not 'clear'."
  exit 1
fi

RC=0

report() {
  local label="$1" pattern="$2" hits
  # -I skips binaries. Case-insensitive because a product name is still a product name in
  # lower case, and -E for the alternations above.
  hits=$(printf '%s\0' "${FILES[@]}" | xargs -0 grep -nIiE "$pattern" 2>/dev/null \
         | grep -viE 'candidate interval|checks/attribution\.sh' || true)
  if [ -n "$hits" ]; then
    echo "attribution STOP: $label"
    printf '%s\n' "$hits" | sed 's/^/    /' | head -30
    RC=1
  fi
}

report "a company, product or exercise name appears in a tracked file" "$NAMES"
report "a requirement is credited to someone else rather than stated as a decision" "$ATTRIB"

if [ "$RC" -eq 0 ]; then
  echo "attribution clear: checked $COUNT file(s), tracked and untracked-not-ignored, for names and for handed-down phrasing"
else
  echo ""
  echo "Fix by stating the fact as a decision with no author, e.g."
  echo "  'the mask the brief specifies'  ->  'this project fixes the mask at 15 degrees'"
  echo "Anything that genuinely must name the company belongs outside this repo."
fi

exit "$RC"
