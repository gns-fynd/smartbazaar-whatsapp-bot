import json

import httpx
import pytest

from smartbazaar.tts import FishAudioTTS, TTSError, spoken_text


def client(handler, **kwargs):
    return FishAudioTTS("fish-key", transport=httpx.MockTransport(handler), **kwargs)


@pytest.mark.asyncio
async def test_synthesize_requests_opus_first_with_the_documented_request_shape():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Fish Audio's own content-type for this is "audio/opus"; the client must still
        # report "audio/ogg" (the mime WhatsApp's media upload expects) from the magic bytes.
        return httpx.Response(200, content=b"OggS" + b"\x00" * 20, headers={"content-type": "audio/opus"})

    tts = client(handler, model="s2.1-pro-free", voice_id="manager-voice-1")
    audio, mime = await tts.synthesize("Your order is on the way.")
    await tts.close()

    assert mime == "audio/ogg" and audio.startswith(b"OggS")
    assert len(seen) == 1
    request = seen[0]
    assert request.url == "https://api.fish.audio/v1/tts"
    assert request.headers["authorization"] == "Bearer fish-key"
    assert request.headers["model"] == "s2.1-pro-free"
    body = json.loads(request.content)
    assert body["text"] == "Your order is on the way."
    assert body["format"] == "opus"
    assert body["reference_id"] == "manager-voice-1"


@pytest.mark.asyncio
async def test_synthesize_omits_reference_id_when_no_voice_is_configured():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"OggS" + b"\x00" * 10)

    tts = client(handler)
    await tts.synthesize("Hi")
    await tts.close()

    assert "reference_id" not in json.loads(seen[0].content)


@pytest.mark.asyncio
async def test_synthesize_falls_back_to_mp3_when_the_opus_response_is_not_an_ogg_container():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content)["format"] == "opus":
            return httpx.Response(200, content=b"not-ogg-bytes")
        return httpx.Response(200, content=b"ID3mp3-bytes")

    tts = client(handler)
    audio, mime = await tts.synthesize("Hi")
    await tts.close()

    assert mime == "audio/mpeg" and audio == b"ID3mp3-bytes"
    assert [json.loads(r.content)["format"] for r in requests] == ["opus", "mp3"]


@pytest.mark.asyncio
async def test_synthesize_raises_tts_error_on_http_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    tts = client(handler)
    with pytest.raises(TTSError) as failure:
        await tts.synthesize("Hi")
    await tts.close()
    assert failure.value.code == "http_500"


@pytest.mark.asyncio
async def test_synthesize_bounds_the_response_size():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"OggS" + b"\x00" * (6 * 1024 * 1024))

    tts = client(handler)
    with pytest.raises(TTSError) as failure:
        await tts.synthesize("Hi")
    await tts.close()
    assert failure.value.code == "too_large"


@pytest.mark.asyncio
async def test_synthesize_raises_tts_error_on_transport_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow network")

    tts = client(handler)
    with pytest.raises(TTSError) as failure:
        await tts.synthesize("Hi")
    await tts.close()
    assert failure.value.code == "transport"


def test_spoken_text_drops_markup_and_expands_amounts_and_quantities():
    reply = (
        "🛒 *Your cart*\n"
        "• 2 × Amul Milk — ₹2,849\n"
        "See https://www.jiomart.com/product/amul-milk for details."
    )
    # Each line is a sentence and separators become pauses, so lists don't run together.
    assert spoken_text(reply) == "Your cart. 2 Amul Milk, 2849 rupees. See for details."


def test_spoken_text_drops_a_link_label_left_empty_and_keeps_hinglish():
    reply = "Amul Masti Dahi 400 g · ₹35\nSee it on JioMart: https://www.jiomart.com/product/x\nAapka order ho gaya"
    assert spoken_text(reply) == "Amul Masti Dahi 400 g, 35 rupees. Aapka order ho gaya."


def test_spoken_text_keeps_hindi_and_hinglish_as_is():
    reply = "aapka order 2849 rupaye ka hai, dhanyavaad!"
    assert spoken_text(reply) == reply


def test_spoken_text_truncates_at_a_sentence_boundary_with_a_pointer():
    sentence = "Your order has been placed successfully and is on its way to your address today."
    reply = (sentence + " ") * 6
    result = spoken_text(reply)
    assert len(result) <= 350 + len(" Details are in the message.")
    assert result.endswith("Details are in the message.")
    assert result.count("Your order has been placed") < reply.count("Your order has been placed")


def test_spoken_text_truncates_on_a_word_boundary_when_there_is_no_sentence_end():
    reply = "milk " * 100
    result = spoken_text(reply)
    assert result.endswith("Details are in the message.")
    assert len(result) <= 350 + len(" Details are in the message.")
    assert "  " not in result


def test_spoken_text_handles_non_string_input():
    assert spoken_text(None) == ""
