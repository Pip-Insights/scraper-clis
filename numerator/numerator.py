#!/usr/bin/env python3
"""Numerator Insights CLI — headless, zero-dependency (Python 3 stdlib only).

Reverse-engineered from the Numerator Insights web app. NO browser, NO external
dependencies. Mirrors the SPINS Satori CLI in spirit.

Auth: replicates the Okta (authz.numerator.com) login the website performs —
  POST /api/v1/authn {username,password} -> one-time sessionToken (no MFA), then
  the app's OIDC code flow (insights /sso/login -> Okta /authorize?sessionToken=
  -> insights /sso/callback) which sets the Django `sessionid` + `csrftoken`
  cookies that authenticate every API call. POSTs also send `X-CSRFToken`.

Usage:
  export NMR_USER='you@example.com'  NMR_PASS='secret'
  ./numerator.py login
  ./numerator.py whoami
  ./numerator.py jobs                          # saved / recent reports
  ./numerator.py doc advanced-shopper-profile  # a report type's params (prompts)
  ./numerator.py search advanced-shopper-profile location --q midwest
  ./numerator.py job 12657345                  # job detail + items
  ./numerator.py data 12657345                 # pull the result tables
  ./numerator.py export 12657345 -o out.xlsx   # download + gunzip the xlsx
  ./numerator.py run advanced-shopper-profile --answers answers.json --name "My run"

Credentials: --user/--pass flags, or NMR_USER/NMR_PASS env vars. Session cookies
are cached in ~/.numerator_session so repeat commands don't re-authenticate.
"""
import argparse
import gzip
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Shared credential resolver (repo-root `common/`): env vars locally, the portal
# Worker inside the Pip container. Same logic for every CLI. realpath (not
# abspath) so the import still resolves when the entrypoint is symlinked onto
# PATH by install.sh (the symlink points back into the repo, where common/ lives).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from common import pip_creds

CONNECTOR = "numerator"
CRED_VARS = ["NMR_USER", "NMR_PASS"]

INSIGHTS = "https://insights.numerator.com"
AUTHZ = "https://authz.numerator.com"
SESSION_FILE = os.path.expanduser("~/.numerator_session")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")


# HTTPErrorProcessor that does NOT raise on 4xx/5xx and does NOT auto-follow
# redirects — we follow them manually so cookies set mid-chain (across the
# insights <-> authz hosts) are captured and we can inject the Okta sessionToken
# into the /authorize hop. (Same approach as satori-auth.js.)
class _PassThrough(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):
        return response
    https_response = http_response


