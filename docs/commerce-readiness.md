# Jio Commerce setup and capability check

**Historical development experiment — superseded as the POC route on 18 October 2026.** The user requires an existing JioMart consumer account and a real order in that same account. Continue with the [JioMart account integration plan](jiomart-account-integration-plan.md). Do not resume the development checkout or request its missing test mobile. The observations below are retained as evidence, not as the active implementation plan.

Observed on **17 October 2026**. This is an access and documentation assessment with bounded development API reads, not a T9G8 or live-order integration test.

## Result so far

The public documentation describes the commerce areas required by the shopping POC. The user-created partner organization **SmartBazaar whatsapp bot** is verified (organization ID `6ad2944aa84d821e2270bdfb`). Its development company **SmartBazaar whatsapp bot**, **Company ID 153**, is now verified in both the company listing and platform dashboard. The dashboard explicitly labels it DEVELOPMENT COMPANY and links the sales channel `https://sb-wh-ganesh.jiocommerce.io`.

On 17 October 2026, development Company **153**, application **`6ad295642243111f24dd498f`**, and store **2962 / HS-e93f6** were verified. The created **SmartBazaar POC Development** client authenticates against **`https://api.jiocommerce.io`**. A synthetic brand **POC Test Groceries** (24868) and product **POC Test Rice 1 kg** (82146727; SKU `POC-RICE-1KG-001`, size OS) were created. Store stock is **20**, MRP **₹100**, selling price **₹90**. Product, brand and inventory API read-backs returned **200 with one item** after stocking. The storefront guest cart was tested at quantity 1 → 2 → 1, with totals ₹90 → ₹180 → ₹90. No order was placed.

Accepted/Pending managed-company lists were empty; development Company 153 does not establish access to T9G8. Customer UI shows no customers. Guest checkout reaches an address form requiring a mobile number; Google Maps reports a configuration/loading error. Customer history, direct storefront API session setup, payment, order creation and idempotency remain unverified.

## Verified setup observations

| Check | Observation | Implication |
| --- | --- | --- |
| Jio Commerce partner portal | Organization SmartBazaar whatsapp bot dashboard is visible | Organization creation verified; this organization ID is not a company ID |
| Company discovery | Development listing now shows one company, ID 153; Accepted/Pending managed lists were empty | Development-company access is verified, but target-company access is not |
| Development company | User saved SmartBazaar whatsapp bot, Company ID 153; dashboard explicitly says DEVELOPMENT COMPANY | Verified at https://platform.jiocommerce.io/company/153/home/ |
| Sales channel | Dashboard names SmartBazaar whatsapp bot and links https://sb-wh-ganesh.jiocommerce.io | API application `6ad295642243111f24dd498f` is confirmed |
| Catalog readiness | One synthetic brand and rice product created; app product/brand/inventory reads each return one item | Useful for a bounded fixture test, not representative T9G8 assortment |
| Managed-company access route | Request Company Access form requires Company Id and role/channel permissions | T9G8 store code is insufficient; no access request was submitted |
| Live Fynd connector | No callable Fynd commerce server tools were exposed in this session | No connector-derived company/store access claim is possible |
| Local runtime | Node.js v22.23.1 and npm 10.9.8 are installed; `fdk` was not found | Node satisfies the documented v18+ requirement; JCP CLI remains unavailable |
| Official CLI repository | Read-only SSH `git ls-remote` to `GoFynd/JCPLibraries/fdk-cli` returned TF401019: repository does not exist or permission is missing | Current SSH access cannot retrieve the documented CLI; the error does not distinguish missing repository from missing permission |
| Runtime host and token | `https://api.jiocommerce.io`; token response status 200 with standard token fields | Secret is excluded from the repository; development token scope is recorded only in sanitized evidence |
| API validation | Company profile observed route, applications, stores, brands, products, inventory, and shipments were read | Company profile documented route and app locations route returned 404; see `work/commerce-validation/results.json` |

No secrets, OTPs, or customer records were collected into this file. Meta setup was not rechecked.

Historical setup note: the user authorized any test address in Andheri, Mumbai. The saved development location was verified in Andheri East, Mumbai, pincode 400069; contact values are not copied into this report. The user saved the company with name/domain shown above. Do not recreate it or reuse the earlier unsaved domain proposal. No secrets, OTPs, or customer records are retained here.

