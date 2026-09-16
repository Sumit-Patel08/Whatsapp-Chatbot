"""Voice with Google Gemini: ffmpeg + Gemini speech-to-text + Gemini text-to-speech.

Voice note in  : OGG/Opus -> ffmpeg -> 16 kHz mono WAV -> Gemini (audio understanding)
                 -> JSON {language, transcript}
Voice note out : text -> Gemini TTS (raw 16-bit PCM) -> WAV -> ffmpeg -> OGG/Opus mono -> WhatsApp

Everything uses the same GEMINI_API_KEY as the answers (see brain.GeminiClient).

ffmpeg runs in a worker thread via subprocess.run. That works with every event loop,
including uvicorn's on Windows, where asyncio subprocesses are not always available.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field

from app.brain import supports_thinking_level
from app.lang import LANGUAGE_NAMES
from app.retry import with_retry

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"  # tests may point this at another binary
FFMPEG_TIMEOUT = 120
MAX_PARALLEL_FFMPEG = 4  # a 2 vCPU box copes well with this many

STT_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000  # Gemini TTS returns raw 16-bit little-endian PCM, 24 kHz, mono
TTS_CHAR_LIMIT = 2000  # voice answers are <= 80 words; this only guards unusual long text

TRANSCRIBE_INSTRUCTION = """\
You transcribe WhatsApp voice notes sent by women farmers in Gujarat, India.
- Write down exactly what was said. Do not translate, summarise, correct, answer or add anything.
- Use the script of the spoken language: Gujarati script for Gujarati, Devanagari for Hindi,
  Latin letters for English. Keep English farming words (DAP, urea, spray, pump) as spoken.
- If there is no clear speech (silence, noise, music), return an empty transcript.
- "language" is the main language spoken: gu-IN, hi-IN, en-IN, or other.
"""

TTS_STYLE = (
    "Read the following aloud in {language}, in a warm, gentle and kind voice, "
    "at a calm, slightly slow pace, like a caring elder sister from the village:"
)


class SpeechError(RuntimeError):
    pass


class Transcription(BaseModel):
    """The JSON shape Gemini must return for a voice note."""

    language: Literal["gu-IN", "hi-IN", "en-IN", "other"] = Field(description="Main spoken language")
    transcript: str = Field(description="Exact words spoken, in the native script")


@dataclass
class PreparedVoice:
    duration: float
    too_long: bool
    wav: bytes = b""  # 16 kHz mono WAV, ready for Gemini


# ── Helpers ─────────────────────────────────────────────────────────────────


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def pcm_to_wav(pcm: bytes, rate: int = TTS_SAMPLE_RATE) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def sample_rate_from_mime(mime_type: str | None, default: int = TTS_SAMPLE_RATE) -> int:
    """'audio/L16;codec=pcm;rate=24000' -> 24000."""
    match = re.search(r"rate=(\d+)", mime_type or "")
    return int(match.group(1)) if match else default


def clean_for_speech(text: str) -> str:
    """Remove markdown, emojis, bullets and links so TTS reads only words."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`~>|•]+", " ", text)
    text = re.sub(r"^\s*(\d+[\.\)]|[-–])\s+", "", text, flags=re.MULTILINE)
    # Drop emoji/symbols and variation selectors; keep ZWJ/ZWNJ used by Indic scripts.
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch) not in {"So", "Sk", "Cs", "Co"} and ch != "\ufe0f"
    )
    return re.sub(r"\s+", " ", text).strip()


def split_for_tts(text: str, limit: int = TTS_CHAR_LIMIT) -> list[str]:
    """Split on sentence ends so each TTS request stays under the character limit."""
    sentences = re.split(r"(?<=[\.\?\!।])\s+", text)
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > limit:  # a single giant "sentence"
            parts.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


# ── Gemini speech ───────────────────────────────────────────────────────────


