"""Shared credential resolution for the scraper CLIs — zero-dependency (stdlib).

ONE helper, used by every CLI's `login` command, so credential handling is
written once and improved once for all of them.

Two environments, one code path:

  * Local (your own shell / Claude): you export the CLI's documented env vars
    (e.g. NMR_USER / NMR_PASS). resolve() finds them and just hands them back —
    it never touches the network.

  * Pip container: those vars are NOT set. Instead the container has
    PIP_WORKER_URL + PIP_AGENT_TOKEN (injected per conversation by the portal
    Worker). resolve() then asks the Worker for THIS connector's credentials
    for the pinned client, just-in-time. No credentials sit in the container at
    rest, and Pip never has to know any of this — it just runs the CLI.

The Worker contract:

    GET {PIP_WORKER_URL}/api/agent-tools/creds/{connector}
        Authorization: Bearer {PIP_AGENT_TOKEN}
        CF-Access-Client-Id / CF-Access-Client-Secret   (if those env vars are set)
    -> 200 {"NMR_USER": "...", "NMR_PASS": "..."}

The JSON keys are the CLI's own env var names — so this helper stays generic and
the Worker route is the only place that maps a connector to its secret store.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

WORKER_URL_VAR = "PIP_WORKER_URL"
AGENT_TOKEN_VAR = "PIP_AGENT_TOKEN"


class CredentialError(RuntimeError):
    """Couldn't resolve credentials. Subclass of RuntimeError so a CLI's existing
    `except RuntimeError` turns it into a clean `error: ...` exit."""


def resolve(connector, var_names):
    """Return {var_name: value} for `var_names`, from env or the portal Worker.

    connector: the connector id used in the Worker route (e.g. "numerator").
    var_names: the env var names this CLI needs, e.g. ["NMR_USER", "NMR_PASS"].

    Resolution order:
      1. If every var is already in the environment, use those (local path, and
         also the case where the Worker pre-injected them).
      2. Otherwise fetch them from the Worker (Pip container path) and cache the
         values back into os.environ for the rest of the process.
    Raises CredentialError with an actionable message if neither path works.
    """
    if all(os.environ.get(v) for v in var_names):
        return {v: os.environ[v] for v in var_names}

    base = os.environ.get(WORKER_URL_VAR)
    token = os.environ.get(AGENT_TOKEN_VAR)
    if not base or not token:
        raise CredentialError(
            f"{connector}: no credentials available. Set {' / '.join(var_names)} "
            f"in your environment, or run inside the Pip container with "
            f"{WORKER_URL_VAR} + {AGENT_TOKEN_VAR} set."
        )

    creds = _fetch(base, token, connector)
    out = {}
    for v in var_names:
        if v not in creds:
            raise CredentialError(
                f"{connector}: creds endpoint did not return '{v}' "
                f"(got: {', '.join(creds) or 'nothing'})"
            )
        out[v] = creds[v]
        os.environ.setdefault(v, creds[v])
    return out


def _fetch(base, token, connector):
    url = base.rstrip("/") + "/api/agent-tools/creds/" + urllib.parse.quote(connector)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    # Forward the Cloudflare Access service token if present — without it the
    # call gets an HTML login page instead of JSON (same as the MCP skills).
    for header, env_var in (("CF-Access-Client-Id", "CF_ACCESS_CLIENT_ID"),
                            ("CF-Access-Client-Secret", "CF_ACCESS_CLIENT_SECRET")):
        val = os.environ.get(env_var)
        if val:
            headers[header] = val

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise CredentialError(f"{connector}: creds endpoint HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise CredentialError(f"{connector}: cannot reach creds endpoint {url}: {e.reason}")

    try:
        data = json.loads(body)
    except Exception:
        raise CredentialError(f"{connector}: creds endpoint returned non-JSON: {body[:200]}")
    if not isinstance(data, dict):
        raise CredentialError(
            f"{connector}: expected a JSON object of credentials, got {type(data).__name__}")
    return data
