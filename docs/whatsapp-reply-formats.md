# WhatsApp reply formats

## Implemented direction

For an active user-started conversation, use a Meta interactive reply-button message with a real JioMart catalog image, concise product name/pack/price and deterministic browse actions. This is not a pre-approved outbound notification template or a Meta catalog product message. It does not require copying JioMart inventory into a Meta catalog.

A search should show one leading candidate per query, with at most three actions: View details, Next option (when another validated candidate exists), and Search again. Alternatives must be labelled as alternatives, with the original query retained; Coke Zero must not be represented as Diet Coke. Prices are observed listings, not a checkout quote or quantity-stock promise. Product images must come from the catalog response rather than generated illustrations.

## Interaction boundary

Reply IDs are opaque and resolved against the current customer's encrypted state, not sent to the language model. Actions must expire and become invalid when authentication or location changes. Unknown, stale or another customer's buttons cannot select products. These actions browse existing search results only; cart/payment/order writes remain outside scope. A missing or rejected image should still leave usable text, with no fabricated picture.

## Meta formats and references

- [Meta reply-button request](https://www.postman.com/meta/whatsapp-business-platform/request/x0kd1at/send-reply-button): outgoing `type=interactive` uses the messages endpoint and returns a message ID whose delivery is tracked separately.
- [Meta interactive object reference](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/types/InteractiveObject/): button body up to 1024 characters, optional header/footer and reply actions. The SDK itself is archived; wire behavior is validated separately against the live API.
- [Meta pre-approved interactive templates](https://www.postman.com/meta/whatsapp-business-platform/request/lwtlz1k/send-message-template-interactive): a different `type=template` format, requiring template creation/approval and supporting quick replies or call-to-action buttons. Creating those notification templates is not part of this active-chat change.

## Acceptance

Verify the real product image and buttons arrive in WhatsApp, a tap returns the correct product details or next candidate, and the normal text/OTP flow still works. API acceptance alone is insufficient to claim recipient delivery. No image generation or checkout buttons are included in this milestone.

## Verified local and live result — 18 October 2026

45 local tests pass and Ruff is clean. A real authenticated milk search produced a Mother Dairy Cow Milk 500 ml card at the observed ₹30 listing price, with its catalog image and View details / Next option buttons. It was queued through the encrypted outbox and Meta reported `delivered`, with no delivery error. A real human button tap is requested; button handler correctness is covered locally but not yet observed end to end.

Actions are single-use and expire after 15 minutes. Missing images use text results. Synchronous card-send errors downgrade to text; asynchronous media delivery failures are recorded, not automatically retried as text. The fallback message itself preserves product and alternative wording.

The owner subsequently confirmed the product image and button interactions are working in WhatsApp, then asked about a swipeable carousel for multiple products. Carousel support is under review and is not yet implemented.

## Natural replies and quick actions — 18 October 2026

Greetings/help now reach the AI planner before or after account connection. The model
returns bounded natural prose and a presentation preference (`auto`, `text`, `buttons`,
or `product_card`). It also requests up to three supported navigation actions; it cannot
invent command IDs, URLs or capabilities. Server code chooses a valid layout based on
that preference and available evidence. Search defaults to real catalog image cards;
an explicit text presentation uses the complete text results.

`quick_reply` renders an image-free Meta interactive button message: body up to 1024
characters, optional footer up to 60, and one to three uniquely identified buttons with
titles up to 20 characters. Connect JioMart starts login for the WhatsApp sender; HELP,
LIST, HISTORY and search guidance have corresponding navigation actions. Owner-scoped
opaque `sbqa_` IDs expire after 15 minutes, are single-use and bind to current account
and location. No cart, payment, checkout or destructive button is exposed. Missing
support or synchronous send failure retains actionable plain-text fallback.

Tool results supply facts directly; planner prose never replaces list or purchase
receipts. The model can wrap a short tool result in quick actions. Long lists remain
split text without dropping lines. Product imagery is still catalog-sourced, never
generated. Tests cover signed guest ingress, real payload shape, text-only search,
context boundaries, replay/foreign/stale buttons, and fallback. The deployed build
passed 148 tests; see project status for human acceptance.

## Checkout, payment and order replies — 18 October 2026 (evening)

This supersedes the earlier "no cart, payment or checkout button" boundary when
the server's cart-write and checkout flags are on. The CHECKOUT review is a
`quick_reply` with up to three buttons: "Pay online ₹X" and/or "Cash on delivery"
(depending on JioMart's payment options) and, when the cart holds unrelated lines,
"Remove other items". Their `sbco_` IDs are single-use, expire after 10 minutes and are
bound to the account, saved address, list version and a snapshot of the reviewed cart.
Tapping a payment button places the order; the body says so. ORDERS replies use
`sbor_` buttons (Track latest, Reorder latest, Latest details) with a 15-minute expiry.

Online orders receive a Meta `cta_url` interactive message: a body of up to 1024
characters and one "Pay ₹X" link button (up to 20 characters) opening the bot's
HTTPS `/pay/{token}` page. The transport rejects non-HTTPS links and URLs carrying
credentials. The saved text fallback includes the link. Order updates (payment
confirmed, shipped, out for delivery, delivered, cancelled, payment nudge) are plain
text. They are sent only within WhatsApp's customer-service window; otherwise they are
shown with the reply to the customer's next message. Approved templates are not used.
Live delivery and taps of these new formats are not yet verified; see
[status](project-status.md).

## Late evening 18 October 2026: agentic replies, tap menus and the picture cart card

This supersedes parts of the evening section above. The "Remove other items" button is retired, and the CHECKOUT review is now normally a picture card. The online payment link now leads to a server-opened payment session. Live: the picture cart card with payment buttons was delivered, the `cta_url` Pay button led to a paid order, and composed replies passed the fact check. See [status](project-status.md).

**AI-written replies (`composer.py`).** Tool drafts are merged by the model into one reply in the customer's language. The model may also reword the lead-in (`body`) of one quick-reply, `cta_url` or cart card. A deterministic check keeps the draft unless every ₹ amount, order ID and “N × Product” line survives and no new amount appears. Button titles and IDs, URLs, list menus, product cards and text fallbacks beyond the reworded lead-in are not model-written. Login/OTP/address replies and planner-written greetings/help are never recomposed. See [decision 5](architecture/decisions/0005-agentic-reply-composition.md).

**Product choice menus (`type: list`).** When a list line has several candidate products, `ShoppingTools.next_choice` returns an interactive list message. The body is “Which X would you like?”, the button label is “Choose”, and there is one “Options” section with one row per candidate (at most three are kept, Meta allows ten). A row title is the product name (24 characters) and its description is “₹price · pack · brand” (72 characters). Row IDs are opaque `sbch_…` tokens kept in encrypted shopping memory and bound to that list version. Meta's `list_reply.id` arrives as `/button:sbch_…` and is mapped to “choose N” before any model call; unknown or stale tokens become “that choice has expired”. Typed “choose N”, “option N” and “pick N” are deterministic too. The text fallback lists numbered names and “Reply ‘choose 1’”.

**Picture cart card (`type: cart_card`).** CART and the CHECKOUT review produce a card when at least one line has a catalog image. It holds up to nine `{image_url, quantity}` items and a body of at most 1,024 characters: an intro, “• N × Name — ₹price” bullets (trimmed to “…and N more” when long), the charge breakup and closing lines. It also carries up to three buttons and a text fallback. Before sending, `WhatsApp` draws the picture with `cart_image.py`: a white JPEG grid, three per row, 240-pixel tiles with ×N badges, fetched only from `https://cdn1.jiomartjcp.com`. It uploads the picture to `/{phone_id}/media` and sends an interactive button message with an `image` header (media ID). Without buttons it sends an image with the body as caption. Missing or failed images, a failed upload or any other send error replace the outbox row with the saved text fallback. An authentication rejection is the exception: it is held in `auth_wait` like other replies.

- CART card buttons: **Checkout** (opens the review) and **Empty cart**. Both are navigation-only `sbco_` actions; the review issues its own fresh confirmation.
- Review card buttons: **Pay online ₹X** (only with `JIOMART_ONLINE_PAYMENT` on, a JIOPP option and a public origin) and/or **Cash on delivery**. Tapping one places the order. Notes such as “Already in your cart, left as it is” are sent as a separate text before the card.
- Without images the review falls back to the earlier text review plus a `quick_reply` with the same payment buttons.

**Charge lines.** Items, Discount, Coupon, Delivery, Cash on delivery fee and Convenience fee are shown when present. Unmapped JioMart charges appear as “Delivery & other charges” (the remainder, when at least ₹0.50), followed by Total.

**Pay button (`cta_url`).** The body says the order is created and to pay ₹X on JioPay within 45 minutes, then return to WhatsApp. The button is “Pay ₹X” (20 characters) linking to `/pay/{token}`, and the fallback contains the link. `/paid` shows a “Back to WhatsApp” button (`wa.me/<WHATSAPP_DISPLAY_NUMBER>`). ORDER STATUS re-sends the Pay button while the order awaits payment and its link is live.

**Welcome quick actions.** Connected help/greeting replies without model-chosen actions offer **Reorder last order**, **My cart** and **Track my order**; guests get **Connect JioMart** and **What can you do?**. The `review` action is labelled **Pending choices**.

**Read receipt and typing indicator.** For each inbound Meta message the worker sends, best effort and in the background, a `status: read` request carrying `typing_indicator: {type: text}` for that message ID. Failures are ignored.

## 19 October 2026: buttons on every reply, picture choices, product links and documents

Implemented and locally tested; not deployed or verified in WhatsApp yet (see [status](project-status.md)). This supersedes the sections above where they differ: there is no shopping list, the **Pending choices** quick action is gone, the welcome **My cart** button is now **View cart**, and button lifetimes changed.

**Buttons on every reply.** After composition, `Conversation._with_buttons` turns the last text reply (if it is at most 1,024 characters) into a `quick_reply` with two or three next steps. A longer text keeps its text and is followed by a separate "What would you like to do next?" prompt with the buttons. If the reply ends with a PDF document, the buttons go on the text before it. A reply that already ends with its own interactive message (product card, cart card, choice, menu, pay link or a card with buttons) is left as it is. The choice (`_next_steps`):

| Situation | Buttons |
|---|---|
| Guest | Connect JioMart · What can you do? |
| OTP requested, not yet entered | Resend OTP (to the number being linked) · Cancel login |
| Connected, no delivery area | Change address · What can you do? |
| After an add, choice, skip, reorder, cart edit or "keep shopping" | View cart · Checkout · Keep shopping |
| After ORDERS, ORDER STATUS, TRACK, CANCEL ORDER or INVOICE | My orders · Track my order · Keep shopping |
| After EMPTY CART | Keep shopping · Reorder last order · My orders |
| Otherwise | Keep shopping · View cart · My orders |

Guests never get account buttons. The model can still request up to three supported quick actions for a greeting or help reply; a connected greeting without them gets Reorder last order · View cart · Track my order.

**Button lifetimes.** Quick-action (`sbqa_`) and product (`sbpc_`) buttons last 24 hours (was 15 minutes); at most 40 are kept per customer, oldest dropped first. Checkout buttons (`sbco_`) last 30 minutes (was 10) and order buttons (`sbor_`) 24 hours (was 15 minutes; at most 15 kept). All are still opaque, single-use and bound to the account (and, except order buttons, to the delivery area). A payment button still places an order only if the re-read cart matches the reviewed snapshot.

**Product card.** Buttons: **Add to cart** (only with cart writes on), **View details**, and **Next option** when another validated candidate exists, otherwise **Search again**. Footer: "Listing price · Tap Add to cart to buy" (with cart writes off: "Listing price · Not added to cart"). Add to cart puts that product into the JioMart cart. View details replies with brand, pack, listing price and the product page link `https://www.jiomart.com/product/<slug>` (built by `catalog.product_link` from a plain catalog slug; the `/product/:slug` route comes from the route table in JioMart's storefront bundle). Its buttons are Add to cart · Keep shopping · View cart. The product just shown is remembered for 6 hours, so "add it" or "add this" adds it.

**Picture choices (`type: choice_card`).** When a product needs the customer's choice and all its options (at most three) have catalog images, the question is one numbered picture: `cart_image.build_collage(..., labels=["1", "2", "3"])`, uploaded as the image header of a reply-button message. The body is "Which X would you like?" with numbered lines (name, pack, price), and "(1 of N to choose)" when several items wait. Buttons are short labels such as "1. Chitale 200 g" (or "Option 1" when labels would collide), plus **Skip** when there are fewer than three options; with three, the customer can say "skip". If the images cannot be fetched or uploaded, `WhatsApp.send` sends the stored tap menu instead.

**Tap menu (`type: list`).** Used when options lack images, or as the picture fallback. Rows are the options ("name" / "₹price · pack · brand") plus a **Skip this item** row. Tokens are `sbch_…`, stored with the open question; a tapped Skip maps to SKIP. The text fallback ends "Reply ‘choose 1’ (or another number), or ‘skip’."

**Cart card and review.** CART card buttons: **Checkout · Keep shopping · Empty cart**. A review with blockers shows **Remove unavailable** (only when a line is unavailable) · **Keep shopping** · **Empty cart**, and never a payment button. A review without blockers keeps **Pay online ₹X** and/or **Cash on delivery** and says the buttons expire in 30 minutes.

**Order cards.** ORDER n (and the Latest details button) is a button card: **Track order · Invoice · Cancel order**, or **Reorder** instead of Cancel order when no shipment can be cancelled or cancellation is switched off. CANCEL ORDER n shows a confirmation card, **Yes, cancel it · Keep my order**; only that button cancels. The ORDERS list keeps Track latest · Reorder latest · Latest details.

**Documents (`type: document`).** A PDF travels through the encrypted outbox as base64 with a filename (`[A-Za-z0-9 ._-]`, ending `.pdf`), caption and text fallback. At send time `WhatsApp._document` checks it starts with `%PDF` and is at most 5 MB, uploads it to `/{phone_id}/media` and sends a `document` message by media ID. Two kinds exist:

- JioMart's tax invoice, from INVOICE n, the Invoice button, or attached to the watcher's "Delivered" push when available ("Invoice · <order>").
- The bot's own order summary (`receipt.py`): one A4 page with order ID, time (IST), payment method, lines with line totals, the charge breakup and total, labelled "not a tax invoice". Amounts print as "Rs." because host fonts may lack the ₹ glyph. It follows the cash-on-delivery "✅ Order placed" reply and the online "✅ Payment received" push ("Order summary · <order>").

A failed document send falls back to its text, which points to My Orders in the JioMart app.

**Voice replies.** A reply to a voice note starts with "🎤 “<transcript, up to 300 characters>”" so a mis-heard note is obvious; it is joined to the first text reply when short enough, otherwise sent first.

**Order pushes.** The cancelled push now reads "❌ Order X is cancelled." with a refund note for online orders. The online payment push and the Delivered push may be followed by the documents above.

## 19 October 2026: substitutes for unavailable products

Implemented and locally tested; not deployed or verified in WhatsApp yet (see [status](project-status.md)). The problem statement asks the agent to "surface personalised substitutions based on purchase history". This supersedes the review-button description above where they differ.

**When an add finds no sellable exact match.** The question uses the same picture choice or tap menu as any other product question, but its first line says plainly that it is a substitute:

| Situation | First line |
|---|---|
| JioMart lists the exact product but marks it not sellable | "Amul Masti Dahi 400 g is unavailable. Would one of these do?" |
| The customer's usual product is listed but not sellable (other exact matches remain) | "Your usual Amul Masti Dahi 400 g is unavailable. Would one of these do?" |
| The usual product didn't come back from its own name search | "I couldn’t find your usual Amul Masti Dahi 400 g near you. Would one of these do?" |
| Nothing exact was found | "I couldn’t find “amul masti curd 400 g” near you. Would one of these do?" |

Only a listing marked not sellable is called "unavailable"; a search miss is never described as a stock fact. The lines list up to three options as usual (name, pack, price, and "(you've bought this before)"), then "Tap a number to add it to your cart instead, or Skip." The menu's Skip row reads "Don’t add a substitute". Nothing is added until the customer taps an option. If no option passes the checks, the reply is "Amul Masti Dahi 400 g is unavailable. I didn’t find a close substitute, so nothing was added. Tell me another brand or pack size and I’ll look again."

**Which options.** At most three. Anything the customer has bought before (from their own JioMart orders, or an order placed from this chat) comes first. When the search didn't return it, the bot looks up one such product by name. Products in the unavailable product's own JioMart category come next, then the model's alternatives, then search order. Every option passes `catalog.product_is_substitute`: the existing relevance check (no keychains or syrup, milk only from dairy, the Diet Coke rule), any requested diet/zero/sugar-free formulation, a different product, and the same kind. "Same kind" means JioMart's own category when both listings have one, so butter is never offered for dahi. Otherwise the requested product noun must be the option's own noun or appear in its category, so "Amul Milk Chocolate" is never offered for milk.

**At checkout.** A blocked review with unavailable lines now says "N item(s) can’t be delivered right now. Find substitutes for them, or remove them first." Its buttons are **Find substitutes · Remove unavailable · Keep shopping**; Empty cart drops off because a message holds three buttons. EMPTY CART still works as text. The CART text hint adds ‘find substitutes’. Find substitutes (also typed: "find substitutes", "substitutes", "find alternatives") searches each unavailable line by its name, at most three lines, and asks about one line at a time:

> Amul Fruit & Nut Dark Chocolate 150 g is unavailable. Would one of these do?
> 1. Cadbury Dairy Milk Silk Chocolate Bar 60 g — 60 g · ₹80 (you've bought this before)
> 2. Cadbury Dairy Milk Fruit & Nut Chocolate 80 g — 80 g · ₹95
> Tap a number to replace it in your cart, or Skip.

The Skip row reads "Leave your cart as it is". A tap adds the chosen product and, only once it is in the cart, removes that one unavailable line: "🛒 Added to your JioMart cart: • 1 × … / Removed Amul Fruit & Nut Dark Chocolate 150 g (unavailable) from your cart." The substitute takes the unavailable line's quantity. Skip replies "No substitute for X. It stays in your cart, marked unavailable." Lines without a close option are named in one message that points to ‘remove unavailable items’. "Nothing in your JioMart cart is marked unavailable right now." answers a request when no line is unavailable.

**Guards kept.** The Find substitutes button is an `sbco_` checkout button: single-use, account- and address-bound, 30 minutes. Option taps are `sbch_` tokens on the open question. Substitute questions are ordinary `memory["pending"]` questions, so any newer add request or Find substitutes replaces them, and an old tap gets "That choice is from an earlier request." No list exists beyond the JioMart cart.

## PRD polish — 19 October 2026

- **Photo lists, line by line.** `media._image_shopping_text` returns `PhotoList`: a `str` (`buy a; b`) with `.items` (clearly read lines, up to 12, with pack quantities) and `.unclear` (best guesses for lines it couldn't read). `Conversation._photo_list` adds the clear lines through the normal add path without a planner call and starts with "📝 Read N item(s) from your list." It then asks about unclear lines ("I couldn’t read 1 line(s) clearly: “chini?”. Type what they are…") before any picture question. A photo with nothing legible keeps the "send a sharper photo" message. Lines that look like commands are dropped.
- **AI disclosure.** Once a day per customer, the first button message carries the grey footer "🤖 I'm SmartBazaar's AI shopping assistant." (in the customer's language). The reply text is unchanged.
- **Changed cart at payment.** When a payment button's reviewed snapshot no longer matches the cart, the bot says the cart changed (nothing was ordered) and shows the new review with fresh buttons. It no longer asks the customer to send CHECKOUT.
- **Language.** `state["lang"]` (`en`, `hi`, `hinglish`) comes from each typed or spoken message (`i18n.detect`, refined by the planner's `Intent.language`). Taps keep the last value. Button titles (`i18n.label`, ≤ 20 characters), the product card heading and footer, choice-card frames, cart-card intro and edit hint, and the blocked/ready review headings follow it. Product cards read "Here's a good match for curd 👇 / *Heritage Total Curd 1 kg* / ₹69", with "Price for 400083" in the footer. After a tap, the composer gets the last language so AI-written text follows it too.
