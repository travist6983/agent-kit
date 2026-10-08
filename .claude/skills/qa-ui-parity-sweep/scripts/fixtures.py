#!/usr/bin/env python3
"""
fixtures.py <run_dir> — write the offline test capture into an initialised run directory.

Fixture dumps stand in for what probe.js posts, so every grading rule, the coverage accounting and
the report can be checked without a browser. One copy of the fixture feeds both
`scripts/test_sweep.sh` and the shipped offline eval (`evals/files/eval-3-run`). Labels are the
neutral example domain (workspaces, projects, tasks, members); environments are the internal
"baseline" / "candidate" sides.

Routes and what each one exercises (ids match the test routes file):
  alpha    the grading matrix: style drift, missing/hidden/disabled controls, label drift, a data pair,
           a missing heading, an extra control, colour within tolerance, a text-transform case,
           a candidate console error (+ a duplicate, a noise line, a warning), one refused write,
           and five dialogs (share: fails on the disabled trigger · invite: does not open · rename:
           trigger not found · notes: guard-blocked · details: opens on both, one style delta)
  beta     measured while the tab was hidden → no style findings, partial coverage
  gamma    the candidate did not render its content column
  zeta     the baseline page is a placeholder while the candidate has a real page
  delta    nothing captured (the caller records a reason for one side only)
  epsilon  nothing captured and NO reason → the silent skip the report must catch
"""
import datetime
import json
import os
import sys


def write(run):
    AT = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")   # as the probe stamps meta.at

    def s(**kw):
        base = {"display": "inline-flex", "visibility": "visible", "opacity": "1", "color": "rgb(15,23,42)", "background-color": "rgb(255,255,255)",
                "font-family": "Inter, sans-serif", "font-size": "13px", "font-weight": "500", "line-height": "18.2px",
                "padding-top": "6px", "padding-right": "12px", "padding-bottom": "6px", "padding-left": "12px",
                "border-top-left-radius": "6px", "box-shadow": "none", "cursor": "pointer", "text-transform": "none"}
        base.update(kw)
        return base

    def it(i, role, name, cls, y=10, w=80, h=30, visible=True, disabled=False, icon=None, **st):
        return {"i": i, "role": role, "name": name, "tag": "h2" if role == "heading" else "button", "text": name, "visible": visible,
                "disabled": disabled, "box": {"x": 0, "y": y, "w": w, "h": h}, "cls": cls, "icon": icon, "s": s(**st)}

    def dump(route, env, items, **meta):
        m = {"route": route, "env": env, "visibility": "visible", "rootFound": True, "url": f"https://{env}.example.com/{route}",
             "console": [], "blocked": [], "at": AT}
        m.update(meta)
        d = os.path.join(run, "dumps", route, env)
        os.makedirs(d, exist_ok=True)
        json.dump({"meta": m, "items": items, "dialogs": []}, open(os.path.join(d, "base.json"), "w"))

    def modal(route, env, name, items=None, error=None, label=None):
        d = os.path.join(run, "dumps", route, env)
        os.makedirs(d, exist_ok=True)
        meta = {"name": name, "label": label or name.capitalize(), "at": AT}
        body = {"meta": meta, **({"error": error} if error else {
            "shell": {"box": {"w": 480, "h": 300}, "s": s(**{"box-shadow": "rgba(0,0,0,0.2) 0px 8px 24px 0px"})}, "items": items or []})}
        json.dump(body, open(os.path.join(d, f"modal-{name}.json"), "w"))

    def console(route, env, msgs):
        d = os.path.join(run, "console")
        os.makedirs(d, exist_ok=True)
        json.dump({"messages": msgs, "at": AT}, open(os.path.join(d, f"{route}.{env}.json"), "w"))

    C = lambda *c: [list(c), ["page-header"]]
    # alpha: the grading matrix
    base = [
        it(0, "heading", "Projects", C("page-title"), y=0, h=28, **{"font-size": "24px", "font-weight": "600"}),
        it(1, "button", "New project", C("new-project-btn"), y=40),                                 # style drift ≥2px (medium)
        it(2, "button", "Export", C("export-btn"), y=40),                                            # missing in the candidate (high)
        it(3, "button", "Filter", C("filter-btn"), y=40),                                            # hidden in the candidate (high)
        it(4, "button", "Share", C("share-btn"), y=40),                                              # disabled in the candidate (high)
        it(5, "button", "Task list", C("tasks-btn"), y=80),                                          # label drift (medium)
        it(6, "button", "Sort", C("sort-btn"), y=80),                                                # sub-2px (low)
        it(7, "button", "Task one", C("task-row", "task-list"), y=120),                               # data pair
        it(8, "heading", "Upcoming", C("section-title"), y=160, h=20),                               # missing heading (medium)
        it(9, "button", "Tint", C("tint-btn"), y=200, color="rgb(15,23,42)"),                        # colour within 3 → low
        it(11, "heading", "Overview", C("ov-title"), y=300, h=20),                                   # uppercased by CSS in the candidate
    ]
    cand = [
        it(0, "heading", "Projects", C("page-title"), y=0, h=28, **{"font-size": "24px", "font-weight": "600"}),
        it(1, "button", "New project", C("new-project-btn"), y=40, **{"padding-top": "4px", "padding-bottom": "4px", "background-color": "rgb(37,99,235)"}),
        it(3, "button", "Filter", C("filter-btn"), y=40, visible=False),
        it(4, "button", "Share", C("share-btn"), y=40, disabled=True),
        it(5, "button", "Task List", C("tasks-btn"), y=80),
        it(6, "button", "Sort", C("sort-btn"), y=80, **{"padding-left": "13px"}),
        it(7, "button", "Real task title", C("task-row", "task-list"), y=120),
        it(10, "button", "Beta feedback", C("beta-btn"), y=240),                                      # extra in the candidate (medium)
        it(9, "button", "Tint", C("tint-btn"), y=200, color="rgb(16,24,42)"),
        it(11, "heading", "OVERVIEW", C("ov-title"), y=300, h=20, **{"text-transform": "uppercase"}),
    ]
    dump("alpha", "baseline", base)
    dump("alpha", "candidate", cand, blocked=[{"kind": "fetch", "method": "PUT", "url": "https://candidate.example.com/api/views"}])
    console("alpha", "baseline", [{"level": "error", "text": "Hydration mismatch in <ProjectCard>"}])
    console("alpha", "candidate", [
        {"level": "error", "text": "TypeError: Cannot read properties of undefined (reading 'tasks')"},
        {"level": "error", "text": "TypeError: Cannot read properties of undefined (reading 'tasks')"},   # deduped
        {"level": "error", "text": "chat-widget: boot failed"},                                            # noise (routes file console_noise)
        {"level": "warn", "text": "[framework warn]: Extraneous non-props attributes"},                    # warning → info
        {"level": "log", "text": "hello"},
    ])
    modal("alpha", "baseline", "share", items=[it(0, "button", "Copy link", C("copy-btn"))])
    modal("alpha", "candidate", "share", error="no-dialog-opened")
    modal("alpha", "baseline", "invite", items=[it(0, "button", "Copy invite", C("copy-invite-btn"))])
    modal("alpha", "candidate", "invite", error="no-dialog-opened", label="Invite people")
    modal("alpha", "baseline", "rename", items=[it(0, "button", "Cancel", C("cancel-btn"))])
    modal("alpha", "candidate", "rename", error="trigger-not-found")
    modal("alpha", "baseline", "notes", items=[it(0, "button", "Done", C("done-btn"))])
    modal("alpha", "candidate", "notes", error="guard-blocked")
    modal("alpha", "baseline", "details", items=[it(0, "button", "Close", C("close-btn"))])
    modal("alpha", "candidate", "details", items=[it(0, "button", "Close", C("close-btn"), **{"font-size": "15px"})])

    # beta: measured while hidden → no style findings, partial coverage
    dump("beta", "baseline", [it(0, "button", "X", C("x-btn"))])
    dump("beta", "candidate", [it(0, "button", "X", C("x-btn"), **{"font-size": "20px"})], visibility="hidden")
    console("beta", "baseline", []); console("beta", "candidate", [])
    # gamma: the candidate page did not render its root
    dump("gamma", "baseline", [it(0, "heading", "Workspace", C("ws-title"))]); dump("gamma", "candidate", [], rootFound=False)
    console("gamma", "baseline", []); console("gamma", "candidate", [])
    # zeta: the baseline page is a placeholder (a title), the candidate has a real page → not 20 "extra" findings
    dump("zeta", "baseline", [it(0, "heading", "Workspace", C("page-title"))])
    dump("zeta", "candidate", [it(0, "heading", "Workspace", C("page-title"))] + [it(i, "button", f"Widget {i}", C(f"w{i}-btn"), y=40 + i) for i in range(1, 21)])
    console("zeta", "baseline", []); console("zeta", "candidate", [])
    # delta: nothing captured, but a reason recorded by the caller → partial, not silent
    # epsilon: nothing captured and NO reason → silent skip