## Documented onboarding

The [official extension guide](https://partners-doc.jiocommerce.io/help/docs/partners/extension/general/get-started/) requires a partner account and a development company. The documented development-company path is: choose organization → Companies → Development → Add → fill required details → Save. A development company supplies test scope; it does not grant access to SmartBazaar T9G8.

The guide requires the JCP-specific FDK CLI from the official Azure DevOps repository. It explicitly says the public `@gofynd/fdk-cli` npm package is a separate, incompatible release line. Do not substitute it. The guide describes `fdk extension init` for private extension setup and `fdk populate` for a one-time predefined product dataset in the selected development company.

Choose the access route after inspecting the actual account. A direct company client may avoid extension boilerplate if supported and authorized for the target company. Creating both is unnecessary until a requirement justifies it.

## Historical account validation sequence — not the current plan

1. Sign in to the intended Jio Commerce partner/platform account; inspect accessible organizations and companies.
2. Resolve whether T9G8 is accessible, including actual company, application/channel, and store identifiers. Do not select an unrelated company as a substitute.
3. If needed, create a clearly named development company for simulated/test integration and record that environment separately.
4. Establish the supported API version, environment host, and the minimum necessary authentication scope. Record identifiers and credential storage location only; never record secret values.
5. Test catalog, sellable variants, store stock/pricing, authorized customer history, and cart/quote behavior in that verified scope.
6. Establish payment, serviceability, test-order fulfilment behavior, and order lookup/idempotency before a test checkout.
7. Record each operation as successful, failed, or untested, with redacted evidence. An empty response is not proof of useful data coverage.

## Published API capability map — remaining pilot validation

| Capability | Documentation evidence | Verification still required |
| --- | --- | --- |
| Catalog | [Storefront catalog](https://partners-doc.jiocommerce.io/help/docs/sdk/latest/application/catalog): product listing, detail, variants, and sizes | T9G8 assortment, identifier/pack mapping, and search results |
| Stock/pricing | Same catalog reference describes stock-status and seller operations | Store-level scope, serviceability, current quantity, and effective price |
| Purchase history | [Storefront order reference](https://partners-doc.jiocommerce.io/help/docs/sdk/latest/application/order): `GET /service/application/order/v1.0/orders` and `GET /service/application/order/v1.0/orders/{order_id}` | Authenticated customer context and whether the test user's JioMart history is visible in this application |
| Cart | [Official Fynd cart reference](https://docs.fynd.com/partners/commerce/sdk/latest/application/cart): `POST` and `PUT /service/application/cart/v1.0/detail`; `HEAD` on the detail route exposes cart modification information | Equivalent JCP pilot-version contract, session/bootstrap, valid products, totals, stock errors, and version handling |
| Checkout | Same Fynd cart reference: `POST /service/application/cart/v2.0/checkout` places the order | Supported JCP version, required address/payment fields, sanctioned test mode, and idempotency behavior; do not also create a second order |
| Order lookup | Storefront order detail plus [Platform order reference](https://partners-doc.jiocommerce.io/help/docs/sdk/latest/platform/application/order) | External reference lookup and reconciliation after an unknown submission result |

The cart route evidence above comes from official Fynd documentation and requires compatibility verification against the JCP pilot version. The development storefront UI successfully added and updated a guest cart; direct storefront API authentication/session handling has not been reproduced by the local probe. Some official Platform examples use `api.fynd.com`; that example host must not be adopted as the T9G8 host without verification.

The [Platform API documentation](https://partners-doc.jiocommerce.io/help/docs/sdk/latest/platform/client-libraries) exposes `v1.10.3` in its platform-version selector. This establishes a documented version option, not the actual T9G8 deployment version. Company client credentials and extension OAuth are documented access routes. Storefront customer-history and checkout calls require a validated customer/application authentication context; company credentials alone do not establish that identity.

## Minimum useful data checks

| Area | Proof needed |
| --- | --- |
| Catalog | Real sellable product IDs, brand, pack/variant, unit, and search coverage for representative grocery requests |
| Stock and pricing | Requested sellable unit mapped to T9G8, current availability, authoritative currency/price, and freshness |
| Customer/history | Authorized test customer mapping and item-level orders that map to catalog products; explicit evidence if no history exists |
| Cart/quote | Add, update, remove, totals/charges, cart version, stock/quantity errors, and checkout requirements |
| Checkout/order | Sanctioned test flow, accepted external order reference, retrievable result, and safe handling of ambiguous responses |

See the [technical PRD](technical-prd.md) for the product contract and [project status](project-status.md) for other integration dependencies.

## Development client and sanitized validation

Name: **SmartBazaar POC Development**. Company: **153**. Custom company groups selected: **Profiles, Sales Channels, Products, Orders**. Application selection: **Specific Sales Channels**, only **SmartBazaar whatsapp bot**, with **Customer and Orders**. The client is created and a development token returned HTTP 200; no secret value is recorded. The UI exposes grouped permissions rather than a read/write split, so this must not be described as a read-only credential.

The sanitized validation evidence is in [`work/commerce-validation/results.json`](../work/commerce-validation/results.json). It records Company 153, application `6ad295642243111f24dd498f`, store UID `2962` / code `HS-e93f6`, initially empty lists, later product/brand/inventory read-backs with the synthetic fixture, empty shipments, and the observed company-profile route. The created client secret is stored in the git-ignored local `secrets/jcp-company-153.json` file (file mode 600, parent directory 700) and must not be read into context.


## Executed checks and remaining gaps

All paths below use `https://api.jiocommerce.io`. `{app}` is `6ad295642243111f24dd498f`. API response bodies and tokens are not retained in documentation.

| Check | Result |
| --- | --- |
| POST `/service/panel/authentication/v1.0/company/153/oauth/token` | 200; company client credentials flow |
| GET `/service/platform/configuration/v1.0/company/153/application` | 200; one application |
| GET `/service/platform/configuration/v1.0/company/153/application/{app}/stores` | 200; store 2962, HS-e93f6 |
| GET `/service/platform/company-profile/v1.0/company/153/` | 200; route observed in admin UI |
| GET `/service/platform/catalog/v2.0/company/153/products` | 200; created product 82146727 read back |
| GET `/service/platform/catalog/v1.0/company/153/application/{app}/products` | 200; one fixture with INR marked 100/effective 90 |
| GET `/service/platform/catalog/v1.0/company/153/application/{app}/brands` | 200; brand 24868 after stocking product |
| GET `/service/platform/catalog/v1.0/company/153/application/{app}/inventory` | 200; stock 20, size OS, store 2962 |
| GET `/service/platform/order/v1.0/company/153/application/{app}/shipments` | 200; empty before any test checkout |
| Public-doc company profile `/service/platform/catalog/v2.0/company/153` | 404; do not copy as working runtime route |
| Public-doc app locations `/service/platform/catalog/v2.0/company/153/application/{app}/locations` | 404; configuration stores route worked |
| Storefront product/serviceability | Product and ₹90 price visible; pincode 400069 accepted with delivery estimate |
| Storefront cart add/update | Guest cart 1 unit ₹90; increment 2 units ₹180; decrement 1 unit ₹90 |
| Guest checkout prerequisites | Address form opens; mandatory name/mobile/address, optional email; Maps loading error; no address submitted |
| Customer history/order | No customers shown in admin UI; no order placed, history unavailable |

The product-detail and cart screens displayed different delivery dates for the same pincode; delivery-promise consistency remains unvalidated. The product uses the default missing-image placeholder, so catalog image quality is not validated. Remove-item, out-of-stock/over-quantity, cart version, payment and duplicate-order behavior are still untested.

The local one-off stdlib probe lives under ignored `work/commerce-validation/`; it is not a shopping-agent adapter. Use `/usr/bin/python3` on this machine: macOS System Keychain validates its existing Check Point/Perimeter81 trust chain. Conda Python's PEM bundle failed issuer verification; no TLS verification was disabled or trust setting changed. `fdk` remains unnecessary for these REST checks.

These were the development experiment's remaining gaps. They are no longer the next tasks. The active dependency is supported access to the actual JioMart consumer application and its customer-login/session contract, followed by proof of the same account, real catalog/cart and a confirmed real order. See the [current integration plan](jiomart-account-integration-plan.md).
