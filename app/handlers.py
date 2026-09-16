"""The message flow: text / audio / image / interactive.

`Bot.schedule` is called by the webhook *after* it has decided to answer Meta
with 200; all the slow work (AI, speech, sending) happens here in the background.

Order for every incoming message:
  1. de-duplicate by WhatsApp message id
  2. mark as read + typing indicator
  3. language menu handling (button taps, "ભાષા"/"भाषा"/"language", first-time Latin text)
  4. daily limit
  5. answer by type (text / audio / image / other)
  6. write one chat-log entry
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from dataclasses import dataclass
from typing import Any

from app import lang as L
from app.brain import Brain
from app.config import Settings
from app.speech import Speech
from app.store import ChatLogEntry, Store
from app.whatsapp import WhatsAppClient, mask_phone, to_whatsapp_format

log = logging.getLogger(__name__)


@dataclass
class Exchange:
    """What happened with one incoming message; becomes a chat-log entry."""

    phone: str
    message_id: str
    type: str
    lang: str
    question: str = ""
    answer: str = ""
    status: str = "ok"


class Bot:
    def __init__(
        self, settings: Settings, store: Store, wa: WhatsAppClient, brain: Brain, speech: Speech
    ) -> None:
        self.settings = settings
        self.store = store
        self.wa = wa
        self.brain = brain
        self.speech = speech
        self._tasks: set[asyncio.Task[None]] = set()
        # One lock per farmer so her messages are answered in order and history
        # isn't written concurrently. (Ordering only: no state is kept here.)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    # ── Background task management ─────────────────────────────────────────

    def schedule(self, payload: dict[str, Any]) -> None:
        """Start processing a webhook payload without waiting for it."""
        task = asyncio.create_task(self.handle_payload(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for in-flight work (used on shutdown and in tests)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    # ── Entry point ────────────────────────────────────────────────────────

    async def handle_payload(self, payload: dict[str, Any]) -> None:
        jobs = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []) or []:  # "statuses" updates are ignored
                    jobs.append(self._handle_safely(msg))
        if jobs:
            await asyncio.gather(*jobs)

    async def _handle_safely(self, msg: dict[str, Any]) -> None:
        phone = msg.get("from", "")
        lock = self._locks.get(phone)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[phone] = lock
        async with lock:
            try:
                await self.handle_message(msg)
            except Exception:
                log.exception("unhandled error for %s", mask_phone(phone))

    async def handle_message(self, msg: dict[str, Any]) -> None:
        phone: str = msg["from"]
        msg_id: str = msg["id"]

        # Meta may deliver the same webhook more than once.
        if not await self.store.first_time_seen(msg_id):
            log.info("duplicate %s from %s ignored", msg_id, mask_phone(phone))
            return

        started = time.monotonic()
        await self.wa.mark_read_with_typing(msg_id)
        saved_lang = await self.store.get_language(phone)
        ex = Exchange(phone, msg_id, msg.get("type", "unknown"), saved_lang or self.settings.default_language)

        try:
            await self._dispatch(msg, ex, saved_lang)
        except Exception:
            log.exception("failed to answer %s message from %s", ex.type, mask_phone(phone))
            ex.status = "error"
            ex.answer = L.message("error", ex.lang)
            try:
                await self.wa.send_text(phone, ex.answer)
            except Exception:
                log.exception("could not send error message")

        await self._log(ex, started)

    async def _dispatch(self, msg: dict[str, Any], ex: Exchange, saved_lang: str | None) -> None:
        # Language menu interactions are free: they don't count toward the daily limit.
        if ex.type == "interactive":
            if await self._on_language_button(msg, ex):
                return
        elif ex.type == "text":
            text = msg["text"]["body"]
            ex.question = text
            if L.is_language_command(text) or (saved_lang is None and L.detect_script(text) is None):
                await self._ask_language(ex)
                return

        if not await self._within_daily_limit(ex):
            return

        if ex.type == "text":
            await self._on_text(ex, saved_lang)
        elif ex.type == "audio":
            await self._on_audio(msg, ex, saved_lang)
        elif ex.type == "image":
            await self._on_image(msg, ex, saved_lang)
        else:
            await self._on_unsupported(ex)

    # ── Flows ──────────────────────────────────────────────────────────────

    async def _ask_language(self, ex: Exchange) -> None:
        await self.wa.send_buttons(ex.phone, L.CHOOSE_LANGUAGE, L.LANGUAGE_BUTTONS)
        ex.status, ex.answer = "language_menu", "[language buttons]"

    async def _on_language_button(self, msg: dict[str, Any], ex: Exchange) -> bool:
        reply = msg.get("interactive", {}).get("button_reply") or {}
        chosen = L.language_from_button(reply.get("id", ""))
        if chosen is None:
            return False
        await self.store.set_language(ex.phone, chosen)
        ex.lang, ex.question, ex.status = chosen, reply.get("title", chosen), "language_set"
        ex.answer = L.message("welcome", chosen)
        await self.wa.send_text(ex.phone, ex.answer)
        return True

    async def _within_daily_limit(self, ex: Exchange) -> bool:
        count = await self.store.count_message(ex.phone)
        if count <= self.settings.daily_message_limit:
            return True
        ex.status = "limited"
        if await self.store.claim_limit_notice(ex.phone):  # only once per IST day
            ex.answer = L.message("limit_reached", ex.lang)
            await self.wa.send_text(ex.phone, ex.answer)
        return False

    async def _on_text(self, ex: Exchange, saved_lang: str | None) -> None:
        script_lang = L.detect_script(ex.question)
        if script_lang:
            ex.lang = script_lang  # reply in the script she just used
            if saved_lang is None:
                await self.store.set_language(ex.phone, script_lang)
        ex.answer = await self._ask_brain(ex)
        await self.wa.send_text(ex.phone, to_whatsapp_format(ex.answer))
        await self.store.add_history(ex.phone, ex.question, ex.answer)

    async def _on_audio(self, msg: dict[str, Any], ex: Exchange, saved_lang: str | None) -> None:
        ex.question = "[voice]"
        audio, _mime = await self.wa.download_media(msg["audio"]["id"])

        voice = await self.speech.prepare_voice(audio, self.settings.max_voice_seconds)
        if voice.too_long:
            ex.status = "voice_too_long"
            ex.answer = L.message("voice_too_long", ex.lang, limit=self.settings.max_voice_seconds)
            await self.wa.send_text(ex.phone, ex.answer)
            return

        transcript, detected = await self.speech.transcribe(voice.wav)
        if not transcript:
            ex.status = "voice_unclear"
            ex.answer = L.message("voice_unclear", ex.lang)
            await self.wa.send_text(ex.phone, ex.answer)
            return

        ex.question = transcript
        spoken_lang = L.normalize_language(detected)
        if spoken_lang:
            ex.lang = spoken_lang
            if saved_lang is None:
                await self.store.set_language(ex.phone, spoken_lang)

        ex.answer = await self._ask_brain(ex, voice=True)
        await self.store.add_history(ex.phone, ex.question, ex.answer)

        # Voice reply; on any failure fall back to a single text reply.
        try:
            note = await self.speech.synthesize_voice_note(ex.answer, ex.lang)
            media_id = await self.wa.upload_media(note, "audio/ogg", "krishi-sakhi.ogg")
            await self.wa.send_audio(ex.phone, media_id, voice=True)
        except Exception:
            log.exception("voice reply failed for %s, sending text instead", mask_phone(ex.phone))
            ex.status = "voice_fallback_text"
            await self.wa.send_text(ex.phone, to_whatsapp_format(ex.answer))
            return
        if self.settings.voice_reply_also_text:  # costs a second WhatsApp message
            await self.wa.send_text(ex.phone, to_whatsapp_format(ex.answer))

    async def _on_image(self, msg: dict[str, Any], ex: Exchange, saved_lang: str | None) -> None:
        image = msg["image"]
        caption = (image.get("caption") or "").strip()
        ex.question = f"[photo] {caption}".strip()  # how it is remembered and logged
        script_lang = L.detect_script(caption)
        if script_lang:
            ex.lang = script_lang
            if saved_lang is None:
                await self.store.set_language(ex.phone, script_lang)

        data, mime_type = await self.wa.download_media(image["id"])
        mime_type = mime_type.split(";")[0].strip() or "image/jpeg"
        ex.answer = await self._ask_brain(ex, question=caption, image=(data, mime_type))
        await self.wa.send_text(ex.phone, to_whatsapp_format(ex.answer))
        await self.store.add_history(ex.phone, ex.question, ex.answer)

    async def _on_unsupported(self, ex: Exchange) -> None:
        ex.status = "unsupported"
        ex.answer = L.message("unsupported", ex.lang)
        await self.wa.send_text(ex.phone, ex.answer)

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _ask_brain(
        self,
        ex: Exchange,
        question: str | None = None,
        image: tuple[bytes, str] | None = None,
        voice: bool = False,
    ) -> str:
        history = await self.store.get_history(ex.phone)
        return await self.brain.answer(
            ex.question if question is None else question,
            ex.lang, history=history, image=image, voice=voice,
        )

    async def _log(self, ex: Exchange, started: float) -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        log.info("%s %s lang=%s status=%s %dms", mask_phone(ex.phone), ex.type, ex.lang, ex.status, latency_ms)
        try:
            await self.store.log_exchange(
                ChatLogEntry(
                    phone=ex.phone, type=ex.type, language=ex.lang, question=ex.question,
                    answer=ex.answer, latency_ms=latency_ms, status=ex.status, message_id=ex.message_id,
                )
            )
        except Exception:
            log.exception("chat log write failed")
