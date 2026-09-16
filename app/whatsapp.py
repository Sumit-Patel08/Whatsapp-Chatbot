"""WhatsApp Cloud API client: send text / buttons / audio, read + typing,
media download and upload, and webhook signature check."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

import httpx

from app.retry import with_retry

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
TEXT_LIMIT = 4096
BUTTON_TITLE_LIMIT = 20
MAX_MEDIA_BYTES = 16 * 1024 * 1024  # WhatsApp's own limit for audio


class WhatsAppError(RuntimeError):
    pass


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's `X-Hub-Signature-256: sha256=<hex>` header against the raw body."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split a long reply into WhatsApp-sized parts, preferring paragraph,
    line, then sentence boundaries."""
    text = text.strip()
    parts: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = -1
        for sep in ("\n\n", "\n", "। ", ". ", "? ", "! ", " "):
            idx = window.rfind(sep)
            if idx > limit // 2:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def to_whatsapp_format(text: str) -> str:
    """Convert common Markdown from the model into WhatsApp formatting."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)  # **bold** -> *bold*
    text = re.sub(r"__(.+?)__", r"_\1_", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"^(\s*)[\*\-]\s+", r"\1• ", text, flags=re.MULTILINE)  # bullets
    return text.strip()


def mask_phone(phone: str) -> str:
    """For application logs: keep only the last 4 digits."""
    return f"***{phone[-4:]}" if len(phone) > 4 else "***"


class WhatsAppClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        access_token: str,
        phone_number_id: str,
        api_version: str,
    ) -> None:
        self._http = http
        self._token = access_token
        self._base = f"{GRAPH_BASE}/{api_version}"
        self._messages_url = f"{self._base}/{phone_number_id}/messages"
        self._media_url = f"{self._base}/{phone_number_id}/media"

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = {"messaging_product": "whatsapp", **payload}
        resp = await self._http.post(self._messages_url, json=body, headers=self._auth)
        if resp.is_error:
            # Meta puts the useful explanation (error code, details) in the body.
            log.error("WhatsApp send failed %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return resp.json()

    # ── Sending ────────────────────────────────────────────────────────────

    async def send_text(self, to: str, text: str) -> None:
        for part in split_text(text):
            await self._post_message(
                {
                    "recipient_type": "individual",
                    "to": to,
                    "type": "text",
                    "text": {"preview_url": False, "body": part},
                }
            )

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
        if not 1 <= len(buttons) <= 3:
            raise ValueError("WhatsApp allows 1 to 3 reply buttons")
        await self._post_message(
            {
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body[:1024]},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": bid, "title": title[:BUTTON_TITLE_LIMIT]}}
                            for bid, title in buttons
                        ]
                    },
                },
            }
        )

    async def send_audio(self, to: str, media_id: str, voice: bool = True) -> None:
        """Send uploaded audio. `voice=True` shows it as a voice note (needs OGG/Opus mono)."""
        await self._post_message(
            {
                "recipient_type": "individual",
                "to": to,
                "type": "audio",
                "audio": {"id": media_id, "voice": voice},
            }
        )

    async def mark_read_with_typing(self, message_id: str) -> None:
        """Blue ticks + "typing…" (shown up to 25 s or until we reply). Never raises."""
        try:
            await self._post_message(
                {"status": "read", "message_id": message_id, "typing_indicator": {"type": "text"}}
            )
        except Exception as exc:  # cosmetic only; never block the reply
            log.warning("mark read/typing failed: %s", exc)

    # ── Media ──────────────────────────────────────────────────────────────

    async def download_media(self, media_id: str, max_bytes: int = MAX_MEDIA_BYTES) -> tuple[bytes, str]:
        """Two steps: media id -> temporary URL -> bytes. Returns (data, mime_type)."""

        async def _get_meta() -> dict[str, Any]:
            r = await self._http.get(f"{self._base}/{media_id}", headers=self._auth)
            r.raise_for_status()
            return r.json()

        meta = await with_retry(_get_meta, what="media lookup")
        if int(meta.get("file_size") or 0) > max_bytes:
            raise WhatsAppError(f"media too large: {meta.get('file_size')} bytes")

        async def _get_bytes() -> bytes:
            r = await self._http.get(meta["url"], headers=self._auth)
            r.raise_for_status()
            return r.content

        data = await with_retry(_get_bytes, what="media download")
        if len(data) > max_bytes:
            raise WhatsAppError(f"media too large: {len(data)} bytes")
        return data, str(meta.get("mime_type") or "application/octet-stream")

    async def upload_media(self, data: bytes, mime_type: str, filename: str) -> str:
        resp = await self._http.post(
            self._media_url,
            headers=self._auth,
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, data, mime_type)},
        )
        if resp.is_error:
            log.error("WhatsApp media upload failed %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return str(resp.json()["id"])
