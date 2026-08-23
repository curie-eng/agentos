#!/usr/bin/env python3
"""Verify the security layers end to end, both directions, on an enforcing CNI.

Four checks, each with its own negative or positive control so a pass cannot be
vacuous:

  1. Does the CNI enforce NetworkPolicy at all?  (if not, 2-4 say nothing)
  2. BOUND pod + no token   -> must be REJECTED   (positive control for the gate)
  3. WARM pod + no token    -> observed behaviour (the gap)
  4. sandbox A -> sandbox B:8080 -> must be BLOCKED by A's default-deny egress
"""
import json, os, subprocess, sys, time
NS = "curie"
CTX = os.environ.get("VER_CONTEXT", "curie-ver")
assert CTX.startswith("curie-ver"), f"refusing context {CTX!r}"
REF = os.environ["BUNDLE_REF"]
G, R, Y, B, D, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"
OUT = {}

def kc(*a, inp=None, tries=3, ns=True):
    cmd = ["kubectl", "--context", CTX] + (["-n", NS] if ns else []) + ["--request-timeout=25s"] + list(a)
    for i in range(tries):
        r = subprocess.run(cmd, capture_output=True, text=True, input=inp)
        if r.returncode == 0:
            return r.stdout
        if "NotFound" in (r.stderr or ""):
            return ""
        time.sleep(2 ** i)
    return ""

ENV = [{"name": "CURIE_BUNDLE_REF", "value": REF},
       {"name": "CURIE_PLUGIN_DIR", "value": "/bundles/current"},
       {"containerName": "bundle-fetch", "name": "CURIE_BUNDLE_REF", "value": REF},
       {"containerName": "bundle-extract", "name": "CURIE_BUNDLE_REF", "value": REF}]
VK_POOL, VK_TMPL = "ver-pool", "ver-runner"

def pool(n=2):
    t = json.loads(kc("get", "sandboxtemplate", "curie-runner", "-o", "json"))
    t["metadata"] = {"name": VK_TMPL, "namespace": NS, "labels": {"ver": "1"}}
    def setenv(c, k, v):
        for e in c.setdefault("env", []):
            if e.get("name") == k:
                e.clear(); e["name"] = k; e["value"] = v; return
        c["env"].append({"name": k, "value": v})
    def walk(o):
        if isinstance(o, dict):
            if o.get("name") == "runner" and "image" in o:
                setenv(o, "CURIE_BUNDLE_REF", REF); setenv(o, "CURIE_PLUGIN_DIR", "/bundles/current")
            if o.get("name") in ("bundle-fetch", "bundle-extract"):
                setenv(o, "CURIE_BUNDLE_REF", REF)
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for i in o: walk(i)
    walk(t["spec"]); kc("apply", "-f", "-", inp=json.dumps(t))
    kc("apply", "-f", "-", inp=json.dumps({
        "apiVersion": "extensions.agents.x-k8s.io/v1beta1", "kind": "SandboxWarmPool",
        "metadata": {"name": VK_POOL, "namespace": NS, "labels": {"ver": "1"}},
        "spec": {"replicas": n, "updateStrategy": {"type": "OnReplenish"},
                 "sandboxTemplateRef": {"name": VK_TMPL}}}))
    t0 = time.time()
    while time.time() - t0 < 300:
        s = kc("get", "sandboxwarmpool", VK_POOL, "-o", "jsonpath={.status.readyReplicas}")
        if s.strip() and int(s) >= n: return True
        time.sleep(3)
    return False

def claim(name, p, env):
    body = {"apiVersion": "extensions.agents.x-k8s.io/v1beta1", "kind": "SandboxClaim",
            "metadata": {"name": name, "namespace": NS, "labels": {"ver": "1"}},
            "spec": {"warmPoolRef": {"name": p}}}
    if env: body["spec"]["env"] = env
    kc("apply", "-f", "-", inp=json.dumps(body))
    t0 = time.time()
    while time.time() - t0 < 150:
        sb = kc("get", "sandboxclaim", name, "-o", "jsonpath={.status.sandbox.name}").strip()
        if sb:
            rdy = kc("get", "pod", sb, "-o",
                     'jsonpath={.status.containerStatuses[?(@.name=="runner")].ready}').strip()
            if rdy == "true": return sb
        time.sleep(0.4)
    return None

def has_token(pod):
    e = kc("get", "pod", pod, "-o",
           'jsonpath={.spec.containers[?(@.name=="runner")].env[?(@.name=="CURIE_RUNNER_TOKEN")].name}')
    return bool(e.strip())

def post_event(pod, note):
    """POST /v1/event from INSIDE the pod, no Authorization header."""
    script = ("import json,urllib.request,urllib.error\n"
              "f={'kind':'event','type':'message','text':'probe','ts':'1','user':'U'}\n"
              "r=urllib.request.Request('http://localhost:8080/v1/event',"
              "data=json.dumps(f).encode(),headers={'Content-Type':'application/json'},method='POST')\n"
              "try:\n"
              "    resp=urllib.request.urlopen(r,timeout=90); print('HTTP', resp.status)\n"
              "except urllib.error.HTTPError as e: print('HTTP', e.code)\n"
              "except Exception as e: print('ERR', type(e).__name__)\n")
    out = kc("exec", pod, "-c", "runner", "--", "/app/.venv/bin/python", "-c", script, tries=2)
    line = [l for l in out.strip().splitlines() if l.startswith(("HTTP", "ERR"))]
    return (line[-1] if line else "no-output")

