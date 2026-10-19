from types import SimpleNamespace

import httpx
import pytest

from smartbazaar.media import MAX_AUDIO_BYTES, MediaError, MediaProcessor, media_reference


class FakeAudio:
    def __init__(self, text="milk and eggs"):
        self.text = text
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text)


class FakeResponses:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.parsed)


class FakeAgent:
    model = "test-model"

    def __init__(self, text="milk and eggs", image=None):
        self.audio = SimpleNamespace(transcriptions=FakeAudio(text))
        self.responses = FakeResponses(image or {"queries": ["toothpaste"], "uncertain": False})
        self.client = SimpleNamespace(audio=self.audio, responses=self.responses)


def processor(handler, agent=None):
    client = httpx.AsyncClient(
        base_url="https://graph.facebook.com/v25.0/", transport=httpx.MockTransport(handler)
    )
    return MediaProcessor("private", "v25.0", agent or FakeAgent(), client=client), client


def test_signed_ogg_voice_reference_normalizes_codec_parameters():
    assert media_reference(
        {"type": "audio", "audio": {"id": "media-1", "mime_type": "audio/ogg; codecs=opus"}}
    ) == {"kind": "audio", "media_id": "media-1", "mime_type": "audio/ogg"}


@pytest.mark.asyncio
async def test_audio_download_is_bounded_and_transcribed_without_persisting_media():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "graph.facebook.com":
            return httpx.Response(
                200,
                json={"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/a", "mime_type": "audio/ogg"},
            )
        return httpx.Response(200, headers={"content-type": "audio/ogg"}, content=b"voice")

    agent = FakeAgent()
    subject, client = processor(handler, agent)
    try:
        assert await subject.resolve({"kind": "audio", "media_id": "media-1", "mime_type": "audio/ogg"}) == "milk and eggs"
    finally:
        await client.aclose()

    assert len(seen) == 2
    assert agent.audio.transcriptions.calls[0]["model"] == "whisper-1"
    assert agent.audio.transcriptions.calls[0]["file"][1] == b"voice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata_url, headers, content, code",
    [
        ("https://example.test/media", {"content-type": "audio/ogg"}, b"voice", "invalid_metadata"),
        ("https://lookaside.fbsbx.com/media", {"content-type": "audio/ogg"}, b"x" * (MAX_AUDIO_BYTES + 1), "download_size"),
        ("https://lookaside.fbsbx.com/media", {"content-type": "text/plain"}, b"voice", "download_type"),
    ],
)
async def test_media_download_rejects_untrusted_host_type_and_size(metadata_url, headers, content, code):
    def handler(request):
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": metadata_url, "mime_type": "audio/ogg"})
        return httpx.Response(200, headers=headers, content=content)

    subject, client = processor(handler)
    try:
        with pytest.raises(MediaError, match=code):
            await subject.resolve({"kind": "audio", "media_id": "media-1", "mime_type": "audio/ogg"})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_media_rejects_metadata_mime_that_differs_from_the_signed_webhook_claim():
    def handler(request):
        return httpx.Response(
            200,
            json={"url": "https://lookaside.fbsbx.com/media", "mime_type": "audio/mpeg"},
        )

    subject, client = processor(handler)
    try:
        with pytest.raises(MediaError, match="invalid_metadata"):
            await subject.resolve(
                {"kind": "audio", "media_id": "media-1", "mime_type": "audio/ogg"}
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["audio/aac", "audio/opus"])
async def test_media_rejects_audio_formats_without_direct_whisper_support(mime_type):
    subject, client = processor(lambda _: pytest.fail("unsupported MIME must not download"))
    try:
        with pytest.raises(MediaError, match="unsupported_type"):
            await subject.resolve({"kind": "audio", "media_id": "media-1", "mime_type": mime_type})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_photo_list_keeps_clear_lines_and_returns_unclear_ones_as_guesses():
    def handler(request):
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media", "mime_type": "image/jpeg"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"image")

    agent = FakeAgent(image={"lines": [
        {"item": "Amul milk 1 litre", "quantity": 2, "clear": True},
        {"item": "curd", "quantity": 1, "clear": True},
        {"item": "", "clear": False, "guess": "chini?"},
    ]})
    subject, client = processor(handler, agent)
    try:
        result = await subject.resolve({"kind": "image", "media_id": "image-1", "mime_type": "image/jpeg"})
    finally:
        await client.aclose()
    assert result == "buy Amul milk 1 litre; curd"
    assert result.items == [("Amul milk 1 litre", 2), ("curd", 1)] and result.unclear == ["chini?"]
    call = agent.responses.calls[0]
    assert call["store"] is False
    assert call["input"][0]["content"][1]["type"] == "input_image"

    # Nothing clear, but something guessable: no items, and the guess goes back as a question.
    only_unclear = FakeAgent(image={"lines": [{"item": "", "clear": False, "guess": "maida?"}]})
    subject, client = processor(handler, only_unclear)
    try:
        result = await subject.resolve({"kind": "image", "media_id": "image-2", "mime_type": "image/jpeg"})
    finally:
        await client.aclose()
    assert result == "" and result.items == [] and result.unclear == ["maida?"]

    nothing = FakeAgent(image={"lines": []})
    subject, client = processor(handler, nothing)
    try:
        with pytest.raises(MediaError, match="image_uncertain"):
            await subject.resolve({"kind": "image", "media_id": "image-3", "mime_type": "image/jpeg"})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_photo_lines_that_look_like_commands_are_dropped():
    def handler(request):
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media", "mime_type": "image/jpeg"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"image")

    agent = FakeAgent(image={"lines": [{"item": "LOGIN 9000000001"}, {"item": "checkout"}, {"item": "bread"}]})
    subject, client = processor(handler, agent)
    try:
        result = await subject.resolve({"kind": "image", "media_id": "image-1", "mime_type": "image/jpeg"})
    finally:
        await client.aclose()
    assert result.items == [("bread", 1)]

    only_commands = FakeAgent(image={"lines": [{"item": "LOGIN 9000000001"}, {"item": "remove line 2"}]})
    subject, client = processor(handler, only_commands)
    try:
        with pytest.raises(MediaError, match="image_uncertain"):
            await subject.resolve({"kind": "image", "media_id": "image-2", "mime_type": "image/jpeg"})
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("spoken", ["buy apples", "memory card", "remove line 2", "where is my order"])
async def test_voice_note_is_the_customer_speaking_and_passes_through_as_text(spoken):
    def handler(request):
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media", "mime_type": "audio/ogg"})
        return httpx.Response(200, headers={"content-type": "audio/ogg"}, content=b"voice")

    subject, client = processor(handler, FakeAgent(text=f"  {spoken} "))
    try:
        assert await subject.resolve({"kind": "audio", "media_id": "voice-1", "mime_type": "audio/ogg"}) == spoken
    finally:
        await client.aclose()

    subject, client = processor(handler, FakeAgent(text="   "))
    try:
        with pytest.raises(MediaError, match="empty_extraction"):
            await subject.resolve({"kind": "audio", "media_id": "voice-2", "mime_type": "audio/ogg"})
    finally:
        await client.aclose()
