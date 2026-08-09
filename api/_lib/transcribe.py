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

import json
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
# 🔴 "AS A SINGLE SHORT COMMAND" USED TO BE IN THIS PROMPT AND IT WAS ACTIVELY HARMFUL.
# Verbatim and "as a command" are two instructions in tension, and the second one won:
# asked out loud, "what is a backhaul" came back as "backhaul", so the operator watched the
# console answer a question they had not asked and had no way to tell whether it had
# misheard them or reinterpreted them.
#
# 🔑 THE TRANSCRIPT IS EVIDENCE, NOT INPUT. It is the only thing on screen that says what
# the microphone actually received, which is what makes a misread visible rather than
# mysterious. A transcriber that tidies an utterance into a command destroys the one signal
# the display has for distinguishing "it did not hear me" from "it did not understand me",
# and those need completely different reactions from the person at the keyboard.
#
# Spelling is a different matter and is still corrected: the vocabulary hint below exists so
# that "Daymark" does not arrive as "day mark". Fixing how a word is written is not the same
# as deciding which words were said.
PROMPT = (
    "Transcribe this audio verbatim. Write down every word that was spoken, in the order "
    "they were spoken. Do NOT shorten it, do not turn it into a command, do not drop "
    "question words, and do not summarise: a question must come back as a question. "
    "Output only the words spoken, in lower case, with no trailing punctuation, no "
    "quotation marks and no commentary. Proper nouns keep their capitals. "
    "If the audio contains no intelligible speech, output exactly: NO_SPEECH"
)

