"""Catalog normalization, deterministic matching checks, and compact search text."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

_MAX_PRODUCTS = 50
_NON_PRODUCT_TERMS = {
    "keychain",
    "key chain",
    "charm",
    "merchandise",
    "accessory",
    "syrup",
    "mixer",
    "energy drink",
}
_FORMULATIONS = {"diet", "zero", "sugar free", "sugar-free", "no sugar", "regular"}
_CATEGORY_TERMS = {
    "drink": {"drink", "beverage", "cold drink", "juice", "soda", "cola"},
    "milk": {"milk", "dairy"},
    "egg": {"egg", "eggs"},
}
_CATALOG_IMAGE_HOSTS = {"cdn1.jiomartjcp.com"}


class Selection(BaseModel):
    """Only IDs from the supplied product set may appear in a selection."""

    exact_ids: list[str] = Field(default_factory=list, max_length=3)
    alternative_ids: list[str] = Field(default_factory=list, max_length=2)
    explanation: str | None = Field(default=None, max_length=280)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _price(raw: Mapping[str, Any], key: str) -> Any:
    value = raw.get(key)
    if isinstance(value, Mapping):
        value = value.get("effective") or value.get("marked") or value.get("min")
        if isinstance(value, Mapping):
            value = value.get("min")
    return value


def _pack(raw: Mapping[str, Any], name: str) -> str | None:
    value = raw.get("pack") or raw.get("net_quantity")
    if value:
        return _text(value)
    quantity, unit = raw.get("net-quantity-value"), raw.get("net-quantity-unit")
    if quantity is not None and unit:
        return f"{quantity} {unit}"
    match = re.search(r"\b\d+(?:\.\d+)?\s*(?:ml|l|g|kg|pcs?|pack of \d+)\b", name, re.I)
    return match.group(0) if match else None


def _image_url(raw: Mapping[str, Any]) -> str | None:
    """Keep only a public JioMart catalog image, never a caller-provided URL."""
    medias = raw.get("medias")
    if not isinstance(medias, Sequence) or isinstance(medias, (str, bytes)):
        return None
    for media in medias:
        if not isinstance(media, Mapping) or media.get("type") != "image":
            continue
        value = media.get("url")
        if not isinstance(value, str) or len(value) > 2048:
            continue
        parsed = urlparse(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in _CATALOG_IMAGE_HOSTS
            and not parsed.username
            and not parsed.password
            and parsed.path
        ):
            return value
    return None


_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _safe_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if _SAFE_ID.fullmatch(text) else None


def _bounded_int(value: Any) -> int | None:
    return value if type(value) is int and 0 < value <= 999 else None


def _cart_reference(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Keep the listing's own cart identifiers; never synthesize a seller.

    JioMart's web cart sends item, size, seller and vertical; the server assigns
    the fulfilling store, so ``store_id`` is informational only.
    """
    attributes = raw.get("attributes") if isinstance(raw.get("attributes"), Mapping) else {}
    item_id = _safe_id(raw.get("uid") or raw.get("item_id") or attributes.get("item_id"))
    sizes = raw.get("sizes")
    size = _safe_text_id(sizes[0]) if isinstance(sizes, Sequence) and not isinstance(sizes, str) and len(sizes) == 1 else None
    store_ids = raw.get("store_ids")
    store_id = (
        _safe_id(store_ids[0])
        if isinstance(store_ids, Sequence) and not isinstance(store_ids, str) and len(store_ids) == 1
        else None
    )
    seller_id = _safe_id(raw.get("seller_id"))
    if not item_id or not size or not seller_id:
        return None
    vertical = _text(attributes.get("vertical-code"))
    moq = raw.get("moq") if isinstance(raw.get("moq"), Mapping) else {}
    limits = {
        "minimum": _bounded_int(moq.get("minimum")) or 1,
        "maximum": _bounded_int(moq.get("maximum")),
        "increment": _bounded_int(moq.get("increment_unit")) or 1,
        "max_in_order": _bounded_int(attributes.get("max-qty-in-order")),
    }
    return {
        "item_id": item_id,
        "item_size": size,
        "seller_id": seller_id,
        "store_id": store_id,
        "vertical_code": vertical if re.fullmatch(r"[A-Za-z_]{1,40}", vertical) else None,
        "journey": _text(raw.get("journey"))[:20] or None,
        "limits": limits,
    }


