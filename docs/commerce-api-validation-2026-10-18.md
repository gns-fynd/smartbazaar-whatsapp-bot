# Actual JioMart API validation — 18 October 2026

Sanitized record of API-only tests authorized in this chat. No browser automation was used for these OTP/address/search tests. No cart write, payment or order was performed. This records limited live observations, not a deployed integration or a stable third-party API guarantee.

## Verified observations

| Test | Result |
| --- | --- |
| Real consumer environment discovery | Public JioMart client configuration identified `https://api.jiomartjcp.com`, application `685945f46c8c7aee3f3af605`; storefront client uses `https://www.jiomart.com/api` |
| Start login | `POST /service/application/user/authentication/v1.0/login/otp` returned HTTP 200, `success: true`, “OTP sent” and a challenge reference |
| Verify login | `POST /service/application/user/authentication/v1.0/otp/mobile/verify` returned HTTP 200, `user_exists: true` and user data; session cookies issued |
| Verify account | `GET /service/application/user/authentication/v1.0/session` returned HTTP 200; requested account and application matched |
| Refresh | `POST /service/application/user/authentication/v1.0/token/refresh` returned HTTP 200 and refreshed session material |
| Addresses | `GET /service/application/cart/v1.0/address` returned HTTP 200 and one saved default address; its pincode was used for search |
| Search | Catalog GET redirected to a storefront extension; supplying location context enabled HTTP 200 product results |

OTP values, phone numbers, full addresses, user IDs, cookies, application tokens and access tokens are intentionally omitted. Session material was kept in the transient testing runtime, not saved in these documents or the product captures. This manual test does not implement production OTP routing, persistence or refresh handling.

## Search contract observed

The logical request was:

```http
GET https://www.jiomart.com/api/service/application/catalog/v1.0/products?q=diet%20coke&page_size=50&page_no=1
```

The server redirected to:

```http
GET https://www.jiomart.com/ext/vertex/application/api/v1.0/products?q=diet%20coke&page_size=50&page_no=1
```

Without a location header the extension returned HTTP 400, `invalid pincode`. `x-location-detail` containing country, country ISO code, pincode, city and state from the saved default address enabled the search; the tested delivery pincode was **400053**. No street address or coordinates are needed in this evidence document.

Following the extension redirect on the bare API host returned 404; the observed storefront gateway route worked. The adapter must handle verified host-specific routes rather than blindly treating every relative redirect as portable. Public documentation describes application authentication differently from the live client's encoded application bearer header; isolate and validate the actual transport contract. Do not hardcode or publish application/session credentials from the test.

The response contained `filters`, `items`, `page`, `sort_on` and `meta`. It reported **64 total results**, but returned **12 items** despite `page_size=50`. A request using the advertised `next_id` as `page_id` repeated those same items. Pagination is therefore unresolved; 64 distinct products were not collected. The user asked to stop at the top 10, which were saved with all original product fields.

Each saved product had 38 top-level fields, including SKU/item codes, product UID and slug, brand, attributes/categories, images, prices/MRP, discounts, quantity/pack descriptors, seller/store IDs, sizes/variants, rating, tags and `sellable`. A sellability flag is not proof of requested stock quantity, final serviceability, delivery promise or checkout total.

The first results included Coke Zero, Coca-Cola No Sugar, regular Coke, a Diet Cokke keychain, Pepsi Black, syrup and energy drinks. None of this proves Diet Coke is unavailable; it demonstrates that keyword search is broad and exact-product validation is necessary.

Product-only capture directory (git-ignored; UTC timestamp in name, local observation date 18 October):

`work/catalog-search/diet-coke-2026-10-17T23-40-12-808Z/`

- `diet-coke-top-10-full-products.json`: first 10 product objects, all fields preserved; read-back equality checked.
- `search-page-1.raw.json`: original first-page response, including 12 items and metadata.
- `per-product/`: individual full product objects.
- `capture-info.json`: non-secret request/selection metadata; no authentication headers.

These ignored captures may not accompany a future checkout of the repository; this sanitized record preserves the verified conclusions without requiring customer/session data.

## History remains unresolved

The customer order endpoint returned HTTP 200 with zero items and no next page. The API's filters initially indicated a recent-history default. Explicit wider date ranges, the all-status value, storefront gateway access and session refresh did not yield past orders. A previously observed shipment lookup returned 404. These observations do not prove that the customer has no purchases, nor identify the cause of the data/access mismatch.

One live schema discrepancy: an ISO-formatted `start_date` was rejected with an error requiring `%m-%d-%Y`; the formatted retry still returned zero orders. Record this in contract tests rather than assuming the latest generic docs match every production route.

## Not yet established

Quantity-aware stock and eligible sellers, customer cart/quote writes, final fees, payment methods/links, checkout behavior, idempotency, order reconciliation, same-account new-order visibility, historical-order access, T9G8/store mapping, supported third-party deployment access and long-lived session behavior all remain to be validated. WhatsApp two-way messaging was not tested in these API probes.

Use this milestone to begin the [agent experience/build plan](whatsapp-agent-experience-plan.md). Preserve the real-account requirement and the [integration plan](jiomart-account-integration-plan.md); do not resume the historical development-store path.
