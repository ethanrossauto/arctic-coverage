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
MODEL = "gemini-3.6-flash"

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
                PROMPT,
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
