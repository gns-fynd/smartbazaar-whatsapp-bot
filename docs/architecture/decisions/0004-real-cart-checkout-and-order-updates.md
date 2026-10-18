# Real JioMart cart, confirmed checkout and order updates

Date: 18 October 2026. Status: implemented for the local POC behind server flags;
live acceptance pending.

## Context

The POC had a versioned local shopping list, cart reads and disabled cart-write
adapters. [ADR 1](0001-shopping-agent-memory.md) kept checkout gated until real
consumer contracts, explicit confirmation and uncertain-outcome reconciliation existed.
The owner requires a real purchase on the customer's existing JioMart account, visible
in that account, with hosted payment and a return to WhatsApp acceptable. JioMart
exposes no cart version or idempotency key to this integration. Its website places
orders through the consumer cart/checkout routes and a JioPay hosted form. WhatsApp
allows free-form business messages only within 24 hours of the customer's last message.

## Decision

Treat the account's own JioMart cart as the cart. CHECKOUT copies resolved list lines
into it, using the listing's item, size and seller (with JioMart assigning the store),
and selects the saved address. It then reads JioMart's lines, fees and total back for
review. The local list remains where the customer edits intent. Real cart edits are
explicit CART commands and planner actions.

Allow an order only from a server-issued, single-use, 10-minute button. The button
binds account, address, list version and a SHA-256 snapshot of the reviewed cart
lines, prices, availability, total and address. Offer only payment methods JioMart
returns: the JioPay hosted page (when a public HTTPS origin exists) and direct cash
on delivery. Withhold buttons for blockers and above a configurable total cap.

Persist an `order_attempts` row before the checkout call, keyed by the button. Shield
the submission from handler timeouts. Classify outcomes as definite rejection, placed,
awaiting payment or unknown. An unknown outcome blocks new orders until reconciliation
against JioMart's order list finds the order or the customer clears the check. Never
retry checkout automatically.

For online payment, keep JioMart's returned form behind a random, expiring token
and post it to the vetted Jio-domain payment endpoint from the bot's own page.
Treat the browser's return as navigation only. Confirm payment and delivery stages
only from JioMart's order record, through a background watcher. It pushes each stage
once within WhatsApp's window, holds updates outside it for the next inbound message,
and never extends the sign-in.

Gate everything with `JIOMART_CART_WRITES` and `JIOMART_CHECKOUT` (default off),
enforced in the adapter and the flow, not by the model.

## Consequences and verification

Duplicate taps, webhook redelivery, timeouts and crashes cannot submit a second order
from the same confirmation within this single process. An uncertain outcome becomes
visible and blocking instead of being retried. Reconciliation is a total-and-time
heuristic, not an upstream idempotency guarantee. JioMart's cart is shared state:
lines added in the JioMart app are shown and protected by the snapshot, not owned
by the bot. Payment links depend on a public origin. The free ngrok tunnel changes
URL on restart and shows a browser warning page before `/pay`. A stable HTTPS host
is the production direction. Updates outside the 24-hour window wait for the
customer; approved templates are not implemented. Cancellation and refunds are out
of scope.

231 local tests and Ruff passed. On 18 October a live CART read showed the real cart.
The runtime was redeployed with green readiness and both flags on, and the public pay
route was reachable. A live cart write, payment option names, any order placement,
JioPay acceptance, callback behavior and watcher pushes remain unverified. Older orders
on this account sit behind JioMart's legacy website-cookie order history and are not
visible to the bot.

See [agent architecture](../../agent-architecture.md),
[API map](../../agent-api-map.md),
[operations](../../operations-reliability.md) and
[dated status](../../project-status.md).

## Amendment — late evening, 18 October 2026

Status: implemented; first online order verified live (JM6AD4FE…, paid by UPI and
confirmed by the watcher). This amends two parts of the decision above.

### The JioMart cart is the only basket

Live testing showed the original "CHECKOUT copies the list into the cart" rule
surprising customers. Items removed from the cart came back at the next CHECKOUT
because the old list was re-synced, duplicate requests summed (3 kg of tomatoes),
and "Remove other items" deleted the customer's own cart lines.

Decision: the account's JioMart cart is the only basket. CHECKOUT reviews the cart as
it is and never adds list items. With cart writes on, add/replace/choose move only
the lines that request created or resolved into the cart, and moved lines leave the
list. A product already in the cart keeps its cart quantity. The same product
requested twice is one line at the larger quantity. ADD MY LIST is the explicit
command to move every chosen list line; CLEAR LIST and EMPTY CART clear each side
separately. "Remove other items" is retired, and stale list items are not mentioned
at checkout.

Consequences: a removed item cannot reappear behind the customer's back, and the
review shows exactly what JioMart will charge. The list becomes a holding area for
unresolved choices. Some reply copy still mentions "CHECKOUT adds your list"
(see status).

### The bot opens JioMart's payment session server-side

For this account the online option is JIOPP through the JioOnePay aggregator.
Checkout returns merchant order and transaction IDs. The payment link
(`payment_session` on `www.jiomart.com`) requires the account's JioMart sign-in,
which a browser opened from WhatsApp does not have. Live, direct browser attempts
failed ("Invalid session" or HTTP 405).

Decision: keep the merchant IDs behind the random, expiring `/pay/{token}`. When the
customer opens it, the bot POSTs them to the vetted payment link with the account's
own session cookie. It follows redirects only while they stay on JioMart hosts and
hands the browser the payment provider's form or redirect. The browser return
remains navigation only, and payment is still confirmed only from JioMart's order
record. Online payment has its own flag, `JIOMART_ONLINE_PAYMENT` (default off).

Consequences: the account cookie is sent only to JioMart hosts and never reaches
the browser. A JioMart payment session opens once and each Pay online tap creates a
new JioMart order, so a newer chat order marks older unpaid attempts `superseded`
and revokes their links. An unpaid order JioMart will not return stops being watched
after 3 hours. Whether a re-sent link can reopen a session is unverified. Live chain:
`www.jiomart.com` → `store1.jiomartjcp.com` form → `payments.jio.com`.

Related adapter changes: account requests follow one JioMart-host 307/308 redirect,
moving `/ext/…` extension paths to `www.jiomart.com` with the session cookie. Order
reads fall back to the `www.jiomart.com/api` gateway. A 401/403 triggers one token
refresh with `{"access_token": …}` and one retry; that refresh body is not yet
verified live. 261 local tests pass.
