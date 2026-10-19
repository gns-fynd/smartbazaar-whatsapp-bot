from smartbazaar import i18n


def test_every_translated_button_fits_whatsapps_20_character_limit():
    for english, translations in i18n.LABELS.items():
        for language in i18n.LANGUAGES:
            assert len(i18n.label(english, {"lang": language})) <= 20, (english, language)
    assert i18n.label("Pay online ₹2,849", {"lang": "hinglish"}) == "Online pay ₹2,849"


def test_language_detection_and_phrases():
    assert i18n.detect("do amul doodh aur ek bread dal do") == "hinglish"
    assert i18n.detect("दो अमूल दूध") == "hi"
    assert i18n.detect("show my cart") == "en"
    assert i18n.detect("/button:sbqa_x") is None and i18n.detect("ok") is None
    assert i18n.phrase("which", {"lang": "hinglish"}, query="dahi") == "Kaunsa dahi chahiye?"
    assert i18n.phrase("which", {}, query="curd") == "Which curd would you like?"
