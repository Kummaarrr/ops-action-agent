"""Phase 4: the same five MCP servers over real sandboxes instead of the simulated company.

- `clients.py`   thin HTTP clients for the Shopify Admin REST API, the Stripe API (test mode)
                 and a Zendesk-shaped helpdesk API; the same clients talk to the mock
- `mock_api.py`  an in-process HTTP mock of the few endpoints used, with the system rules
                 the real APIs enforce (so everything runs without credentials)
- `mirror.py`    a local SQLite read model in the simulated company's schema: the guard, the
                 checker and the scorer read it unchanged; every adapter call refreshes it
- `backend.py`   `SandboxBackend`: the Backend method surface over the clients + the mirror
- `facts.py`     `SandboxFacts`: the policy engine's facts read live through the adapters
- `seed.py`      the small smoke dataset, created through the same clients (idempotent)
- `smoke_tasks.py` hand-written tickets with expectations for `scripts/sandbox_smoke.py`
"""
