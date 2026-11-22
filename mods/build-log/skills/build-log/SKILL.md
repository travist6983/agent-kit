---
name: build-log
description: >-
  Turns what happened while working with the kit into post drafts: a long build log for the
  owner's own site, a short LinkedIn version that links to it, and an X thread. Also says when to
  save a moment as you go. Trigger for "/buildlog draft", "draft a build log", "write up this
  week for my site", "turn that into a LinkedIn post", "make an X thread about the retry fix",
  "save this for the build log". Reads moments and shots through the build-log mod's tools and
  saves drafts only through save_draft, which runs the redaction check. Drafts only: never
  posts, publishes, schedules or emails anything, and calls no social or email API.
---

# build-log

The build-log mod keeps private notes ("moments") and screenshots outside every repo, under the
Claude config folder. This skill turns them into drafts the owner reads and publishes by hand.

## Hard rules

- **Draft only.** Never post, publish, schedule, email or upload anything, by any tool or API,
  and never open a social site in a browser to post. The output is files in the drafts folder.
- **Save through `save_draft` only.** It runs the redaction check and writes nothing when the
  check blocks. Never write a draft to disk any other way, and never paste a blocked draft into
  the chat as a workaround.
- **Never name the employer, the client, their people, products or projects.** Keep the
  industry out: "a production codebase", "a client project", "a codebase I'm learning".
- **No costs or hours from work.** Figures from moments marked `own` (the kit or the owner's
  own repos) may appear; the check allows exactly those.
- **The kit is the subject, never the job.** Keep timing vague: no "this week at work".
- **No dashes** (en or em). Plain words, full sentences.
- If the denylist isn't ready (`list_moments` says NOT READY), stop and tell the owner to run
  `/buildlog init` and fill it in. Don't draft around it.

## Saving moments as you go

Call `log_moment` when something happens that a reader would learn from, in the template's
words, and only then:

- a recipe run that retried, escalated, rolled back or hit the budget cap, and why;
- a mod catching something (secret-catcher swapping a key, traffic-control holding a push,
  burn-meter stopping a turn);
- a bug and its root cause, a before and after, a lesson the owner said out loud;
- a PR merged in the kit (the mod saves the bare fact by itself; add the story).

Write moments from work repos generically from the start. The mod also notes merged PRs and
notable router verdicts on its own, as one-line seeds.

## Screenshots

- Prefer shots of the kit's own panes (`/mods`, `/recipe`, `/brain`, burn-meter's status line)
  or of a demo or sandbox repo. A browser shot of a demo app with `playwright-cli` is fine.
- Keep one with `add_screenshot`. From a work repo the mod asks the owner and flags the shot;
  a flagged shot blocks any draft that uses it until the owner looks and runs `/buildlog ok <id>`.
- Before using any shot, open it (Read the file) and say in your report what it shows. The text
  check cannot see inside images: file paths, branch names, tab titles, URLs, ticket keys, repo
  names in a status line and env values all leak through pictures.
- Reference a shot in a draft as `![caption](shot:<id>)`.

## Drafting

1. `list_moments` (default: since the last draft). Pick the one story worth telling. One post,
   one lesson. Leftover moments wait for the next one.
2. Write the **site** version, 600 to 1,200 words, Markdown, with these sections in order:
   - `## Problem`: two or three sentences on what went wrong or slowed things down.
   - `## What the kit did`: the skill, recipe or mod, with one screenshot or clip.
   - `## What broke`: the honest failure. This is the part people read.
   - `## What I changed`: the fix, linked to the commit or PR in agent-kit.
   - `## Try it`: one install command or one file to read.
   The title is the lesson or result, not the tool name.
3. Write the **LinkedIn** version, 150 to 250 words: the lesson in the first line, then three
   short paragraphs (problem, what broke, what changed), one image, a link to the site post.
4. Write the **X thread**, 4 to 6 posts of 280 characters or fewer: hook with the clip, the
   problem, what broke, the fix with a screenshot, then links to the log and the repo. Leftover
   screenshots with one sentence each go in `x_singles` for quiet weeks.
5. Call `save_draft`. If it blocks, rewrite the flagged lines (don't just delete the noun and
   leave a hole) and call it again. Warnings don't block; fix them when they're cheap.
6. Report the draft folder, the warnings you left, and each image used with what it shows, so
   the owner can check them before posting.

Links allowed in a draft: the owner's site and the agent-kit repo (anything listed under
`allow_urls` in their config). Everything else is blocked, bare domains included.

## Cadence (from the posting plan)

Alternate two weeks. Week A: the long log on the site, the LinkedIn version the next day, the
X thread later that week and one clip at the weekend. Week B: no long log; two X singles and
replies to other builders. About eight logs in sixteen weeks.