class Client:
    def __init__(self):
        self.cj = http.cookiejar.LWPCookieJar(SESSION_FILE)
        if os.path.exists(SESSION_FILE):
            try:
                self.cj.load(ignore_discard=True)
            except Exception:
                pass
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            _PassThrough(),
        )

    def save(self):
        try:
            self.cj.save(ignore_discard=True)
        except Exception:
            pass

    def _csrf(self):
        for c in self.cj:
            if c.name == "csrftoken" and "numerator.com" in c.domain:
                return c.value
        return None

    # ---- low level: one hop, no auto-redirect, no raise ----
    def _hop(self, url, data=None, method=None, headers=None):
        h = {"User-Agent": UA, "Accept": "*/*"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        resp = self.opener.open(req, timeout=60)
        body = resp.read()
        return resp.status, resp.headers, body, resp.geturl()

    # ---- follow redirects manually (max 20). on_authorize lets login inject
    #      the sessionToken into the Okta /authorize hop. ----
    def request(self, url, data=None, method=None, headers=None, follow=True, on_redirect=None):
        cur_url, cur_method, cur_data = url, method, data
        for _ in range(20):
            if on_redirect:
                cur_url = on_redirect(cur_url)
            status, hdrs, body, final = self._hop(cur_url, cur_data, cur_method, headers)
            if follow and 300 <= status < 400 and hdrs.get("Location"):
                loc = urllib.parse.urljoin(cur_url, hdrs["Location"])
                if status in (301, 302, 303):
                    cur_method, cur_data = "GET", None
                cur_url = loc
                continue
            return status, hdrs, body, final
        raise RuntimeError("too many redirects")

    # ---- JSON helpers against the insights API ----
    def get_json(self, path, host=INSIGHTS):
        status, _, body, _ = self.request(host + path)
        return _parse(status, body, path)

    def _mutate(self, method, path, payload=None, host=INSIGHTS):
        data = json.dumps(payload).encode() if payload is not None else b""
        headers = {"Content-Type": "application/json", "Referer": INSIGHTS + "/"}
        csrf = self._csrf()
        if csrf:
            headers["X-CSRFToken"] = csrf
        status, _, body, _ = self.request(host + path, data=data, method=method, headers=headers)
        return _parse(status, body, path)

    def post_json(self, path, payload, host=INSIGHTS):
        return self._mutate("POST", path, payload, host)

    def put_json(self, path, payload, host=INSIGHTS):
        return self._mutate("PUT", path, payload, host)

    def delete_json(self, path, payload=None, host=INSIGHTS):
        return self._mutate("DELETE", path, payload, host)

    # ---- auth ----
    def logged_in(self):
        status, _, _, _ = self.request(INSIGHTS + "/api/user/")
        return status == 200

    def login(self, username, password):
        # 1. Okta primary auth -> one-time sessionToken (no MFA expected).
        data = json.dumps({
            "username": username, "password": password,
            "options": {"warnBeforePasswordExpired": True, "multiOptionalFactorEnroll": True},
        }).encode()
        status, _, body, _ = self.request(
            AUTHZ + "/api/v1/authn", data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        j = _parse(status, body, "/api/v1/authn")
        if j.get("status") != "SUCCESS" or not j.get("sessionToken"):
            raise RuntimeError(f"Okta auth not SUCCESS: status={j.get('status')} "
                               f"(factors/MFA not handled). body={str(j)[:200]}")
        token = j["sessionToken"]

        # 2. App OIDC code flow; inject sessionToken into the Okta /authorize hop
        #    so Okta issues the code without the login UI.
        injected = {"done": False}

        def inject(u):
            if not injected["done"] and "authz.numerator.com" in u and "/v1/authorize" in u:
                injected["done"] = True
                return u + ("&" if "?" in u else "?") + "sessionToken=" + urllib.parse.quote(token)
            return u

        status, _, _, final = self.request(
            INSIGHTS + "/sso/login?next=%2Fdashboard", follow=True, on_redirect=inject)
        if not self.logged_in():
            raise RuntimeError(f"login did not establish a session (last url={final}, "
                               f"sessionToken injected={injected['done']})")
        self.save()


def _parse(status, body, where):
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
    if status == 401:
        raise RuntimeError(f"{where}: 401 unauthorized — run `login` (or session expired)")
    if status < 200 or status >= 300:
        raise RuntimeError(f"{where}: HTTP {status}: {text[:300]}")
    if not text.strip():
        return None  # e.g. DELETE returns 200 with an empty body
    try:
        return json.loads(text)
    except Exception:
        raise RuntimeError(f"{where}: expected JSON, got: {text[:200]}")


def _no_sample(d):
    """True if a job item / result is tagged NO_SAMPLE (insufficient panel)."""
    return any(str(t).upper() == "NO_SAMPLE" for t in (d.get("tags") or []))


def _find_error_text(node):
    """Best-effort pull of a human-readable error out of a result's `layout`
    (e.g. the NO_SAMPLE message). The layout shape varies — often a JSX-ish
    string like `<Error description="..." header="..." />` — so we scan for a
    string that mentions insufficient sample (or sits under an `error` key) and
    extract the clean `description`/`header` if present."""
    hits = []

    def walk(n, key=None):
        if isinstance(n, str):
            if "insufficient sample" in n.lower() or (key and str(key).lower() == "error"):
                hits.append(n)
        elif isinstance(n, dict):
            for k, v in n.items():
                walk(v, k)
        elif isinstance(n, list):
            for x in n:
                walk(x, key)
    walk(node)
    if not hits:
        return None
    s = hits[0]
    m = re.search(r'description="([^"]+)"', s) or re.search(r'header="([^"]+)"', s)
    return m.group(1) if m else " ".join(s.split())[:200]


def _elapsed(start):
    s = int(time.monotonic() - start)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _wait_for_job(c, job_id, interval=30, timeout=1800):
    """Poll a job until it completes. Job status: 1=pending, 2=queued,
    3=running, 6=complete. Wait through the in-progress states; stop at
    complete (or an unexpected terminal status). Prints transitions to stderr."""
    names = {1: "pending", 2: "queued", 3: "running", 6: "complete"}
    in_progress = {1, 2, 3}
    start, last = time.monotonic(), None
    while True:
        j = c.get_json(f"/api/jobs/{job_id}")
        st = j.get("status")
        if st != last:
            print(f"  [{_elapsed(start)}] {names.get(st, f'status {st}')}", file=sys.stderr)
            last = st
        if st == 6 or st not in in_progress:
            return j
        if time.monotonic() - start > timeout:
            print(f"  gave up waiting after {_elapsed(start)} (status={st})", file=sys.stderr)
            return j
        time.sleep(interval)


def _wait_for_flow(c, flow_id, interval=30, timeout=1800):
    """Poll a narrative flow until it leaves the in-progress states. Flow status
    is a string: SETUP -> RUNNING -> COMPLETED. Prints transitions to stderr."""
    in_progress = {"SETUP", "RUNNING", "QUEUED", "PENDING"}
    start, last = time.monotonic(), None
    while True:
        f = c.get_json(f"/api/flows/{flow_id}")
        st = f.get("status")
        if st != last:
            print(f"  [{_elapsed(start)}] {st}", file=sys.stderr)
            last = st
        if st not in in_progress:
            return f
        if time.monotonic() - start > timeout:
            print(f"  gave up waiting after {_elapsed(start)} (status={st})", file=sys.stderr)
            return f
        time.sleep(interval)


# ───────────────────────── commands ─────────────────────────

def _credentials(args):
    """Resolve (user, password). Explicit --user/--pass win; otherwise defer to
    the shared resolver (env vars locally, the portal Worker in the container)."""
    if args.user and args.pw:
        return args.user, args.pw
    creds = pip_creds.resolve(CONNECTOR, CRED_VARS)
    return args.user or creds["NMR_USER"], args.pw or creds["NMR_PASS"]


def _client_logged_in(args):
    c = Client()
    if not c.logged_in():
        user, pw = _credentials(args)
        c.login(user, pw)
    return c


def cmd_login(args):
    c = Client()
    user, pw = _credentials(args)
    c.login(user, pw)
    me = c.get_json("/api/user/")
    print(f"logged in as {me.get('email') or me.get('username') or me}")


def cmd_whoami(args):
    c = _client_logged_in(args)
    print(json.dumps(c.get_json("/api/user/"), indent=2))


def cmd_jobs(args):
    c = _client_logged_in(args)
    qs = urllib.parse.urlencode({"page": args.page, "size": args.size,
                                 "sort_by": "created_datetime", "order_by": "desc"})
    res = c.get_json("/v2/api/jobs?" + qs)
    items = res.get("items", [])
    if args.json:
        print(json.dumps(res, indent=2)); return
    print(f"# {res.get('total','?')} jobs (page {res.get('page')}/{res.get('pages')})")
    for j in items:
        print(f"  {j.get('id'):>10}  {str(j.get('status')):>10}  {j.get('document_id'):28s}  {j.get('name')}")


def cmd_job_delete(args):
    """Delete one or more saved reports (DELETE /v2/api/jobs, array body — NOT
    DELETE /api/jobs/{id}, which returns 204 but is a no-op). Refuses to delete
    a job that's still in progress: the completion pipeline re-creates a job
    deleted mid-run, so it would just reappear once it finishes."""
    c = _client_logged_in(args)
    busy = {1: "pending", 2: "queued", 3: "running"}
    ok, skip = [], []
    for jid in (int(x) for x in args.ids):
        try:
            st = c.get_json(f"/api/jobs/{jid}").get("status")
        except RuntimeError as e:
            skip.append(f"job {jid}: could not check status ({e})")
            continue
        if st in busy:
            skip.append(f"job {jid}: still {busy[st]} — let it finish first "
                        f"(a job deleted mid-run reappears once complete)")
        else:
            ok.append(jid)
    if ok:
        c.delete_json("/v2/api/jobs", ok)
        print(f"deleted job(s): {', '.join(map(str, ok))}")
    for s in skip:
        print("NOT deleted: " + s, file=sys.stderr)
    if skip:
        sys.exit(1)


def cmd_docs(args):
    """List all report types (the catalog) from appcontext.routes."""
    c = _client_logged_in(args)
    ctx = c.get_json("/api/appcontext")
    found = []

    def kids(n):
        for k in ("children", "routes", "items", "subroutes"):
            if isinstance(n.get(k), list):
                return n[k]
        return []

    def walk(n):
        if not isinstance(n, dict):
            return
        if n.get("id") == "all-reports":
            for ch in kids(n):
                found.append(ch)
        for ch in kids(n):
            walk(ch)
    for r in ctx.get("routes", []):
        walk(r)
    if args.json:
        print(json.dumps(found, indent=2)); return
    print(f"# {len(found)} report types")
    for ch in found:
        label = ch.get("label") or ch.get("title") or ch.get("name")
        print(f"  {ch.get('id'):28s} {label}")
        if args.long and ch.get("description"):
            print(f"      {ch['description']}")


def cmd_groups(args):
    """List saved groups. type = product | store | people | trip."""
    c = _client_logged_in(args)
    t = args.type
    if t in ("product", "store"):
        res = c.get_json(f"/v2/api/customgroups/list/{t}")
    elif t == "people":
        res = c.get_json("/v2/api/peoplegroups/")
    elif t == "trip":
        res = c.get_json("/v2/api/tripgroups/")
    else:
        sys.exit("type must be one of: product, store, people, trip")
    items = res.get("items", res) if isinstance(res, dict) else res
    if args.json:
        print(json.dumps(res, indent=2)); return
    print(f"# {(res.get('total') if isinstance(res, dict) else len(items))} {t} groups")
    for g in items:
        print(f"  {str(g.get('id')):>10}  {g.get('name')}")


def _read_arg(v):
    """A CLI value that's either inline JSON or @path-to-json-file."""
    if v.startswith("@"):
        with open(os.path.expanduser(v[1:])) as f:
            return f.read()
    return v


def cmd_group_get(args):
    """Full detail of one custom (product/store) group — also a clone source."""
    c = _client_logged_in(args)
    g = c.get_json(f"/v2/api/customgroups/{args.id}")
    if args.json:
        print(json.dumps(g, indent=2)); return
    print(f"# group {g.get('id')}: {g.get('name')}  (linked={g.get('linked_attribute_id')}, panel={g.get('panel')})")
    print("definition:", json.dumps(g.get("definition"), indent=2))
    if g.get("pretty_definition"):
        print("pretty_definition:", json.dumps(g.get("pretty_definition"), indent=2))


def cmd_group_create(args):
    """Create a custom group. Clone an existing one's definition with --from, or
    pass a raw --definition (inline JSON or @file). definition = {label: rule}."""
    c = _client_logged_in(args)
    definition = {}
    if args.from_id:
        src = c.get_json(f"/v2/api/customgroups/{args.from_id}")
        definition = src.get("definition") or {}
    if args.definition:
        definition = json.loads(_read_arg(args.definition))
    res = c.post_json(f"/v2/api/customgroups?group_type={urllib.parse.quote(args.type)}",
                      {"name": args.name, "definition": definition})
    print(f"created {args.type} group {res.get('id')}: {res.get('name')}")
    if args.json:
        print(json.dumps(res, indent=2))


def cmd_group_update(args):
    """Update a custom group's definition (PUT). --definition inline JSON or @file,
    or clone another group's definition with --from."""
    c = _client_logged_in(args)
    payload = {}
    if args.from_id:
        payload["definition"] = c.get_json(f"/v2/api/customgroups/{args.from_id}").get("definition") or {}
    if args.definition:
        payload["definition"] = json.loads(_read_arg(args.definition))
    if args.name:
        payload["name"] = args.name
    if not payload:
        sys.exit("nothing to update — pass --definition, --from, and/or --name")
    res = c.put_json(f"/v2/api/customgroups/{args.id}", payload)
    print(f"updated group {args.id}")
    if args.json and res:
        print(json.dumps(res, indent=2))


def cmd_group_delete(args):
    """Delete one or more custom groups. The API takes an array body of ids."""
    c = _client_logged_in(args)
    ids = [int(x) for x in args.ids]
    c.delete_json("/v2/api/customgroups", ids)
    print(f"deleted group(s): {', '.join(map(str, ids))}")


# People / trip groups live on their own endpoints and carry an `answers`
# payload (not a custom-group `definition`). No working update endpoint exists,
# so the model is clone-and-create (--from) / delete + recreate.
_PGROUP = {"people": "peoplegroups", "trip": "tripgroups"}


def cmd_pgroup_create(args):
    """Create a people/trip group. Clone an existing one's answers with --from,
    or pass raw --answers (inline JSON or @file)."""
    c = _client_logged_in(args)
    answers, static_group = {}, args.static_group
    if args.from_id:
        src = c.get_json(f"/v2/api/{_PGROUP[args.kind]}/{args.from_id}")
        answers = src.get("answers") or {}
        static_group = static_group or src.get("static_group")
    if args.answers:
        answers = json.loads(_read_arg(args.answers))
    payload = {"name": args.name, "answers": answers,
               "static_group": static_group or "total_commerce"}
    res = c.post_json(f"/v2/api/{_PGROUP[args.kind]}/", payload)
    print(f"created {args.kind} group {res.get('id')}: {res.get('name')}")
    if args.json:
        print(json.dumps(res, indent=2))


def cmd_pgroup_delete(args):
    """Delete one or more people/trip groups (array body of ids, like groups)."""
    c = _client_logged_in(args)
    ids = [int(x) for x in args.ids]
    c.delete_json(f"/v2/api/{_PGROUP[args.kind]}", ids)
    print(f"deleted {args.kind} group(s): {', '.join(map(str, ids))}")


def cmd_doc(args):
    c = _client_logged_in(args)
    path = f"/api/documents/{urllib.parse.quote(args.document_id)}"
    if args.job_id:
        path += "?job_id=" + str(args.job_id)
    d = c.get_json(path)
    if args.json:
        print(json.dumps(d, indent=2)); return
    print(f"# {d.get('id')}  v{d.get('version')}  — {d.get('title')}")
    print(f"  madlib: {d.get('madlib')}")
    print(f"  can_export={d.get('can_export')} can_reprompt={d.get('can_reprompt')} can_share={d.get('can_share')}")
    print("  prompts:")
    for p in d.get("prompts", []):
        print(f"    {p.get('id'):24s} type={p.get('type')} required={p.get('required')} "
              f"search={p.get('search_enabled')}  {p.get('title')}")
    if d.get("default_answers"):
        print("  default_answers:", json.dumps(d["default_answers"])[:400])


def cmd_search(args):
    c = _client_logged_in(args)
    params = {"attributeId": args.attribute or args.prompt, "search": args.q or ""}
    params["narrativeId" if args.narrative else "documentId"] = args.document_id
    qs = urllib.parse.urlencode(params)
    res = c.get_json(f"/api/prompts/{urllib.parse.quote(args.prompt)}/search?" + qs)
    if args.json:
        print(json.dumps(res, indent=2)); return
    for o in res:
        print(f"  {o.get('id'):30s}  {o.get('name')}")


def cmd_job(args):
    c = _client_logged_in(args)
    j = c.get_json(f"/api/jobs/{args.job_id}")
    print(json.dumps(j, indent=2) if args.json else _job_summary(j))


def _job_summary(j):
    out = [f"# job {j.get('id')}  status={j.get('status')}  {j.get('name','')}"]
    items = j.get("items", j.get("job_items", [])) or []
    nosample = 0
    for it in items:
        flag = ""
        if _no_sample(it):
            nosample += 1
            flag = "  ⚠️ NO_SAMPLE (insufficient sample)"
        out.append(f"    item {it.get('id')}  status={it.get('status')}  "
                   f"{it.get('document_item',{}).get('name','')}{flag}")
    if nosample:
        out.append(f"  ⚠️ {nosample}/{len(items)} items returned insufficient sample")
    return "\n".join(out)


def cmd_data(args):
    c = _client_logged_in(args)
    # Need the item ids; the job detail lists them.
    job = c.get_json(f"/api/jobs/{args.job_id}")
    items = job.get("items") or job.get("job_items") or []
    item_ids = [args.item_id] if args.item_id else [it.get("id") for it in items]
    if not item_ids:
        # Fall back: some docs expose items directly under the job's document.
        sys.exit(f"no items found on job {args.job_id}; pass an explicit item id")
    results = []
    for iid in item_ids:
        results.append(c.get_json(f"/api/jobs/{args.job_id}/items/{iid}"))
    if args.json or not args.summary:
        print(json.dumps(results if len(results) > 1 else results[0], indent=2)); return
    for r in results:
        print(f"## {r.get('document_item',{}).get('name','item '+str(r.get('id')))}")
        err = _find_error_text(r.get("layout"))
        if _no_sample(r) or err:
            print("   ⚠️ NO_SAMPLE — " + (err or "insufficient sample to run this report for the answers provided"))
        if (r.get("summary") or {}).get("text"):
            print("   " + r["summary"]["text"])
        for tbl in r.get("data", []):
            print(f"   table {tbl.get('name')}: {len(tbl.get('data',[]) or [])} rows, "
                  f"cols={tbl.get('colLabels')} tooManyRows={tbl.get('tooManyRows')}")


def cmd_export(args):
    c = _client_logged_in(args)
    # /export 307s to a signed S3 URL for a gzipped xlsx.
    status, hdrs, body, final = c.request(
        INSIGHTS + f"/v2/api/jobs/{args.job_id}/export", follow=False)
    if status not in (301, 302, 307, 308) or not hdrs.get("Location"):
        raise SystemExit(f"export: expected redirect, got HTTP {status}: {body[:200]!r}")
    link = hdrs["Location"]
    # Signed URL — public, no cookies needed.
    s2, _, raw, _ = c.request(link, follow=True)
    if s2 != 200:
        raise SystemExit(f"export download HTTP {s2} for {link[:120]}")
    out = args.out or f"job_{args.job_id}.xlsx"
    data = raw
    if ".gz" in link.split("?")[0].lower() or (len(raw) > 2 and raw[0] == 0x1F and raw[1] == 0x8B):
        data = gzip.decompress(raw)
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data)} bytes)")


