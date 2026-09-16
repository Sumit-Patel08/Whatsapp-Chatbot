"""End-to-end flow tests. Everything external is mocked (see conftest.py)."""

from __future__ import annotations

import asyncio
import io
import json
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from app import lang as L
from app.config import Settings
from app.main import create_app
from app.retry import is_retryable
from app.speech import Transcription, clean_for_speech, pcm_to_wav, sample_rate_from_mime
from app.whatsapp import split_text, to_whatsapp_format, verify_signature
from tests.conftest import (
    ADMIN_KEY,
    APP_SECRET,
    FARMER,
    VERIFY_TOKEN,
    audio_msg,
    button_reply,
    image_msg,
    ogg_opus_channels,
    sign,
    text_msg,
)

GU_QUESTION = "કપાસમાં સફેદ માખી આવી છે, શું કરું?"
HI_QUESTION = "गेहूँ में पीले पत्ते क्यों हो रहे हैं?"


# ── Phase 1: webhook + text ────────────────────────────────────────────────


async def test_webhook_verification_ok(h):
    resp = await h.client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "12345",
    })
    assert resp.status_code == 200
    assert resp.text == "12345"


async def test_webhook_verification_wrong_token(h):
    resp = await h.client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345",
    })
    assert resp.status_code == 403


async def test_bad_signature_rejected(h):
    body = json.dumps(text_msg(GU_QUESTION)).encode()
    resp = await h.client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, "wrong-secret")})
    await h.bot.drain()
    assert resp.status_code == 403
    assert h.outgoing() == []
    assert h.gemini.calls == []

    resp = await h.client.post("/webhook", content=body)  # no header at all
    assert resp.status_code == 403


async def test_health(h):
    resp = await h.client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_text_flow_gujarati(h):
    resp = await h.run(text_msg(GU_QUESTION, msg_id="wamid.gu1"))
    assert resp.status_code == 200

    # read receipt + typing indicator first
    receipt = h.outgoing()[0]
    assert receipt == {
        "messaging_product": "whatsapp", "status": "read",
        "message_id": "wamid.gu1", "typing_indicator": {"type": "text"},
    }

    # Gemini got the question, the system prompt and the Gujarati language rule
    call = h.gemini.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    cfg = call["config"]
    assert "Krishi Sakhi" in cfg.system_instruction
    assert "reply in Gujarati" in cfg.system_instruction
    assert "1800-180-1551" in cfg.system_instruction and "108" in cfg.system_instruction
    assert cfg.thinking_config.thinking_level.value.lower() == "low"
    assert cfg.temperature is None and cfg.top_p is None and cfg.top_k is None
    assert call["contents"][-1].role == "user"
    assert call["contents"][-1].parts[-1].text == GU_QUESTION

    # one text reply
    replies = h.replies()
    assert len(replies) == 1
    assert replies[0]["to"] == FARMER and replies[0]["type"] == "text"
    assert replies[0]["text"]["body"] == h.gemini.reply

    # language saved from script, exchange logged
    assert await h.store.get_language(FARMER) == "gu-IN"
    logs = await h.store.recent_logs(FARMER)
    assert logs[0]["type"] == "text" and logs[0]["language"] == "gu-IN"
    assert logs[0]["question"] == GU_QUESTION and logs[0]["answer"] == h.gemini.reply
    assert int(logs[0]["latency_ms"]) >= 0 and logs[0]["timestamp"]


async def test_text_flow_hindi_script_overrides_saved(h):
    await h.store.set_language(FARMER, "gu-IN")
    await h.run(text_msg(HI_QUESTION))
    assert "reply in Hindi" in h.gemini.calls[0]["config"].system_instruction


async def test_gemini_retries_then_error_message(h):
    server_error = genai_errors.ServerError(503, {"error": {"message": "overloaded"}})
    h.gemini.errors = [server_error, server_error, server_error]  # 1 try + 2 retries all fail
    await h.run(text_msg(GU_QUESTION))
    assert len(h.gemini.calls) == 3
    assert h.reply_texts() == [L.message("error", "gu-IN")]
    assert (await h.store.recent_logs(FARMER))[0]["status"] == "error"


