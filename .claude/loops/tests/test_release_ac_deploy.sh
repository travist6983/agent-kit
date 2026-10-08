#!/usr/bin/env bash
# Regression: qa-loop-release-ac `deployed` / `scope` — each deploy track is proven deployed by
# ITS OWN deploy source, never by another track's version.
#
# The bug class: every PR's merge commit was checked for ancestry against the one commit the
# web track publishes (its version endpoint). An api-only PR merged before the web build
# therefore read `deployed` even when the environment's api never received it (a failed or
# cancelled api deploy, a web-only build). Its scenarios then ran against old api code and came
# back as FAILs that were really "not deployed". scope's unlabelled-commit path
# (commitDeployState) had the same hole.
#
# The fix classes each merge commit by the deploy tracks its paths belong to
# (stack.config deploy.tracks[].path_globs — track names are free strings) and checks each
# track against its own source: here `web` against its version_endpoint, `api` against the
# env-wide deploy adapter (falling back to ci_cd.deploy_status). An adapter failure, or no
# source at all, is a `deploy-unknown` stop, never an answer.
#
# Fixture history on main (web is at F2, the environment's api at X):
#   A ─ F1(web) ─ B1(api) ─ X(both) ─ B2(api) ─ B9(api, unlabelled PROJ-9) ─ Y(both) ─ F2(web)
#   F1, B1, X deployed · B2, B9 not-deployed (api never got them) · Y not-deployed (api half)
#
# SAFE BY CONSTRUCTION: a fake curl on PATH, a temp stack.config (LOOPS_STACK_CONFIG) whose
# deploy / ci_cd roles are stub providers (`fake_deploy_adapter:*` on PYTHONPATH), a throwaway
# git repo, no network. Point RELEASE_AC at another copy of release_ac.mjs to compare;
# RELEASE_AC_KEEP=<dir> keeps the emitted JSON.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
RAC="${RELEASE_AC:-$REPO/.claude/skills/qa-loop-release-ac/scripts/release_ac.mjs}"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }

if ! command -v node >/dev/null 2>&1 || [ ! -f "$RAC" ]; then
  echo "  release_ac_deploy: SKIPPED — node or $RAC missing"; exit 0
fi

W="$(mktemp -d -t release-ac-deploy)" || { echo "  release_ac_deploy: SKIPPED (mktemp failed)"; exit 0; }
# RELEASE_AC_KEEP=<dir> keeps the emitted deployed/scope JSON for inspection.
trap '[ -n "${RELEASE_AC_KEEP:-}" ] && mkdir -p "$RELEASE_AC_KEEP" && cp "$W"/d*.json "$W"/scope.json "$RELEASE_AC_KEEP"/ 2>/dev/null; rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/app" "$W/py"

# ── fixture app repo ─────────────────────────────────────────────────────────
G="$W/app"
git -C "$G" init -q -b main
git -C "$G" config user.email t@example.com; git -C "$G" config user.name t
commit(){ # commit <label> <path>...
  local label="$1"; shift
  for f in "$@"; do mkdir -p "$G/$(dirname "$f")"; echo "$label" >> "$G/$f"; done
  git -C "$G" add -A; git -C "$G" commit -qm "$label"; git -C "$G" rev-parse HEAD
}
A=$(commit base README.md)
F1=$(commit F1 web/src/a.js shared/b.js)
B1=$(commit B1 services/foo.py)
X=$(commit X web/src/c.js api/bar.py)
B2=$(commit B2 data/migrations/baz.sql docs-notes.md)
B9=$(commit "PROJ-9 api fix" services/nine.py)   # unlabelled: no PR names it
Y=$(commit Y web/src/d.js services/qux.py)
F2=$(commit F2 web/src/e.js e2e/f.spec.js)
git -C "$G" update-ref refs/remotes/origin/main "$F2"

# ── fake curl: the environment's web track is at F2 ──────────────────────────
cat > "$W/bin/curl" <<SH
#!/usr/bin/env bash
echo "\$*" >> "$W/curl.log"
echo '{"version":"$F2","build":"2026.09.29.9","timestamp":"2026-09-29T20:09:42Z"}'
SH
chmod +x "$W/bin/curl"

