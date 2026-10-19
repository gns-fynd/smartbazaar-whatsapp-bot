"""Private runtime configuration. Never serialize Settings into logs."""

import re
from functools import cached_property

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-6.1-sol"
    meta_access_token: SecretStr = SecretStr("")
    meta_app_secret: SecretStr = SecretStr("")
    meta_phone_number_id: str = ""
    meta_waba_id: str = ""
    meta_app_id: str = ""
    meta_graph_version: str = "v25.0"
    channel_check_interval_seconds: int = Field(default=300, ge=60, le=3600)
    webhook_verify_token: SecretStr = SecretStr("")
    channel_provider: str = "meta"
    openwa_base_url: str = ""
    openwa_api_key: SecretStr = SecretStr("")
    openwa_session_id: str = ""
    openwa_webhook_secret: SecretStr = SecretStr("")
    session_encryption_key: SecretStr = SecretStr("")
    allowed_whatsapp_numbers: str = Field(default="", repr=False)
    jiomart_application_id: str = "685945f46c8c7aee3f3af605"
    jiomart_application_token: SecretStr = SecretStr("")
    # Real-account commerce is opt-in. Checkout additionally requires cart writes.
    jiomart_cart_writes: bool = False
    jiomart_checkout: bool = False
    # Customer-initiated shipment cancellation; independent of the cart/checkout flags.
    jiomart_order_cancel: bool = False
    # POC safety cap on a single confirmed order total, in rupees.
    # Optional ceiling (INR) on a chat order's total; 0 means none. Every order still needs a payment tap.
    checkout_max_total: int = Field(default=0, ge=0, le=200000)
    # Online payment: the bot opens JioMart's payment session server-side with the account's
    # sign-in and hands the browser JioPay's page (proven 18 Oct 2026). Each session opens once.
    jiomart_online_payment: bool = False
    # Stable public HTTPS origin for payment links; empty asks the local ngrok agent API.
    public_base_url: str = ""
    # Set by Render to the service's own https address; used when PUBLIC_BASE_URL is empty.
    render_external_url: str = ""
    # Bot's WhatsApp number (digits) for the "Back to WhatsApp" button after payment.
    whatsapp_display_number: str = ""
    order_watch_interval_seconds: int = Field(default=20, ge=5, le=600)
    # How often the background loop proactively renews live sessions' JioMart access tokens.
    session_keepalive_interval_seconds: int = Field(default=600, ge=60, le=3600)
    database_path: str = "work/runtime/bot.sqlite3"
    # JioMart's own cookie lives far longer than an hour; a background keep-alive now
    # renews the access token so a session need not be re-authenticated within this TTL.
    session_ttl_seconds: int = 2592000
    # Store-manager-voice replies (Fish Audio TTS). Inactive unless the API key is set below.
    fish_audio_api_key: SecretStr = SecretStr("")
    fish_audio_model: str = "s2.1-pro-free"
    # The store manager's cloned voice (Fish Audio reference_id); cloning a real person's voice
    # needs their consent. Empty uses Fish Audio's default voice.
    fish_audio_voice_id: str = ""
    # off | voice (only replies to a transcribed voice note, default) | always
    voice_replies: str = "voice"

    @cached_property
    def allowed_numbers(self) -> set[str]:
        return {re.sub(r"\D", "", n) for n in self.allowed_whatsapp_numbers.split(",") if n.strip()}

    def missing(self) -> list[str]:
        if self.channel_provider not in {"meta", "openwa"}:
            return ["CHANNEL_PROVIDER"]
        names = [
            "openai_api_key",
            "session_encryption_key",
            "allowed_whatsapp_numbers",
        ]
        if self.channel_provider == "meta":
            names += [
                "meta_access_token",
                "meta_app_secret",
                "meta_phone_number_id",
                "meta_waba_id",
                "webhook_verify_token",
            ]
        elif self.channel_provider == "openwa":
            names += ["openwa_base_url", "openwa_api_key", "openwa_session_id", "openwa_webhook_secret"]
        return [
            n.upper()
            for n in names
            if not (
                getattr(self, n).get_secret_value()
                if isinstance(getattr(self, n), SecretStr)
                else getattr(self, n)
            )
        ]