async def test_gemini_retry_recovers(h):
    h.gemini.errors = [genai_errors.ClientError(429, {"error": {"message": "slow down"}})]
    await h.run(text_msg(GU_QUESTION))
    assert len(h.gemini.calls) == 2
    assert h.reply_texts() == [h.gemini.reply]


# ── Phase 2: language, limits, dedup, memory ───────────────────────────────


def _button_titles(message: dict) -> list[str]:
    return [b["reply"]["title"] for b in message["interactive"]["action"]["buttons"]]


async def test_first_time_latin_text_gets_language_buttons(h):
    await h.run(text_msg("hello"))

    replies = h.replies()
    assert len(replies) == 1
    assert replies[0]["type"] == "interactive"
    assert replies[0]["interactive"]["type"] == "button"
    assert _button_titles(replies[0]) == ["ગુજરાતી", "हिंदी", "English"]
    assert all(len(t) <= 20 for t in _button_titles(replies[0]))
    assert h.gemini.calls == []
    assert await h.store.get_language(FARMER) is None

    # Tap "हिंदी": language saved + Hindi welcome
    await h.run(button_reply("lang:hi-IN", "हिंदी"))
    assert await h.store.get_language(FARMER) == "hi-IN"
    assert h.reply_texts()[-1] == L.message("welcome", "hi-IN")

    # Now Latin text (Hinglish) is answered in the saved language
    await h.run(text_msg("gehu me keeda lag gaya hai"))
    assert "reply in Hindi" in h.gemini.calls[-1]["config"].system_instruction
    # menu interactions did not use up the daily quota
    assert await h.store.count_message(FARMER) == 2


@pytest.mark.parametrize("command", ["ભાષા", "भाषा", "bhasha", "Language", " language. "])
async def test_language_command_shows_buttons_again(h, command):
    await h.store.set_language(FARMER, "gu-IN")
    await h.run(text_msg(command))
    replies = h.replies()
    assert len(replies) == 1 and replies[0]["type"] == "interactive"
    assert h.gemini.calls == []


async def test_duplicate_message_processed_once(h):
    payload = text_msg(GU_QUESTION, msg_id="wamid.same")
    first = await h.run(payload)
    second = await h.run(payload)
    assert first.status_code == 200 and second.status_code == 200
    assert len(h.gemini.calls) == 1
    assert len(h.replies()) == 1


async def test_daily_limit(h):
    h.settings.daily_message_limit = 2
    for i in range(5):
        await h.run(text_msg(f"{GU_QUESTION} {i}"))

    assert len(h.gemini.calls) == 2
    texts = h.reply_texts()
    assert texts.count(L.message("limit_reached", "gu-IN")) == 1  # only once per day
    assert len(texts) == 3
    statuses = [e["status"] for e in await h.store.recent_logs(FARMER, limit=10)]
    assert statuses.count("limited") == 3


async def test_memory_sends_last_six_pairs_as_history(h):
    for i in range(8):
        h.gemini.reply = f"જવાબ {i}"
        await h.run(text_msg(f"પ્રશ્ન {i}"))

    history = await h.store.get_history(FARMER)
    assert [t["q"] for t in history] == [f"પ્રશ્ન {i}" for i in range(2, 8)]

    contents = h.gemini.calls[-1]["contents"]  # last call: pairs 1-6 as history + question 7
    assert len(contents) == 6 * 2 + 1
    assert [c.role for c in contents[:4]] == ["user", "model", "user", "model"]
    assert contents[0].parts[0].text == "પ્રશ્ન 1"
    assert contents[1].parts[0].text == "જવાબ 1"
    assert contents[-1].parts[0].text == "પ્રશ્ન 7"
    assert await h.store.redis.ttl(f"ks:hist:{FARMER}") > 6 * 24 * 3600


async def test_unsupported_type(h):
    from tests.conftest import sticker_msg

    await h.store.set_language(FARMER, "hi-IN")
    await h.run(sticker_msg())
    assert h.reply_texts() == [L.message("unsupported", "hi-IN")]
    assert h.gemini.calls == []


