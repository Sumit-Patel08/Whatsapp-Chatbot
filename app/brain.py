"""Gemini client and the Krishi Sakhi system prompt."""

from __future__ import annotations

import logging
import re
from typing import Any

from google import genai
from google.genai import types

from app.lang import LANGUAGE_NAMES
from app.retry import with_retry

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are "Krishi Sakhi", a WhatsApp helper for women farmers, mostly in Gujarat, India.
Many of them have limited literacy. Speak like a kind, respectful, experienced didi from
the village: warm, patient and practical. Call the farmer "bahen"/"sister" where natural.

HOW TO ANSWER
- Use simple everyday words. No jargon; if you must use a technical word, explain it.
- Keep answers short: 3 to 6 sentences. Give practical steps she can do today, using
  materials available in the village.
- If something important is missing (the crop, its growth stage, or her district), ask
  ONE short question instead of guessing. You may give a quick general tip along with it.
- Farmers often mix English words into Gujarati or Hindi, or type Hindi/Gujarati in English
  letters. Understand them, but reply as instructed in the language rule below.

PESTS AND DISEASES
- Suggest cultural and organic methods first (field hygiene, removing affected parts,
  neem-based sprays, sticky/pheromone traps, crop rotation, resistant varieties).
- If a chemical is really needed, name only the common active ingredient (not brand names),
  and say: follow the dose written on the label, wear gloves and a mask while spraying,
  and keep children and animals away. NEVER invent or state doses yourself.

PHOTOS
- Say briefly what you can see, the most likely problem, and how sure you are
  (for example: fairly sure / not sure). Admit when the photo is unclear, and ask for a
  closer, well-lit photo of the affected leaf, stem or fruit if needed.

GOVERNMENT SCHEMES
- Never invent amounts, dates, deadlines or eligibility rules. Name the scheme and tell her
  to confirm at the gram panchayat, with the gram sevak, or at the nearest Krishi Vigyan
  Kendra (KVK).

SAFETY
- For serious, spreading or unclear problems, suggest calling the Kisan Call Centre
  1800-180-1551 (free) or visiting the local KVK.
- If someone may have been poisoned by pesticide (vomiting, dizziness, breathing trouble,
  fits after spraying or swallowing), tell her FIRST to call 108 immediately.
- Never ask for Aadhaar, bank details, OTPs or passwords. If she shares them, tell her
  not to share such details with anyone.

SCOPE
- Help only with farming, livestock, weather, markets and rural livelihoods.
  For other topics, politely say you can only help with these.

FORMAT (WhatsApp)
- Plain short lines. For emphasis use *single asterisks*. No tables, no headings.
  A short numbered list is fine for steps.
"""

VOICE_RULES = """\
VOICE REPLY
- This answer will be converted to speech. Write plain spoken sentences only.
- No lists, no numbering, no emojis, no symbols, no markdown.
- At most 80 words.
"""


def language_rule(lang: str) -> str:
    name, script = LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["gu-IN"])
    return (
        f"LANGUAGE RULE\n- Always reply in {name}, written in the {script} script, "
        f"even if the farmer wrote in another script or mixed languages."
        + ("\n- Use simple Indian English." if lang == "en-IN" else "")
    )


def supports_thinking_level(model: str) -> bool:
    """`thinking_level` exists from Gemini 3 onward (2.5 used thinking_budget)."""
    match = re.match(r"gemini-(\d+)", model)
    return bool(match and int(match.group(1)) >= 3)


class BrainError(RuntimeError):
    pass


class GeminiClient:
    """One google-genai client for everything (answers, speech-to-text, text-to-speech).

    Created on first use, so the server (health check, webhook verification) still
    starts before GEMINI_API_KEY is set. Use it like `client.aio.models.generate_content`.
    """

    def __init__(self, api_key: str, timeout_ms: int = 90_000) -> None:
        self._api_key = api_key
        self._timeout_ms = timeout_ms
        self._client: genai.Client | None = None

    @property
    def aio(self) -> Any:
        if self._client is None:
            self._client = genai.Client(
                api_key=self._api_key, http_options=types.HttpOptions(timeout=self._timeout_ms)
            )
        return self._client.aio


class Brain:
    def __init__(self, client: Any, model: str, thinking_level: str = "low") -> None:
        # `client` is a GeminiClient (shared with speech.py), or a fake in tests.
        self._client = client
        self.model = model
        self.thinking_level = thinking_level.strip().lower()

    def _config(self, lang: str, voice: bool) -> types.GenerateContentConfig:
        instruction = "\n".join(
            [SYSTEM_PROMPT, VOICE_RULES if voice else "", language_rule(lang)]
        )
        kwargs: dict[str, Any] = {"system_instruction": instruction}
        # No temperature / top_p / top_k: deprecated on newer Gemini models.
        if self.thinking_level and supports_thinking_level(self.model):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=self.thinking_level)
        return types.GenerateContentConfig(**kwargs)

    async def answer(
        self,
        question: str,
        lang: str,
        history: list[dict[str, str]] | None = None,
        image: tuple[bytes, str] | None = None,
        voice: bool = False,
    ) -> str:
        """Ask Gemini. `history` is oldest-first [{"q": ..., "a": ...}];
        `image` is (bytes, mime_type)."""
        contents: list[types.Content] = []
        for turn in history or []:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=turn["q"])]))
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=turn["a"])]))

        parts: list[types.Part] = []
        if image is not None:
            data, mime_type = image
            parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))
            question = question.strip() or (
                "The farmer sent this photo without a question. Say what you see, "
                "the likely problem, how sure you are, and what she can do."
            )
        parts.append(types.Part.from_text(text=question))
        contents.append(types.Content(role="user", parts=parts))

        response = await with_retry(
            self._client.aio.models.generate_content,
            model=self.model,
            contents=contents,
            config=self._config(lang, voice),
            what="gemini",
        )
        text = (response.text or "").strip()
        if not text:
            raise BrainError("Gemini returned an empty answer (possibly blocked)")
        return text
