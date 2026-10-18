# Product polish backlog — WhatsApp JioMart shopping agent

Updated 18 October 2026, after the first live end-to-end order. Owner decisions recorded here: spoken/voice **replies are out of scope** (voice-note *input* stays); the experience should be agentic (AI-written replies over verified facts), not template-driven.

Status note, 19 October 2026: several items below are now **implemented and locally tested but not deployed or verified on WhatsApp**. See "Status on 19 October" after the priority table and [status](project-status.md). The tables below are the 18 October record and are left as written.

## What is proven live (real account, real money)

| Journey | Evidence (18 Oct) |
|---|---|
| Voice note → list items | Hindi/English voice note became rabri, tomato and paneer lines |
| Real JioMart cart | CART reads, adds, removals, quantity changes, EMPTY CART on the account's own cart |
| Checkout review | JioMart's own lines, fees and total; address step via JioMart's shipping-fee extension |
| Online payment (UPI) | Order JM6AD4FE…: bot opened JioMart's payment session server-side, JioPay page in browser, paid by UPI |
| Payment confirmation push | Watcher saw `paid`/`confirmed` and messaged "✅ Payment received" within ~30 s |
| Order history | ORDERS lists the new order; it is visible in the JioMart app |
| AI-written replies | Composer replies passed the fact check (`compose message=ai prompt=ai`) |
| Picture cart card | Product image header + item bullets + totals + payment buttons |

## Failures found in live testing and what changed

| Failure seen | Root cause | Fix (live) |
|---|---|---|
| Removed items came back at checkout; 3 kg tomatoes | Old shopping list re-synced into the cart on every CHECKOUT; duplicate lines summed | Cart is the only basket; CHECKOUT never adds; lines move once; duplicates never add up |
| "Remove other items" deleted the customer's own items | Button removed non-list cart lines | Button retired |
| "JioMart didn't accept one of the items" after a successful add | Address step's 307/404 reported as an item error | JioMart extension redirects followed on www.jiomart.com; separate address message |
| Pay link: "Invalid session" / "Method not allowed" | JioMart's payment session needs the account's sign-in; browsers don't send it cross-site | Bot opens the payment session server-side and hands the browser JioPay's page |
| Every Pay online tap created another unpaid order | Payment sessions open once | Older unpaid attempts are superseded and stop being watched after 3 h |
| Sign-in expired every hour | JioMart access JWT lasts ~1 h; refresh sent no body | Refresh sends `{access_token}` per the docs (verify live) |
| Fees didn't add up (₹49 items, ₹79 total) | Unmapped charge names hidden | "Delivery & other charges" = total − items − discounts |
| Card arrived twice | Redeploy during a mid-send | Deploy waits for pending **and sending** messages |
| Robotic multi-bubble replies; "choose 1" typing | Templates per tool | AI composer with fact check; tap-to-choose list menus; read receipt + typing indicator |

## Next, in priority order

| # | Item | Why it matters for judging | Effort |
|---|---|---|---|
| 1 | **Verify session refresh live**; if JioMart rejects it, ask once for LOGIN with a one-tap button and keep the cart | Hourly re-login kills the experience and the watcher | S |
| 2 | **Welcome menu** for "hi"/first contact: *Reorder last order · Send my list · Track order* (buttons) | First 10 seconds decide the impression | S |
| 3 | **Unavailable item → swap suggestion with Accept/Skip** | Source requirement: substitutions with consent | M |
| 4 | **"Your usual"** from real orders now that history works (last 6 months, delivered only) | Source requirement: personalization | M |
| 5 | **One-reply list from voice/photo**: confident items added, uncertain ones as one menu, not one-by-one | Signature input; speed | M |
| 6 | **Retire the visible "shopping list" concept** (LIST shows only pending choices; auto-drop stale lines after a day) | Users think in carts, not lists | S |
| 7 | **Delivery slot/ETA in review** (Quick vs scheduled) and offers/coupons | Feels like a complete store | M |
| 8 | **Cancel order, invoice PDF, support hand-off** | Post-order completeness | M |
| 9 | **Store dashboard** (orders, conversations, unavailable-demand) | SmartBazaar "one store, one customer" pitch | M |
| 10 | **Stable host**: always-on server + domain (removes ngrok warning and URL changes), durable inbound queue | Judges can use it live without surprises | M |

Out of scope by decision: spoken replies / manager voice. Needs Meta setup: business-initiated messages after 24 h (templates + billing flag 141006).

## Status on 19 October 2026

Implemented and locally tested (307 tests), **not yet deployed and not verified live**:

| # | Item | Status |
|---|---|---|
| 1 | Session refresh | Root cause found in live logs: refresh only ran after a 401, when the token had already expired, and that refresh request returned HTTP 400. Now the bot refreshes when the token has under 5 minutes left, falls back once to a cookie-only refresh on HTTP 400, and a background keep-alive renews every session used within the last 30 days. Sessions now last 30 days after the customer's last message. Needs one LOGIN after deploy, then live verification. The "one-tap LOGIN and keep the cart" fallback is not built |
| 5 | One-reply list from voice/photo | Partly: confident items from one request go into the cart in one reply; uncertain ones are still asked one at a time, as numbered pictures when catalog images exist. Voice notes now work for every command, not only shopping |
| 6 | Retire the visible shopping list | Done in code ([ADR 6](architecture/decisions/0006-jiomart-cart-is-the-only-basket.md)): the cart is the only basket; LIST shows the cart; open product questions last 6 hours and any newer add request replaces them |
| 8 | Cancel order, invoice PDF, support hand-off | Partly: cancel is behind `JIOMART_ORDER_CANCEL` (default off) and uses documented routes that JioMart's own site may not use; INVOICE sends JioMart's tax invoice as a PDF when JioMart provides one; an order-summary PDF accompanies order confirmations. Support hand-off is not built |

Also new: every reply ends with two or three context-aware buttons; product cards have Add to cart and a View details link to the product page; checkout buttons last 30 minutes, product and order buttons 24 hours.

New failure found and fixed in code (not yet live):

| Failure seen | Root cause | Fix (in code, 19 Oct) |
|---|---|---|
| "Which bread would you like?" and "Pending choices" resurfacing after unrelated requests | Unresolved list lines stayed on the local list across requests | List retired; open questions expire after 6 hours and are replaced by any newer add request |
| Sign-in still expired every hour (`session_refresh_failed status=400`, then 401) | Refresh ran only after the token had expired, and that refresh returned HTTP 400 | Proactive refresh before expiry, cookie-only fallback, background keep-alive |

Items 2, 3, 4, 7, 9 and 10 are unchanged.

## Demo script (3 minutes)

1. "Hi" → welcome menu. 2. Hinglish voice note with 3 items → one picture cart card. 3. "Remove the rabri" → updated card. 4. CHECKOUT → Pay online → JioPay UPI. 5. "✅ Payment received" push → show the same order in the JioMart app. 6. ORDERS → TRACK 1. Keep a recorded backup.