# ── Phase 3: voice ─────────────────────────────────────────────────────────


async def test_voice_flow(h, tone_ogg):
    h.media["MEDIA_AUDIO"] = (tone_ogg, "audio/ogg; codecs=opus")
    h.gemini.reply = "बहन, पीले चिपचिपे ट्रैप लगाइए और नीम का तेल छिड़किए।"

    await h.run(audio_msg("MEDIA_AUDIO"))

    # Speech-to-text: one Gemini call with the whole note as 16 kHz mono WAV + a JSON schema
    assert len(h.gemini.stt_calls) == 1
    stt = h.gemini.stt_calls[0]
    assert stt["model"] == "gemini-3.8-flash"
    assert stt["config"].response_schema is Transcription
    assert "Do not translate" in stt["config"].system_instruction
    audio_part = stt["contents"][0].parts[0]
    assert audio_part.inline_data.mime_type == "audio/wav"
    with wave.open(io.BytesIO(audio_part.inline_data.data)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert w.getnframes() / w.getframerate() == pytest.approx(40, abs=0.5)

    # Answer: transcript, voice rules and the detected language
    call = h.gemini.calls[0]
    instruction = call["config"].system_instruction
    assert "VOICE REPLY" in instruction and "80 words" in instruction and "reply in Hindi" in instruction
    assert call["contents"][-1].parts[-1].text == h.gemini.transcript
    assert await h.store.get_language(FARMER) == "hi-IN"

    # Text-to-speech: Gemini TTS model, audio output, chosen voice, spoken text without markdown
    tts = h.gemini.tts_calls[0]
    assert tts["model"] == "gemini-3.1-flash-tts-preview"
    assert tts["config"].response_modalities == ["AUDIO"]
    assert tts["config"].speech_config.voice_config.prebuilt_voice_config.voice_name == "Sulafat"
    assert "in Hindi" in tts["contents"] and h.gemini.reply in tts["contents"]

    # Uploaded reply is OGG/Opus, mono, and sent as a voice note, with no extra text
    assert len(h.uploads) == 1
    assert b'name="type"' in h.uploads[0] and b"audio/ogg" in h.uploads[0]
    ogg = h.uploads[0][h.uploads[0].find(b"OggS"):]
    assert ogg_opus_channels(ogg) == 1
    replies = h.replies()
    assert len(replies) == 1
    assert replies[0]["type"] == "audio"
    assert replies[0]["audio"] == {"id": "UPLOADED1", "voice": True}

    history = await h.store.get_history(FARMER)
    assert history[-1]["q"] == h.gemini.transcript
    assert (await h.store.recent_logs(FARMER))[0]["type"] == "audio"


async def test_voice_reply_falls_back_to_text(h, tone_ogg):
    h.media["MEDIA_AUDIO"] = (tone_ogg, "audio/ogg")
    h.gemini.tts_error = RuntimeError("TTS down")
    await h.run(audio_msg("MEDIA_AUDIO"))

    replies = h.replies()
    assert [r["type"] for r in replies] == ["text"]
    assert replies[0]["text"]["body"] == h.gemini.reply
    assert h.uploads == []
    assert (await h.store.recent_logs(FARMER))[0]["status"] == "voice_fallback_text"


async def test_voice_unclear_when_transcript_empty(h, tone_ogg):
    await h.store.set_language(FARMER, "gu-IN")
    h.media["MEDIA_AUDIO"] = (tone_ogg, "audio/ogg")
    h.gemini.transcript = ""
    h.gemini.stt_language = "other"
    await h.run(audio_msg("MEDIA_AUDIO"))

    assert h.gemini.calls == [] and h.gemini.tts_calls == []
    assert h.reply_texts() == [L.message("voice_unclear", "gu-IN")]


async def test_voice_also_text_when_enabled(h, tone_ogg):
    h.settings.voice_reply_also_text = True
    h.media["MEDIA_AUDIO"] = (tone_ogg, "audio/ogg")
    await h.run(audio_msg("MEDIA_AUDIO"))
    assert [r["type"] for r in h.replies()] == ["audio", "text"]


async def test_voice_too_long(h, tone_ogg):
    h.settings.max_voice_seconds = 30
    await h.store.set_language(FARMER, "gu-IN")
    h.media["MEDIA_AUDIO"] = (tone_ogg, "audio/ogg")
    await h.run(audio_msg("MEDIA_AUDIO"))

    assert h.gemini.stt_calls == [] and h.gemini.calls == []
    assert h.reply_texts() == [L.message("voice_too_long", "gu-IN", limit=30)]


# ── Phase 4: images ────────────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"


async def test_image_flow_with_caption(h):
    h.media["MEDIA_IMG"] = (JPEG, "image/jpeg")
    caption = "આ પાંદડા પર શું થયું છે?"
    h.gemini.reply = "મને પાંદડા પર સફેદ ડાઘ દેખાય છે. કદાચ ભૂકી છારો છે (બહુ પાકું નથી)."

    await h.run(image_msg("MEDIA_IMG", caption=caption))

    parts = h.gemini.calls[0]["contents"][-1].parts
    assert parts[0].inline_data.data == JPEG
    assert parts[0].inline_data.mime_type == "image/jpeg"
    assert parts[1].text == caption
    assert "PHOTOS" in h.gemini.calls[0]["config"].system_instruction
    assert h.reply_texts() == [h.gemini.reply]
    assert await h.store.get_language(FARMER) == "gu-IN"
    assert (await h.store.get_history(FARMER))[-1]["q"] == f"[photo] {caption}"


async def test_image_without_caption_uses_saved_language(h):
    await h.store.set_language(FARMER, "hi-IN")
    h.media["MEDIA_IMG"] = (JPEG, "image/jpeg")
    await h.run(image_msg("MEDIA_IMG"))

    call = h.gemini.calls[0]
    assert "reply in Hindi" in call["config"].system_instruction
    assert "without a question" in call["contents"][-1].parts[1].text
    assert len(h.reply_texts()) == 1


async def test_media_download_failure_sends_error(h):
    await h.store.set_language(FARMER, "en-IN")
    await h.run(image_msg("MEDIA_MISSING"))  # graph returns 404
    assert h.gemini.calls == []
    assert h.reply_texts() == [L.message("error", "en-IN")]


# ── Phase 5: admin, reliability, config ────────────────────────────────────


async def test_admin_chats_requires_key_and_filters_by_phone(h):
    other = "919800000002"
    await h.run(text_msg(GU_QUESTION))
    await h.run(text_msg(HI_QUESTION, phone=other))

    assert (await h.client.get("/admin/chats")).status_code == 401
    assert (await h.client.get("/admin/chats", headers={"X-Admin-Key": "wrong"})).status_code == 401

    resp = await h.client.get("/admin/chats", params={"phone": FARMER, "limit": 10},
                              headers={"X-Admin-Key": ADMIN_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["phone"] == FARMER and item["question"] == GU_QUESTION
    assert {"type", "language", "answer", "latency_ms", "timestamp"} <= item.keys()

    all_items = (await h.client.get("/admin/chats", headers={"X-Admin-Key": ADMIN_KEY})).json()
    assert all_items["count"] == 2
    assert all_items["items"][0]["phone"] == other  # newest first


async def test_admin_disabled_when_key_empty(h):
    h.settings.admin_api_key = ""
    resp = await h.client.get("/admin/chats", headers={"X-Admin-Key": ""})
    assert resp.status_code == 401


async def test_webhook_returns_before_ai_work_finishes(h):
    release = asyncio.Event()
    original = h.gemini._generate

    async def slow_generate(**kwargs):
        await release.wait()
        return await original(**kwargs)

    h.gemini.aio.models.generate_content = slow_generate
    body = json.dumps(text_msg(GU_QUESTION)).encode()
    resp = await asyncio.wait_for(
        h.client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)}), timeout=2
    )
    assert resp.status_code == 200
    assert h.replies() == []  # Meta got its 200 while Gemini is still "thinking"

    release.set()
    await h.bot.drain()
    assert h.reply_texts() == [h.gemini.reply]


