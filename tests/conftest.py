"""Shared test fixtures. Nothing here talks to the internet:

- WhatsApp HTTP calls are intercepted with respx (so real payloads are checked),
- Gemini (answers, speech-to-text, text-to-speech) is one fake client injected into
  the real Brain and Speech classes,
- Redis is fakeredis,
- ffmpeg is real (system ffmpeg, or the imageio-ffmpeg binary if installed).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from itertools import count
from types import SimpleNamespace
from typing import Any

import fakeredis
import httpx
import pytest
import respx

# Keep a developer's real .env out of the tests.
os.environ.setdefault("APP_ENV", "test")

from app import retry, speech  # noqa: E402
from app.brain import Brain  # noqa: E402
from app.config import Settings  # noqa: E402
from app.handlers import Bot  # noqa: E402
from app.main import create_app  # noqa: E402
from app.speech import Speech, Transcription  # noqa: E402
from app.store import Store  # noqa: E402
from app.whatsapp import WhatsAppClient  # noqa: E402

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ADMIN_KEY = "test-admin-key"
PHONE_ID = "PNID123"
GRAPH = "https://graph.facebook.com/v23.0"
FARMER = "919800000001"

_ids = count(1)


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(retry, "BASE_DELAY", 0)


# ── Fakes ───────────────────────────────────────────────────────────────────


class FakeGemini:
    """Looks like GeminiClient: `client.aio.models.generate_content(...)`.

    Routes each call like the real API would be used:
      - config.response_modalities == ["AUDIO"]        -> text-to-speech (raw PCM)
      - config.response_mime_type == "application/json" -> voice transcription (JSON)
      - anything else                                   -> a normal answer
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []  # answer calls only
        self.stt_calls: list[dict[str, Any]] = []
        self.tts_calls: list[dict[str, Any]] = []
        self.reply = "જવાબ: લીમડાના તેલનો છંટકાવ કરો."
        self.errors: list[Exception] = []  # raised in order by answer calls
        self.transcript = "कपास में सफेद मक्खी आ गई है"
        self.stt_language = "hi-IN"
        self.tts_error: Exception | None = None
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, **kwargs: Any) -> Any:
        config = kwargs.get("config")
        if config is not None and config.response_modalities == ["AUDIO"]:
            self.tts_calls.append(kwargs)
            if self.tts_error:
                raise self.tts_error
            blob = SimpleNamespace(data=make_pcm(1.2), mime_type="audio/L16;codec=pcm;rate=24000")
            part = SimpleNamespace(inline_data=blob, text=None)
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
        if config is not None and config.response_mime_type == "application/json":
            self.stt_calls.append(kwargs)
            result = Transcription(language=self.stt_language, transcript=self.transcript)
            return SimpleNamespace(parsed=result, text=result.model_dump_json())
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(text=self.reply)


def make_pcm(seconds: float = 1.0, rate: int = 24000, freq: float = 330.0) -> bytes:
    """Raw 16-bit mono PCM sine wave (what Gemini TTS returns)."""
    frames = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(frames)
    )


def ffmpeg_binary() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # optional dev helper when ffmpeg isn't installed

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@pytest.fixture
def ffmpeg(monkeypatch) -> str:
    binary = ffmpeg_binary()
    if not binary:
        pytest.skip("ffmpeg not installed (install ffmpeg, or `pip install imageio-ffmpeg`)")
    monkeypatch.setattr(speech, "FFMPEG", binary)
    return binary


@pytest.fixture
def tone_ogg(ffmpeg, tmp_path) -> bytes:
    """A real 40-second OGG/Opus mono test tone, like a WhatsApp voice note."""
    out = tmp_path / "tone.ogg"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=40", "-ac", "1", "-c:a", "libopus", str(out)],
        check=True, capture_output=True,
    )
    return out.read_bytes()


def ogg_opus_channels(data: bytes) -> int | None:
    """Channel count from the OpusHead packet of an OGG/Opus file (None if not Opus)."""
    idx = data.find(b"OpusHead")
    return data[idx + 9] if idx >= 0 and data.startswith(b"OggS") else None


# ── Payload builders ────────────────────────────────────────────────────────


def _wrap(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": PHONE_ID},
                    "contacts": [{"wa_id": msg["from"], "profile": {"name": "Farmer"}}],
                    "messages": [msg],
                },
            }],
        }],
    }


def _base(kind: str, phone: str, msg_id: str | None) -> dict[str, Any]:
    return {"from": phone, "id": msg_id or f"wamid.in{next(_ids)}", "timestamp": "1760000000", "type": kind}


def text_msg(body: str, phone: str = FARMER, msg_id: str | None = None) -> dict[str, Any]:
    return _wrap({**_base("text", phone, msg_id), "text": {"body": body}})


def button_reply(button_id: str, title: str = "", phone: str = FARMER) -> dict[str, Any]:
    return _wrap({
        **_base("interactive", phone, None),
        "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}},
    })