def cmd_run(args):
    c = _client_logged_in(args)
    with open(args.answers) as f:
        answers = json.load(f)
    # Accept either {"answers": {...}} or a bare answers map.
    if "answers" not in answers:
        answers = {"answers": answers}
    payload = {**answers, "documentId": args.document_id,
               "name": args.name or args.document_id}
    res = c.post_json(f"/api/documents/{urllib.parse.quote(args.document_id)}", payload)
    if res.get("promptErrors"):
        print("promptErrors:", json.dumps(res["promptErrors"], indent=2))
    job_id = res.get("jobId")
    print(f"jobId: {job_id}")
    if args.wait and job_id:
        print(_job_summary(_wait_for_job(c, job_id, args.interval)))


# ── narratives: packaged multi-report analyses with auto-written insights.
#    The flow mirrors reports: narrative=doc, narrative-run=run, flow=job,
#    flow-data=data. A run creates a `flow` whose `topics` each carry a `viz`
#    with tables + a human-readable `read_as` insight. ──

def cmd_narratives(args):
    """List narrative templates (packaged analyses w/ insights)."""
    c = _client_logged_in(args)
    res = c.get_json("/v2/api/narratives")
    if args.json:
        print(json.dumps(res, indent=2)); return
    print(f"# {len(res)} narratives")
    for n in res:
        print(f"  {n.get('id'):22s} {len(n.get('topic_ids') or []):>2} topics  {n.get('name')}")
        if args.long and n.get("description"):
            print(f"      {n['description']}")


