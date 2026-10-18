"""One white "cart picture": product thumbnails in a grid with quantity badges.

Images come only from JioMart's catalog CDN and are fetched with tight size, count and
time limits. Nothing about the customer is drawn or stored; the JPEG is built in memory
for a single WhatsApp upload.
"""

import asyncio
import io
import math
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

CDN_HOSTS = {"cdn1.jiomartjcp.com"}
MAX_IMAGES = 9
MAX_BYTES = 2_000_000
CELL = 240
PAD = 24


def allowed_image(url: object) -> bool:
    if not isinstance(url, str) or len(url) > 2048:
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in CDN_HOSTS and not parsed.username


async def fetch_images(urls: list[str], transport: httpx.AsyncBaseTransport | None = None) -> list[bytes | None]:
    """Download up to MAX_IMAGES catalog images; a failed one becomes None (drawn as a blank tile)."""
    async with httpx.AsyncClient(timeout=6, follow_redirects=False, transport=transport) as client:
        async def one(url: str) -> bytes | None:
            if not allowed_image(url):
                return None
            try:
                response = await client.get(url)
            except httpx.HTTPError:
                return None
            if response.status_code != 200 or len(response.content) > MAX_BYTES:
                return None
            return response.content

        return list(await asyncio.gather(*(one(url) for url in urls[:MAX_IMAGES])))


def build_collage(images: list[bytes | None], quantities: list[int], labels: list[str] | None = None) -> bytes:
    """A white grid (up to 3 per row) of product thumbnails with ×N badges, as JPEG bytes.

    ``labels`` (for product choices) puts a numbered badge in each tile's top-left corner.
    """
    count = max(1, min(len(images), MAX_IMAGES))
    columns = min(3, count)
    rows = math.ceil(count / columns)
    canvas = Image.new("RGB", (PAD + columns * (CELL + PAD), PAD + rows * (CELL + PAD)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=28)
    for index in range(count):
        x = PAD + (index % columns) * (CELL + PAD)
        y = PAD + (index // columns) * (CELL + PAD)
        draw.rounded_rectangle([x, y, x + CELL, y + CELL], radius=18, outline="#e4e7eb", width=2)
        data = images[index] if index < len(images) else None
        if data:
            try:
                with Image.open(io.BytesIO(data)) as tile:
                    tile = tile.convert("RGBA")
                    tile.thumbnail((CELL - 28, CELL - 28))
                    canvas.paste(tile, (x + (CELL - tile.width) // 2, y + (CELL - tile.height) // 2), tile)
            except (UnidentifiedImageError, OSError, ValueError):
                pass
        quantity = quantities[index] if index < len(quantities) else 1
        if quantity and quantity > 1:
            label = f"×{quantity}"
            box = draw.textbbox((0, 0), label, font=font)
            width, height = box[2] - box[0] + 20, box[3] - box[1] + 14
            draw.rounded_rectangle([x + CELL - width - 8, y + 8, x + CELL - 8, y + 8 + height], radius=12, fill="#0c5273")
            draw.text((x + CELL - width + 2, y + 13), label, fill="white", font=font)
        if labels and index < len(labels):
            number = labels[index]
            box = draw.textbbox((0, 0), number, font=font)
            size = max(box[2] - box[0], box[3] - box[1]) + 26
            draw.ellipse([x + 8, y + 8, x + 8 + size, y + 8 + size], fill="#0c5273")
            draw.text((x + 8 + size / 2, y + 8 + size / 2), number, fill="white", font=font, anchor="mm")
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=85, optimize=True)
    return output.getvalue()
