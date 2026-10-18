# Local WhatsApp service reliability

Date: 18 October 2026. Status: implemented for the local POC.

## Context

A temporary developer token expired while inbound webhooks and HTTP liveness
remained healthy. A replacement system-user token then passed phone reads but
could not send because Meta separates the WhatsApp asset from its messaging
account. An initial launchd deployment inside Documents failed before logging;
the free ngrok plan also rejected pinning an unreserved random URL.

## Decision

Use the owner-approved Employee system-user credential with no scheduled expiry.
Check app identity, messaging scope, both expiry fields, phone access and configured
messaging-account access at startup and every five minutes. Keep liveness separate
from HTTP 503 readiness, including cached probe, worker and delivery failure signals.
Keep recent authentication-rejected replies encrypted for at most 15 minutes;
retry only the generated reply, never the shopping action.

Run the app and tunnel under separate macOS LaunchAgents from a dedicated runtime
in Application Support, with a staged virtualenv and one encrypted database.
Use ngrok's dynamic URL and reconcile the existing Meta callback on tunnel startup.
Preserve subscription fields, disable request inspection, reject HTTP redirects,
and keep credentials outside diagnostics. Use caffeinate to prevent idle sleep.

## Consequences and verification

The owner confirmed delivery after the messaging-account permission repair and
again after controlled app and tunnel restart tests. The changed tunnel URL was
automatically registered and verified. The final suite passed 126 tests and Ruff.
These observations do not establish a production SLA. The host must remain logged
in, powered and online; forced sleep, shutdown, upstream revocation and outages
remain outside these safeguards. The inbound queue is still memory-only and no
independent alert destination is configured. An always-on host, stable HTTPS,
durable inbound processing and external alerts remain the production direction.

See [operations](../../operations-reliability.md) and
[dated status](../../project-status.md) for the active paths and recovery procedure.