def cmd_narrative(args):
    """Show a narrative's parameters (prompts). --flow-id reloads a run's answers."""
    c = _client_logged_in(args)
    path = f"/api/narratives/{urllib.parse.quote(args.narrative_id)}/"
    if args.flow_id:
        path += "?flow_id=" + str(args.flow_id)
    d = c.get_json(path)
    if args.json:
        print(json.dumps(d, indent=2)); return
    print(f"# {d.get('id')} — {d.get('name')}")
    print("  prompts:")
    for p in d.get("prompts", []):
        print(f"    {p.get('id'):20s} type={p.get('type')} required={p.get('required')} "
              f"search={p.get('search_enabled')}  {p.get('title')}")


def cmd_narrative_run(args):
    """Run a narrative -> flow_id. --answers = {promptId:[option,...]} or
    {answers:{...}} (note: date_range here is a bare [start,end] array).
    --wait polls until the flow completes."""
    c = _client_logged_in(args)
    with open(args.answers) as f:
        answers = json.load(f)
    if "answers" not in answers:
        answers = {"answers": answers}
    payload = {**answers, "name": args.name or args.narrative_id}
    res = c.post_json(f"/api/narratives/{urllib.parse.quote(args.narrative_id)}", payload)
    flow_id = res.get("flow_id")
    print(f"flow_id: {flow_id}")
    if args.wait and flow_id:
        print(_flow_summary(_wait_for_flow(c, flow_id, args.interval)))


