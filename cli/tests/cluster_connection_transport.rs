//! Binary level regression coverage for cluster API credential transport.
//!
//! The release key is discovered through a fake external `kubectl`, while both
//! candidate HTTP routes are real TCP listeners. The fake port forward proxies
//! localhost port 8123 to one listener. Legacy URL discovery points at a
//! separate NodePort decoy. This makes the security assertion observable at the
//! network boundary: every governance verb must reach the tunnel, and the
//! cleartext decoy must receive nothing.

mod support;

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{IpAddr, TcpListener, TcpStream, UdpSocket};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use support::{serve, MockServer, Request, Response};

const AGENT: &str = "acme-bot";
const NAMESPACE: &str = "acme-system";
const RELEASE: &str = "acme-release";
const DISCOVERED_KEY: &str = "fixture-release-platform-key";
const EXPLICIT_KEY: &str = "fixture-operator-platform-key";
const API_TUNNEL_PORT: u16 = 8123;

static TUNNEL_LOCK: Mutex<()> = Mutex::new(());

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn stub_path(bin_dir: &Path) -> std::ffi::OsString {
    let mut paths = vec![bin_dir.to_path_buf()];
    paths.extend(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    ));
    std::env::join_paths(paths).expect("join fake kubectl PATH")
}

fn write_kubectl_stub(dir: &Path) {
    let path = dir.join("kubectl");
    fs::write(
        &path,
        r#"#!/usr/bin/env python3
import json
import os
import socket
import sys
import threading
from urllib.parse import urlparse

args = sys.argv[1:]
with open(os.environ["CURIE_TEST_KUBECTL_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")

def read_message(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, body = data.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
            break
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body

def proxy(client, backend_host, backend_port):
    try:
        request = read_message(client)
        if not request:
            return
        upstream = socket.create_connection((backend_host, backend_port), timeout=5)
        try:
            upstream.sendall(request)
            response = read_message(upstream)
            if response:
                client.sendall(response)
        finally:
            upstream.close()
    finally:
        client.close()

if "port-forward" in args:
    local_port, remote_port = args[-1].split(":", 1)
    local_port = int(local_port)
    backend = urlparse(os.environ["CURIE_TEST_TUNNEL_BACKEND"])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", local_port))
    listener.listen()
    print(f"Forwarding from 127.0.0.1:{local_port} -> {remote_port}", flush=True)
    while True:
        client, _ = listener.accept()
        threading.Thread(
            target=proxy,
            args=(client, backend.hostname, backend.port),
            daemon=True,
        ).start()

if args[:2] == ["config", "view"]:
    print("https://" + os.environ["CURIE_TEST_NODE_HOST"] + ":6443", end="")
    sys.exit(0)

if "get" in args and "nodes" in args:
    print(json.dumps({
        "items": [{
            "status": {
                "addresses": [{
                    "type": "InternalIP",
                    "address": os.environ["CURIE_TEST_NODE_HOST"],
                }]
            }
        }]
    }), end="")
    sys.exit(0)

if "get" in args and "svc" in args:
    print(json.dumps({
        "spec": {
            "type": "NodePort",
            "ports": [{
                "port": 3000,
                "nodePort": int(os.environ["CURIE_TEST_NODEPORT"]),
            }],
        }
    }), end="")
    sys.exit(0)

if "get" in args and "secret" in args:
    if "-l" in args:
        print("acme-release-secrets")
        sys.exit(0)
    if any("apiKey" in arg for arg in args):
        print(os.environ["CURIE_TEST_DISCOVERED_KEY"], end="")
        sys.exit(0)

print("unexpected fake kubectl invocation: " + " ".join(args), file=sys.stderr)
sys.exit(64)
"#,
    )
    .expect("write fake kubectl");
    let mut permissions = fs::metadata(&path)
        .expect("read fake kubectl metadata")
        .permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&path, permissions).expect("make fake kubectl executable");
}

#[derive(Clone, Debug)]
#[allow(dead_code)]
struct WideRequest {
    method: String,
    path: String,
    api_key: Option<String>,
}

struct WideServer {
    host: IpAddr,
    port: u16,
    requests: Arc<Mutex<Vec<WideRequest>>>,
}

impl WideServer {
    fn start() -> Self {
        let host = local_nonloopback_ip();
        let listener = TcpListener::bind(("0.0.0.0", 0)).expect("bind nonloopback test listener");
        let port = listener.local_addr().expect("read listener address").port();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let recorded = Arc::clone(&requests);
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { break };
                let recorded = Arc::clone(&recorded);
                thread::spawn(move || serve_wide_request(stream, recorded));
            }
        });
        Self {
            host,
            port,
            requests,
        }
    }

    fn base_url(&self) -> String {
        format!("http://{}:{}", self.host, self.port)
    }

    fn recorded(&self) -> Vec<WideRequest> {
        self.requests.lock().unwrap().clone()
    }
}