# ── stub deploy + ci_cd providers ────────────────────────────────────────────
#   FAKE_DEPLOY  ok (returns FAKE_API_COMMIT) | fail (AdapterError: expired token) | empty (None)
#   every call is logged to $FAKE_ADAPTER_LOG
cat > "$W/py/fake_deploy_adapter.py" <<'PY'
import json, os
from lib.adapters.base import AdapterError, BaseAdapter, DeployStatus


def _log(op, **kw):
    with open(os.environ["FAKE_ADAPTER_LOG"], "a") as f:
        f.write(op + " " + json.dumps(kw, sort_keys=True) + "\n")


def _mode():
    mode = os.environ.get("FAKE_DEPLOY", "ok")
    if mode == "fail":
        raise AdapterError("401 Unauthorized: the deploy API token has expired")
    return mode


class FakeDeploy(BaseAdapter):
    provider_name = "fake-deploy"

    def deployed_version(self, env):
        _log("deploy.deployed_version", env=env)
        return None if _mode() == "empty" else os.environ["FAKE_API_COMMIT"]

    def rollback_hint(self, env):
        return "ask a human"


class FakeCi(BaseAdapter):
    provider_name = "fake-ci"

    def deploy_status(self, env):
        _log("ci_cd.deploy_status", env=env)
        version = None if _mode() == "empty" else os.environ["FAKE_API_COMMIT"]
        return DeployStatus(env=env, version=version, status="completed", deployed_at="2026-09-29T10:40:00Z",
                            url="https://ci.example.com/deploys/101", raw={"build": "2026.09.29.5"})

    def build_status(self, ref):
        raise self.unavailable("build_status", "not needed by this test")

    def test_results(self, build_id):
        raise self.unavailable("test_results", "not needed by this test")
PY

python3 - "$W/stack.config.json" <<'PY'
import json, sys
json.dump({
  "version": "1.0.0",
  "project": {"name": "example", "ticket_key_pattern": "^[A-Z][A-Z0-9]+-[0-9]+$", "default_branch": "main"},
  "app": {"repo_dir": None, "environments": {
      "staging": {"url": "https://staging.example.com"},
      "prod": {"url": "https://app.example.com"}}},
  "adapters": {
    "deploy": {"provider": "fake_deploy_adapter:FakeDeploy", "config": {}},
    "ci_cd": {"provider": "none", "config": {}},
  },
  "deploy": {"tracks": [
    {"name": "web", "path_globs": ["web/**", "shared/**"], "version_endpoint": "/version.json"},
    {"name": "api", "path_globs": ["api/**", "services/**", "data/**"]},
  ]},
  "evidence": {"root": "runs"},
  "integrations": {"release_ac": {"release_name_pattern": None}},
}, open(sys.argv[1], "w"), indent=2)
PY

# rac <args...> — release_ac.mjs with the fixture config + stubs; FAKE_* / LOOPS_CFG__* from the caller.
rac(){
  : > "$W/adapter.log"
  LOOPS_STACK_CONFIG="$W/stack.config.json" PYTHONPATH="$W/py${PYTHONPATH:+:$PYTHONPATH}" PATH="$W/bin:$PATH" \
    FAKE_ADAPTER_LOG="$W/adapter.log" FAKE_DEPLOY="${FAKE_DEPLOY:-ok}" FAKE_API_COMMIT="$X" \
    node "$RAC" "$@"
}
adapter_calls(){ wc -l < "$W/adapter.log" | tr -d ' '; }

cat > "$W/prs.json" <<JSON
{"release_version":"4.2.0","since_days":90,"keys":["PROJ-1","PROJ-2","PROJ-3","PROJ-4","PROJ-5"],
 "prs":[
  {"id":1,"keys":["PROJ-1"],"merge_commit":"$F1","mentioned_keys":[]},
  {"id":2,"keys":["PROJ-2"],"merge_commit":"$B1","mentioned_keys":[]},
  {"id":3,"keys":["PROJ-3"],"merge_commit":"$X","mentioned_keys":[]},
  {"id":4,"keys":["PROJ-4"],"merge_commit":"$B2","mentioned_keys":[]},
  {"id":5,"keys":["PROJ-5"],"merge_commit":"$Y","mentioned_keys":[]}]}
JSON