def _flow_summary(f):
    out = [f"# flow {f.get('id')}  status={f.get('status')}  "
           f"{f.get('name','')} ({f.get('narrative_id')})"]
    for t in f.get("topics") or []:
        out.append(f"    topic {t.get('id')}  status={t.get('status')}  {t.get('title','')}")
    return "\n".join(out)


def cmd_flow(args):
    """Show a narrative run's (flow) status + topics."""
    c = _client_logged_in(args)
    f = c.get_json(f"/api/flows/{args.flow_id}")
    print(json.dumps(f, indent=2) if args.json else _flow_summary(f))


def cmd_flow_data(args):
    """Read a narrative run's topic results — each topic's tables + the
    auto-written insight (read_as). Optionally limit to one topic id."""
    c = _client_logged_in(args)
    flow = c.get_json(f"/api/flows/{args.flow_id}")
    topics = flow.get("topics") or []
    if args.topic_id:
        topics = [t for t in topics if str(t.get("id")) == str(args.topic_id)
                  or t.get("topic_id") == args.topic_id]
    if not topics:
        sys.exit(f"flow {args.flow_id} has no topics (status={flow.get('status')}); "
                 f"still running? try `flow {args.flow_id}` or narrative-run --wait")
    results = [c.get_json(f"/api/flows/flowtopics/{t.get('id')}") for t in topics]
    if args.json or not args.summary:
        print(json.dumps(results if len(results) > 1 else results[0], indent=2)); return
    for r in results:
        viz = r.get("viz") or {}
        print(f"## {viz.get('clean_title') or r.get('title') or r.get('topic_id')}  [{r.get('status')}]")
        for ds in viz.get("datasets", []):
            insight = ds.get("clean_read_as") or ds.get("read_as")
            if insight:
                print("   " + insight)
            if ds.get("data"):
                cols = [col.get("Header") for col in ds.get("columns", [])]
                print(f"   table: {len(ds['data'])} rows, cols={cols}")