async def test_status_updates_are_ignored(h):
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.x", "status": "delivered"}]}}]}]}
    resp = await h.run(payload)
    assert resp.status_code == 200 and h.outgoing() == []


def test_env_file_inline_comments(tmp_path, monkeypatch):
    for name in ("APP_ENV", "DATABASE_URL", "WA_VERIFY_TOKEN", "DAILY_MESSAGE_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "APP_ENV=production              # development | production\n"
        "DATABASE_URL=                    # optional, Supabase Postgres (later)\n"
        "DAILY_MESSAGE_LIMIT=50           # per farmer per day\n"
        "WA_VERIFY_TOKEN=ks-verify-8f3a91c2d7   # any string you invent\n",
        encoding="utf-8",
    )
    s = Settings(_env_file=str(env))
    assert s.app_env == "production" and s.database_url == ""
    assert s.daily_message_limit == 50 and s.wa_verify_token == "ks-verify-8f3a91c2d7"
    assert s.gemini_model == "gemini-3.8-flash"


def test_real_app_starts_without_keys():
    """Production wiring boots (no keys, no Redis): webhook verification still works."""
    settings = Settings(_env_file=None, wa_verify_token="tok", redis_url="redis://127.0.0.1:1/0",
                        gemini_api_key="")
    with TestClient(create_app(settings)) as client:
        resp = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "tok",
                                              "hub.challenge": "42"})
        assert resp.text == "42"
        assert client.get("/health").status_code == 503  # Redis unreachable is reported


