# SmartBazaar context guide

Start at [agents.md](agents.md). It maps the implemented local POC, its limits, and the evidence documents. Follow the module or feature link for code work; follow the PRD and status for product scope or external-readiness questions.

The POC is no longer planning-only. It runs as a local service with proven WhatsApp/JioMart login and search. The shopping-list/memory/media build expands that code; production deployment remains unproven. A real checkout/order journey was proven live for one online order on 18 October; cash on delivery and later order stages remain unverified. Since 19 October (implemented, not yet deployed) the local shopping list is retired and the JioMart cart is the only basket. Context documents use three distinct labels: **implemented** describes local source and tests; **observed** describes dated manual/live evidence; **proposed/pending** describes work that must not be treated as available.

```text
.ai/
  agents.md                                  entry point
  modules/login-search-poc.md                runtime module boundary
  modules/project-documents.md               evidence and requirements boundary
  features/whatsapp-login-search.md          user-facing implemented feature
  patterns/                                  recurring safety patterns
  architecture/module-interactions.md        request/data flow
  architecture/data-model.md                 local SQLite records
  context-map.json                           source coverage index
```

Before a code change, read `.ai/agents.md`, select the relevant map entry, inspect source, and preserve unrelated edits. Update this context when module ownership, API behavior, local data, dependencies, or external evidence changes. The concrete context workflow is in the [repo-context skill](../.agents/skills/repo-context/SKILL.md); read its full applicable prompts before regeneration or significant context maintenance.

For verification, run the application checks appropriate to the edit and then `python3 .agents/skills/repo-context/scripts/context.py validate --repo .`. That checker confirms local links and source-map structure; it does not establish that Meta or JioMart calls succeed.