class Speech:
    def __init__(
        self,
        client: Any,
        stt_model: str,
        tts_model: str,
        voice: str,
        thinking_level: str = "low",
    ) -> None:
        # `client` is a brain.GeminiClient (shared with Brain), or a fake in tests.
        self._client = client
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.voice = voice
        self.thinking_level = thinking_level.strip().lower()
        # Created here (inside the running event loop), not at import time.
        self._ffmpeg_slots = asyncio.Semaphore(MAX_PARALLEL_FFMPEG)

    # ── Speech-to-text ─────────────────────────────────────────────────────

    async def transcribe(self, wav: bytes) -> tuple[str, str | None]:
        """Returns (transcript, language code or None if not gu/hi/en)."""
        config: dict[str, Any] = {
            "system_instruction": TRANSCRIBE_INSTRUCTION,
            "response_mime_type": "application/json",
            "response_schema": Transcription,
        }
        if self.thinking_level and supports_thinking_level(self.stt_model):
            config["thinking_config"] = types.ThinkingConfig(thinking_level=self.thinking_level)

        response = await with_retry(
            self._client.aio.models.generate_content,
            model=self.stt_model,
            contents=[
                types.Content(role="user", parts=[
                    types.Part.from_bytes(data=wav, mime_type="audio/wav"),
                    types.Part.from_text(text="Transcribe this voice note."),
                ])
            ],
            config=types.GenerateContentConfig(**config),
            what="gemini stt",
        )

        parsed = getattr(response, "parsed", None)
        if not isinstance(parsed, Transcription):
            try:
                parsed = Transcription.model_validate_json(response.text or "")
            except ValueError as exc:
                raise SpeechError(f"could not read transcription JSON: {(response.text or '')[:200]}") from exc
        language = parsed.language if parsed.language != "other" else None
        return parsed.transcript.strip(), language

    # ── Text-to-speech ─────────────────────────────────────────────────────

    async def synthesize_voice_note(self, text: str, lang: str) -> bytes:
        """Text -> OGG/Opus mono voice note bytes."""
        spoken = clean_for_speech(text)
        if not spoken:
            raise SpeechError("nothing to speak")
        wavs = [await self._tts_wav(part, lang) for part in split_for_tts(spoken)]
        return await self.wavs_to_voice_note(wavs)

    async def _tts_wav(self, text: str, lang: str) -> bytes:
        language = LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["gu-IN"])[0]
        response = await with_retry(
            self._client.aio.models.generate_content,
            model=self.tts_model,
            contents=f"{TTS_STYLE.format(language=language)}\n\n{text}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                    )
                ),
            ),
            what="gemini tts",
        )

        pcm = bytearray()
        rate = TTS_SAMPLE_RATE
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                blob = getattr(part, "inline_data", None)
                if blob is not None and blob.data:
                    if blob.data[:4] == b"RIFF":  # already a WAV file
                        return bytes(blob.data)
                    rate = sample_rate_from_mime(blob.mime_type)
                    pcm.extend(blob.data)
            if pcm:
                break
        if not pcm:
            raise SpeechError("Gemini TTS returned no audio")
        return pcm_to_wav(bytes(pcm), rate)

    # ── ffmpeg ─────────────────────────────────────────────────────────────

    async def run_ffmpeg(self, *args: str, cwd: Path) -> None:
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
        async with self._ffmpeg_slots:
            try:
                proc = await asyncio.to_thread(
                    subprocess.run, cmd, cwd=cwd, capture_output=True, timeout=FFMPEG_TIMEOUT
                )
            except FileNotFoundError as exc:
                raise SpeechError("ffmpeg is not installed or not on PATH") from exc
            except subprocess.TimeoutExpired as exc:
                raise SpeechError("ffmpeg timed out") from exc
        if proc.returncode != 0:
            raise SpeechError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-500:]}")

    async def prepare_voice(self, audio: bytes, max_seconds: int) -> PreparedVoice:
        """Decode any incoming voice note to 16 kHz mono WAV and check its length.

        Gemini accepts long audio in one request, so no chunking is needed. Converting
        to WAV first also covers forwarded audio in formats Gemini doesn't read (e.g. AMR).
        """
        with tempfile.TemporaryDirectory(prefix="ks-in-") as tmp:
            d = Path(tmp)
            (d / "input").write_bytes(audio)
            # Decode at most max_seconds + 1, so a very long note costs little work.
            await self.run_ffmpeg(
                "-i", "input", "-t", str(max_seconds + 1), "-vn", "-ac", "1",
                "-ar", str(STT_SAMPLE_RATE), "-c:a", "pcm_s16le", "voice.wav", cwd=d,
            )
            duration = wav_duration(d / "voice.wav")
            if duration > max_seconds:
                return PreparedVoice(duration=duration, too_long=True)
            return PreparedVoice(duration=duration, too_long=False, wav=(d / "voice.wav").read_bytes())

    async def wavs_to_voice_note(self, wavs: list[bytes]) -> bytes:
        """Join one or more WAV files into a single OGG/Opus mono voice note."""
        if not wavs:
            raise SpeechError("no audio to encode")
        with tempfile.TemporaryDirectory(prefix="ks-out-") as tmp:
            d = Path(tmp)
            inputs: list[str] = []
            filters: list[str] = []
            for i, data in enumerate(wavs):
                (d / f"part{i}.wav").write_bytes(data)
                inputs += ["-i", f"part{i}.wav"]
                filters.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=mono[a{i}]")
            joined = "".join(f"[a{i}]" for i in range(len(wavs)))
            graph = ";".join(filters) + f";{joined}concat=n={len(wavs)}:v=0:a=1[out]"
            await self.run_ffmpeg(
                *inputs, "-filter_complex", graph, "-map", "[out]",
                "-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
                "reply.ogg", cwd=d,
            )
            return (d / "reply.ogg").read_bytes()
