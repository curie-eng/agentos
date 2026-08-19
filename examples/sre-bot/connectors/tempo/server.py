"""A read-only MCP connector for Tempo, reached through Grafana.

Why this exists
---------------
`mcp-grafana` has no tool that speaks Tempo, so the bot could see the datasource
in `list_datasources` and never read a span. Three other routes were tried and
measured before this one was written:

* **`proxied`** -- discovery runs against Grafana's own `/api/mcp`, which is
  behind the `mcpServer` feature toggle most Grafana installs do not enable.
* **Sift** (`find_slow_requests` reads Tempo) -- hidden by `-disable-write`
  (it creates an investigation, which is a write), and dead anyway wherever
  `grafana-ml-app` is not installed: the endpoint 404s.
* **`run_panel_query` on a Tempo-backed panel** -- refuses outright:
  ``datasource type 'tempo' is not supported by run_panel_query``. It carries an
  allowlist (prometheus, loki, clickhouse, cloudwatch, influxdb, bigquery) and
  tempo is not on it. This was recommended as the cheap route before anyone
  tested it; it does not work.

What is left is the datasource proxy, which does work and needs no new
credential: Grafana authenticates the request with the same Viewer token the
other connector already holds, and forwards it to Tempo.

Why NOT talk to Tempo directly
------------------------------
A typical Tempo is `ClusterIP` with no ingress, and it has no authentication at
all -- it trusts its network. Reaching it directly would mean exposing it and
then building auth and TLS in front of it: rebuilding what Grafana already
provides. Going through Grafana is the front door, not a workaround.

Shape
-----
Same contract as every other connector here: the sandbox learns a URL and never
a credential, this process holds the token, and every tool is annotated
`readOnlyHint`. That annotation is a hint to the model, not a boundary -- the
boundary is the Viewer role, which 403s a write server-side.

`readOnlyHint` is also NOT a safety proof on its own: a tool can return a
credential and still be a read. That is exactly how `configuration_view` shipped
in the Kubernetes connector for four days. Nothing here returns configuration;
the tools return spans and tag values, and the token is never echoed.
"""

# NOTE: no `from __future__ import annotations` here, deliberately. It turns
# every annotation into a string, and FastMCP introspects tool signatures with
# `issubclass(param.annotation, Context)` -- which then raises
# `TypeError: issubclass() arg 1 must be a class` at import time, before the
# server ever binds a port. Python 3.12 parses `dict[str, Any] | None` natively,
# so the import buys nothing here and costs the whole process.

import logging
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

log = logging.getLogger("tempo-mcp")

GRAFANA_URL = os.environ.get("GRAFANA_URL", "").rstrip("/")
TOKEN = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
# `__Tempo__` is the uid a provisioned Tempo datasource commonly carries, and it
# is stable once provisioned -- but it is an env var rather than a constant so a
# second install does not need a code change. Find yours with the grafana
# connector's `list_datasources` before assuming this default fits.
DS_UID = os.environ.get("TEMPO_DATASOURCE_UID", "__Tempo__")
TIMEOUT = float(os.environ.get("TEMPO_TIMEOUT_SECONDS", "30"))

# Tempo will happily stream a very large trace. The sandbox pays for that in
# context, so cap it here rather than hoping callers pass a sane limit.
MAX_LIMIT = 50
DEFAULT_LIMIT = 20

# A HARD CEILING ON THE RESPONSE BODY, and `limit` does not provide it.
#
# `limit` bounds how many traces a SEARCH returns; it says nothing about how big
# one of them is. `get_trace` fetches a single trace whole, and a trace from a
# busy request can carry thousands of spans -- megabytes of JSON from one call
# that looked bounded.
#
# Two things break at that size, both worse than a refusal. This connector runs
# under a 256Mi limit, so a big enough body is an OOMKill: the pod dies, every
# in-flight call dies with it, and the symptom is a connector that "randomly
# restarts" rather than a query that was too big. And whatever survives gets
# pasted into a chat channel, where it costs the model's whole context to say
# nothing a summary would not have said better.
#
# So refuse instead, with a sentence naming what to do about it. 1 MiB is
# comfortably more than any answerable trace and far under the memory ceiling.
MAX_RESPONSE_BYTES = 1024 * 1024

