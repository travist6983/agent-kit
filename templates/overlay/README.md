# Overlay template

An overlay holds one owner's config for agent-kit: the brain pointer, per-project settings, and mods that only make sense for that owner. Copy this folder to wherever that owner keeps it (for a client, inside the client's git), then:

1. Rename the marketplace in `mods/.claude-plugin/marketplace.json` (or delete `mods/` if there are no owner-only mods).
2. Fill in `kb.config.json`: `database_url`, `auth.mode`, `scope`, `embedding`.
3. `cp kb.secret.env.example kb.secret.env && chmod 600 kb.secret.env`, then add the secrets.
4. Set `AGENT_KIT_OVERLAY` to this folder in the Claude account's `settings.json` `env` block.
5. Create the brain once: `<kit>/.claude/skills/qa-kb/scripts/kb schema --apply`, then `kb status`.
6. For each repo: `<kit>/scripts/agent-kit init <repo>` (or `/agent-kit init` in a session there), fill in `projects/<repo>/ONBOARDING.md`, and run `agent-kit doctor <repo>` until it reports 0 blocking.
7. Optional: lessons to start with go in `seed/`.

| Path | What |
|---|---|
| `kb.config.json` | The brain pointer (no secrets) |
| `kb.secret.env` | Secrets for the brain and embeddings (gitignored) |
| `projects/<repo>/` | `stack.config.json`, `conventions.json`, `ONBOARDING.md` per repo, plus `runs/` (gitignored) |
| `mods/` | This owner's own mod marketplace |
| `seed/` | Starting lessons |

The full explanation is `docs/setups.md` in the kit.
