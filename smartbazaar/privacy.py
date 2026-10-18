"""Public, factual data-handling notice for the private WhatsApp POC."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

NOTICE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>SmartBazaar POC — Privacy notice</title>
<style>body{font:17px/1.6 system-ui,sans-serif;max-width:760px;margin:48px auto;padding:0 24px;color:#17202a}h1{line-height:1.2}h2{margin-top:32px}small{color:#52606d}</style>
<main><h1>SmartBazaar POC privacy notice</h1>
<small>Updated 19 October 2026 · Private, invited-user test</small>
<p>This independent prototype connects an invited user's existing JioMart account
to a WhatsApp chatbot for sign-in, product search, the account's own JioMart cart, orders
the customer confirms with a button, order updates and invoices. It is not an official
JioMart service.</p>
<p>For the temporary private test, the operator may use a self-hosted OpenWA
linked-device gateway instead of Meta's official Cloud API. This is an unofficial
connection to WhatsApp and is not a production service.</p>
<h2>What the prototype processes</h2>
<p>WhatsApp sends the bot your sender number, message identifier and message content.
When you request LOGIN, your registered mobile number and the OTP you provide go
to JioMart to sign in. Account sessions and saved delivery-area details allow the
bot to search products for the selected pincode.</p>
<p>Conversation and shopping text, up to eight recent sanitized conversation messages,
the names and quantities of the current cart lines, open product questions, preference
fields, recent product queries, pincode and selected catalog fields are sent to OpenAI to
interpret shopping requests.
Voice notes are sent to OpenAI Whisper for transcription; product photos and handwritten
lists are sent to OpenAI for product extraction. Media bytes stay in application memory
and are not written to the local database. The login handler routes OTPs,
session credentials and saved account records outside the AI. Outside an active login challenge, media is sent to OpenAI before its contents can be checked. Avoid sending
unrelated personal information, OTPs in media, passwords or payment details.</p>
<h2>Storage and retention</h2>
<p>The operator runs this POC on a local computer. Account sessions and conversation
state are encrypted in a local database and expire after 30 days without a message from
you. While a session is live, the bot renews JioMart's short-lived sign-in token in the
background so you are not asked to log in again every hour. Separate encrypted shopping
memory stores explicitly saved product preferences, normalized purchase evidence returned by
JioMart, products recently added to the cart and at most a few hours of open product
questions. There is no separate shopping list: the basket is your JioMart cart. Preferences
expire after 180 days; memory records are removed after 180 days without an update.
Short conversational context resets daily in India time or after four idle hours.
Encrypted outgoing messages and hashed message identifiers are retained
for up to seven days for delivery and duplicate handling. Expired records are
removed when the service runs its scheduled cleanup or restarts.</p>
<p>A small recent conversation window is encrypted in the session: at most eight
messages, each truncated to 480 characters. Common credentials and contact details
are filtered; login commands, OTPs and account/address responses are excluded.
This context is available before login, resets daily or after four idle hours,
and is cleared when switching accounts, logging out, or requesting NEW SESSION.
There is no permanent full conversation transcript. OTPs and raw webhook bodies
are never written to application logs or persisted as conversation history.
OpenAI requests use store=false;
this does not override the provider's own data-retention policies. When OpenWA is
enabled, its self-hosted gateway keeps linked-device authentication material and
may retain WhatsApp message data according to its own configured storage and
retention settings. WhatsApp, Meta where used, JioMart, OpenAI and any HTTPS tunnel
provider process data under their own policies.</p>
<h2>Your controls</h2>
<p>Send CANCEL to stop an active login, or LOGOUT to remove this bot's local saved
account session and memory (your JioMart cart stays as it is). Send MEMORY to inspect
preferences, or FORGET to remove preferences and imported purchase context while staying connected.
FORGET also clears recent chat context and pauses automatic history import until you explicitly request HISTORY again.
LOGOUT does not delete delivery records,
messages in WhatsApp, or your JioMart account, and does not revoke every upstream
session. Delivery records expire as described above.</p>
<p>Participation is optional. For questions or a request to remove remaining local
test records, contact the person who invited you to this private POC. Only configured
test numbers can use the bot. There are no advertising or data-sale features.</p>
</main></html>"""


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_notice():
    return NOTICE
