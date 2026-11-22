# agent-kit mods

Claude Code mods (plugins of function hooks) that ship with the kit. The kit repo is a plugin marketplace named `agent-kit` (`.claude-plugin/marketplace.json` at its root). Each mod keeps its data under the active Claude config folder (`$CLAUDE_CONFIG_DIR`, or `~/.claude`), so separate accounts never share it. The one exception is recipe-run's questions and answers, which live beside the run they belong to, in `runs/<KEY>/`.

| Mod | What it does |
|---|---|
| ship | Row above the prompt: branch, changes, commits to push, last deploy. `/ship` commits, pushes and deploys with the repo's saved recipe; `/ship pr`, `/ship set <cmd>`, `/ship show`. The first `/ship` in a repo works the deploy out and saves it. |
| dev-servers | `/servers` pane of running dev servers with URLs. `/up` starts the project's saved servers, `/bounce` restarts them. The first `/up` has Claude start them and save the recipe. |
| burn-meter | Status line with turn time, steps and cost; a toast every 5 min on long turns. `/budget 5`, `/budget 5 hard`, `/budget off`; `/fanout off \| on` blocks or allows multi-agent workflows (renamed from `/workflows`, which is now built in). |
| secret-catcher | Pulls API keys out of prompts before the model sees them, saves them to `<config>/secrets/<project>.env` (mode 600) and leaves `$NAME` placeholders. |
| traffic-control | Shows other live Claude sessions in the same repo and locks push/deploy per repo so parallel sessions can't collide. `/traffic` pane. |
| pr | Status line `PR #42 draft · CI 4/5 · 2 threads`. `/ci` and `/threads` pane, a toast as each check finishes, and a `review_threads` tool so "fix the review comments" works. Needs an authenticated `gh`. |
| brain | Recalls matching lessons from the agent-kit brain into each prompt (fails open when the brain is down), gives Claude a `remember` tool whose lessons wait for your approval, shows `brain: <project> · N lessons · k recalled · p to review`, and `/brain` opens Recalled, Review and Lessons tabs. `/remember <lesson>` or `/remember global: <lesson>` saves one directly. `/agent-kit init`, `doctor` and `list` run the kit's `scripts/agent-kit` for the repo you're in. Needs the kit's `kb` CLI (found beside the mod, or `$AGENT_KIT_HOME`) and `$AGENT_KIT_OVERLAY`. |
| recipe-run | `/recipe` pane for the run in progress: each stage of the recipe with its outcome and tries, the router's last verdicts in plain words (retry the fix, escalated to a human, stopped: budget cap) and spend against the cap. A row above the prompt while a run is live. A Questions tab lists what loops and agents left for a human, blocking first, answered with a button or typed reply. Claude gets an `ask_human` tool for those questions. `/recipe KEY` pins a run, `/recipe auto` follows the newest, `/recipe questions` opens the inbox. |
| mods-panel | `/mods` pane that gathers every agent-kit mod in one place: a strip with how many mods are on, blocking questions and session spend, then a card per mod with its live status (read from that mod's own state) and an On/Off switch (runs `claude plugin enable` or `disable`, applied after `/reload-plugins`). recipe-run's card carries the stage ladder, the router's last call, spend against the cap and a Questions tab you answer from the panel. burn-meter's card shows spend, the 5 hour and weekly limits and cost per turn. A band above the prompt shows the run, spend and blocking count. `/mods band off` hands the row back to recipe-run. A frosted glass look is on by default in the desktop and mobile apps: a dark backdrop of slow colored light with the app's panels frosted over it (the terminal draws no look). It replaces any other look and keeps the app in dark mode. `/mods glass off` clears it and is remembered; `/mods glass on` brings it back. The panel only reads other mods; it never changes them. |
| build-log | Collects post material as you work and drafts posts you publish yourself: a long build log for your site, a short LinkedIn version and an X thread. Claude saves moments with a `log_moment` tool (the mod also notes merged PRs and notable recipe-router verdicts), keeps screenshots, and saves drafts only through `save_draft`, which runs a redaction check against your private denylist and writes nothing when it blocks. `/buildlog` status, `init`, `note`, `shot`, `grab` (macOS), `shots`, `ok`, `drop`, `draft`, `check <file>`, `off` and `on`. It has no network code and never posts. See [build-log](#build-log) below. |

## Install

From GitHub, in any Claude Code session:

```
/plugin install ship --marketplace travist6983/agent-kit
```

(repeat per mod), or from a local clone:

```sh
claude plugin marketplace add <path to agent-kit>
for m in ship dev-servers burn-meter secret-catcher traffic-control pr brain recipe-run mods-panel build-log; do
  claude plugin install $m@agent-kit --scope user
done
```

Installed from a local folder, the mods are read from that folder: edit, then `/reload-plugins`. Removing the marketplace deletes the mods' saved data (deploy and server recipes, budget settings).

Mods that only make sense for one person or one project belong in an overlay's own marketplace, not here.

## Develop

- Code: `<mod>/hooks/register.tsx`; state types in `<mod>/types/index.d.ts`; tests in `<mod>/tests/*.test.ts`.
- Check: `claude plugin validate <mod>` and `claude plugin test <mod>`.
- Add a mod: create its folder, add it to the kit's root `.claude-plugin/marketplace.json`, `claude plugin marketplace update agent-kit`, install.

## Open questions (recipe-run)

Any loop, agent or script can leave a question for a human, interactive or headless. Two files per run, each appended one JSON object per line, so nobody rewrites another writer's lines:

- `runs/<KEY>/questions.jsonl`, written by whoever asks:
  `{"id": "q-1", "kind": "blocking", "question": "Drop the legacy status column?", "from": "fix", "options": ["Drop it", "Keep it"], "context": "Two reports still read it.", "asked_at": "2026-11-21T10:00:00Z"}`
- `runs/<KEY>/answers.jsonl`, written by the pane:
  `{"id": "q-1", "key": "PROJ-123", "answer": "Keep it", "answered_at": "...", "by": "human"}`

`id`, `kind` and `question` are required; `id` is unique within the run. Questions with no run go in `runs/_inbox/`.

- **blocking:** the stage can't go on safely without the answer. Stop the stage and escalate with the question as the reason; do not guess. Resume once `answers.jsonl` has the id.
- **non-blocking:** the stage goes on with a stated `default` (required), and a later answer can change course.

Inside Claude, the `ask_human` tool writes the line for you. In an interactive session a blocking question is also asked on the spot; in a `claude -p` or SDK run it is saved and the tool says to stop and escalate. Answers given in the pane reach Claude with the next message. A new blocking question raises one native notification.

Limits: headless and cloud sessions run the hooks but draw nothing, so the pane shows up once you open an interactive session in the repo. The pane follows the router only when the router runs through Claude's Bash tool; a router called from a plain shell script leaves the stage files, which the pane still reads, but no verdict history.


## build-log

Drafts posts about your work with the kit; you read them and publish them yourself.

**Setup, once per Claude account:** run `/buildlog init`. It writes `<config>/build-log/config.json` (the active Claude config folder, or `$BUILD_LOG_HOME`), outside every repo, from the mod's `config.example.json`. Replace every example entry:

- `deny`: `names`, `products`, `domains`, `repos`, `paths` and `words` that must never reach a post (employer, clients, people, product names, industry words). Spelling variants are caught: "Acme Widgets" also matches `acme-widgets` and `AcmeWidgets`.
- `allow_urls`: your own site. The agent-kit repo is always allowed; every other URL, bare domain or email is blocked.
- `safe_repos`: your own repos. Moments and shots from these and the kit count as yours; everything else, including a kit loop session started for another project (`AGENT_KIT_PROJECT`), counts as work.

Never commit that file or copy real names into the kit; `test_no_leaks.sh` exists for the same reason. Until the denylist holds something other than the examples, `save_draft` and `/buildlog draft` refuse.

**What the check blocks:** denylist hits; URLs, domains and emails not allowed; absolute and home paths, and relative paths outside the kit's own folders; branch names; ticket keys (the kit's `PROJ-123` examples pass); dollar amounts and hour counts, unless the same figure is in a moment from your own repos; phone numbers; token-shaped secrets; en and em dashes; X posts over 280 characters. Hints at the day job ("at work", "my client") and lengths off the plan only warn. `/buildlog check <file>` runs it on any file, such as a draft you edited by hand.

**Screenshots:** shots from the kit or your own repos are kept as they are. From any other repo, `add_screenshot` asks you first, and a kept shot is flagged: a draft that uses it is blocked until you look at it and run `/buildlog ok <id>`. Typing `/buildlog shot <path>` or `/buildlog grab` yourself counts as your yes for that one shot, and it is still flagged. **Images can leak what the text check can't see:** file paths, branch names, tab titles, URLs, ticket keys, repo names in a status line, env values. Look at every image before posting, crop, or shoot on a demo repo.

**Storage:** `moments.jsonl`, `shots.jsonl`, `shots/` and `drafts/<date>-<slug>/` (`site.md`, `linkedin.md`, `x.md`, `check.json`), all under the same private folder, created with owner-only permissions. Each Claude account keeps its own, so notes taken under a work account stay there; only a draft that passed the check is something to move. If you point `BUILD_LOG_HOME` inside a repo, use `.build-log/`, which the kit ignores.

**Drafting:** `/buildlog draft` (or "draft a build log") loads the mod's `build-log` skill: one story per post, in the template Problem, What the kit did, What broke, What I changed, Try it.
