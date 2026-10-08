#!/usr/bin/env bash
# Regression: qa-personas selector — picks sensible personas per bug type from the configured pack
# (personas_file in stack.config → the neutral sample pack: 6 personas, 4 roles, 5 engagement tiers on
# the Workspaces / Projects / Tasks / Invoices example domain). Consumers reference ids, roles and tier
# ids only, so that is all this suite pins. No DB / network / model calls.
SK="$(cd "$(dirname "$0")/../../skills" && pwd)"
P="$SK/qa-personas/scripts/pick_persona.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); else echo "  [FAIL] $1 — got '$2' want '$3'"; fail=$((fail+1)); fi; }
topid(){ python3 "$P" "$@" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['id'] if d else 0)"; }
ids(){ python3 "$P" "$@" | python3 -c "import sys,json;print(','.join(str(x['id']) for x in json.load(sys.stdin)))"; }
intop(){ python3 "$P" "$@" --k 3 | python3 -c "import sys,json,os;ids={x['id'] for x in json.load(sys.stdin)};print('Y' if ids & set(map(int,os.environ['SET'].split(','))) else 'N')"; }

# ---------- query → the persona whose lens matches ----------------------------------------------
export SET=1; ck "mobile bug → the phone-first member (#1) in top-3" \
  "$(intop --query 'cant update a task on my phone in the field tap targets too small')" "Y"
export SET=2; ck "data-trust bug → the spreadsheet-minded admin (#2) in top-3" \
  "$(intop --query 'invoice total doesnt reconcile across views export drops columns numbers wrong')" "Y"
ck "onboarding top is the first-week member (#4)" \
  "$(topid --query 'brand new user confusing first-run onboarding empty states is this worth my time')" "4"
ck "audit / data-exposure top is the external reviewer (#6)" \
  "$(topid --query 'auditor with read-only access can see data from another workspace privacy')" "6"
ck "churn-risk top is the skeptical admin (#5)" \
  "$(topid --query 'too many notifications noise thinking about cancelling and switching back to the old tool')" "5"
ck "multi-workspace scale top is the owner (#3)" \
  "$(topid --query 'switching workspaces shows the wrong members permissions bulk actions')" "3"

# ---------- filters: tier (id or alias), role, lens ------------------------------------------------
ck "--tier skeptic → #5"             "$(ids --tier skeptic)" "5"
ck "--tier alias 'churn' → #5"       "$(ids --tier churn)" "5"
ck "--engagement alias still works"  "$(ids --engagement at-risk)" "5"
ck "--role owner → #3"               "$(ids --role owner)" "3"
ck "--lens csv → #2"                 "$(ids --lens csv)" "2"
ck "--spread → one per tier (5)"     "$(python3 "$P" --spread | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d), len({x["tier"] for x in d}))')" "5 5"

# ---------- pack shape ---------------------------------------------------------------------------
ck "6 personas loaded"               "$(python3 "$P" --list | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')" "6"
ck "--tiers reports roles + count"   "$(python3 "$P" --tiers | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d["roles"]), len(d["engagement_tiers"]), d["count"])')" "4 5 6"
ck "id 1 --full carries a profile"   "$(python3 "$P" --id 1 --full | python3 -c 'import sys,json;p=json.load(sys.stdin)[0];print("Y" if len(p.get("profile_markdown",""))>300 and p.get("goals") and p.get("what_broken_looks_like") else "N")')" "Y"
ck "--list is slim (no profile)"     "$(python3 "$P" --list | python3 -c 'import sys,json;print("N" if any("profile_markdown" in p for p in json.load(sys.stdin)) else "Y")')" "Y"

# ---------- an empty match is an honest empty, exit 0 (the caller treats it as needs-info) --------
O=$(python3 "$P" --query 'zxqv flurble' 2>/dev/null); rc=$?
ck "nonsense query → [] exit 0"      "$rc $O" "0 []"
O=$(python3 "$P" --query 'phone' --tier onboarding 2>/dev/null); rc=$?
ck "no match inside a tier → [] exit 0" "$rc $O" "0 []"

# ---------- a bad pack fails loudly (exit 2), never a silent empty ----------------------------------
echo '{"roles":["owner"],"engagement_tiers":["core"],"personas":[{"id":1,"role":"ghost","tier":"core","goals":["x"],"habits":["x"],"lenses":["x"]}]}' > "$TMP/bad.json"
python3 "$P" --pack "$TMP/bad.json" --list >/dev/null 2>"$TMP/err"; rc=$?
ck "persona with an unknown role → exit 2" "$rc" "2"
grep -q "not one of the pack's roles" "$TMP/err" && pass=$((pass+1)) || { echo "  [FAIL] validation names the bad role"; fail=$((fail+1)); }
python3 "$P" --pack "$TMP/missing.json" --list >/dev/null 2>&1; ck "missing pack → exit 2" "$?" "2"
echo '{ not json' > "$TMP/broken.json"
python3 "$P" --pack "$TMP/broken.json" --list >/dev/null 2>&1; ck "unparseable pack → exit 2" "$?" "2"
python3 "$P" --tier nosuchtier >/dev/null 2>&1; ck "unknown tier → exit 2" "$?" "2"

echo "  personas: $pass passed, $fail failed"
[ "$fail" = 0 ]