def cmd_flow_delete(args):
    """Delete one or more narrative runs (DELETE /v2/api/flows, body
    {"flow_ids": [...]}). Refuses to delete a flow that's still SETUP/RUNNING,
    mirroring job-delete (let it finish first)."""
    c = _client_logged_in(args)
    busy = {"SETUP", "RUNNING"}
    ok, skip = [], []
    for fid in (int(x) for x in args.ids):
        try:
            st = c.get_json(f"/api/flows/{fid}").get("status")
        except RuntimeError as e:
            skip.append(f"flow {fid}: could not check status ({e})")
            continue
        if st in busy:
            skip.append(f"flow {fid}: still {st} — let it finish first")
        else:
            ok.append(fid)
    if ok:
        c.delete_json("/v2/api/flows", {"flow_ids": ok})
        print(f"deleted flow(s): {', '.join(map(str, ok))}")
    for s in skip:
        print("NOT deleted: " + s, file=sys.stderr)
    if skip:
        sys.exit(1)


def cmd_raw(args):
    """GET an arbitrary insights API path — for exploring/probing endpoints."""
    c = _client_logged_in(args)
    path = args.path if args.path.startswith("/") else "/" + args.path
    status, _, body, _ = c.request(INSIGHTS + path)
    text = body.decode("utf-8", "replace")
    print(f"# HTTP {status}")
    try:
        print(json.dumps(json.loads(text), indent=2)[:args.limit])
    except Exception:
        print(text[:args.limit])


