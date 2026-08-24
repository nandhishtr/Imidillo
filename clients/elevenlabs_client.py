"""
ElevenLabs voice client — TTS (text → MP3) and STT (audio → text).

The API key lives server-side ONLY (config/.env): the browser widget never
talks to ElevenLabs directly. api.py proxies /voice/* through this client so
the key cannot be scraped from page source (the original prototype had the
key inlined in JavaScript — anyone could lift it from view-source).

TTS uses the low-latency flash model with `previous_text` forwarded for
prosody continuity, because api.py synthesizes answers SENTENCE BY SENTENCE
as they stream from the LLM — each chunk is a separate synthesis call, and
previous_text keeps the intonation from resetting at every sentence start.

STT is ElevenLabs Scribe — multipart upload, auto language detection. Used
only as the fallback path for browsers without the Web Speech API (the
widget prefers on-device/browser STT: free and lower latency).

Failures never raise into route handlers as transport exceptions — both
methods return/raise the explicit VoiceError with a human-readable message
so api.py can map it to a clean HTTP status.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

import httpx

_BASE_URL = "https://api.elevenlabs.io"

# TTS synthesis of a long sentence can take a few seconds; connect stays tight.
_SHARED_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_SHARED_LIMITS = httpx.Limits(max_keepalive_connections=10, max_connections=20)

# similarity_boost stays fixed; stability is per-instance (config
# ELEVENLABS_TTS_STABILITY). On v3: 0.0 Creative / 0.5 Natural / 1.0 Robust —
# raise it when the voice's mood jumps between chunk generations.
_SIMILARITY_BOOST = 0.75

_ASYNC_CLIENT: Optional[httpx.AsyncClient] = None
_ASYNC_CLIENT_LOCK = threading.Lock()


def _client() -> httpx.AsyncClient:
    """Module-level shared AsyncClient (one keepalive pool per process)."""
    global _ASYNC_CLIENT
    with _ASYNC_CLIENT_LOCK:
        if _ASYNC_CLIENT is None or _ASYNC_CLIENT.is_closed:
            _ASYNC_CLIENT = httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=_SHARED_TIMEOUT,
                limits=_SHARED_LIMITS,
            )
        return _ASYNC_CLIENT


class VoiceError(RuntimeError):
    """Upstream ElevenLabs failure with a message safe to log/surface."""


class ElevenLabsClient:
    """Async ElevenLabs client. All methods are safe to call concurrently."""

    def __init__(self, api_key: str, voice_id: str, tts_model: str, stt_model: str,
                 stability: float = 0.5):
        self.api_key = (api_key or "").strip()
        self.voice_id = voice_id
        self.tts_model = tts_model
        self.stt_model = stt_model
        self.stability = stability

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # Text-to-speech
    # ------------------------------------------------------------------
    async def atts(
        self,
        text: str,
        *,
        language_code: Optional[str] = None,
        previous_text: Optional[str] = None,
    ) -> bytes:
        """Synthesize `text` and return the complete MP3 (44.1 kHz, 128 kbps).

        Returned as full bytes rather than a stream: the widget plays clips
        via Audio+Blob (which needs the whole body anyway), and full bytes let
        api.py disk-cache short clips (the instant acknowledgment phrases).

        Raises VoiceError on any upstream failure.
        """
        if not self.available:
            raise VoiceError("ElevenLabs API key not configured")
        body: dict = {
            "text": text,
            "model_id": self.tts_model,
            "voice_settings": {"stability": self.stability,
                               "similarity_boost": _SIMILARITY_BOOST},
        }
        # eleven_v3 hard-rejects previous_text/next_text (HTTP 400
        # "unsupported_model", verified 2026-07-12) — sending it would fail
        # every sentence chunk after the first. v3's own expressiveness (and
        # the audio tags) carry the tone instead of prosody stitching.
        if previous_text and not self.tts_model.startswith("eleven_v3"):
            body["previous_text"] = previous_text
        # language_code pins the output language (supported on the v2.5
        # flash/turbo models) — keeps German street names from drifting into
        # English pronunciation mid-answer and vice versa.
        if language_code:
            body["language_code"] = language_code
        try:
            resp = await _client().post(
                f"/v1/text-to-speech/{self.voice_id}",
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": self.api_key},
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise VoiceError(f"TTS timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise VoiceError(f"TTS transport error: {exc}") from exc
        if resp.status_code != 200:
            raise VoiceError(f"TTS HTTP {resp.status_code}: {resp.text[:300]}")
        audio = resp.content
        if not audio:
            raise VoiceError("TTS returned empty audio")
        return audio

    # ------------------------------------------------------------------
    # Speech-to-text (Scribe)
    # ------------------------------------------------------------------
    async def astt(
        self,
        audio: bytes,
        *,
        filename: str = "audio.webm",
        content_type: str = "audio/webm",
        language_code: Optional[str] = None,
    ) -> dict:
        """Transcribe an audio blob. Returns {"text", "language_code"}.

        Scribe auto-detects the spoken language when `language_code` is None —
        handy for a campus where questions arrive in German and English alike.
        Raises VoiceError on any upstream failure.
        """
        if not self.available:
            raise VoiceError("ElevenLabs API key not configured")
        data = {"model_id": self.stt_model, "tag_audio_events": "false"}
        if language_code:
            data["language_code"] = language_code
        try:
            resp = await _client().post(
                "/v1/speech-to-text",
                headers={"xi-api-key": self.api_key},
                data=data,
                files={"file": (filename, audio, content_type)},
            )
        except httpx.TimeoutException as exc:
            raise VoiceError(f"STT timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise VoiceError(f"STT transport error: {exc}") from exc
        if resp.status_code != 200:
            raise VoiceError(f"STT HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise VoiceError(f"STT returned non-JSON: {exc}") from exc
        text = (payload.get("text") or "").strip()
        return {"text": text, "language_code": payload.get("language_code")}


if __name__ == "__main__":
    from config import (
        ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID,
        ELEVENLABS_TTS_MODEL, ELEVENLABS_STT_MODEL,
    )

    async def _selftest() -> None:
        c = ElevenLabsClient(ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID,
                             ELEVENLABS_TTS_MODEL, ELEVENLABS_STT_MODEL)
        if not c.available:
            print("ELEVENLABS_API_KEY not set — skipping live test")
            return
        audio = await c.atts("Hmm, okay. Let me check the city.", language_code="en")
        out = "elevenlabs_selftest.mp3"
        with open(out, "wb") as f:
            f.write(audio)
        print(f"TTS OK — wrote {len(audio)} bytes to {out}")

    asyncio.run(_selftest())