state(){ python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print({p['id']:p for p in d['prs']}[int(sys.argv[2])].get(sys.argv[3]))" "$@"; }
field(){ python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=d
for k in sys.argv[2].split('.'): v=(v or {}).get(k) if isinstance(v,dict) else None
print(v)" "$@"; }

# ── 1. healthy: each track checked against its own deploy source ─────────────
rac deployed --prs "$W/prs.json" --repo "$G" --out "$W/d1.json" >/dev/null 2>"$W/d1.err"
ck "healthy: exit 0 (no stop)" "$?" "0"
ck "web-only PR 1 deployed (vs web's version endpoint)" "$(state "$W/d1.json" 1 state)" "deployed"
ck "api-only PR 2 deployed (ancestor of the api's deployed commit)" "$(state "$W/d1.json" 2 state)" "deployed"
ck "both-tracks PR 3 deployed (web AND api)" "$(state "$W/d1.json" 3 state)" "deployed"
ck "api-only PR 4 NOT deployed although the web build contains it" "$(state "$W/d1.json" 4 state)" "not-deployed"
ck "both-tracks PR 5 NOT deployed — its api half never reached the env" "$(state "$W/d1.json" 5 state)" "not-deployed"
ck "PR 4 classed api" "$(state "$W/d1.json" 4 track)" "api"
ck "PR 5 classed web+api" "$(state "$W/d1.json" 5 track)" "web+api"
ck "api's deployed commit comes from its own source (deploy adapter)" "$(field "$W/d1.json" tracks.api.commit)" "$X"
ck "api's source is the deploy adapter, not web's endpoint" "$(field "$W/d1.json" tracks.api.source)" "deploy.deployed_version"
ck "web's source is its version endpoint on the env" "$(field "$W/d1.json" tracks.web.source)" "version_endpoint https://staging.example.com/version.json"
ck "web's commit is the endpoint's version" "$(field "$W/d1.json" tracks.web.commit)" "$F2"

# ── 2. scope: an unlabelled api commit is judged by the api deploy too ───────
cat > "$W/tickets.json" <<JSON
{"tickets":[
 {"key":"PROJ-1","title":"s1","status":"Done","labels":[],"raw":{"fix_versions":[{"name":"Sparrow 4.2.0"}]}},
 {"key":"PROJ-2","title":"s2","status":"Done","labels":[],"raw":{"fix_versions":[{"name":"Sparrow 4.2.0"}]}},
 {"key":"PROJ-3","title":"s3","status":"Done","labels":[],"raw":{"fix_versions":[{"name":"Sparrow 4.2.0"}]}},
 {"key":"PROJ-4","title":"s4","status":"Done","labels":[],"raw":{"fix_versions":[{"name":"Sparrow 4.2.0"}]}},
 {"key":"PROJ-5","title":"s5","status":"Done","labels":[],"raw":{"fix_versions":[{"name":"Sparrow 4.2.0"}]}},
 {"key":"PROJ-9","title":"s9","status":"Done","labels":[],"raw":{"fix_versions":[{"name":"Sparrow 4.2.0"}]}}]}
JSON
# PROJ-9 shipped under the unlabelled api commit B9: inside the web build, not on the api.
rac scope --prs "$W/prs.json" --tickets "$W/tickets.json" --deployed "$W/d1.json" \
  --repo "$G" --out "$W/scope.json" >/dev/null 2>&1
tdeploy(){ python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print({t['key']:t for t in d['tickets']}[sys.argv[2]]['deploy'])" "$@"; }
ck "scope: PROJ-4 (api PR) not-deployed" "$(tdeploy "$W/scope.json" PROJ-4)" "not-deployed"
ck "scope: PROJ-9 unlabelled api commit not-deployed (was checked vs web)" "$(tdeploy "$W/scope.json" PROJ-9)" "not-deployed"
ck "scope: PROJ-2 api PR deployed" "$(tdeploy "$W/scope.json" PROJ-2)" "deployed"

# ── 3. the api's deploy adapter fails: loud stop, api PRs unverified — never deployed ─
FAKE_DEPLOY=fail rac deployed --prs "$W/prs.json" --repo "$G" --out "$W/d3.json" >/dev/null 2>&1
ck "adapter failure: exit 3 (stop)" "$?" "3"
ck "adapter failure: deploy-unknown stop names the api track and the adapter's error" \
  "$(python3 -c "import json; d=json.load(open('$W/d3.json')); print(any(s['code']=='deploy-unknown' and s['detail'].startswith('api:') and 'token has expired' in s['detail'] for s in d['stops']))")" "True"
ck "adapter failure: api PR 2 unverified (not deployed, not not-deployed)" "$(state "$W/d3.json" 2 state)" "unverified"
ck "adapter failure: api PR 4 unverified" "$(state "$W/d3.json" 4 state)" "unverified"
ck "adapter failure: web PR 1 still judged by its version endpoint" "$(state "$W/d3.json" 1 state)" "deployed"
ck "adapter failure: both-tracks PR 5 unverified (web deployed, api unknown)" "$(state "$W/d3.json" 5 state)" "unverified"
# An adapter that answers "nothing" is not "nothing deployed" either.
FAKE_DEPLOY=empty rac deployed --prs "$W/prs.json" --repo "$G" --out "$W/d3e.json" >/dev/null 2>&1
ck "empty answer: exit 3 (stop)" "$?" "3"
ck "empty answer: api PR 2 unverified" "$(state "$W/d3e.json" 2 state)" "unverified"

# ── 4. no deploy source for the api at all → unverified with a reason, a stop ─
LOOPS_CFG__ADAPTERS__DEPLOY__PROVIDER=none rac deployed --prs "$W/prs.json" --repo "$G" --out "$W/d4.json" >/dev/null 2>&1
ck "no api source: exit 3 (stop)" "$?" "3"
ck "no api source: stop reason adapter-unavailable" \
  "$(python3 -c "import json; d=json.load(open('$W/d4.json')); print([s.get('reason') for s in d['stops'] if s['code']=='deploy-unknown'])")" "['adapter-unavailable:deploy']"
ck "no api source: api PR 2 unverified" "$(state "$W/d4.json" 2 state)" "unverified"
ck "no api source: reason given" "$(python3 -c "import json; d=json.load(open('$W/d4.json')); print('no deploy source for track \"api\"' in ({p['id']:p for p in d['prs']}[2].get('why') or ''))")" "True"
ck "no api source: web PR 1 still deployed" "$(state "$W/d4.json" 1 state)" "deployed"

# ── 5. a web-only release never needs the api's source ───────────────────────
python3 -c "import json; d=json.load(open('$W/prs.json')); d['prs']=[p for p in d['prs'] if p['id']==1]; json.dump(d,open('$W/prs-web.json','w'))"
FAKE_DEPLOY=fail rac deployed --prs "$W/prs-web.json" --repo "$G" --out "$W/d5.json" >/dev/null 2>&1
ck "web-only release: exit 0 even with the deploy adapter down" "$?" "0"
ck "web-only release: PR 1 deployed" "$(state "$W/d5.json" 1 state)" "deployed"
ck "web-only release: deploy adapter never asked" "$(adapter_calls)" "0"

# ── 6. the fallback chain: deploy adapter absent → ci_cd.deploy_status names the api commit ─
LOOPS_CFG__ADAPTERS__DEPLOY__PROVIDER=none LOOPS_CFG__ADAPTERS__CI_CD__PROVIDER=fake_deploy_adapter:FakeCi \
  rac deployed --prs "$W/prs.json" --repo "$G" --out "$W/d6.json" >/dev/null 2>&1
ck "ci fallback: exit 0" "$?" "0"
ck "ci fallback: api source is ci_cd.deploy_status" "$(field "$W/d6.json" tracks.api.source)" "ci_cd.deploy_status"
ck "ci fallback: api commit is the deploy status's version" "$(field "$W/d6.json" tracks.api.commit)" "$X"
ck "ci fallback: api-only PR 2 deployed" "$(state "$W/d6.json" 2 state)" "deployed"
ck "ci fallback: api-only PR 4 not-deployed" "$(state "$W/d6.json" 4 state)" "not-deployed"
ck "ci fallback: env-wide source asked once for the env" "$(grep -c 'ci_cd.deploy_status {"env": "staging"}' "$W/adapter.log" | tr -d ' ')" "1"

# ── 7. production is refused before anything is read ────────────────────────
rac deployed --prs "$W/prs.json" --repo "$G" --env prod --out "$W/d7.json" >/dev/null 2>&1
ck "prod env: refused (exit 2)" "$?" "2"
ck "prod env: nothing written" "$([ -e "$W/d7.json" ] && echo yes || echo no)" "no"

echo "  release_ac_deploy: $pass passed, $fail failed"
[ "$fail" = 0 ]
