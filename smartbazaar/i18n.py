"""The customer's language for fixed texts: button labels and card frames.

AI-written replies already follow the customer's language. Button titles and the few fixed
lines on cards don't go through the AI, so they're translated here. ``state["lang"]`` is
``en``, ``hi`` (Hindi in Devanagari) or ``hinglish`` (Hindi in Latin letters), updated from
each typed or spoken message; taps keep the last one. WhatsApp button titles stay ≤ 20
characters in every language.
"""

import re

LANGUAGES = ("en", "hi", "hinglish")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Common Hindi words written in Latin letters; two or more means the message is Hinglish.
_HINGLISH = re.compile(
    r"\b(?:hai|hain|ho|karo|karna|kar|kardo|chahiye|chaiye|mujhe|mera|meri|mere|aur|kya|nahi|nahin|"
    r"daal|dal|do|de|dena|bhai|bhaiya|ek|teen|char|paanch|wala|wali|kitna|kitne|abhi|bhi|"
    r"liye|ke|ka|ki|mein|bhejo|dikhao|dekho|hata|hatao|jaldi|haan|ji|thoda|zyada)\b",
    re.I,
)

LABELS: dict[str, dict[str, str]] = {
    "Connect JioMart": {"hi": "JioMart जोड़ें", "hinglish": "JioMart connect karo"},
    "What can you do?": {"hi": "आप क्या करते हैं?", "hinglish": "Aap kya karte ho?"},
    "My orders": {"hi": "मेरे ऑर्डर", "hinglish": "Mere orders"},
    "Keep shopping": {"hi": "और खरीदें", "hinglish": "Aur shopping"},
    "Reorder last order": {"hi": "पिछला ऑर्डर दोबारा", "hinglish": "Last order dobara"},
    "View cart": {"hi": "कार्ट देखें", "hinglish": "Cart dekho"},
    "Checkout": {"hi": "चेकआउट", "hinglish": "Checkout karo"},
    "Track my order": {"hi": "ऑर्डर ट्रैक करें", "hinglish": "Order track karo"},
    "Change address": {"hi": "पता बदलें", "hinglish": "Address badlo"},
    "Cancel login": {"hi": "लॉगिन रद्द करें", "hinglish": "Login cancel karo"},
    "Resend OTP": {"hi": "OTP दोबारा भेजें", "hinglish": "OTP dobara bhejo"},
    "Add to cart": {"hi": "कार्ट में डालें", "hinglish": "Cart mein daalo"},
    "View details": {"hi": "विवरण देखें", "hinglish": "Details dekho"},
    "Next option": {"hi": "अगला विकल्प", "hinglish": "Agla option"},
    "Search again": {"hi": "फिर से खोजें", "hinglish": "Phir se dhoondo"},
    "Empty cart": {"hi": "कार्ट खाली करें", "hinglish": "Cart khaali karo"},
    "Remove unavailable": {"hi": "अनुपलब्ध हटाएँ", "hinglish": "Unavailable hatao"},
    "Find substitutes": {"hi": "विकल्प दिखाएँ", "hinglish": "Doosre option"},
    "Skip": {"hi": "छोड़ें", "hinglish": "Chhodo"},
    "Skip this item": {"hi": "यह छोड़ें", "hinglish": "Ye chhodo"},
    "Cash on delivery": {"hi": "कैश ऑन डिलीवरी", "hinglish": "Cash on delivery"},
    "Track latest": {"hi": "ताज़ा ट्रैक करें", "hinglish": "Latest track karo"},
    "Reorder latest": {"hi": "ताज़ा दोबारा", "hinglish": "Latest dobara"},
    "Latest details": {"hi": "ताज़ा विवरण", "hinglish": "Latest details"},
    "Track order": {"hi": "ऑर्डर ट्रैक करें", "hinglish": "Order track karo"},
    "Invoice": {"hi": "इनवॉइस", "hinglish": "Invoice"},
    "Cancel order": {"hi": "ऑर्डर रद्द करें", "hinglish": "Order cancel karo"},
    "Reorder": {"hi": "दोबारा मँगाएँ", "hinglish": "Dobara mangao"},
    "Yes, cancel it": {"hi": "हाँ, रद्द करें", "hinglish": "Haan, cancel karo"},
    "Keep my order": {"hi": "ऑर्डर रखें", "hinglish": "Order rehne do"},
    "Choose": {"hi": "चुनें", "hinglish": "Chuno"},
}

