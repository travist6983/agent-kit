# Persona pack format — build one for your product

The pack is a single JSON file named by `personas_file` in `.claude/loops/stack.config.json` (default
`.claude/loops/personas.json`). The shipped file is a **neutral sample** on the example domain; replace
its contents with personas drawn from your product and keep this shape. `pick_persona.py` validates the
shape on every run and exits 2 with the list of problems when it does not hold.

## Shape

```jsonc
{
  "version": "1.0.0",
  "title": "QA tester personas — <your product>",
  "description": "free text: what the pack covers, how it was built",
  "provenance": { "generated_from": "…", "anonymization": "…" },   // optional, free text

  "roles": ["owner", "admin", "member", "observer"],                 // REQUIRED — sign-in roles; mirror
                                                                      // adapters.auth_provider.config.roles
  "engagement_tiers": [                                               // REQUIRED — the spread axis
    { "id": "core",       "label": "core practitioner", "description": "…", "aliases": ["daily", "power"] },
    { "id": "onboarding", "label": "first weeks",        "description": "…", "aliases": ["new", "trial"] }
    // a bare string ("core") is accepted; aliases and labels resolve on --tier
  ],
  "profile_sections": ["goals", "habits", "lenses", "standards", "what_broken_looks_like"],   // optional, documents what a profile carries

  "overlays": [                                                       // optional behavioral layers
    { "id": "adversarial", "label": "…", "habits": ["…"], "lenses": ["…"] },
    { "id": "marathon",    "label": "…", "habits": ["…"], "lenses": ["…"] }
  ],

  "personas": [                                                       // REQUIRED — one object each
    {
      "id": 1,                                   // REQUIRED, unique (int or string); consumers cite it
      "role": "member",                          // REQUIRED, one of roles
      "tier": "core",                            // REQUIRED, one of engagement_tiers[].id
      "label": "field member on a phone",        // short handle — a category, never a person's name
      "summary": "one sentence",
      "device": "phone, one-handed, weak signal",
      "goals":  ["…"],                           // REQUIRED, non-empty
      "habits": ["…"],                           // REQUIRED, non-empty — how they drive the product
      "lenses": ["mobile", "offline", "tap targets"],   // REQUIRED, non-empty — scored x3 on --query
      "standards": "the bar this persona holds the product to",
      "what_broken_looks_like": ["…"],
      "profile_markdown": "## … the full role to adopt, returned by --full",
      "composite_source": "~N <category> accounts"     // optional provenance, category + count only
    }
  ]
}
```

Any extra key is preserved and ignored by the selector, so a pack can carry product-specific fields.

## How selection uses the fields
`--query` tokenizes the request and scores each persona: overlap with `lenses` ×3, with
`role`+`label`+`device`+`tier` ×2, with `summary`/`goals`/`habits`/`standards`/
`what_broken_looks_like`/`profile_markdown` ×1. Write lenses in the words a bug report would use. If
your product has its own nouns, map the generic ones in `conventions.domain_glossary`
(`.claude/loops/conventions.json`) — the query is expanded with both sides, so a report that says
"the weekly numbers page" can still reach a persona whose lens says "reports".

`--tier` accepts an id, a label or an alias. `--spread` returns the best persona per tier (in the
pack's tier order), which is the shape `qa-explore` wants.

## Authoring guidance
- **Composites, not customers.** Cluster your users by role × engagement × what they complain about,
  then write one persona per cluster. Fictional names, companies and places only; prefer a category
  `label` over a name entirely. The pack must be safe to commit.
- **One lens family per persona, stated bluntly.** A persona who notices everything selects for nothing.
- **Cover the spread.** At minimum: a daily practitioner, a lighter regular user, someone in their
  first week, a skeptic or lapsing user, and an outside reviewer with scoped access. Add tiers only
  when a tier changes what the persona catches.
- **Make `what_broken_looks_like` concrete.** Those lines are what the agent checks against during a
  walk; "it feels slow" catches nothing, "a spinner over two seconds on Task save" does.
- **Match roles to sign-in roles.** A persona whose role the `auth_provider` adapter cannot serve is a
  reported gap in the spread, not a reason to walk as whoever is already signed in.
- **Sources** (all optional): product analytics segments, support-ticket themes, churn interviews,
  sales objections. Record only the category and approximate count in `composite_source`.

## Validate a pack
```bash
python3 .claude/skills/qa-personas/scripts/pick_persona.py --pack path/to/personas.json --tiers
```
Exit 0 prints roles, tiers, overlays and the persona count; exit 2 prints the shape problems on stderr.
