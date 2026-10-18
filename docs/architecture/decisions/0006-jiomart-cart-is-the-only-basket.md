# The JioMart cart is the only basket

Date: 19 October 2026. Status: implemented and locally tested (307 tests); not yet
deployed to the supervised runtime and not verified on WhatsApp.

## Context

[ADR 1](0001-shopping-agent-memory.md) introduced a versioned local shopping list
(`memory["draft"]`) with one pending product choice. [ADR 4](0004-real-cart-checkout-and-order-updates.md)
made the account's real JioMart cart the place orders come from, and its late-evening
amendment stopped CHECKOUT from copying the list. Two baskets still existed: resolved
lines moved into the cart, but unresolved lines ("1 × bread — choose a product") stayed
on the list. The owner reported that these lines lingered across requests and resurfaced
later ("Pending choices", "Which bread would you like?" after an unrelated request).
Customers think in terms of a cart, not a list, and a second basket made every reply
explain which one it meant.

## Decision

Retire the local shopping list. The customer's real JioMart cart is the only basket.

- Confident matches from an add request, a tapped product card, "add it" for the
  product just shown, a numbered choice or REORDER go straight into the cart through
  `CheckoutFlow.add_products`. Products already in the cart are left as they are, and
  the same product asked for twice is one line at the larger quantity.
- A product that needs the customer's choice becomes a short-lived question in
  `memory["pending"]`: at most three options per item, asked one at a time, bound to
  the pincode, expiring after 6 hours. Any newer add request replaces all open
  questions. A location change, CANCEL, "clear list"/"skip all" and session rotation
  after expiry also drop them. Nothing unanswered is carried into a later request.
- LIST and "my list" show the JioMart cart. The ADD MY LIST, `add_list`, `clear_list`,
  `replace`, `review`, `remove` and `set_quantity` planner actions are retired. Cart
  edits go through the existing `cart_remove`/`cart_quantity` actions and CART commands.
- Checkout buttons no longer carry a list version. The reviewed-cart snapshot hash is
  the only version they bind to.
- `roll_session` deletes any `draft`/`choices` left by older builds; there is no
  migration step.

## Consequences

Older requests cannot resurface, and replies talk about one basket. The cart can
contain items the customer added in the JioMart app; they are shown and reviewed as
part of the same basket. A question the customer ignores for 6 hours, or answers after
asking for something else, is gone and must be asked again. Every add now writes to
the real cart immediately, so the cart-write flag matters more: with
`JIOMART_CART_WRITES` off the bot cannot hold any basket and says adding is not
switched on. Product details for "remember cart line 2 for curd" are kept separately in
`memory["cart_products"]` (at most 40 entries). After an order, `clear_ordered_lines`
only records the order in `memory["orders"]`; JioMart empties its own cart.

This supersedes ADR 1's local-draft basket and the parts of ADR 4 that describe list
lines, ADD MY LIST and list-version binding. ADR 4's confirmation, persisted-attempt
and unknown-outcome rules are unchanged.

Verification: the shopping-tools, commerce-tools, checkout and conversation tests
cover adds, questions, skipping, replacement by a newer request, expiry and the retired
commands. Seven synthetic calls to the configured live planner on 19 October routed
"add it and checkout", "one more milk please", "I don't want bread", a Hindi two-item
add and "show my list" as intended. Live WhatsApp behavior is unverified.

See [agent architecture](../../agent-architecture.md),
[reply formats](../../whatsapp-reply-formats.md) and
[status](../../project-status.md).
