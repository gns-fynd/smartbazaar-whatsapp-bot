"""Fish Audio text-to-speech client. Never logs the reply text, the API key or audio bytes."""

import re

import httpx

MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_SPOKEN_CHARS = 350

_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE0F]+"
)
_MARKDOWN = re.compile(r"[*_]")
_BULLET = re.compile(r"^[ \t]*[•\-–]\s*", re.MULTILINE)
# "2 × Amul Milk" / "2 x Amul Milk" -> "2 Amul Milk"; the quantity always leads the product name.
_QUANTITY = re.compile(r"(?<!\d)(\d+)\s*[×x]\s*")
_RUPEES = re.compile(r"₹\s?(-?[\d,]+(?:\.\d+)?)")
_WHITESPACE = re.compile(r"\s+")
_SEPARATOR = re.compile(r"\s+[—–·|]\s+|\s*·\s*")
_SENTENCE_END = re.compile(r"[.!?][\"'”’)]*\s")


class TTSError(Exception):
    """A safe-to-log TTS failure; never carries the reply text or audio."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class FishAudioTTS:
    """Async Fish Audio TTS client. Free tier (``s2.1-pro-free``) is fair-use with no SLA."""

    def __init__(self, api_key: str, model: str = "s2.1-pro-free", voice_id: str = "", transport=None):
        self.voice_id = voice_id
        self.client = httpx.AsyncClient(
            base_url="https://api.fish.audio/",
            headers={"Authorization": f"Bearer {api_key}", "model": model},
            timeout=20,
            transport=transport,
        )

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """Return ``(audio bytes, mime type)``. Raises :class:`TTSError` on any failure."""
        content = await self._request(text, "opus")
        if content.startswith(b"OggS"):
            # Fish Audio's own content-type for this is "audio/opus"; WhatsApp's media upload
            # wants the Ogg container's mime ("audio/ogg") instead, so the magic bytes decide.
            return content, "audio/ogg"
        # Not an Ogg container (free-tier output can vary); fall back to a format WhatsApp
        # can still deliver, just without the voice-note waveform/mic treatment.
        content = await self._request(text, "mp3")
        return content, "audio/mpeg"

    async def _request(self, text: str, fmt: str) -> bytes:
        body: dict = {"text": text, "format": fmt, "latency": "normal", "normalize": True}
        if fmt == "opus":
            body["opus_bitrate"] = -1000
        if self.voice_id:
            body["reference_id"] = self.voice_id
        try:
            async with self.client.stream("POST", "v1/tts", json=body) as response:
                if response.status_code != 200:
                    raise TTSError(f"http_{response.status_code}")
                length = response.headers.get("content-length")
                if length and (not length.isdecimal() or int(length) > MAX_AUDIO_BYTES):
                    raise TTSError("too_large")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise TTSError("too_large")
                    chunks.append(chunk)
            if not chunks:
                raise TTSError("empty_audio")
            return b"".join(chunks)
        except TTSError:
            raise
        except httpx.HTTPError:
            raise TTSError("transport") from None

    async def close(self):
        await self.client.aclose()


def spoken_text(reply: str) -> str:
    """Turn a bot reply into something natural to say. Hindi/Hinglish text passes through as is."""
    if not isinstance(reply, str):
        return ""
    sentences = []
    for line in reply.splitlines():
        had_link = bool(_URL.search(line))
        line = _URL.sub("", line)
        line = _EMOJI.sub("", line)
        line = _MARKDOWN.sub("", line)
        line = _BULLET.sub("", line)
        line = _QUANTITY.sub(r"\1 ", line)
        line = _RUPEES.sub(lambda m: m.group(1).replace(",", "") + " rupees", line)
        # Separators are for the eye; a comma is how they sound.
        line = _SEPARATOR.sub(", ", line)
        line = _WHITESPACE.sub(" ", line).strip(" ,")
        if not line or (had_link and line.endswith(":")):
            continue  # "See it on JioMart:" means nothing once the link is gone.
        # Each list line is its own sentence, so "58 rupees. 1 Britannia…" doesn't run together.
        sentences.append(line if line.endswith((".", "!", "?", ":")) else line + ".")
    text = " ".join(sentences)
    if len(text) <= MAX_SPOKEN_CHARS:
        return text
    window = text[:MAX_SPOKEN_CHARS]
    boundary = None
    for match in _SENTENCE_END.finditer(window):
        # The match's trailing \s is exactly one character; keep everything before it.
        boundary = match.end() - 1
    if boundary is None:
        space = window.rfind(" ")
        boundary = space if space > 0 else MAX_SPOKEN_CHARS
    cut = window[:boundary].rstrip()
    pointer = " Details are in the message." if cut.endswith((".", "!", "?")) else ". Details are in the message."
    return cut + pointer
