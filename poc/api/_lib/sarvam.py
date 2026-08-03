"""Sarvam ASR adapter.

Deliberately shaped as an adapter, not a direct call: DECISIONS.md D1 puts every ASR
backend behind one interface so the Phase 1 bakeoff can swap Vosk / whisper.cpp / Android
SpeechRecognizer in without touching the parser. This is the cloud one, for the PoC only —
cloud ASR is ruled out of the product on cost (PLAN.md).

Endpoint, model and language are env-configurable because the Sarvam API surface moves;
verify against current docs rather than trusting these defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

SARVAM_URL = os.environ.get("SARVAM_STT_URL", "https://api.sarvam.ai/speech-to-text")
SARVAM_MODEL = os.environ.get("SARVAM_MODEL", "saarika:v2.5")
SARVAM_LANG = os.environ.get("SARVAM_LANGUAGE", "ta-IN")

# Sarvam does not return a per-utterance confidence score. That is a real limitation: it
# means the accept/confirm/reject gate leans almost entirely on catalog match quality.
# Vosk exposes per-token confidence, which is a point in its favour for Phase 1 that isn't
# about accuracy at all.
ASSUMED_ASR_CONFIDENCE = float(os.environ.get("ASSUMED_ASR_CONFIDENCE", "0.92"))


@dataclass
class Transcription:
    text: str
    confidence: float
    language: str = ""
    error: str = ""


class SarvamASR:
    name = "sarvam"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SARVAM_API_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def transcribe(self, audio: bytes, filename: str = "clip.webm") -> Transcription:
        if not self.configured:
            return Transcription("", 0.0, error="SARVAM_API_KEY not set")
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                r = await client.post(
                    SARVAM_URL,
                    headers={"api-subscription-key": self.api_key},
                    files={"file": (filename, audio, "audio/webm")},
                    data={"model": SARVAM_MODEL, "language_code": SARVAM_LANG},
                )
            if r.status_code >= 400:
                return Transcription("", 0.0, error=f"sarvam {r.status_code}: {r.text[:200]}")
            body = r.json()
            return Transcription(
                text=(body.get("transcript") or "").strip(),
                confidence=ASSUMED_ASR_CONFIDENCE,
                language=body.get("language_code", ""),
            )
        except httpx.TimeoutException:
            return Transcription("", 0.0, error="ASR timed out")
        except Exception as exc:                       # noqa: BLE001 — demo must never 500
            return Transcription("", 0.0, error=f"{type(exc).__name__}: {exc}")


class EchoASR:
    """Text-in stand-in used by /api/parse and by the offline demo path, so the whole
    pipeline can be exercised (and demoed) with no network and no API key."""

    name = "echo"
    configured = True

    async def transcribe(self, audio: bytes, filename: str = "") -> Transcription:
        return Transcription("", 0.0, error="echo backend takes text, not audio")


def get_asr():
    backend = SarvamASR()
    return backend if backend.configured else EchoASR()
