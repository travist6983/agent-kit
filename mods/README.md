# agent-kit mods

Claude Code mods (plugins of function hooks) that ship with the kit. This folder is a plugin marketplace named `agent-kit`. Each mod keeps its data under the active Claude config folder (`$CLAUDE_CONFIG_DIR`, or `~/.claude`), so separate accounts never share it.

| Mod | What it does |
|---|---|
| ship | Row above the prompt: branch, changes, commits to push, last deploy. `/ship` commits, pushes and deploys with the repo's saved recipe; `/ship pr`, `/ship set <cmd>`, `/ship show`. The first `/ship` in a repo works the deploy out and saves it. |
| dev-servers | `/servers` pane of running dev servers with URLs. `/up` starts the project's saved servers, `/bounce` restarts them. The first `/up` has Claude start them and save the recipe. |
| burn-meter | Status line with turn time, steps and cost; a toast every 5 min on long turns. `/budget 5`, `/budget 5 hard`, `/budget off`; `/workflows off \| on`. |
| secret-catcher | Pulls API keys out of prompts before the model sees them, saves them to `<config>/secrets/<project>.env` (mode 600) and leaves `$NAME` placeholders. |
| traffic-control | Shows other live Claude sessions in the same repo and locks push/deploy per repo so parallel sessions can't collide. `/traffic` pane. |
| pr | Status line `PR #42 draft · CI 4/5 · 2 threads`. `/ci` and `/threads` pane, a toast as each check finishes, and a `review_threads` tool so "fix the review comments" works. Needs an authenticated `gh`. |

## Install

From GitHub, in any Claude Code session:

```
/plugin install ship --marketplace travist6983/agent-kit
```

(repeat per mod), or from a local clone:

```sh
claude plugin marketplace add <path to agent-kit>/mods
for m in ship dev-servers burn-meter secret-catcher traffic-control pr; do
  claude plugin install $m@agent-kit --scope user
done
```

Installed from a local folder, the mods are read from that folder: edit, then `/reload-plugins`. Removing the marketplace deletes the mods' saved data (deploy and server recipes, budget settings).

Mods that only make sense for one person or one project belong in an overlay's own marketplace, not here.

## Develop

- Code: `<mod>/hooks/register.tsx`; state types in `<mod>/types/index.d.ts`; tests in `<mod>/tests/*.test.ts`.
- Check: `claude plugin validate <mod>` and `claude plugin test <mod>`.
- Add a mod: create its folder, add it to `.claude-plugin/marketplace.json`, `claude plugin marketplace update agent-kit`, install.
