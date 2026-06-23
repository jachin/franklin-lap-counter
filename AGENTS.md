# AGENTS.md

1. Do everything we can through `devbox` tasks.
2. If we need to do something repeatedly, ask whether we should make a `devbox` task for it.
3. A feature is not considered complete until we run linters and fix any resulting errors or warnings.
4. `docs/redis-message-reference.md` is the canonical source for Redis channels/messages and pub/sub ownership; when Redis contracts change, update that file first and have other docs reference it.
5. `franklin-gui.py` uses GTK4; ensure all GTK calls use GTK4 APIs and patterns.