# 🔴 TWO ANSWERS FROM ONE CALL, AND THE REASON IS A FAILURE THIS PATH HAD ALREADY MEASURED
# ONCE. The vocabulary hint below is what makes asset names transcribe correctly, and it is
# also what let "hide everything except for unknowns" come back as "hide everything except
# UNKNOWN 01, UNKNOWN 02, UNKNOWN 03". The operator then reads their own sentence rewritten
# into words they did not say, cannot tell whether the microphone failed or the system
# decided something, and has no way to correct it.
#
# 🔑 SO THE SNAPPING IS SEPARATED FROM THE HEARING RATHER THAN SOFTENED. `heard` is what
# the audio contained, with the hint list explicitly not applied. `command` is that same
# sentence with names matched to what is on the map, which is the version worth running.
# The display shows what was heard and says out loud when it assumed something.
#
# ⚠️ ONE CALL, NOT TWO. A second pass would double the cost and the latency of every spoken
# command to produce a string the first call already has. The model is asked for both
# fields at once, from the same audio.
JSON_INSTRUCTION = (
    "\n\nReturn a JSON object with exactly two string fields and nothing else:\n"
    '"heard": the verbatim transcription described above, using ONLY the words actually '
    "spoken. Never substitute a name from any list into this field, never expand a plural "
    "into individual names, and never add a word the speaker did not say.\n"
    '"command": the same sentence with any proper noun corrected to the spelling used on '
    "the map, and with spoken numbers written as digits. If nothing needed correcting, "
    'repeat "heard" exactly.\n'
    'If the audio contains no intelligible speech, set both fields to "NO_SPEECH".\n'
    "⚠️ The names listed below exist to spell real speech correctly. NEVER assemble a "
    "command out of them to fill silence, faint audio or noise: if you cannot make out "
    'actual words, the answer is "NO_SPEECH", not a plausible sentence.'
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
    # 🔑 CHANGING WHAT IS DRAWN, and these are the words a test cannot notice are missing.
    # The vocabulary check pins the static half to the code's enums, so a new fault, overlay
    # or kind gets caught automatically. A new VERB is in no enum at all: nothing knew the
    # display had learned "hide" until somebody said it and got the wrong command back, with
    # every asset name in the sentence heard perfectly.
    "hide", "unhide", "only", "except", "show only", "hide everything except",
    "show all assets", "show everything", "bring back",
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


class NoSpeech(TranscriptionError):
    """There was nothing to transcribe. A real outcome, not a failure.

    🔑 A SUBCLASS SO THE DISPLAY CAN TELL IT APART FROM A BROKEN MICROPHONE, which are two
    situations needing opposite reactions. A missing key, an unsupported format or a
    provider that will not answer are all worth telling somebody about. Silence is not:
    they pressed record, said nothing, and already know it, and the level meter beside the
    button is what answers "is it hearing me at all".

    ⚠️ IT IS STILL LOGGED. The operator is not told, and the audit row is still written,
    because "the microphone produced nothing" is worth being able to see afterwards even
    though it is not worth interrupting anybody over.
    """


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
    """The instruction plus whatever is on the map right now.

    ⚠️ THE OUTPUT INSTRUCTION IS APPENDED ON BOTH BRANCHES, and the first version of this
    put it on only one. The branch it was missing from is the one that runs in production,
    because a live world always has a vocabulary, so the model was never told to answer as
    JSON and the reply always fell back to a single field. The feature would have been dead
    everywhere except a database outage, and the fallback is silent by design, so nothing
    would have said so.
    """
    vocabulary = _live_vocabulary()
    if not vocabulary:
        return PROMPT + JSON_INSTRUCTION

    return (
        PROMPT
        + "\n\nThe speaker is looking at a map containing these names and is likely to "
        'say some of them. These names apply to the "command" field ONLY: when the audio '
        'is a close match to one, use its spelling exactly as written here in "command". '
        "Do NOT force a match: if the speaker clearly says something else, keep what they "
        'said. The "heard" field never uses this list at all.\n'
        + ", ".join(vocabulary)
        + "\n\nCoordinates are spoken as numbers and must be written as digits: "
        '"seventy three point two minus ninety five point nine" is "73.2 -95.9". '
        'Write "minus" or "negative" before a coordinate as a leading hyphen. '
        "Identifiers mixing a word and a number keep the number as digits: "
        '"barrow strait oh five" is "Barrow Strait 05".'
        + JSON_INSTRUCTION
    )


def _tidy(value: str) -> str:
    """Belt and braces against punctuation the parser would then miss."""
    return value.strip().strip('"').rstrip(".").strip()


def _two_fields(raw: str) -> tuple[str, str]:
    """`(heard, command)` out of the model's reply.

    🔒 FALLS BACK TO ONE STRING RATHER THAN FAILING. The reply is asked for as JSON, and a
    model can still return a bare sentence, wrap it in a code fence, or add a stray word.
    None of that is worth losing an operator's command over: an unparseable reply is
    treated as the transcription itself and used for both fields, which is exactly the
    behaviour this path had before there were two.

    ⚠️ THE FALLBACK IS INDISTINGUISHABLE FROM A CORRECTION-FREE UTTERANCE, on purpose. Both
    mean "nothing was assumed", which is what the display needs to know.
    """
    text = raw.strip()
    if text.startswith("```"):
        # A fenced block: drop the fence and any language tag on the opening line.
        text = text.strip("`")
        if "\n" in text:
            first, rest = text.split("\n", 1)
            if first.strip().lower() in ("json", ""):
                text = rest
    text = text.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            heard = _tidy(str(parsed.get("heard", "")))
            command = _tidy(str(parsed.get("command", ""))) or heard
            return heard or command, command
    flat = _tidy(text)
    return flat, flat


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
    heard, text = _two_fields(response.text or "")

    # The model was told to say this rather than invent words for silence. Treated as a
    # real outcome, not an error: the operator pressed record and said nothing.
    if not text or text == "NO_SPEECH":
        raise NoSpeech("no speech in that recording")

    usage = getattr(response, "usage_metadata", None)
    return {
        "text": text,
        # What the audio actually contained, before any name was matched to the map. The
        # display shows this, so the operator always reads their own sentence.
        "heard": heard,
        "model": MODEL,
        "latency_ms": elapsed_ms,
        "audio_bytes": len(audio),
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
    }