def test_pcm_helpers():
    wav = pcm_to_wav(b"  " * 24000)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 24000, 24000)
    assert sample_rate_from_mime("audio/L16;codec=pcm;rate=16000") == 16000
    assert sample_rate_from_mime(None) == 24000


def test_clean_for_speech():
    assert clean_for_speech("**નમસ્તે** 🙏\n1. લીમડો 🌿\n- પાણી https://x.y") == "નમસ્તે લીમડો પાણી"


# ── Unit checks ────────────────────────────────────────────────────────────


def test_detect_script():
    assert L.detect_script(GU_QUESTION) == "gu-IN"
    assert L.detect_script(HI_QUESTION) == "hi-IN"
    assert L.detect_script("mere kapas me keede hai") is None
    assert L.detect_script("DAP ખાતર ક્યારે આપવું?") == "gu-IN"  # mixed with English


def test_signature():
    body = b'{"a":1}'
    assert verify_signature(APP_SECRET, body, sign(body))
    assert not verify_signature(APP_SECRET, body, sign(b'{"a":2}'))
    assert not verify_signature(APP_SECRET, body, None)
    assert not verify_signature("", body, sign(body, ""))


def test_split_text_respects_limit():
    long = ("આ એક લાંબું વાક્ય છે. " * 400).strip()
    parts = split_text(long)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts).replace(" ", "") == long.replace(" ", "")


def test_whatsapp_format():
    assert to_whatsapp_format("**Neem** oil\n- step one\n## Tips") == "*Neem* oil\n• step one\n*Tips*"


def test_retryable_errors():
    assert is_retryable(genai_errors.ClientError(429, {}))
    assert is_retryable(genai_errors.ServerError(500, {}))
    assert not is_retryable(genai_errors.ClientError(400, {}))
    assert is_retryable(httpx.ConnectTimeout("boom"))
    assert not is_retryable(ValueError("nope"))


@pytest.mark.parametrize("key", list(L.MESSAGES))
def test_all_fixed_messages_in_three_languages(key):
    assert set(L.MESSAGES[key]) == {"gu-IN", "hi-IN", "en-IN"}
    assert L.detect_script(L.MESSAGES[key]["gu-IN"]) == "gu-IN"
    assert L.detect_script(L.MESSAGES[key]["hi-IN"]) == "hi-IN"
    assert L.detect_script(L.MESSAGES[key]["en-IN"]) is None