def audio_msg(media_id: str = "MEDIA_AUDIO", phone: str = FARMER) -> dict[str, Any]:
    return _wrap({**_base("audio", phone, None),
                  "audio": {"id": media_id, "mime_type": "audio/ogg; codecs=opus", "voice": True}})


def image_msg(media_id: str = "MEDIA_IMG", caption: str | None = None, phone: str = FARMER) -> dict[str, Any]:
    image: dict[str, Any] = {"id": media_id, "mime_type": "image/jpeg"}
    if caption is not None:
        image["caption"] = caption
    return _wrap({**_base("image", phone, None), "image": image})


def sticker_msg(phone: str = FARMER) -> dict[str, Any]:
    return _wrap({**_base("sticker", phone, None), "sticker": {"id": "STK", "mime_type": "image/webp"}})


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── Harness ─────────────────────────────────────────────────────────────────


@dataclass
class Harness:
    app: Any
    client: httpx.AsyncClient
    bot: Bot
    store: Store
    gemini: FakeGemini
    graph: respx.MockRouter
    settings: Settings
    media: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    uploads: list[bytes] = field(default_factory=list)

    async def run(self, payload: dict[str, Any]) -> httpx.Response:
        """POST a signed webhook and wait for the background work to finish."""
        body = json.dumps(payload).encode()
        resp = await self.client.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"},
        )
        await self.bot.drain()
        return resp

    def outgoing(self) -> list[dict[str, Any]]:
        """Every body POSTed to /messages, in order (includes read receipts)."""
        return [json.loads(c.request.content) for c in self.graph.calls
                if c.request.method == "POST" and c.request.url.path.endswith("/messages")]

    def replies(self) -> list[dict[str, Any]]:
        """Messages actually sent to the farmer (read/typing receipts excluded)."""
        return [m for m in self.outgoing() if m.get("status") != "read"]

    def reply_texts(self) -> list[str]:
        return [m["text"]["body"] for m in self.replies() if m["type"] == "text"]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        admin_api_key=ADMIN_KEY,
        wa_access_token="test-token",
        wa_phone_number_id=PHONE_ID,
        wa_app_secret=APP_SECRET,
        wa_verify_token=VERIFY_TOKEN,
        graph_api_version="v23.0",
        gemini_api_key="test",
        gemini_model="gemini-3.8-flash",
        gemini_thinking_level="low",
        gemini_stt_model="gemini-3.8-flash",
        gemini_tts_model="gemini-3.1-flash-tts-preview",
        gemini_tts_voice="Sulafat",
        default_language="gu-IN",
        daily_message_limit=50,
        max_voice_seconds=120,
        voice_reply_also_text=False,
    )


@pytest.fixture
async def redis():
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def h(settings: Settings, redis) -> Harness:
    gemini = FakeGemini()
    media: dict[str, tuple[bytes, str]] = {}
    uploads: list[bytes] = []

    with respx.mock(assert_all_called=False, assert_all_mocked=True) as graph:
        graph.post(f"{GRAPH}/{PHONE_ID}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})
        )

        def _upload(request: httpx.Request) -> httpx.Response:
            uploads.append(request.content)
            return httpx.Response(200, json={"id": f"UPLOADED{len(uploads)}"})

        graph.post(f"{GRAPH}/{PHONE_ID}/media").mock(side_effect=_upload)

        def _media_meta(request: httpx.Request, media_id: str) -> httpx.Response:
            if media_id not in media:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            data, mime = media[media_id]
            return httpx.Response(200, json={
                "url": f"https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid={media_id}",
                "mime_type": mime, "file_size": len(data), "id": media_id,
            })

        graph.get(url__regex=rf"^{GRAPH}/(?P<media_id>MEDIA_\w+)$").mock(side_effect=_media_meta)

        def _media_bytes(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer test-token"
            return httpx.Response(200, content=media[request.url.params["mid"]][0])

        graph.get(url__startswith="https://lookaside.fbsbx.com/").mock(side_effect=_media_bytes)

        async with httpx.AsyncClient() as http:
            store = Store(redis)
            wa = WhatsAppClient(http, settings.wa_access_token, PHONE_ID, settings.graph_api_version)
            brain = Brain(gemini, settings.gemini_model, settings.gemini_thinking_level)
            voice = Speech(gemini, settings.gemini_stt_model, settings.gemini_tts_model,
                           settings.gemini_tts_voice, settings.gemini_thinking_level)
            bot = Bot(settings, store, wa, brain, voice)
            app = create_app(settings=settings, store=store, bot=bot)
            transport = httpx.ASGITransport(app=app)
            # respx must let requests to the in-process app through.
            graph.route(host="app").pass_through()
            async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
                yield Harness(app, client, bot, store, gemini, graph, settings, media, uploads)
