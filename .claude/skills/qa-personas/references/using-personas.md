# Using the QA-tester personas — adopt the right user, surface the right failures

## What they are
A **persona pack**: fictional **composite** QA-tester personas, each standing for a category of your
product's users — never a single customer. Every persona has a `role` (mirroring the sign-in roles the
`auth_provider` adapter serves), an engagement `tier` (from the pack's `engagement_tiers`), and a set of
`lenses` — the things that person notices first and tolerates least. The pack is `personas_file` in
`.claude/loops/stack.config.json`; the shipped default is a neutral sample on the example domain
(Workspaces, Projects, Tasks, Comments, Invoices, Members, Settings). Build your own from your product's
usage signals and support history — `pack-format.md` says how — and keep the shape.

## Why a persona changes what you catch
The same screen breaks differently for different users. A phone-first field member catches a tap-target
bug a desk power-user never touches; a spreadsheet-minded admin catches a report total that doesn't
reconcile with the export; a first-week member catches onboarding friction; an external reviewer with
read-only access catches a permission that is hidden but not enforced. Reproducing or exploring **as**
the persona whose lens matches the bug reproduces it the way a real user actually hit it — and finds the
*neighboring* failures that user would also hit.

## How to adopt a persona (role-play, in-character)
1. **Pick:** `pick_persona.py --query "<the bug/surface in plain words>" --k 3` → ranked candidates.
   Then `pick_persona.py --id <id> --full` for the full profile.
2. **Become them:** read the profile — their **goals**, **habits** (one persona is fast, keyboard-light,
   tests by trying to break it; another opens the developer tools and counts requests), **standards**,
   and **"what broken looks like to them."**
3. **Act in-character on the live surface:** sign in as the persona's **role** (credentials by role
   through the `auth_provider` adapter — never pasted), then drive the feature the way they would (one
   thumb on a phone; exporting to CSV to check the math), and react with their standards. Don't narrate a
   generic walkthrough — *be the critic this persona is.*
4. **Record provenance:** `persona: <id> (<role>, <tier>)` in the artifact's metadata.

## How many personas
- **`qa-repro`** — the **1–2** personas whose lens matches the bug (reproduce it as the user who'd hit it).
- **`qa-explore`** — a **spread across tiers** (`pick_persona.py --spread`: one persona per engagement
  tier in the pack — typically a core practitioner, an onboarding user, a skeptic and an external
  reviewer) so one walk surfaces a wide range of objections and failure modes. Layer the pack's
  behavioral **overlays** (`adversarial`, `marathon`) on top when the surface takes free-text input or
  long sessions.
- **`qa-verify`** — the persona who'd care: *"Would the phone-first member consider this actually fixed,
  on their device?"*
- **feature loops** — check each acceptance criterion against the relevant persona's reaction.

## Lens → what they catch (a reusable taxonomy)
The lenses below recur in every product. The pack decides which personas carry which; list a pack's
lenses with `pick_persona.py --list` and filter with `--lens <word>`.

| Lens family | What that persona catches first |
|---|---|
| Mobile / field / phone-only | tap targets, keyboard covering the action, saves lost on a weak signal |
| Data-trust / reconcile / export | totals that disagree with their rows or the export, dropped columns, rounding |
| Scale / multi-tenant / rollups | context bleed between workspaces, bulk actions that touch one page, slow long lists |
| Security / audit / privacy | hidden-but-not-forbidden actions, leaks through search/export/notifications |
| Onboarding / first-run | empty states with no next step, invitations that dead-end, jargon labels |
| Lapsing / switching-cost / skeptic | notification noise, exports that don't round-trip, stale context on return |
| Standards fidelity / external review | rules the product displays but does not enforce |

## No match is an answer
If `--query` returns nothing (or only score-1 matches), the pack has no persona for this surface. Stop
with `needs-info`, show the caller `--tiers`, and let a human pick or extend the pack. Forcing the
nearest persona produces a confident repro under the wrong lens — `wrong-persona`, the first failure
class this skill declares.

## Anonymization (safe to bundle/commit)
A pack is safe to commit when no persona is traceable to a person or an account. Keep personas as
category composites; if you record provenance, cite the *category* and an approximate *count* merged
(`composite_source`), never identifying detail. Names, companies and locations in a pack are fictional
placeholders — consumers cite personas by `id` and `role`, not by name.