ROUTES = {
    "_doc": "Test routes for qa-ui-parity-sweep's offline suite. Neutral selectors and example-domain areas.",
    "roots": {"baseline": "main", "candidate": ".page-content"},
    "console_noise": [{"pattern": "chat-widget", "why": "support chat widget"}],
    "source": {"candidate_paths": ["src"], "baseline_paths": ["components", "pages"],
               "extensions": [".tsx", ".scss", ".css"], "component_extensions": [".tsx"], "style_extensions": [".scss", ".css"]},
    "routes": [
        {"id": "alpha", "area": "projects", "path": "/a"},
        {"id": "beta", "area": "projects", "path": "/b"},
        {"id": "gamma", "area": "workspace", "path": "/"},
        {"id": "delta", "area": "projects", "path": "/d"},
        {"id": "epsilon", "area": "projects", "path": "/e"},
        {"id": "zeta", "area": "workspace", "path": "/z"},
    ],
}

STACK = {
    "_doc": "A minimal stack.config for the offline tests (LOOPS_STACK_CONFIG points here).",
    "version": "1.0.0",
    "project": {"name": "example-app", "default_branch": "main", "branch_prefixes": {"fix": "fix/"}},
    "app": {"repo_dir": None, "url": "http://localhost:3000",
            "environments": {"design": {"url": "https://design.example.com"}, "staging": {"url": "https://staging.example.com"}}},
    "stack": {"conventions_file": ".claude/loops/conventions.json"},
    "adapters": {},
    "evidence": {"root": "runs"},
    "limits_file": ".claude/loops/loop-limits.json",
    "integrations": {"ui_parity": {"baseline_env": "design", "candidate_env": "staging", "routes": []}},
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    run = os.path.abspath(sys.argv[1])
    if "--routes" in sys.argv:
        json.dump(ROUTES, open(run, "w"), indent=1)
    elif "--stack" in sys.argv:
        json.dump(STACK, open(run, "w"), indent=1)
    else:
        write(run)
    print(json.dumps({"ok": True, "wrote": run}))