# Fixed lines on cards. ``{name}`` style fields are filled in by the caller.
PHRASES: dict[str, dict[str, str]] = {
    "ai_note": {
        "en": "🤖 I'm SmartBazaar's AI shopping assistant.",
        "hi": "🤖 मैं SmartBazaar का AI शॉपिंग असिस्टेंट हूँ।",
        "hinglish": "🤖 Main SmartBazaar ka AI shopping assistant hoon.",
    },
    "which": {
        "en": "Which {query} would you like?",
        "hi": "कौन सा {query} चाहिए?",
        "hinglish": "Kaunsa {query} chahiye?",
    },
    "would_do": {"en": "Would one of these do?", "hi": "इनमें से कोई चलेगा?", "hinglish": "Inmein se koi chalega?"},
    "of_n": {"en": " (1 of {n} to choose)", "hi": " ({n} में से पहला)", "hinglish": " ({n} mein se pehla)"},
    "tap_or_skip": {
        "en": "Tap a number to add it to your cart, or Skip.",
        "hi": "नंबर दबाइए, मैं कार्ट में डाल दूँगा। या छोड़ें दबाइए।",
        "hinglish": "Number dabaiye, main cart mein daal dunga. Ya Chhodo dabaiye.",
    },
    "tap_or_say_skip": {
        "en": "Tap a number to add it to your cart, or say ‘skip’.",
        "hi": "नंबर दबाइए, मैं कार्ट में डाल दूँगा। या ‘skip’ लिखिए।",
        "hinglish": "Number dabaiye, main cart mein daal dunga. Ya ‘skip’ likhiye.",
    },
    "bought_before": {"en": " (you've bought this before)", "hi": " (आपने पहले लिया है)", "hinglish": " (aapne pehle liya hai)"},
    "match": {"en": "Here's a good match for {query} 👇", "hi": "{query} के लिए यह मिला 👇", "hinglish": "{query} ke liye ye mila 👇"},
    "alternative": {
        "en": "Couldn't find exactly {query}, but this is close 👇",
        "hi": "बिल्कुल {query} नहीं मिला, पर यह मिलता-जुलता है 👇",
        "hinglish": "Exact {query} nahi mila, par ye milta-julta hai 👇",
    },
    "card_footer": {
        "en": "Price for {pincode} · Tap Add to cart to buy",
        "hi": "{pincode} का दाम · खरीदने के लिए कार्ट में डालें",
        "hinglish": "{pincode} ka daam · Cart mein daalo",
    },
    "cart_items": {"en": "🛒 These are your cart items:", "hi": "🛒 आपके कार्ट में:", "hinglish": "🛒 Aapke cart mein:"},
    "cart_edit_hint": {
        "en": "To change anything, say ‘remove 2 from cart’ or ‘make 1 two in cart’.",
        "hi": "बदलने के लिए लिखिए ‘remove 2 from cart’ या ‘make 1 two in cart’।",
        "hinglish": "Badalne ke liye likhiye ‘remove 2 from cart’ ya ‘make 1 two in cart’.",
    },
    "ready_to_order": {
        "en": "🧾 Ready to order. Please check and choose how to pay:",
        "hi": "🧾 ऑर्डर तैयार है। एक बार देख लीजिए और पेमेंट चुनिए:",
        "hinglish": "🧾 Order ready hai. Ek baar dekh lijiye aur payment chuniye:",
    },
    "cant_order": {
        "en": "⚠️ Can’t place this order yet:",
        "hi": "⚠️ यह ऑर्डर अभी नहीं हो सकता:",
        "hinglish": "⚠️ Ye order abhi nahi ho sakta:",
    },
    "in_your_cart": {"en": "In your cart:", "hi": "आपके कार्ट में:", "hinglish": "Aapke cart mein:"},
    "cart_changed": {
        "en": "Your cart changed since that review (price or stock), so here’s the updated one. Nothing was ordered.",
        "hi": "पिछली बार के बाद आपका कार्ट बदल गया (दाम या स्टॉक), इसलिए नया देखिए। कोई ऑर्डर नहीं हुआ।",
        "hinglish": "Pichhli baar ke baad cart badal gaya (daam ya stock), toh naya dekhiye. Koi order nahi hua.",
    },
    "photo_read": {
        "en": "📝 Read {n} item(s) from your list.",
        "hi": "📝 आपकी लिस्ट से {n} चीज़ें पढ़ीं।",
        "hinglish": "📝 Aapki list se {n} cheezein padhi.",
    },
    "photo_unclear": {
        "en": "I couldn’t read {n} line(s) clearly: {lines}. Type what they are and I’ll add them.",
        "hi": "{n} लाइनें साफ़ नहीं पढ़ पाया: {lines}। लिखकर बताइए, मैं जोड़ दूँगा।",
        "hinglish": "{n} line(s) saaf nahi padh paaya: {lines}. Likh ke bataiye, main add kar dunga.",
    },
}


def detect(text: str) -> str | None:
    """Best guess of the language a customer wrote or spoke in, or None for no signal."""
    if not isinstance(text, str) or not text.strip() or text.startswith("/button:"):
        return None
    if _DEVANAGARI.search(text):
        return "hi"
    if len(_HINGLISH.findall(text)) >= 2:
        return "hinglish"
    return "en" if re.search(r"[A-Za-z]{3,}", text) else None


def language(state: dict) -> str:
    lang = state.get("lang") if isinstance(state, dict) else None
    return lang if lang in LANGUAGES else "en"


def label(text: str, state: dict) -> str:
    """A button title in the customer's language (≤ 20 characters), else the English one."""
    lang = language(state)
    if lang == "en":
        return text
    if text.startswith("Pay online "):
        return {"hi": "ऑनलाइन दें ", "hinglish": "Online pay "}[lang] + text.removeprefix("Pay online ")
    return LABELS.get(text, {}).get(lang, text)[:20]


def phrase(key: str, state: dict, **fields) -> str:
    lang = language(state)
    template = PHRASES[key].get(lang) or PHRASES[key]["en"]
    return template.format(**fields)
