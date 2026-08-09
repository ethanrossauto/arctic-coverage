"""What may appear on the screen, once something else has decided what to say.

🔑 ONE PLACE, BECAUSE THREE LAYERS PRODUCE OPERATOR-FACING TEXT. The model writes an
answer, the validator writes a reason, and a tool writes a result message. Each of those
reaches the same line of the display, so the rules about what that line may contain belong
in one module rather than in three sets of habits.

⚠️ THIS IS HYGIENE, NOT MEANING. Nothing here decides whether a sentence is true, useful
or allowed. It removes characters that misrepresent the text on the way to a screen, and
it bounds a length. A module that started rewording answers would be a second author
nobody asked for.
"""
from __future__ import annotations

import re

# 🔴 CHARACTERS THAT LIE ABOUT THE TEXT THEY ARE IN. A right-to-left override reorders
# everything after it, so a crafted command can render in the audit panel as a different
# sentence from the one that was logged, which is the one thing a record must never do.
# The zero-width family is the quieter version of the same trick. Stripping them changes
# no word anyone typed: none of them has a visible glyph.
#
# ⚠️ CSS DOES NOT COVER THIS. The panel already wraps and scrolls, so long text is
# harmless; bidi is a different failure and word-wrap has nothing to say about it.
#
# ⚠️ WRITTEN AS ESCAPES ON PURPOSE. These characters are invisible, so a literal class
# here would be a line nobody could read, review or even see they had edited.
_INVISIBLE = re.compile(
    "["
    "​-‏"  # zero width space through the right-to-left mark
    "‪-‮"  # the embedding and override family
    "⁠-⁤"  # word joiner and the invisible operators
    "⁦-⁩"  # the isolates
    "﻿"  # a byte order mark that arrived as text
    "]"
)

# C0 controls apart from tab and newline, which are legitimate in a logged parameter.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_RANGE_DASH = re.compile(r"(?<=\d)\s*[—–]\s*(?=\d)")
_PROSE_DASH = re.compile(r"\s*[—–]\s*")
_DOUBLED_COMMA = re.compile(r",\s*,+")
_SPACE_BEFORE_COMMA = re.compile(r"\s+,")


def visible(text: str) -> str:
    """Text with the characters that have no glyph but change the rendering removed.

    🔑 THIS IS THE ONE SAFE TO RUN ON WHAT A PERSON TYPED. It deletes nothing anybody can
    see, so the audit row still holds their sentence exactly as they said it, which is a
    promise this console makes and has broken before. The dash rewriting below is a
    different act with a different justification, and applying it to an operator's own
    words would be this program editing them.
    """
    if not text:
        return ""
    return _CONTROLS.sub("", _INVISIBLE.sub("", text))


def plain(text: str) -> str:
    """Model or tool text, made safe to render and consistent with everything written here.

    🔑 THE LONG DASH IS REMOVED FOR A REASON THAT IS NOT TASTE. Every other sentence on
    this display and in its documentation was written without one, so an answer that
    arrives carrying two reads as though a different author wrote it, which is exactly what
    happened. A comma carries the same clause break and matches the surrounding prose.

    ⚠️ A RANGE IS NOT A CLAUSE BREAK. `2019-2024` between digits becomes a hyphen rather
    than a comma, because turning a span into a list changes what the sentence claims.
    """
    if not text:
        return ""
    out = visible(text)
    out = _RANGE_DASH.sub("-", out)
    out = _PROSE_DASH.sub(", ", out)
    out = _DOUBLED_COMMA.sub(",", out)
    out = _SPACE_BEFORE_COMMA.sub(",", out)
    return out.strip()


def clipped(text: str, limit: int) -> str:
    """The text, cut to `limit` characters at the last sentence that fits.

    ⚠️ A HARD CUT MID-WORD READS AS A CRASH. Ending on a full stop reads as an answer that
    finished. Where no sentence boundary fits, it falls back to a word boundary and marks
    the cut, because a silently truncated sentence is a claim the writer did not make.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    for end in (". ", "! ", "? "):
        cut = window.rfind(end)
        if cut > limit // 2:
            return window[: cut + 1].strip()
    cut = window.rfind(" ")
    return (window[:cut] if cut > limit // 2 else window).strip() + "..."