fn local_nonloopback_ip() -> IpAddr {
    let socket = UdpSocket::bind(("0.0.0.0", 0)).expect("bind address probe");
    socket
        .connect(("192.0.2.1", 9))
        .expect("select the local routed address");
    let ip = socket.local_addr().expect("read routed address").ip();
    assert!(
        !ip.is_loopback() && !ip.is_unspecified(),
        "security test needs a nonloopback local address, got {ip}"
    );
    ip
}

fn serve_wide_request(stream: TcpStream, requests: Arc<Mutex<Vec<WideRequest>>>) {
    let mut reader = BufReader::new(stream);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
        return;
    }
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let path = parts.next().unwrap_or_default().to_string();
    let mut api_key = None;
    let mut content_length = 0usize;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            return;
        }
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("x-api-key") {
                api_key = Some(value.trim().to_string());
            }
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    if content_length > 0 {
        let mut body = vec![0; content_length];
        let _ = reader.read_exact(&mut body);
    }
    requests.lock().unwrap().push(WideRequest {
        method,
        path: path.clone(),
        api_key,
    });

    let body = if path.ends_with("/agents") {
        r#"[{"id":"agent-1","name":"acme-bot","channel":{"kind":"slack","address":"C0EXAMPLE1"}}]"#
    } else if path.ends_with("/agents/agent-1/versions") {
        "[]"
    } else {
        r#"{"detail":"unexpected fixture path"}"#
    };
    let status = if path.ends_with("/agents") || path.ends_with("/agents/agent-1/versions") {
        "200 OK"
    } else {
        "500 Internal Server Error"
    };
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let stream = reader.get_mut();
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

struct Fixture {
    _tools: tempfile::TempDir,
    kubectl_path: std::ffi::OsString,
    kubectl_log: PathBuf,
    tunnel_backend: MockServer,
    nodeport_decoy: WideServer,
    proxy_decoy: MockServer,
}

impl Fixture {
    fn new() -> Self {
        let tools = tempfile::tempdir().expect("create fake tool directory");
        write_kubectl_stub(tools.path());
        let kubectl_path = stub_path(tools.path());
        let kubectl_log = tools.path().join("kubectl.log");
        let tunnel_backend = serve(|_| Response::json(200, "[]"));
        let nodeport_decoy = WideServer::start();
        let proxy_decoy = serve(versions_api);
        Self {
            _tools: tools,
            kubectl_path,
            kubectl_log,
            tunnel_backend,
            nodeport_decoy,
            proxy_decoy,
        }
    }

    fn run(&self, args: &[&str]) -> Output {
        self.run_with_env(args, &[])
    }

    fn run_with_env(&self, args: &[&str], env: &[(&str, &str)]) -> Output {
        let mut command = Command::new(bin());
        command
            .args(args)
            .args(["--namespace", NAMESPACE, "--release", RELEASE])
            .env("PATH", &self.kubectl_path)
            .env("CURIE_TEST_KUBECTL_LOG", &self.kubectl_log)
            .env("CURIE_TEST_TUNNEL_BACKEND", &self.tunnel_backend.base_url)
            .env("CURIE_TEST_NODEPORT", self.nodeport_decoy.port.to_string())
            .env("CURIE_TEST_NODE_HOST", self.nodeport_decoy.host.to_string())
            .env("CURIE_TEST_DISCOVERED_KEY", DISCOVERED_KEY)
            .env("HTTP_PROXY", &self.proxy_decoy.base_url)
            .env("http_proxy", &self.proxy_decoy.base_url)
            .env("ALL_PROXY", &self.proxy_decoy.base_url)
            .env("all_proxy", &self.proxy_decoy.base_url)
            .env_remove("NO_PROXY")
            .env_remove("no_proxy")
            .env_remove("CURIE_API_URL")
            .env_remove("CURIE_API_KEY");
        for (name, value) in env {
            command.env(name, value);
        }
        command
            .output()
            .unwrap_or_else(|error| panic!("run curie {}: {error}", args.join(" ")))
    }

