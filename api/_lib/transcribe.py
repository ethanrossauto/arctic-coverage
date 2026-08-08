"""Speech to text, so a spoken command can join the one command path.

🔑 VOICE IS AN INPUT METHOD, NOT A SECOND COMMAND PATH. This module turns audio into a
sentence and stops. The sentence then goes to `/api/command` exactly as a typed one does,
through the same parser, the same validator, the same audit log. Nothing downstream knows
which way it arrived except the `source` field, which is recorded rather than inferred.

⚠️ THE AUDIO LEAVES THE BROWSER, and that is the one place this application makes a
runtime network call. Everything else is vendored specifically so that no third party can
break a demo. This cannot be: transcription needs a model, and shipping one would mean
every visitor downloading tens of megabytes before the page was usable. The README says
so plainly, because a UI that feels local while uploading a microphone recording would be
the kind of quiet claim this project keeps deleting.

🔒 IT FAILS SOFT. No key, no package, or a transport error all raise TranscriptionError,
which the route turns into an honest "voice is unavailable" rather than a 500. Typing has
to keep working when the microphone path does not.
"""
from __future__ import annotations

import os
import time
from typing import Any

# Whisper-class models will happily "transcribe" silence into a plausible sentence, and a
# hallucinated command is worse than no command, so anything shorter than this is refused
# before it costs anything.
MIN_AUDIO_BYTES = 1200

# Generous enough for a spoken sentence, small enough that nobody uploads a podcast.
MAX_AUDIO_BYTES = 8 * 1024 * 1024

# ⚠️ PINNED TO A CONCRETE VERSION, NOT `gemini-flash-latest`. The alias works and is
# tempting, but it moves under you: a model swap upstream would change transcription
# behaviour with no commit here to explain it. Pinned, this file is the record of what
# actually transcribed the audio.
#
# It is also not 2.5-flash, which the model list still advertises but which now returns
# 404 "no longer available to new users" on a real call. Listing a model is not the same
# as being able to call it, so this was chosen by calling it.
MODEL = "gemini-3.5-flash-lite"

# 🔴 THE LITE TIER, CHOSEN AFTER TIMING BOTH ON THE SAME CLIP. The larger flash model took
# 21.1 s and then 9.8 s on an identical three-second, 96 KB upload; the lite model took
# 2.3 s. That gap is the whole of the wait a person notices after letting go of the
# microphone button, and it dwarfs anything downstream: the command that follows a
# transcription is answered in three to four seconds.
#
# The task justifies the smaller model rather than merely tolerating it. This is a few
# seconds of clean, close-mic audio containing one short imperative sentence, which is the
# easiest thing an ASR model is ever asked to do. Capability that goes unused still costs
# latency.
#
# ⚠️ WHAT WAS MEASURED IS THE ROUND TRIP, NOT THE ACCURACY. The test clip is a tone, so it
# exercises the upload, the model invocation and the no-speech guard, and it says nothing
# about how either model transcribes real words. There is no text-to-speech on the build
# machine, so a spoken clip could not be synthesised to compare them properly. Speaking one
# command into the microphone settles it, and the audit log already records the
# transcription leg's latency separately from the command's.

# The transcript is a COMMAND, not prose, so the prompt says so. Without this the model
# punctuates and capitalises into something the deterministic parser then misses, and a
# spoken "mesh status" comes back as "Mesh status." and falls through to tier 2 for no
# reason. Cheap prompt, real saving.
PROMPT = (
    "Transcribe this audio verbatim as a single short command for a mapping console. "
    "Output only the words spoken, in lower case, with no trailing punctuation, no "
    "quotation marks and no commentary. Proper nouns keep their capitals. "
    "If the audio contains no intelligible speech, output exactly: NO_SPEECH"
)

# 🔑 THE OPERATOR IS READING THEIR OWN SCREEN, SO THE SCREEN IS THE VOCABULARY. Almost
# every word that matters here is one a general speech model has weak priors for: place
# names it has barely seen ("Nares", "Kugaaruk"), an initialism said as letters ("UAS"
# becomes "you ass" or "u a s"), a coined callsign ("Daymark" becomes "day mark" or
# "danemark"), and identifiers that are half word and half number ("Barrow Strait 05").
#
# Handing the model the actual list turns a guess into a match. It is the cheapest
# accuracy win available on this path: no extra call, no extra latency worth measuring,
# and it improves exactly the utterances that matter most, because a misheard asset name
# is a command that resolves to nothing or, worse, to the wrong asset.
#
# ⚠️ IT IS A HINT, NOT A CONSTRAINT. The instruction says prefer these spellings when the
# audio is close, never force them. An operator who says a word that is not on the map
# must still be transcribed faithfully, or "place a marker at Fury and Hecla" becomes
# impossible to say the first time.

# What people say that is not a name: the verbs the parser listens for, the states the
# display shows, and the domain nouns. A misheard verb loses the whole command even when
# every name in it was perfect.
COMMAND_VOCABULARY: tuple[str, ...] = (
    # what to do
    "isolate", "frame", "focus", "zoom out", "reset the view", "place", "remove", "delete",
    "task", "send", "history", "track", "describe", "show", "list", "filter",
    # breaking and fixing, which are near-homophones of ordinary words
    "kill", "silence", "take down", "fault", "maintenance", "unserviceable", "out of service",
    "fix", "repair", "restore", "clear the fault",
    # the states the map draws
    "overdue", "nominal", "silent", "isolated", "unreachable", "not broadcasting", "dark",
    "tracked", "untracked", "undetected", "coverage", "blind spot",
    # domain nouns that sound like other words
    "mesh", "backhaul", "gateway", "uplink", "AIS", "sea ice", "concentration",
    "hydrophone", "sonobuoy", "beacon", "heartbeat", "endurance", "on station",
)