def cmd_seed_from_capture(args):
    """Bootstrap the session cache from a Playwright capture state.json (lets us
    test the data layer before the login flow is trusted)."""
    st = json.load(open(args.state))
    c = Client()
    n = 0
    for ck in st.get("cookies", []):
        if "numerator.com" not in ck["domain"]:
            continue
        c.cj.set_cookie(http.cookiejar.Cookie(
            0, ck["name"], ck["value"], None, False,
            ck["domain"], True, ck["domain"].startswith("."),
            ck.get("path", "/"), True, ck.get("secure", True),
            None, False, None, None, {}))
        n += 1
    c.save()
    print(f"seeded {n} numerator cookies into {SESSION_FILE}")


EPILOG = """\
setup:
  1. Python 3 (any recent version; no packages to install — stdlib only).
  2. Your Numerator login, via env vars or flags:
       export NMR_USER='you@example.com'
       export NMR_PASS='your-password'
     (or pass --user / --pass on any command). The login session is cached in
     ~/.numerator_session so you only log in once.

common flows:
  python3 numerator.py login                 # sign in once
  python3 numerator.py docs --long           # what report types exist
  python3 numerator.py jobs                   # your saved / recent reports
  python3 numerator.py data <jobId> --summary # read a report's results
  python3 numerator.py export <jobId> -o out.xlsx

Run `python3 numerator.py <command> -h` for any command's own options.
"""


