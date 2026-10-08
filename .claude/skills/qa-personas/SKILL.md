---
name: qa-personas
description: >-
  Pick and adopt a realistic QA persona from the target repo's persona pack (`personas_file` in
  stack.config) — fictional composites of real user categories, each with a role, an engagement tier
  and a critical lens the pack declares (for example mobile/field, data-trust, onboarding friction,
  scale, security/audit, external review). Trigger for "reproduce this as a real user", "which persona
  would hit this", "test this from a skeptic's view", or as the persona-selection step inside qa-repro
  / qa-explore / qa-verify / the feature loops. It supplies the role and critical lens to adopt — a
  producer of REALISM for other skills' walks and repros. Does NOT itself explore, reproduce, fix, or
  verify (qa-explore owns feature walks).
allowed-tools: Read, Bash
---

# qa-personas — reproduce and explore as a real user, not a generic one

A bug report is filed by a *person* with a job, a device, and a tolerance. Generic "click through the
happy path" testing misses what that person hits first. `qa-personas` supplies the **persona pack** the
target repo configures — fictional composites of its user categories — and the discipline to *become*
the right one, so a repro or a walk finds the failures a real user finds. Read
`references/using-personas.md` — how to pick, how to role-play, and the lens→failure-mode map all live
there. `references/pack-format.md` is the pack schema and the authoring guide for your own pack.

## What this block does NOT do (atomic boundary)
It does **not** reproduce, fix, verify, or explore on its own. It **selects** the persona(s) and hands
the caller the role (the full profile) to adopt. The caller (`qa-repro`/`qa-explore`/`qa-verify`/a
feature loop) does the work *in character*.

## Where the pack comes from
`personas_file` in `.claude/loops/stack.config.json` (default `.claude/loops/personas.json`, a neutral
sample on the example domain — Workspaces, Projects, Tasks, Comments, Invoices, Members, Settings —
with roles `owner/admin/member/observer`). Replace the sample with a pack built from **your** product's
users; keep the shape. The pack's `roles` should mirror `adapters.auth_provider.config.roles` so a
persona can be signed in as its role. Consumers refer to personas by **`id` and `role`** only.

## Inputs / Outputs
**Input:** a bug/surface/feature in plain words (or a tier / role / lens filter, or an id).
**Output:** ranked persona(s) `{ id, label, role, tier, lenses[], score }`; with `--full` also `summary`,
`device`, `goals[]`, `habits[]`, `standards`, `what_broken_looks_like[]` and `profile_markdown` — the
role to adopt. Printed as JSON. Exit 0 on success (an **empty** result is a valid answer — see stop
conditions); exit 2 when the pack is missing, unparsable or fails shape validation.

## Procedure
### 1. Pick the persona(s) whose lens matches
```bash
python3 scripts/pick_persona.py --query "<the bug/surface in plain words>" --k 3   # ranked candidates
python3 scripts/pick_persona.py --id <id> --full                                   # full role to adopt
python3 scripts/pick_persona.py --spread                                           # one per engagement tier
# filters: --tier <id|alias>  --role <role>  --lens <substring>  --list  --tiers (roles/tiers/overlays)
```
Selection is deterministic and token-free (keyword overlap: lenses ×3, role/label/device ×2,
goals/habits/profile ×1). If `conventions.domain_glossary` maps generic nouns to your product's own
feature names, the query is expanded with both so product vocabulary matches (`--no-glossary` to skip).

How many: **repro** → the 1–2 whose lens matches; **explore** → `--spread` (one per engagement tier,
plus the pack's `adversarial` / `marathon` overlays when the surface warrants); **verify** → the
persona who reported or would care.

### 2. Adopt the role (in-character)
Read the profile's **goals**, **habits**, **standards**, and **"what broken looks like"** (the pack's
`profile_sections`). Then drive the live surface *as that person* — their device, their speed, their
standards — signed in as their **role** through the `auth_provider` adapter, and react with their
critical lens. Be the critic the persona is; don't narrate a neutral walkthrough.

### 3. Hand the reaction back
Return what that persona hit/objected to. `qa-repro` turns it into a reproduction; `qa-explore` proposes
expectations/flakiness from it; `qa-verify` judges the fix from that lens; a feature loop checks ACs
against it.

## Success criteria & failure categories (declared up front)
**Success:** the chosen persona genuinely matches the surface, and the run is performed *in character*
(real device/speed/standards), surfacing failures a generic walkthrough would miss.
**Failure categories:**
- `wrong-persona` — picked a lens that doesn't touch this surface (a read-only compliance reviewer for a
  phone tap-target bug).
- `out-of-character` — selected a persona then tested generically (the realism is the whole point).
- `single-lens-explore` — explored with one persona when a spread was needed (missed the range of tiers).

## Stop conditions
- **No match / low confidence** (`--query` returns `[]`, or every score is 1): do **not** force a weak
  match. Report `needs-info` with the query and the pack's tiers (`--tiers`) and let the caller pick or
  ask. A fabricated persona is worse than none.
- **Pack missing or invalid** (exit 2): stop and surface the validation problems; never fall back to an
  invented persona.
- **A different tenant/workspace implies a different data shape.** If the repro would need to switch
  workspace or account, ASK — the persona's lens assumes its own data.

## Knowledge brain (qa-kb)
Writes nothing canonical. The persona used is useful **provenance** on a repro/expectation (record it in
the artifact's metadata as `persona: <id> (<role>, <tier>)`), so a future run can reproduce under the
same lens.

## Key principles
1. **Real users, not happy paths.** The persona is the difference between "it works" and "it works for
   the field member on a phone."
2. **Match the lens to the surface.** Mobile bug → a phone-first persona; data bug → a reconcile-the-
   export persona; new-user bug → an onboarding persona. The lens vocabulary is the pack's, not yours.
3. **Explore with a spread.** One walk across the pack's engagement tiers surfaces the widest range.
4. **Stay in character.** Selecting a persona and then testing generically wastes it.
5. **Composites, not customers.** A pack holds fictional composites keyed by category — safe to commit,
   cite by id and role, never by identity.