mcp = FastMCP(
    "tempo",
    host=os.environ.get("BIND_ADDRESS", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8000")),
    # Mount at "/" so BOTH /mcp and /mcp/ are served.
    #
    # FastMCP's default mounts the handler at "/mcp", which makes Starlette
    # 307-redirect a POST to "/mcp" over to "/mcp/". Curie builds the sandbox's
    # URL as `http://<service>:8000/mcp` -- matching the other two connectors,
    # which serve that path directly -- and an MCP client is not obliged to
    # replay a POST body across a redirect. The symptom would be a connector
    # that is up, healthy, answers curl, and registers zero tools.
    streamable_http_path="/",
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


def _proxy(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET one Tempo path through Grafana's datasource proxy.

    Every error is returned as a STRING rather than raised. A raised exception
    reaches the model as a stack trace, which it will either paste into Slack or
    treat as a transient failure and retry. A sentence it can read out loud is
    more useful and stops the retry loop.
    """

    if not GRAFANA_URL or not TOKEN:
        return "not configured: GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN must both be set"
    url = f"{GRAFANA_URL}/api/datasources/proxy/uid/{quote(DS_UID)}{path}"
    try:
        r = httpx.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except httpx.TimeoutException:
        return f"Tempo did not respond within {TIMEOUT:g}s. Narrow the time range and try again."
    except httpx.HTTPError as exc:
        # str(exc) carries the URL, never the Authorization header.
        return f"could not reach Grafana: {exc}"

    if r.status_code == 401 or r.status_code == 403:
        return (
            f"Grafana refused the request ({r.status_code}). The service account token is "
            "missing or lacks access to the Tempo datasource. This will not fix itself on retry."
        )
    if r.status_code == 404:
        return (
            f"no such Tempo endpoint or datasource uid {DS_UID!r} ({r.status_code}). "
            "Check TEMPO_DATASOURCE_UID."
        )
    if r.status_code >= 400:
        return f"Tempo returned {r.status_code}: {r.text[:300]}"

    # Check the advertised size first so an oversized body can be refused
    # without parsing it, then fall back to what actually arrived -- Content-
    # Length is absent on a chunked response, which is exactly how a very large
    # trace tends to come back.
    too_big = _oversized(r)
    if too_big is not None:
        return too_big

    try:
        return r.json()
    except ValueError:
        return f"Tempo returned non-JSON: {r.text[:300]}"


def _oversized(r: httpx.Response) -> str | None:
    """The refusal message if this body exceeds the cap, else None."""

    declared = r.headers.get("content-length")
    size = None
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            size = None
    if size is None:
        size = len(r.content)
    if size <= MAX_RESPONSE_BYTES:
        return None
    return (
        f"the response is {size} bytes, over this connector's {MAX_RESPONSE_BYTES}-byte "
        "cap, so it was not read. Narrow the query: search with a tighter TraceQL "
        "filter and a smaller time range to find the span you want, rather than "
        "fetching a whole trace. This is a ceiling, not a transient error -- the "
        "same call will be refused again."
    )


@mcp.tool(annotations=READ_ONLY)
def search_traces(
    query: str,
    start: str = "",
    end: str = "",
    limit: int = DEFAULT_LIMIT,
) -> Any:
    """Search traces with TraceQL.

    `query` is TraceQL, e.g. `{duration > 500ms}`,
    `{status = error}`, or `{resource.service.name = "my-api" && duration > 1s}`.

    IMPORTANT: the service name in a trace is whatever the instrumentation was
    configured to report, which is often NOT the Kubernetes Deployment name --
    `my-api` where the workload is called `api` is the common shape. A wrong
    name matches nothing and looks like an absence of traces rather than a wrong
    query, so call `list_trace_tag_values` on `resource.service.name` and use
    what it returns rather than guessing from the workload name.

    `start` and `end` are Unix seconds. Omit BOTH to let Tempo use its default
    window, which is roughly the last hour -- so an empty result may mean
    "nothing recently" rather than "nothing ever". Widen the range before
    reporting an absence.
    """

    limit = max(1, min(int(limit), MAX_LIMIT))
    params: dict[str, Any] = {"q": query, "limit": limit}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return _proxy("/api/search", params)


@mcp.tool(annotations=READ_ONLY)
def get_trace(trace_id: str) -> Any:
    """Fetch one complete trace by ID, with all of its spans.

    Get the ID from `search_traces` first. A trace can be large; prefer
    searching and reading the summary unless the span detail is the question.

    Responses over 1 MiB are REFUSED rather than returned, so a trace from a
    very busy request will not come back at all. That is a ceiling, not a
    transient failure -- narrow the search instead of retrying.
    """

    trace_id = trace_id.strip()
    if not trace_id:
        return "trace_id is required -- get one from search_traces"
    return _proxy(f"/api/traces/{quote(trace_id)}")


@mcp.tool(annotations=READ_ONLY)
def list_trace_tags() -> Any:
    """List the span/resource attribute names Tempo knows about.

    Use this to discover what a TraceQL query can filter on before guessing at
    an attribute name.
    """

    return _proxy("/api/search/tags")


@mcp.tool(annotations=READ_ONLY)
def list_trace_tag_values(tag: str) -> Any:
    """List the observed values for one tag, e.g. `resource.service.name`.

    This is how to find the real service names rather than assuming them.
    """

    tag = tag.strip()
    if not tag:
        return "tag is required, e.g. resource.service.name"
    return _proxy(f"/api/v2/search/tag/{quote(tag)}/values")


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr)
    required = (("GRAFANA_URL", GRAFANA_URL), ("GRAFANA_SERVICE_ACCOUNT_TOKEN", TOKEN))
    missing = [name for name, value in required if not value]
    if missing:
        # Refuse to start rather than serve tools that fail on every call. A
        # connector that answers "not configured" to everything looks healthy to
        # Kubernetes and is useless to the agent.
        log.error("refusing to start: missing %s", ", ".join(missing))
        return 1
    log.info("tempo connector -> %s (datasource %s)", GRAFANA_URL, DS_UID)
    mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
