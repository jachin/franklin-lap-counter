# AGENTS.md

1. Do everything we can through `devbox` tasks.
2. If we need to do something repeatedly, ask whether we should make a `devbox` task for it.
3. A feature is not considered complete until we run linters and fix any resulting errors or warnings.
4. `docs/redis-message-reference.md` is the canonical source for Redis channels/messages and pub/sub ownership; when Redis contracts change, update that file first and have other docs reference it.
5. `franklin-gui.py` uses GTK4; ensure all GTK calls use GTK4 APIs and patterns.
6. When you discover important information about this codebase (new patterns, gotchas, architectural changes, new commands, etc.), update `~/.agents/skills/franklin/SKILL.md` so the shared skill stays current. This is how the project's knowledge grows over time.
7. **Deploy commands to push fixes to the Pi:**
   - `devbox run deploy` — builds `.deb` then runs `ansible:deploy`
   - `devbox run ansible:deploy` — pushes files + `.deb` to Pi
8. **Before running any deploy, restart, or diagnose on the Pi**, confirm with the user in that session that they want live testing. Do not push changes to the Pi without explicit confirmation.