def _safe_text_id(value: Any) -> str | None:
    """Sizes are labels such as ``OS`` or ``ONE SIZE``; keep only short plain text."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if re.fullmatch(r"[A-Za-z0-9 ._/-]{1,40}", text) else None


def normalize_products(rawitems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a small, display-safe product shape without inventing availability."""
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rawitems[:_MAX_PRODUCTS]:
        if not isinstance(raw, Mapping):
            continue
        item_id = raw.get("id") or raw.get("uid") or raw.get("item_id") or raw.get("sku_code")
        name = _text(raw.get("name"))[:240]
        if item_id is None or not name:
            continue
        product_id = _text(item_id)
        if product_id in seen:
            continue
        seen.add(product_id)
        brand = raw.get("brand")
        if isinstance(brand, Mapping):
            brand = brand.get("name")
        category = raw.get("category")
        if isinstance(category, Mapping):
            category = category.get("name")
        if not category:
            categories = raw.get("categories")
            if (
                isinstance(categories, Sequence)
                and categories
                and isinstance(categories[0], Mapping)
            ):
                category = categories[0].get("name")
        if not category and isinstance(raw.get("hierarchy"), Mapping):
            hierarchy = raw["hierarchy"]
            category = (hierarchy.get("l3_category") or hierarchy.get("l2_category") or {}).get(
                "name"
            )
        url = raw.get("url") or raw.get("slug")
        products.append(
            {
                "id": product_id,
                "name": name,
                "brand": _text(brand) or None,
                "price": _price(raw, "price"),
                "mrp": _price(raw, "mrp")
                or _price(raw, "marked_price")
                or _price(raw, "marked")
                or (
                    raw.get("price", {}).get("marked", {}).get("min")
                    if isinstance(raw.get("price"), Mapping)
                    and isinstance(raw["price"].get("marked"), Mapping)
                    else None
                ),
                "pack": _pack(raw, name),
                "url": _text(url) or None,
                "category": _text(category) or None,
                "sellable": raw.get("sellable") if isinstance(raw.get("sellable"), bool) else None,
                "image_url": _image_url(raw),
                "cart": _cart_reference(raw),
            }
        )
    return products


_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,199}")


def product_link(product: Mapping[str, Any]) -> str | None:
    """The product's public JioMart page, built only from a plain catalog slug."""
    slug = product.get("url")
    if isinstance(slug, str) and _SLUG.fullmatch(slug):
        return f"https://www.jiomart.com/product/{slug}"
    return None


