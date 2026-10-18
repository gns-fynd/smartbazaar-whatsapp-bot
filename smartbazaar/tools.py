"""Scoped agent tools. Identity, sessions and URLs are never model arguments.

The customer's real JioMart cart is the only basket. A request's confident matches go
straight into it. A product that needs the customer's choice becomes a short-lived
question (``memory["pending"]``) asked one at a time with pictures; any newer add
request replaces those questions, so older requests never resurface.
"""

import secrets
import time

from .agent import AgentError
from .catalog import normalize_products, product_matches_exact, sanitize_selection
from .jiomart import CommerceError
from .memory import PENDING_TTL, PREFERENCE_TTL, canonical_query, remembered_choice

FOCUS_TTL = 6 * 3600


def _price(value) -> str:
    return f"₹{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else ""


def _short_label(index: int, product: dict) -> str:
    """A ≤20-character button title that tells similar products apart."""
    words = (product.get("brand") or product.get("name") or "").split()
    brand = " ".join(words[:2])
    pack = product.get("pack") or ""
    label = f"{index}. {brand} {pack}".strip() if pack else f"{index}. {product.get('name') or ''}"
    return label[:20].rstrip()


class ShoppingTools:
    def __init__(self, commerce, agent, cart):
        self.commerce, self.agent, self.cart = commerce, agent, cart

    async def refresh_history(self, state: dict, memory: dict, force=False):
        now = time.time()
        if not force and memory.get("history_import_disabled"):
            return
        if force:
            memory.pop("history_import_disabled", None)
        if not force and now - memory.get("history", {}).get("checked_at", 0) < 86400:
            return
        try:
            result = await self.commerce.get_purchase_history(state["session"])
        except AttributeError:
            result = {"status": "unavailable", "items": []}
        except CommerceError:
            # History is optional and may have separate consumer access scopes.
            # A failure here must not destroy a working search/account session.
            result = {"status": "unavailable", "items": []}
        memory["history"] = {**result, "checked_at": now}

    @staticmethod
    def context(memory: dict, state: dict | None = None) -> dict:
        items = memory.get("pending", {}).get("items", [])
        viewed = ShoppingTools.focus(state or {})
        return {
            "previous_queries": memory.get("active", {}).get("queries", []),
            "choices": [
                {"choice": i, "name": p["name"]}
                for i, p in enumerate(items[0]["products"] if items else [], 1)
            ],
            "pending": [{"query": item["query"], "quantity": item["quantity"]} for item in items],
            "preferences": [
                {"category": key, "name": val["product"]["name"]}
                for key, val in memory.get("preferences", {}).items()
            ],
            "viewed": viewed["name"] if viewed else None,
        }

    @staticmethod
    def remember_focus(state: dict, product: dict) -> None:
        """The product the customer just saw, so “add it” or an Add to cart tap means this one."""
        state["focus"] = {"product": product, "pincode": state.get("location", {}).get("pincode"),
                          "expires": time.time() + FOCUS_TTL}

    @staticmethod
    def focus(state: dict) -> dict | None:
        focus = state.get("focus") if isinstance(state.get("focus"), dict) else {}
        if focus.get("expires", 0) < time.time() or focus.get("pincode") != state.get("location", {}).get("pincode"):
            return None
        product = focus.get("product")
        return product if isinstance(product, dict) and product.get("name") else None

    @staticmethod
    def next_choice(memory: dict, state: dict) -> dict | None:
        """The current product question as numbered pictures with tap buttons (and a list fallback)."""
        pending = memory.get("pending") or {}
        items = pending.get("items", [])
        if (not items or pending.get("expires", 0) < time.time()
                or pending.get("pincode") != state.get("location", {}).get("pincode")):
            memory.pop("pending", None)
            return None
        item = items[0]
        products = item["products"]
        # Each option is a tap-able button; its opaque ID maps back to this exact question.
        tokens = {"sbch_" + secrets.token_urlsafe(12): i for i in range(len(products))}
        skip = "sbch_" + secrets.token_urlsafe(12)
        pending["tokens"] = {**tokens, skip: -1}
        lines = [f"Which {item['query']} would you like?" + (f" (1 of {len(items)} to choose)" if len(items) > 1 else "")]
        for i, product in enumerate(products, 1):
            detail = " · ".join(part for part in (product.get("pack") or "", _price(product.get("price"))) if part)
            lines.append(f"{i}. {product['name']}" + (f" — {detail}" if detail else ""))
        lines.append("Tap a number to add it to your cart" + (", or Skip." if len(products) < 3 else ", or say ‘skip’."))
        body = "\n".join(lines)[:1024]
        buttons = [{"id": token, "title": _short_label(i + 1, products[i])} for token, i in tokens.items()]
        if len({b["title"] for b in buttons}) < len(buttons):
            buttons = [{"id": token, "title": f"Option {i + 1}"} for token, i in tokens.items()]
        if len(buttons) < 3:
            buttons.append({"id": skip, "title": "Skip"})
        rows = []
        for token, i in tokens.items():
            product = products[i]
            detail = " · ".join(part for part in (_price(product.get("price")), product.get("pack") or "",
                                                  product.get("brand") or "") if part)
            rows.append({"id": token, "title": product["name"][:24], "description": (detail or product["name"])[:72]})
        rows.append({"id": skip, "title": "Skip this item", "description": f"Don’t add {item['query']}"[:72]})
        fallback = body + "\nReply ‘choose 1’ (or another number), or ‘skip’."
        menu = {"type": "list", "body": body, "button": "Choose", "rows": rows, "fallback": fallback}
        if not all(isinstance(p.get("image_url"), str) for p in products):
            return menu
        return {"type": "choice_card", "items": [{"image_url": p["image_url"]} for p in products],
                "body": body, "buttons": buttons[:3], "list": menu, "fallback": fallback}

    async def execute(self, intent, state: dict, memory: dict) -> list:
        action = intent.action
        if action == "history":
            await self.refresh_history(state, memory, force=True)
            history = memory["history"]
            if not history.get("items"):
                return ["I couldn’t retrieve usable past purchases from JioMart. That doesn’t mean you have no previous orders. You can still tell me your preferred brands."]
            return ["Previously delivered products returned by JioMart:\n" + "\n".join(
                f"• {p['name']}" for p in history["items"][:10]
            ) + "\nThis may be only part of your history."]
        if action == "memory":
            preferences = memory.get("preferences", {})
            lines = [f"• {key}: {value['product']['name']} (you asked me to remember)" for key, value in preferences.items()]
            return ["Your saved preferences:\n" + "\n".join(lines) if lines else "You haven’t saved any preferences yet. After adding something, say ‘remember cart line 1 for curd’."]
        if action == "forget":
            if intent.preference_category:
                category = canonical_query(intent.preference_category)
                removed = memory.get("preferences", {}).pop(category, None)
                return [f"I’ve forgotten your saved preference for {category}." if removed else f"There isn’t a saved preference for {category}."]
            # Suppress automatic re-import until explicitly asking for history again.
            memory.clear()
            memory["history_import_disabled"] = True
            memory["history"] = {"status": "forgotten", "items": [], "checked_at": time.time()}
            return ["I’ve forgotten your preferences and imported purchase context. Your JioMart cart and account stay as they are."]
        if action == "new_session":
            memory.pop("active", None)
            memory.pop("pending", None)
            return ["Let’s start fresh. Your JioMart cart and saved preferences are still here."]
        if action == "remember":
            return await self._remember(intent, state, memory)
        if action == "skip":
            return self._skip(memory, state)
        if action == "choose":
            return await self._choose(intent, state, memory)
        if action != "add":
            return ["Tell me what you’d like to buy, or send CART to see your JioMart cart."]
        if not state.get("location"):
            return ["Send your delivery pincode first so I can find products for your area."]
        return await self._add(intent, state, memory)

    async def _remember(self, intent, state, memory) -> list[str]:
        category = canonical_query(intent.preference_category or "")
        if not intent.line_number or not category:
            return ["Tell me the cart line and category, such as ‘remember cart line 1 for curd’."]
        item = await self.cart.cart_item(state, intent.line_number)
        if isinstance(item, str):
            return [item]
        key = str((str(item["identifiers"].get("product_uid") or item["identifiers"].get("item_id")),
                   str(item.get("item_size"))))
        product = memory.get("cart_products", {}).get(key) or {
            "id": str(item["identifiers"].get("product_uid") or ""), "name": item.get("name") or "",
            "brand": item.get("brand"), "pack": item.get("pack"), "category": item.get("category"),
        }
        if not product.get("name") or not product_matches_exact(category, product):
            return [f"That cart line doesn’t look like {category}. Send CART and pick the matching line."]
        preferences = memory.setdefault("preferences", {})
        if len(preferences) >= 50 and category not in preferences:
            return ["Your preference list is full. Use FORGET to clear saved memory first."]
        preferences[category] = {
            "product": {key: product.get(key) for key in ("id", "name", "brand", "pack", "category")},
            "source": "explicit", "expires": time.time() + PREFERENCE_TTL,
        }
        return [f"I’ll remember {product['name']} for {category}. You can override it any time or send FORGET."]

    def _skip(self, memory, state) -> list:
        pending = memory.get("pending") or {}
        items = pending.get("items", [])
        if not items:
            return ["There’s nothing waiting for a choice. Tell me what you’d like to buy."]
        skipped = items.pop(0)
        prompt = self.next_choice(memory, state)
        return [f"Skipped {skipped['query']}."] + ([prompt] if prompt else [])

    async def _choose(self, intent, state, memory) -> list:
        pending = memory.get("pending") or {}
        items = pending.get("items", [])
        index = (intent.choice_number or 0) - 1
        if (not items or pending.get("pincode") != state.get("location", {}).get("pincode")
                or pending.get("expires", 0) < time.time()):
            memory.pop("pending", None)
            return ["That choice is from an earlier request. Tell me what you’d like and I’ll look again."]
        if not 0 <= index < len(items[0]["products"]):
            # An old button or a wrong number never discards the question being asked now.
            prompt = self.next_choice(memory, state)
            return ["That choice is from an earlier request."] + ([prompt] if prompt else [])
        item = items.pop(0)
        product = item["products"][index]
        replies = await self._receipt(state, memory, [{"product": product, "quantity": item["quantity"]}])
        prompt = self.next_choice(memory, state)
        return replies + ([prompt] if prompt else [])

    async def _add(self, intent, state, memory) -> list:
        if intent.viewed:
            product = self.focus(state)
            if not product:
                return ["Which product would you like? Tell me its name and I’ll add it."]
            quantity = intent.items[0].quantity if intent.items else 1
            # The newest request wins: older unanswered questions are dropped.
            memory.pop("pending", None)
            replies = await self._receipt(state, memory, [{"product": product, "quantity": quantity}])
            return replies
        if not intent.items:
            return ["What would you like to add to your cart?"]
        await self.refresh_history(state, memory)
        confident, questions, missing, why = [], [], [], {}
        for item in intent.items:
            query = canonical_query(item.query)
            preference = remembered_choice(memory, query, time.time())
            search_query = preference["product"]["name"] if preference else query
            try:
                result = await self.commerce.search(search_query, state["location"], state["session"])
                products = normalize_products(result.get("items", []))
                ranked = await self.agent.rank(query, products) if products else None
                safe = sanitize_selection(query, products, ranked) if ranked else None
                by_id = {p["id"]: p for p in products if p.get("sellable") is not False}
                exact = [by_id[i] for i in safe.exact_ids if i in by_id] if safe else []
                alternatives = [by_id[i] for i in safe.alternative_ids if i in by_id] if safe else []
            except CommerceError as exc:
                if exc.code == "auth_expired":
                    raise
                exact, alternatives = [], []
            except AgentError:
                exact, alternatives = [], []
            chosen = None
            if preference:
                chosen = next((p for p in exact if p["id"] == preference["product"]["id"]), None)
                if chosen:
                    why[chosen["id"]] = preference["source"]
            elif len(exact) == 1:
                chosen = exact[0]
            if chosen:
                confident.append({"product": chosen, "quantity": item.quantity})
            elif exact or alternatives:
                questions.append({"query": query, "quantity": item.quantity, "products": (exact or alternatives)[:3]})
            else:
                missing.append(query)
        # The newest request wins: older unanswered questions are dropped, never carried forward.
        memory.pop("pending", None)
        if questions:
            memory["pending"] = {"items": questions, "pincode": state["location"].get("pincode"),
                                 "expires": time.time() + PENDING_TTL}
        memory.setdefault("active", {})["queries"] = [canonical_query(i.query) for i in intent.items]
        replies = await self._receipt(state, memory, confident, why) if confident else []
        for query in missing:
            replies.append(f"I couldn’t find a clear match for “{query}” near you. Tell me a brand or pack size and I’ll look again.")
        prompt = self.next_choice(memory, state)
        if prompt:
            replies.append(prompt)
        return replies

    async def _receipt(self, state, memory, lines, why=None) -> list[str]:
        """Add to the real cart and say exactly what happened."""
        result = await self.cart.add_products(state, memory, lines)
        if result["error"]:
            return [result["error"]]
        why = why or {}
        notes = []
        if result["added"]:
            reasons = {line["product"]["name"]: why.get(line["product"].get("id")) for line in lines}
            named = []
            for entry in result["added"]:
                name = entry.split(" × ", 1)[-1]
                source = reasons.get(name)
                suffix = " (your saved choice)" if source == "explicit" else " (from your past orders)" if source == "purchase_history" else ""
                named.append(entry + suffix)
            notes.append("🛒 Added to your JioMart cart:\n" + "\n".join(f"• {entry}" for entry in named))
        if result["already"]:
            notes.append("Already in your cart, left as it is: " + ", ".join(result["already"][:6])
                         + ". Say ‘make 1 two in cart’ to change a quantity.")
        notes.extend(result["notes"])
        return ["\n".join(notes)] if notes else []