print(f"{B}=== verifying the security layers on an enforcing CNI ==={X}\n")

# ---- 1. Is NetworkPolicy enforced? ------------------------------------------
print(f"{B}[1] does the CNI enforce NetworkPolicy?{X}")
cni = kc("get", "pods", "-l", "k8s-app=calico-node", "--no-headers", ns=False)
calico = cni.count("1/1")
print(f"    calico-node pods ready: {calico}")
OUT["cni_calico_ready"] = calico

if not pool(2):
    print(f"{R}pool never warmed; aborting{X}"); sys.exit(1)

# ---- 2/3. token presence and gate behaviour ---------------------------------
print(f"\n{B}[2] BOUND pod (worker-style claim, carries a token){X}")
bound = claim("ver-bound", VK_POOL, ENV)
tok_b = has_token(bound) if bound else None
print(f"    pod={bound}  CURIE_RUNNER_TOKEN present: {G if tok_b else R}{tok_b}{X}")
res_b = post_event(bound, "bound") if bound else "n/a"
ok2 = res_b.startswith("HTTP 4")
print(f"    unauthenticated POST /v1/event -> {(G if ok2 else R)}{res_b}{X}"
      f"   {'(rejected: gate works)' if ok2 else '(ACCEPTED: gate not enforcing)'}")
OUT["bound_has_token"], OUT["bound_unauth_result"] = tok_b, res_b

print(f"\n{B}[3] WARM pool pod (no claim, so no token was ever minted){X}")
warm = [p for p in kc("get", "pods", "-l", "agents.x-k8s.io/warm-pool-sandbox",
                      "-o", "jsonpath={range .items[*]}{.metadata.name} {end}").split()
        if p != bound]
w = warm[0] if warm else None
tok_w = has_token(w) if w else None
print(f"    pod={w}  CURIE_RUNNER_TOKEN present: {R if not tok_w else G}{tok_w}{X}")
res_w = post_event(w, "warm") if w else "n/a"
print(f"    unauthenticated POST /v1/event -> {(R if res_w.startswith('HTTP 2') else G)}{res_w}{X}"
      f"   {'(ACCEPTED: the gap)' if res_w.startswith('HTTP 2') else ''}")
OUT["warm_has_token"], OUT["warm_unauth_result"] = tok_w, res_w

# ---- 4. can sandbox A reach sandbox B on the ACI port? ----------------------
print(f"\n{B}[4] can one sandbox reach another sandbox's ACI port?{X}")
ip_w = kc("get", "pod", w, "-o", "jsonpath={.status.podIP}").strip() if w else ""
print(f"    {D}target: {w} at {ip_w}:8080, dialled FROM {bound}{X}")
script = ("import socket\n"
          "s=socket.socket(); s.settimeout(8)\n"
          "try:\n"
          f"    s.connect(('{ip_w}',8080)); print('CONNECTED')\n"
          "except Exception as e: print('BLOCKED', type(e).__name__)\n")
r4 = kc("exec", bound, "-c", "runner", "--", "/app/.venv/bin/python", "-c", script, tries=2)
verdict4 = [l for l in r4.strip().splitlines() if l.startswith(("CONNECTED", "BLOCKED"))]
v4 = verdict4[-1] if verdict4 else "no-output"
blocked = v4.startswith("BLOCKED")
print(f"    result: {(G if blocked else R)}{v4}{X}"
      f"   {'(default-deny egress holds)' if blocked else '(REACHABLE: egress not blocking)'}")
OUT["sandbox_to_sandbox"] = v4

# negative control: can it reach something it IS allowed to? DNS.
script_dns = ("import socket\n"
              "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(5)\n"
              "try:\n"
              "    socket.getaddrinfo('kubernetes.default.svc.cluster.local',None); print('DNS_OK')\n"
              "except Exception as e: print('DNS_FAIL', type(e).__name__)\n")
rdns = kc("exec", bound, "-c", "runner", "--", "/app/.venv/bin/python", "-c", script_dns, tries=2)
dns = [l for l in rdns.strip().splitlines() if l.startswith("DNS")]
print(f"    {D}non-vacuity control -- an ALLOWED egress (DNS): {dns[-1] if dns else '?'}{X}")
OUT["dns_control"] = dns[-1] if dns else None

kc("delete", "sandboxclaim", "-l", "ver=1", "--ignore-not-found")
kc("delete", "sandboxwarmpool", VK_POOL, "--ignore-not-found")
kc("delete", "sandboxtemplate", VK_TMPL, "--ignore-not-found")
json.dump(OUT, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json"), "w"), indent=1)
print(f"\n{D}written results.json{X}")