def _normal(value: str | None) -> str:
    value = re.sub(r"\b(?:dahi|dahe)\b|दही", "curd", (value or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _has_explicit_size(query: str) -> bool:
    return bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:ml|l|g|kg|pcs?|pack)\b", query))


def _has_requested_size(query: str, listing: str) -> bool:
    requested_size = re.search(r"\b\d+(?:\.\d+)?\s*(?:ml|l|g|kg)\b", query)
    return not requested_size or requested_size.group(0).replace(" ", "") in listing.replace(
        " ", ""
    )


def product_is_relevant(query: str, product: Mapping[str, Any]) -> bool:
    """Reject categorical mismatches before they can become alternatives."""
    q = _normal(query)
    name = _normal(_text(product.get("name")))
    brand = _normal(_text(product.get("brand")))
    category = _normal(_text(product.get("category")))
    listing = f"{name} {brand} {category}"
    if not q or not name:
        return False
    # A keychain or syrup is valid only when the shopper actually asked for it.
    if any(term in name and term not in q for term in _NON_PRODUCT_TERMS):
        return False
    for category_word, allowed in _CATEGORY_TERMS.items():
        if category_word in q and not any(word in listing for word in allowed):
            return False
    if "diet coke" in q:
        return any(token in listing for token in ("coke", "coca cola", "pepsi"))
    return True


def _word_matches_listing(word: str, listing: str) -> bool:
    if word == "cola":
        return "cola" in listing or "coke" in listing
    return (
        "egg" in listing
        if word == "eggs"
        else bool(re.search(r"\b" + re.escape(word) + r"\b", listing))
    )


def product_matches_exact(query: str, product: Mapping[str, Any]) -> bool:
    """Conservative product-identity gate used after model ranking.

    Catalog text is untrusted data. This intentionally requires clear evidence in
    the listing and treats uncertainty as non-exact.
    """
    q = _normal(query)
    name = _normal(_text(product.get("name")))
    brand = _normal(_text(product.get("brand")))
    category = _normal(_text(product.get("category")))
    listing = f"{name} {brand} {category}"
    if not product_is_relevant(query, product):
        return False
    if not _has_requested_size(
        query.lower(), f"{product.get('name', '')} {product.get('pack', '')}".lower()
    ):
        return False
    if "diet coke" in q:
        return "diet coke" in name or "diet coke" in brand
    if "coke zero" in q or "coca cola zero" in q:
        return "zero" in listing and ("coke" in listing or "coca cola" in listing)
    if "pepsi" in q and "pepsi" not in listing:
        return False
    if not _keeps_formulation(q, listing):
        return False
    words = [
        word
        for word in q.split()
        if len(word) > 2
        and word
        not in {
            "order",
            "please",
            "want",
            "need",
            "some",
            "with",
            "for",
            "the",
            "and",
            "diet",
            "zero",
            "sugar",
            "free",
            "drink",
        }
    ]
    return bool(words) and all(_word_matches_listing(word, listing) for word in words)


def _keeps_formulation(q: str, listing: str) -> bool:
    """A requested diet/zero/sugar-free/regular formulation must appear in the listing."""
    requested = {term for term in _FORMULATIONS if term in q}
    sugar_free = {"sugar free", "sugar-free", "no sugar"}
    if requested & sugar_free and not any(
        term in listing for term in ("sugar free", "zero sugar", "no sugar")
    ):
        return False
    return all(term in listing for term in requested - sugar_free)


# Words that never name what a product is: sizes, packaging and request filler.
_NOT_KINDS = {
    "order", "please", "want", "need", "some", "with", "for", "the", "and", "pack", "of", "ml", "gm",
    "gms", "kg", "pc", "pcs", "piece", "pieces", "diet", "zero", "sugar", "free", "no", "regular", "new",
    "fresh", "combo", "pouch", "bottle", "can", "cup", "tub", "jar", "box", "packet", "carton", "bar",
}


def _kind(text: str) -> str | None:
    """The product noun, usually the last plain word: “Amul Masti Dahi 400 g” → ``curd``."""
    words = [
        word for word in _normal(text).split()
        if len(word) > 1 and not any(char.isdigit() for char in word) and word not in _NOT_KINDS
    ]
    return words[-1] if words else None


def _same_word(a: str, b: str) -> bool:
    return a == b or a + "s" == b or b + "s" == a or a + "es" == b or b + "es" == a


def same_category(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Both listings carry the same JioMart category (unknown never counts as the same)."""
    first, second = _normal(_text(a.get("category"))), _normal(_text(b.get("category")))
    return bool(first) and first == second


def product_is_substitute(
    query: str,
    product: Mapping[str, Any],
    wanted: Mapping[str, Any] | None = None,
    *,
    ranked: bool = False,
) -> bool:
    """Could ``product`` stand in for what was asked? Deterministic, like the exact gate.

    ``wanted`` is the product that can't be bought (when known). A substitute passes the
    relevance check for the request and for ``wanted``, keeps a requested formulation
    (diet, zero, sugar-free), is a different product, and is the same kind: JioMart's own
    category when both listings have one, otherwise the requested product noun as the
    listing's own noun or in its category. A model-ranked alternative skips only that noun
    test; a different category still rules it out.
    """
    if not product_is_relevant(query, product):
        return False
    listing = " ".join(
        _normal(_text(product.get(key))) for key in ("name", "brand", "category")
    )
    if not _keeps_formulation(_normal(query), listing):
        return False
    if wanted:
        cart = product.get("cart") if isinstance(product.get("cart"), Mapping) else {}
        ids = {_text(product.get("id")), _text(cart.get("item_id"))} - {""}
        if _text(wanted.get("id")) in ids:
            return False
        name = _text(wanted.get("name"))
        if name and (
            not product_is_relevant(name, product) or not _keeps_formulation(_normal(name), listing)
        ):
            return False
        if _text(wanted.get("category")) and _text(product.get("category")):
            return same_category(wanted, product)
    if ranked:
        return True
    kind = _kind(query) or (_kind(_text(wanted.get("name"))) if wanted else None)
    own = _kind(_text(product.get("name")))
    if not kind:
        return False
    category = _normal(_text(product.get("category"))).split()
    return bool(own and _same_word(kind, own)) or any(_same_word(kind, word) for word in category)


def sanitize_selection(
    query: str, products: list[dict[str, Any]], selection: Selection
) -> Selection:
    """Discard model IDs and exact labels that cannot be supported by candidates."""
    by_id = {str(product["id"]): product for product in products}
    exact = [
        item_id
        for item_id in selection.exact_ids
        if item_id in by_id and product_matches_exact(query, by_id[item_id])
    ]
    alternatives = [
        item_id
        for item_id in selection.alternative_ids
        if item_id in by_id and item_id not in exact and product_is_relevant(query, by_id[item_id])
    ]
    return Selection(
        exact_ids=exact[:3], alternative_ids=alternatives[:2], explanation=selection.explanation
    )


def _format_price(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"₹{value:g}"
    return None


def render_search(
    query: str, products: list[dict[str, Any]], selection: Selection, pincode: str | None
) -> str:
    """Render listing evidence, never a stock or cart assertion."""
    safe = sanitize_selection(query, products, selection)
    by_id = {str(product["id"]): product for product in products}
    location = f" for pincode {pincode}" if pincode else ""
    if safe.exact_ids:
        intro = f"Found matching listings{location}:"
        selected = safe.exact_ids
    elif safe.alternative_ids:
        intro = f"I couldn't find an exact match in these results{location}. Alternatives:"
        selected = safe.alternative_ids
    else:
        return f"I haven't found an exact match in these results{location}."
    lines = [intro]
    for item_id in selected:
        product = by_id[item_id]
        details = product["name"]
        price = _format_price(product.get("price"))
        if price:
            details += f" — {price}"
        if product.get("sellable") is False:
            details += " (listing currently not sellable)"
        lines.append(f"• {details}")
    lines.append("Listing status and price need confirmation before adding anything.")
    return "\n".join(lines)