    fn kubectl_log(&self) -> String {
        fs::read_to_string(&self.kubectl_log).unwrap_or_default()
    }
}

fn wait_for_tunnel_port_free() {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if TcpListener::bind(("127.0.0.1", API_TUNNEL_PORT)).is_ok() {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "localhost:{API_TUNNEL_PORT} remained occupied after the prior command"
        );
        thread::sleep(Duration::from_millis(20));
    }
}

fn describe(output: &Output) -> String {
    format!(
        "status: {}\nstdout: {}\nstderr: {}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn versions_api(request: &Request) -> Response {
    if request.path.ends_with("/agents") {
        Response::json(
            200,
            r#"[{"id":"agent-1","name":"acme-bot","channel":{"kind":"slack","address":"C0EXAMPLE1"}}]"#,
        )
    } else if request.path.ends_with("/agents/agent-1/versions") {
        Response::json(200, "[]")
    } else {
        Response::json(500, r#"{"detail":"unexpected fixture path"}"#)
    }
}

#[test]
fn all_discovered_cluster_api_verbs_avoid_the_nodeport() {
    let _tunnel_lock = TUNNEL_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let fixture = Fixture::new();
    let cases: &[(&str, &[&str])] = &[
        ("versions", &["cluster", "versions", AGENT]),
        ("kill", &["cluster", "kill", AGENT, "--yes"]),
        ("resume", &["cluster", "resume", AGENT]),
        ("budget", &["cluster", "budget", AGENT, "--limit", "1"]),
        ("overrides", &["cluster", "overrides", AGENT]),
        (
            "reset thread",
            &[
                "cluster",
                "reset-thread",
                AGENT,
                "--thread-key",
                "thread-1",
                "--yes",
            ],
        ),
        ("memory", &["cluster", "memory", AGENT]),
        (
            "memory add",
            &[
                "cluster",
                "memory",
                AGENT,
                "--add",
                "ask before translating to French",
            ],
        ),
        ("approvals", &["cluster", "approvals", AGENT, "--list"]),
        ("delete", &["cluster", "delete", AGENT, "--yes"]),
    ];

    for (name, args) in cases {
        wait_for_tunnel_port_free();
        let before = fixture.tunnel_backend.recorded().len();
        let output = fixture.run(args);
        let requests = fixture.tunnel_backend.recorded();
        assert_eq!(
            requests.len(),
            before + 1,
            "{name} must make its agent lookup through the loopback tunnel\n{}\nkubectl:\n{}",
            describe(&output),
            fixture.kubectl_log()
        );
        let request = &requests[before];
        assert_eq!(request.method, "GET", "{name} must resolve the agent");
        assert_eq!(request.path, "/agents", "{name} used the wrong API base");
        assert_eq!(
            request.header("x-api-key"),
            Some(DISCOVERED_KEY),
            "{name} must authenticate only through the tunnel"
        );
        assert!(
            fixture.nodeport_decoy.recorded().is_empty(),
            "{name} sent a request to the discovered cleartext NodePort: {:?}",
            fixture.nodeport_decoy.recorded()
        );
        assert!(
            fixture.proxy_decoy.recorded().is_empty(),
            "{name} exposed the discovered key to the system HTTP proxy: {:?}",
            fixture.proxy_decoy.recorded()
        );
    }

    assert_eq!(
        fixture.tunnel_backend.recorded().len(),
        cases.len(),
        "every named cluster API verb must exercise the shared tunnel"
    );
}

#[test]
fn discovered_key_with_explicit_remote_http_refuses_before_api_client_construction() {
    let fixture = Fixture::new();
    let url = fixture.nodeport_decoy.base_url();
    let output = fixture.run(&["cluster", "versions", AGENT, "--api-url", &url]);
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert!(
        !output.status.success(),
        "cleartext request must be refused"
    );
    assert!(
        fixture.nodeport_decoy.recorded().is_empty(),
        "refusal must happen before any API request: {:?}",
        fixture.nodeport_decoy.recorded()
    );
    assert!(
        stderr.contains("refusing to send the auto-discovered release key over cleartext HTTP"),
        "missing refusal reason: {stderr}"
    );
    for recovery in ["--api-key", "https://", "omit --api-url"] {
        assert!(
            stderr.contains(recovery),
            "refusal must name recovery {recovery:?}: {stderr}"
        );
    }
    assert!(
        !stderr.contains("warning: API endpoint"),
        "ApiClient was constructed before the refusal: {stderr}"
    );
}

#[test]
fn blank_environment_key_is_omitted_for_refusal_and_automatic_discovery() {
    let fixture = Fixture::new();
    let remote_url = fixture.nodeport_decoy.base_url();
    let refused = fixture.run_with_env(
        &["cluster", "versions", AGENT, "--api-url", &remote_url],
        &[("CURIE_API_KEY", "")],
    );
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(
        !refused.status.success(),
        "a blank environment key must not acknowledge remote HTTP"
    );
    assert!(
        stderr.contains("refusing to send the auto-discovered release key over cleartext HTTP"),
        "blank CURIE_API_KEY must take the discovered key refusal path: {stderr}"
    );
    assert!(
        fixture.nodeport_decoy.recorded().is_empty(),
        "blank CURIE_API_KEY leaked to remote HTTP: {:?}",
        fixture.nodeport_decoy.recorded()
    );
    assert!(
        fixture.proxy_decoy.recorded().is_empty(),
        "blank CURIE_API_KEY leaked through the system proxy: {:?}",
        fixture.proxy_decoy.recorded()
    );
    fs::write(&fixture.kubectl_log, "").expect("clear kubectl log between blank key cases");

    let _tunnel_lock = TUNNEL_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    wait_for_tunnel_port_free();
    let automatic = fixture.run_with_env(&["cluster", "versions", AGENT], &[("CURIE_API_KEY", "")]);
    let requests = fixture.tunnel_backend.recorded();
    assert_eq!(
        requests.len(),
        1,
        "automatic mode must use the tunnel after normalizing a blank key\n{}\nkubectl:\n{}",
        describe(&automatic),
        fixture.kubectl_log()
    );
    assert_eq!(requests[0].path, "/agents");
    assert_eq!(requests[0].header("x-api-key"), Some(DISCOVERED_KEY));
    assert!(
        fixture.kubectl_log().contains("apiKey"),
        "automatic mode must discover the real release key: {}",
        fixture.kubectl_log()
    );
    assert!(
        fixture.nodeport_decoy.recorded().is_empty(),
        "automatic mode must not use the NodePort"
    );
    assert!(
        fixture.proxy_decoy.recorded().is_empty(),
        "automatic mode must bypass the system proxy"
    );
}

#[test]
fn automatic_dry_run_is_cluster_and_network_offline() {
    let _tunnel_lock = TUNNEL_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    wait_for_tunnel_port_free();
    let fixture = Fixture::new();
    let output = fixture.run(&["cluster", "versions", AGENT, "--dry-run"]);

    assert!(
        output.status.success(),
        "automatic dry run must remain offline and succeed\n{}",
        describe(&output)
    );
    assert!(
        fixture.kubectl_log().is_empty(),
        "dry run must not discover a Secret or start a tunnel: {}",
        fixture.kubectl_log()
    );
    assert!(fixture.tunnel_backend.recorded().is_empty());
    assert!(fixture.nodeport_decoy.recorded().is_empty());
    assert!(fixture.proxy_decoy.recorded().is_empty());
}

#[test]
fn loopback_looking_userinfo_does_not_hide_a_remote_http_host() {
    let fixture = Fixture::new();
    let url = format!(
        "http://127.0.0.1@{}:{}",
        fixture.nodeport_decoy.host, fixture.nodeport_decoy.port
    );
    let output = fixture.run(&["cluster", "versions", AGENT, "--api-url", &url]);
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert!(!output.status.success(), "remote HTTP must be refused");
    assert!(
        stderr.contains("refusing to send the auto-discovered release key over cleartext HTTP"),
        "userinfo must not fool the remote host classifier: {stderr}"
    );
    assert!(
        fixture.nodeport_decoy.recorded().is_empty(),
        "userinfo bypass sent the key to the actual remote host: {:?}",
        fixture.nodeport_decoy.recorded()
    );
    assert!(
        fixture.proxy_decoy.recorded().is_empty(),
        "userinfo bypass sent the key through the system proxy: {:?}",
        fixture.proxy_decoy.recorded()
    );
    assert!(fixture.tunnel_backend.recorded().is_empty());
}

#[test]
fn discovered_key_with_explicit_https_is_accepted_without_a_tunnel() {
    let fixture = Fixture::new();
    let output = fixture.run(&[
        "cluster",
        "versions",
        AGENT,
        "--api-url",
        "https://api.example.com",
        "--dry-run",
    ]);
    assert!(
        output.status.success(),
        "discovered credentials over HTTPS must remain supported\n{}",
        describe(&output)
    );
    assert!(
        !fixture.kubectl_log().contains("port-forward"),
        "an explicit HTTPS URL must direct dial: {}",
        fixture.kubectl_log()
    );
}

#[test]
fn explicit_key_acknowledges_reachable_remote_http() {
    let fixture = Fixture::new();
    let url = fixture.nodeport_decoy.base_url();
    let output = fixture.run(&[
        "cluster",
        "versions",
        AGENT,
        "--api-url",
        &url,
        "--api-key",
        EXPLICIT_KEY,
    ]);
    assert!(
        output.status.success(),
        "an explicit key is the existing cleartext acknowledgement\n{}",
        describe(&output)
    );
    assert!(
        fixture.nodeport_decoy.recorded().is_empty(),
        "proxied explicit traffic must not contact the remote target directly: {:?}",
        fixture.nodeport_decoy.recorded()
    );
    let requests = fixture.proxy_decoy.recorded();
    assert_eq!(
        requests.len(),
        2,
        "the proxy must carry the explicit agent lookup and versions request"
    );
    assert!(requests[0].path.ends_with("/agents"));
    assert!(requests[1].path.ends_with("/agents/agent-1/versions"));
    assert!(requests.iter().all(|request| {
        request.method == "GET" && request.header("x-api-key") == Some(EXPLICIT_KEY)
    }));
    assert!(
        fixture.kubectl_log().is_empty(),
        "fully explicit HTTP credentials must not invoke kubectl: {}",
        fixture.kubectl_log()
    );
}

#[test]
fn discovered_key_with_explicit_loopback_http_remains_supported() {
    let fixture = Fixture::new();
    let server = serve(versions_api);
    let output = fixture.run(&["cluster", "versions", AGENT, "--api-url", &server.base_url]);
    assert!(
        output.status.success(),
        "loopback HTTP must remain supported\n{}",
        describe(&output)
    );
    let requests = server.recorded();
    assert_eq!(
        requests.len(),
        2,
        "versions must resolve then list versions"
    );
    assert!(requests.iter().all(|request| {
        request.method == "GET" && request.header("x-api-key") == Some(DISCOVERED_KEY)
    }));
    assert!(
        !fixture.kubectl_log().contains("port-forward"),
        "an explicit loopback URL must not start a tunnel: {}",
        fixture.kubectl_log()
    );
}

#[test]
fn fully_explicit_https_connection_never_invokes_kubectl() {
    let fixture = Fixture::new();
    let output = fixture.run(&[
        "cluster",
        "versions",
        AGENT,
        "--api-url",
        "https://api.example.com",
        "--api-key",
        EXPLICIT_KEY,
        "--dry-run",
    ]);
    assert!(
        output.status.success(),
        "fully explicit HTTPS connection must remain supported\n{}",
        describe(&output)
    );
    assert!(
        fixture.kubectl_log().is_empty(),
        "fully explicit connection must skip every discovery call: {}",
        fixture.kubectl_log()
    );
}