def main():
    ap = argparse.ArgumentParser(
        description="Numerator Insights CLI — headless, zero-dependency (stdlib only). "
                    "Does anything you can do clicking around Numerator Insights: browse and "
                    "run reports, read/export results, and manage custom groups.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", help="Numerator username (or NMR_USER env var)")
    ap.add_argument("--pass", dest="pw", help="Numerator password (or NMR_PASS env var)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")

    sub.add_parser("login", help="sign in to Numerator (caches the session)").set_defaults(fn=cmd_login)
    sub.add_parser("whoami", help="show the signed-in user").set_defaults(fn=cmd_whoami)

    p = sub.add_parser("jobs", help="list your saved / recent reports"); p.add_argument("--page", type=int, default=1)
    p.add_argument("--size", type=int, default=20); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_jobs)

    p = sub.add_parser("docs", help="list all report types (the catalog)"); p.add_argument("--json", action="store_true")
    p.add_argument("--long", action="store_true", help="show descriptions")
    p.set_defaults(fn=cmd_docs)

    p = sub.add_parser("groups", help="list saved groups (product/store/people/trip)")
    p.add_argument("type", choices=["product", "store", "people", "trip"])
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_groups)

    p = sub.add_parser("group-get", help="show one custom group's definition")
    p.add_argument("id"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_group_get)

    p = sub.add_parser("group-create", help="create a custom product/store group")
    p.add_argument("name")
    p.add_argument("--type", choices=["product", "store"], default="product")
    p.add_argument("--from", dest="from_id", help="clone this group's definition")
    p.add_argument("--definition", help="definition as inline JSON or @file")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_group_create)

    p = sub.add_parser("group-update", help="update a custom group's definition")
    p.add_argument("id")
    p.add_argument("--from", dest="from_id", help="clone this group's definition")
    p.add_argument("--definition", help="definition as inline JSON or @file")
    p.add_argument("--name"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_group_update)

    p = sub.add_parser("group-delete", help="delete one or more custom groups by id")
    p.add_argument("ids", nargs="+"); p.set_defaults(fn=cmd_group_delete)

    for kind in ("people", "trip"):
        p = sub.add_parser(f"{kind}-group-create", help=f"create a {kind} group (clone with --from)")
        p.add_argument("name")
        p.add_argument("--from", dest="from_id", help="clone this group's answers")
        p.add_argument("--answers", help="answers as inline JSON or @file (overrides --from)")
        p.add_argument("--static-group", dest="static_group",
                       help="static_group (default: cloned, else total_commerce)")
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=cmd_pgroup_create, kind=kind)

        p = sub.add_parser(f"{kind}-group-delete", help=f"delete one or more {kind} groups by id")
        p.add_argument("ids", nargs="+"); p.set_defaults(fn=cmd_pgroup_delete, kind=kind)

    p = sub.add_parser("doc", help="show one report type's parameters (prompts)")
    p.add_argument("document_id")
    p.add_argument("--job-id"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doc)

    p = sub.add_parser("search", help="look up valid values for a report parameter")
    p.add_argument("document_id"); p.add_argument("prompt")
    p.add_argument("--attribute", help="attributeId (defaults to prompt name)")
    p.add_argument("--narrative", action="store_true", help="treat the id as a narrativeId")
    p.add_argument("--q", default=""); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("job", help="show a report run's status + items")
    p.add_argument("job_id"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_job)

    p = sub.add_parser("job-delete", help="delete one or more saved reports by id")
    p.add_argument("ids", nargs="+"); p.set_defaults(fn=cmd_job_delete)

    p = sub.add_parser("data", help="read a report's result tables (+ AI summary)")
    p.add_argument("job_id"); p.add_argument("item_id", nargs="?")
    p.add_argument("--summary", action="store_true", help="print a compact summary instead of full JSON")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_data)

    p = sub.add_parser("export", help="download a report as an .xlsx file")
    p.add_argument("job_id"); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("run", help="run a new report (submit parameter selections)")
    p.add_argument("document_id")
    p.add_argument("--answers", required=True, help="JSON file: {promptId:[option,...]} or {answers:{...}}")
    p.add_argument("--name")
    p.add_argument("--wait", action="store_true", help="poll until the report finishes")
    p.add_argument("--interval", type=int, default=30, help="--wait poll interval in seconds (default 30)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("narratives", help="list narrative templates (packaged analyses)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--long", action="store_true", help="show descriptions")
    p.set_defaults(fn=cmd_narratives)

    p = sub.add_parser("narrative", help="show a narrative's parameters (prompts)")
    p.add_argument("narrative_id"); p.add_argument("--flow-id", dest="flow_id")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_narrative)

    p = sub.add_parser("narrative-run", help="run a narrative (returns a flow_id)")
    p.add_argument("narrative_id")
    p.add_argument("--answers", required=True, help="JSON file: {promptId:[option,...]} or {answers:{...}}")
    p.add_argument("--name")
    p.add_argument("--wait", action="store_true", help="poll until the flow finishes")
    p.add_argument("--interval", type=int, default=30, help="--wait poll interval in seconds (default 30)")
    p.set_defaults(fn=cmd_narrative_run)

    p = sub.add_parser("flow", help="show a narrative run's status + topics")
    p.add_argument("flow_id"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_flow)

    p = sub.add_parser("flow-data", help="read a narrative run's topic results (+ insights)")
    p.add_argument("flow_id"); p.add_argument("topic_id", nargs="?")
    p.add_argument("--summary", action="store_true", help="compact summary instead of full JSON")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_flow_data)

    p = sub.add_parser("flow-delete", help="delete one or more narrative runs by flow id")
    p.add_argument("ids", nargs="+"); p.set_defaults(fn=cmd_flow_delete)

    p = sub.add_parser("raw", help="GET an arbitrary API path (for power users)")
    p.add_argument("path"); p.add_argument("--limit", type=int, default=4000)
    p.set_defaults(fn=cmd_raw)

    # dev-only bootstrap; omitting help= keeps it invocable but hidden from --help.
    p = sub.add_parser("seed-from-capture")
    p.add_argument("state"); p.set_defaults(fn=cmd_seed_from_capture)

    if len(sys.argv) == 1:
        ap.print_help()
        return
    args = ap.parse_args()
    try:
        args.fn(args)
    except (RuntimeError, urllib.error.URLError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