# The map is small, so every name fits. A world of thousands would need the visible ones
# only, and this is the line where that decision would go.
MAX_VOCABULARY_TERMS = 400

ALLOWED_MIME = {
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/flac",
}


class TranscriptionError(RuntimeError):
    """Voice is unavailable, or the audio was unusable. Never a crash."""


def normalise_mime(content_type: str | None) -> str:
    """Strip codec parameters and check the type is one the model accepts.

    Browsers send `audio/webm;codecs=opus`, which the API rejects as an unknown type, so
    the parameters are dropped here rather than being discovered in a 400 from upstream.
    """
    base = (content_type or "").split(";")[0].strip().lower()
    if base not in ALLOWED_MIME:
        raise TranscriptionError(f"unsupported audio type: {base or 'none given'}")
    return base


def _live_vocabulary() -> list[str]:
    """Every proper noun currently on the map, plus the words that act on them.

    🔒 GUARDED AND OPTIONAL. If the world cannot be read, transcription still happens with
    the static half. Losing the hint costs accuracy on names; failing the call costs the
    operator their voice, and those are not comparable.

    ⚠️ READ FRESH, NOT CACHED, and that is a deliberate trade. An operator who places a
    marker and immediately says "remove Marker 01" would be defeated by a stale list, and
    the read costs a fraction of a call that already takes two seconds.
    """
    terms: list[str] = list(COMMAND_VOCABULARY)

    try:
        from . import db, parser  # noqa: PLC0415 - deferred; voice must survive their absence

        # The spoken forms of every kind: "drone", "uas", "ranger", "launch site".
        terms.extend(parser.KIND_WORDS.keys())

        for row in db.fetch_entities():
            name = (row.get("name") or "").strip()
            if name:
                terms.append(name)
            # ⚠️ THE ID IS SAID OUT LOUD TOO, and it is the harder half: "node-barrow-05"
            # is spoken "node barrow oh five", which no general model spells back with
            # hyphens. Offering both forms lets a match land on either.
            ident = (row.get("id") or "").strip()
            if ident:
                terms.append(ident.replace("-", " "))
    except Exception:  # noqa: BLE001 - the static vocabulary still helps
        pass

    # Deduplicate case-insensitively while keeping the first spelling seen, so the model
    # is shown "Barrow Strait 05" rather than a lower-cased version of it.
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique[:MAX_VOCABULARY_TERMS]


def build_prompt() -> str:
    """The instruction plus whatever is on the map right now."""
    vocabulary = _live_vocabulary()
    if not vocabulary:
        return PROMPT

    return (
        PROMPT
        + "\n\nThe speaker is looking at a map containing these names and is likely to "
        "say some of them. When the audio is a close match to one, prefer its spelling "
        "exactly as written here. Do NOT force a match: if the speaker clearly says "
        "something else, transcribe what they said.\n"
        + ", ".join(vocabulary)
        + "\n\nCoordinates are spoken as numbers and must be written as digits: "
        '"seventy three point two minus ninety five point nine" is "73.2 -95.9". '
        'Write "minus" or "negative" before a coordinate as a leading hyphen. '
        "Identifiers mixing a word and a number keep the number as digits: "
        '"barrow strait oh five" is "Barrow Strait 05".'
    )


def transcribe(audio: bytes, content_type: str | None) -> dict[str, Any]:
    """Audio in, one line of text out, with usage for the audit log.

    Returns a dict rather than a bare string so the caller can log what the call cost,
    the same way tier 2 does. A voice path that spends money invisibly would be the one
    hole in the spend story.
    """
    mime = normalise_mime(content_type)

    if len(audio) < MIN_AUDIO_BYTES:
        raise TranscriptionError("that recording was too short to contain a command")
    if len(audio) > MAX_AUDIO_BYTES:
        raise TranscriptionError("that recording is too long")

    if not os.environ.get("GEMINI_API_KEY"):
        raise TranscriptionError("GEMINI_API_KEY is not set")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - depends on the deploy image
        raise TranscriptionError("the google-genai package is not installed") from exc

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    started = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[
                build_prompt(),
                types.Part.from_bytes(data=audio, mime_type=mime),
            ],
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is "voice unavailable"
        raise TranscriptionError(f"{type(exc).__name__}: {exc}") from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    text = (response.text or "").strip()

    # The model was told to say this rather than invent words for silence. Treated as a
    # real outcome, not an error: the operator pressed record and said nothing.
    if not text or text == "NO_SPEECH":
        raise TranscriptionError("no speech in that recording")

    # Belt and braces against the model adding punctuation the parser would then miss.
    text = text.strip().strip('"').rstrip(".").strip()

    usage = getattr(response, "usage_metadata", None)
    return {
        "text": text,
        "model": MODEL,
        "latency_ms": elapsed_ms,
        "audio_bytes": len(audio),
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
    }
