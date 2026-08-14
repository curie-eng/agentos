{{/*
Shared template helpers for the Curie umbrella chart.

Naming: every backing store's Service name is derived here so both the store's
own template and its consumers (Langfuse, the OTel Collector) agree. When a
store is BYO (`<dep>.deploy: false`), the helper returns the operator-supplied
host instead of the in-cluster Service name. This is the single-block BYO idiom
lifted from Langfuse's chart: flip `deploy` and fill `host` on the same block.
*/}}

{{- define "curie.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "curie.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "curie.labels" -}}
app.kubernetes.io/name: {{ include "curie.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{/* Component selector labels. Pass a dict with "root" (the top context) and
     "component" (the component name). */}}
{{- define "curie.selectorLabels" -}}
app.kubernetes.io/name: {{ include "curie.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Secret name that carries all credential material. */}}
{{- define "curie.secretName" -}}
{{- printf "%s-secrets" (include "curie.fullname" .) -}}
{{- end -}}

{{/* ---- Reserved connector-secret boot-env names (#457, ADR-0009) ----
     The non-CURIE_-prefixed runner credential keys a per-agent connector
     secret must never declare, kept in list-parity with the Python source of
     truth in packages/plugin-format (module reserved_env). This is the
     unavoidable second copy -- Helm cannot import Python -- so the completeness
     pin apps/worker/tests/binding/test_reserved_boot_env_pin.py parses THIS
     define's body and fails CI if the two lists drift.

     IMPORTANT: the pin scans this body for env-name-shaped uppercase tokens, so
     the body must contain EXACTLY these eight keys and no other stray ones (this
     comment lives OUTSIDE the define so it is never scanned): the four runner
     credential keys plus the four redirect/capture-capable keys (#487). The whole
     CURIE_ namespace is fenced separately by the hasPrefix rule in the
     connector-secret guard, so it is intentionally absent here. Emitted
     space-separated for consumption via `splitList " "`. */}}
{{- define "curie.reservedConnectorSecretNames" -}}
ANTHROPIC_BASE_URL ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN HTTPS_PROXY HTTP_PROXY NODE_EXTRA_CA_CERTS ANTHROPIC_CUSTOM_HEADERS
{{- end -}}

{{/* ---- Backing-store hosts (in-cluster Service name, or BYO host) ---- */}}

{{- define "curie.postgres.host" -}}
{{- if .Values.postgres.deploy -}}
{{- printf "%s-postgres" (include "curie.fullname" .) -}}
{{- else -}}
{{- required "postgres.deploy is false: set postgres.host to your external Postgres" .Values.postgres.host -}}
{{- end -}}
{{- end -}}

{{- define "curie.valkey.host" -}}
{{- if .Values.valkey.deploy -}}
{{- printf "%s-valkey" (include "curie.fullname" .) -}}
{{- else -}}
{{- required "valkey.deploy is false: set valkey.host to your external Valkey/Redis" .Values.valkey.host -}}
{{- end -}}
{{- end -}}

{{- define "curie.clickhouse.host" -}}
{{- if .Values.clickhouse.deploy -}}
{{- printf "%s-clickhouse" (include "curie.fullname" .) -}}
{{- else -}}
{{- required "clickhouse.deploy is false: set clickhouse.host to your external ClickHouse" .Values.clickhouse.host -}}
{{- end -}}
{{- end -}}

{{- define "curie.clickhouse.loggingConfig" -}}
<clickhouse>
  <logger>
    <level>{{ .Values.clickhouse.logLevel }}</level>
    <!-- Also to stdout, so `kubectl logs` works. The image logs only to
         files under /var/log/clickhouse-server/, which in Kubernetes means
         the diagnostics live inside the container and die with the pod,
         exactly when you most want them. Cheap at `warning`. -->
    <console>1</console>
  </logger>
  <!-- text_log is KEPT, and level-filtered rather than removed.
       The image ships <text_log><level>trace</level></text_log>, and that
       `level` is the whole problem: an hour of it produced 253,727 Debug
       and 58,186 Trace rows against 15 Information. Filtering to
       `{{ .Values.clickhouse.logLevel }}` drops the volume by ~4 orders of
       magnitude while keeping the table queryable, and a queryable
       text_log is precisely what diagnosed the incident this fixes. It also
       outlives the pod, which the log file does not. -->
  <text_log>
    <level>{{ .Values.clickhouse.logLevel }}</level>
    <ttl>event_date + INTERVAL {{ .Values.clickhouse.systemLogs.retentionDays }} DAY DELETE</ttl>
  </text_log>
{{- if .Values.clickhouse.systemLogs.enabled }}
  <!-- Profiling and per-second resource sampling, on but TTL-bounded. -->
  <trace_log><ttl>event_date + INTERVAL {{ .Values.clickhouse.systemLogs.retentionDays }} DAY DELETE</ttl></trace_log>
  <metric_log><ttl>event_date + INTERVAL {{ .Values.clickhouse.systemLogs.retentionDays }} DAY DELETE</ttl></metric_log>
  <asynchronous_metric_log><ttl>event_date + INTERVAL {{ .Values.clickhouse.systemLogs.retentionDays }} DAY DELETE</ttl></asynchronous_metric_log>
{{- else }}
  <!-- Removed. Unlike text_log these have no level filter; they sample on
       a timer regardless of whether anything is happening, so the only
       lever is on/off. metric_log was the table whose merge wedged and
       started the spiral. Prometheus and node metrics already cover what
       they measure, from outside the process that is failing. -->
  <trace_log remove="1"/>
  <metric_log remove="1"/>
  <asynchronous_metric_log remove="1"/>
{{- end }}
  <!-- Kept regardless: low volume, useful when ClickHouse itself
       misbehaves. TTL'd so none can become the next text_log.
       processors_profile_log is here because a live boot showed the image
       creates it too. It is easy to miss precisely because it is small
       today, which is what text_log also was once. -->
  <query_log><ttl>event_date + INTERVAL {{ .Values.clickhouse.systemLogs.retentionDays }} DAY DELETE</ttl></query_log>
  <part_log><ttl>event_date + INTERVAL {{ .Values.clickhouse.systemLogs.retentionDays }} DAY DELETE</ttl></part_log>
  <error_log><ttl>event_date + INTERVAL {{ .Values.clickhouse.systemLogs.retentionDays }} DAY DELETE</ttl></error_log>
  <processors_profile_log><ttl>event_date + INTERVAL {{ .Values.clickhouse.systemLogs.retentionDays }} DAY DELETE</ttl></processors_profile_log>
</clickhouse>
{{- end }}

{{- define "curie.rustfs.host" -}}
{{- if .Values.rustfs.deploy -}}
{{- printf "%s-rustfs" (include "curie.fullname" .) -}}
{{- else -}}
{{- required "rustfs.deploy is false: set rustfs.host to your external S3-compatible endpoint" .Values.rustfs.host -}}
{{- end -}}
{{- end -}}

{{- define "curie.rustfs.scheme" -}}
{{- if and (not .Values.rustfs.deploy) (eq (printf "%v" .Values.rustfs.port) "443") -}}
https
{{- else -}}
http
{{- end -}}
{{- end -}}

{{- define "curie.rustfs.endpoint" -}}
{{- include "curie.rustfs.scheme" . }}://{{ include "curie.rustfs.host" . }}:{{ .Values.rustfs.port }}
{{- end -}}

{{- define "curie.langfuse.webHost" -}}
{{- printf "%s-langfuse-web" (include "curie.fullname" .) -}}
{{- end -}}

{{/* base64("<publicKey>:<secretKey>") for the OTel Collector's Authorization
     header. Uses the operator override when set, otherwise derives it from the
     Langfuse init keys so the trace path authenticates with no manual step. */}}
{{- define "curie.otlpAuthHeader" -}}
{{- if .Values.otelCollector.otlpAuthHeader -}}
{{- .Values.otelCollector.otlpAuthHeader -}}
{{- else -}}
{{- printf "Basic %s" (printf "%s:%s" .Values.langfuse.init.projectPublicKey .Values.langfuse.init.projectSecretKey | b64enc) -}}
{{- end -}}
{{- end -}}

{{- define "curie.otelCollector.config" -}}
# Receives OTLP over gRPC (4317) and HTTP (4318) from app services and
# forwards to Langfuse over HTTP. Langfuse OTLP ingest is HTTP-only (gRPC is
# silently unsupported), so the collector is the adapter. Langfuse appends
# /v1/traces to the otlphttp base path itself.
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
processors:
  batch: {}
exporters:
  otlphttp/langfuse:
    endpoint: http://{{ include "curie.langfuse.webHost" . }}:{{ .Values.langfuse.web.service.port }}/api/public/otel
    headers:
      Authorization: ${env:LANGFUSE_OTLP_AUTH_HEADER}
  debug:
    verbosity: normal
extensions:
  health_check:
    endpoint: 0.0.0.0:13133
service:
  extensions: [health_check]
  telemetry:
    metrics:
      level: none
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/langfuse, debug]
{{- end }}

{{/* ---- Default-credential gate (issue #198) ----
     When security.checkDefaultCredentials is on, refuse to render if a Langfuse
     bootstrap identity still carries the published dev default from values.yaml.
     Unlike the nine store/control-plane secrets, these init identities seed the
     org/project on first boot (a different lifecycle), so #57 deliberately
     excludes them from its render-time gate; this closes that gap. The published
     admin password is a Langfuse admin-takeover risk on a reachable UI, and the
     project secret key also feeds the OTel Collector auth header. The operator
     clears the gate by overriding the value or supplying langfuse.existingSecret
     (the #169 secretKeyRef escape carries both keys).

     Off by default so the flagship zero-secret bare install stays green and the
     dev/e2e overlays render unchanged; flip it on for a shared/production
     cluster. #57 will fold the store/control-plane secrets into this same helper
     (hence the general name) once its design pass lands. */}}
{{- define "curie.checkDefaultCredentials" -}}
{{- if .Values.security.checkDefaultCredentials -}}
{{- if not .Values.langfuse.existingSecret -}}
{{- if eq .Values.langfuse.init.projectSecretKey "sk-lf-curie-dev" -}}
{{- fail "security.checkDefaultCredentials is on but langfuse.init.projectSecretKey is still the published dev default \"sk-lf-curie-dev\". Override it (or set langfuse.existingSecret) before installing on a shared/production cluster -- this key also feeds the OTel Collector auth header." -}}
{{- end -}}
{{- if eq .Values.langfuse.init.userPassword "curie-dev-password" -}}
{{- fail "security.checkDefaultCredentials is on but langfuse.init.userPassword is still the published dev default \"curie-dev-password\". Override it (or set langfuse.existingSecret) before installing on a shared/production cluster -- the published admin password allows Langfuse admin takeover on a reachable UI." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---- Auto-generated per-release chart credential (issue #195) ----
     Resolve one chart-owned secret value, generating a strong random per release
     for a sealed install instead of shipping the published dev default. Call with
     a dict: root (the top context), key (the stringData key, matching an existing
     Secret's data), value (.Values.<path>), default (the published dev default),
     hex (true for the 64-hex encryption key, else false).

     The existing Secret's data is looked up ONCE by the caller (secrets.yaml) and
     passed in as `.existingData` (an always-present dict, empty under `helm
     template`/--dry-run/first install), so this helper does no per-key lookup.

     Four branches, in PRECEDENCE order, and WHY this order is correct:
       1. allowDevDefaults: the deterministic dev/CI escape hatch (values-dev.yaml
          sets it true). Return the value verbatim so the dev/e2e path renders the
          published defaults unchanged, byte-for-byte reproducible. Taking this
          first also means `--dev` reverts to the defaults even if a random was
          previously generated into the release Secret. Gate on positive equality
          against the literal "true" (`eq (toString ...) "true"`), NOT plain
          truthiness: Go templates treat any non-empty string as truthy, so a
          quoted `--set security.allowDevDefaults="false"` would otherwise read as
          truthy and ship the published default -- a fail-OPEN regression.
       2. Explicit override: if the operator/CLI supplied a value that differs from
          the published default (`ne value default`), it WINS -- even on `helm
          upgrade`. This is operator intent (a rotation, a recovery, a `--set`, or
          an `existingSecret`-equivalent value), so it must beat the persisted
          value; matches Bitnami's `providedPasswordValue`-first precedence. It
          MUST sit ahead of the persist branch or an explicit rotation on
          upgrade would be silently ignored.
       3. Persist existing: no override, so if a prior install already GENERATED
          this key, re-use it. `helm upgrade` must NEVER rotate a live store
          credential (Postgres would reject the new password against its persisted
          data), so we return the stored value from `.existingData` when present.
          Generated secrets always have value==published-default (nobody set them),
          so they never take branch 2 and always land here on upgrade -- exactly
          the "upgrade must not rotate" guarantee. `.existingData` is always a dict
          (the caller applies `| default dict`), empty under `helm
          template`/--dry-run and on first install, so a missing key falls through
          to generation.
       4. Generate: a first sealed install (value still equals the published
          default, no prior Secret) gets a strong random. `randAlphaNum` is
          crypto-backed (Sprig). hex=true hashes it to 64 lowercase-hex chars (the
          encryption key format); otherwise a 32-char alphanumeric.

     Net effect: an operator who forgets to re-pass `--set` on a later upgrade
     safely reverts value to the default, which then reuses the persisted generated
     value via branch 3 rather than rotating it. */}}
{{- define "curie.managedSecret" -}}
{{- if eq (toString .root.Values.security.allowDevDefaults) "true" -}}{{/* string-coercion safety -- a quoted "false" must not read as truthy and silently ship a published default (fail closed to generation). */}}
{{- .value -}}
{{- else if ne (toString .value) (toString .default) -}}
{{- .value -}}
{{- else if hasKey .existingData .key -}}
{{- index .existingData .key | b64dec -}}
{{- else if .hex -}}
{{- randAlphaNum 32 | sha256sum -}}
{{- else -}}
{{- randAlphaNum 32 -}}
{{- end -}}
{{- end -}}

{{/* ---- Shared first-party-app environment fragments ---- */}}

{{/* Postgres connection env for the app services. POSTGRES_PASSWORD comes from
     the Secret and DATABASE_URL is composed with $(POSTGRES_PASSWORD) so the
     password never lands in the rendered manifest. Both the API and the worker
     use the asyncpg driver and the dedicated `curie` schema. */}}
{{- define "curie.env.postgres" -}}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgres.existingSecret | default (include "curie.secretName" .) }}
      key: postgresPassword
- name: DATABASE_URL
  value: postgresql+asyncpg://{{ .Values.postgres.auth.username }}:$(POSTGRES_PASSWORD)@{{ include "curie.postgres.host" . }}:{{ .Values.postgres.port }}/{{ .Values.postgres.auth.database }}
- name: DB_SCHEMA
  value: curie
{{- end -}}

{{/* Valkey connection env for the app services (host/port + password from the
     Secret). The apps build their own redis DSN from these parts. */}}
{{- define "curie.env.valkey" -}}
- name: VALKEY_HOST
  value: {{ include "curie.valkey.host" . | quote }}
- name: VALKEY_PORT
  value: {{ .Values.valkey.port | quote }}
- name: VALKEY_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.valkey.existingSecret | default (include "curie.secretName" .) }}
      key: valkeyPassword
{{- end -}}

{{/* Platform-API connection env for the first-party services that CALL the API
     (today the dispatcher; the chart worker's identical gap is a tracked
     follow-up). Exists as a helper for the same reason curie.env.postgres and
     curie.env.valkey do: the API URL env has now been forgotten three
     times on new callers, while the store envs never recurred, because those had
     a helper to include and this did not. Wire a new API caller by including
     this rather than re-deriving the URL inline.

     The BYO override is .Values.dispatcher.apiBaseUrl. Note the deliberate
     absence of a `required` call for the api.deploy=false case that the sibling
     `X.host` helpers use: an empty override with api.deploy=false yields a
     CrashLoopBackOff by design (documented in NOTES.txt and the README), not a
     render-time failure. Include with `nindent 12` to land at a container's env
     column. */}}
{{- define "curie.env.api" -}}
# Where the platform API lives. The dispatcher POSTs an approval
# resolve here when someone clicks Approve in Slack, so an unwired
# value means the click dead-ends: the code default
# http://localhost:8000 is, inside this pod, the dispatcher itself.
# Empty dispatcher.apiBaseUrl (the default) derives the in-chart API
# Service; a set value renders verbatim and is the BYO answer, and
# the only correct one when api.deploy is false. The port comes from
# api.service.port so the two sides cannot drift.
- name: CURIE_API_URL
  value: {{ .Values.dispatcher.apiBaseUrl | default (printf "http://%s-api:%v" (include "curie.fullname" .) .Values.api.service.port) | quote }}
# The same chart Secret key api.yaml consumes as API_KEY, so the
# caller and the API cannot drift apart. By reference only: an inline
# value would put the shared platform key into `helm get manifest`
# output and into any rendered artifact CI uploads.
- name: CURIE_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "curie.secretName" . }}
      key: apiKey
{{- end -}}

{{/* Heartbeat exec probes for the worker and dispatcher. Neither has an HTTP
     port, so an exec probe checks CURIE_HEARTBEAT_FILE freshness (< 30s)
     instead of hitting a port. Each Deployment sets its own heartbeat path via
     that env var, so the probe body is path-agnostic and both callers share
     identical timings -- the helper therefore takes no params. Include with
     `nindent 10` so the probe keys land at the container's 10-space column. */}}
{{- define "curie.heartbeatProbes" -}}
readinessProbe:
  exec:
    command:
      - python
      - -c
      - |
        import os, sys, time
        p = os.environ["CURIE_HEARTBEAT_FILE"]
        sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 30 else 1)
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 3
livenessProbe:
  exec:
    command:
      - python
      - -c
      - |
        import os, sys, time
        p = os.environ["CURIE_HEARTBEAT_FILE"]
        sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 30 else 1)
  initialDelaySeconds: 30
  periodSeconds: 15
  timeoutSeconds: 5
  failureThreshold: 4
{{- end }}

{{/* ---- Langfuse shared environment (mirrors compose.dev.yaml's
        x-langfuse-env anchor). Rendered into both web and worker. ---- */}}
{{- define "curie.langfuse.env" -}}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "curie.secretName" . }}
      key: postgresPassword
- name: DATABASE_URL
  value: postgresql://{{ .Values.postgres.auth.username }}:$(POSTGRES_PASSWORD)@{{ include "curie.postgres.host" . }}:{{ .Values.postgres.port }}/{{ .Values.postgres.auth.database }}
- name: SALT
  valueFrom:
    secretKeyRef:
      name: {{ include "curie.secretName" . }}
      key: langfuseSalt
- name: ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "curie.secretName" . }}
      key: langfuseEncryptionKey
- name: TELEMETRY_ENABLED
  value: {{ .Values.langfuse.telemetryEnabled | quote }}
- name: LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES
  value: {{ .Values.langfuse.enableExperimentalFeatures | quote }}
- name: CLICKHOUSE_MIGRATION_URL
  value: clickhouse://{{ include "curie.clickhouse.host" . }}:{{ .Values.clickhouse.nativePort }}
- name: CLICKHOUSE_URL
  value: http://{{ include "curie.clickhouse.host" . }}:{{ .Values.clickhouse.httpPort }}
- name: CLICKHOUSE_USER
  value: {{ .Values.clickhouse.auth.username | quote }}
- name: CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "curie.secretName" . }}
      key: clickhousePassword
- name: CLICKHOUSE_CLUSTER_ENABLED
  value: {{ .Values.clickhouse.clusterEnabled | quote }}
- name: REDIS_HOST
  value: {{ include "curie.valkey.host" . }}
- name: REDIS_PORT
  value: {{ .Values.valkey.port | quote }}
- name: REDIS_AUTH
  valueFrom:
    secretKeyRef:
      name: {{ include "curie.secretName" . }}
      key: valkeyPassword
- name: LANGFUSE_S3_EVENT_UPLOAD_BUCKET
  value: {{ .Values.rustfs.bucket | quote }}
- name: LANGFUSE_S3_EVENT_UPLOAD_REGION
  value: auto
- name: LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID
  value: {{ .Values.rustfs.auth.accessKey | quote }}
- name: LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.rustfs.existingSecret | default (include "curie.secretName" .) }}
      key: rustfsSecretKey
- name: LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT
  value: http://{{ include "curie.rustfs.host" . }}:{{ .Values.rustfs.port }}
- name: LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE
  value: "true"
- name: LANGFUSE_S3_EVENT_UPLOAD_PREFIX
  value: events/
- name: LANGFUSE_S3_MEDIA_UPLOAD_BUCKET
  value: {{ .Values.rustfs.bucket | quote }}
- name: LANGFUSE_S3_MEDIA_UPLOAD_REGION
  value: auto
- name: LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID
  value: {{ .Values.rustfs.auth.accessKey | quote }}
- name: LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.rustfs.existingSecret | default (include "curie.secretName" .) }}
      key: rustfsSecretKey
- name: LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT
  value: http://{{ include "curie.rustfs.host" . }}:{{ .Values.rustfs.port }}
- name: LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE
  value: "true"
- name: LANGFUSE_S3_MEDIA_UPLOAD_PREFIX
  value: media/
{{- end -}}

{{/* ---- gVisor tri-state (security.gvisor.mode: auto|require|off) ----

     curie.gvisor.className: the RuntimeClass NAME to use/verify when gVisor is
     intended at all (empty only for mode=off). Deterministic (no cluster lookup);
     used by the enforcement preflight, the optional RuntimeClass object, and the
     probe's admission test.

     curie.gvisor.runtimeClassName: the EFFECTIVE runtimeClassName to stamp on a
     runner pod. off -> empty; require -> className; auto -> className when the
     chart itself creates the RuntimeClass (installRuntimeClass=true), otherwise
     only if the class is found by `lookup`. The installRuntimeClass shortcut
     exists because `lookup` cannot see the RuntimeClass the same install is about
     to create (nor anything under `helm template`/--dry-run), which would leave
     first-install runner pods with no runtimeClassName despite the chart
     guaranteeing the object. */}}
{{- define "curie.gvisor.className" -}}
{{- $g := .Values.security.gvisor -}}
{{- if eq ($g.mode | default "auto") "off" -}}
{{- else -}}
{{- $g.runtimeClassName | default "gvisor" -}}
{{- end -}}
{{- end -}}

{{- define "curie.gvisor.runtimeClassName" -}}
{{- $g := .Values.security.gvisor -}}
{{- $mode := $g.mode | default "auto" -}}
{{- $name := $g.runtimeClassName | default "gvisor" -}}
{{- if eq $mode "off" -}}
{{- else if eq $mode "require" -}}
{{- $name -}}
{{- else if $g.installRuntimeClass -}}
{{- $name -}}
{{- else -}}
{{- if lookup "node.k8s.io/v1" "RuntimeClass" "" $name -}}
{{- $name -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---- gVisor enforcement gate ----
     curie.gvisor.preflightRequired: non-empty ("true") when the blocking
     gVisor enforcement preflight Job must render, else empty. It renders in
     `require` (always) and in `auto` WHEN the runner runs a real (non-fake)
     model -- i.e. untrusted agent code executes, so a missing/downgraded runsc
     RuntimeClass must fail the install CLOSED instead of silently landing on
     the host kernel. `auto` with the fake model (the bare-install default)
     still degrades gracefully with only a NOTES warning; `off` never renders.
     Real-model detection mirrors the CURIE_FAKE_MODEL gate in
     agent-sandbox.yaml (fake is in effect only when runner.fakeModel AND NOT
     inference.deploy), so real code runs when `(not fakeModel) OR inference.deploy`.
     Also respects security.gvisorPreflight.enabled and agentSandbox.deploy. */}}
{{- define "curie.gvisor.preflightRequired" -}}
{{- $mode := .Values.security.gvisor.mode | default "auto" -}}
{{- $realModel := or (not .Values.agentSandbox.runner.fakeModel) .Values.inference.deploy -}}
{{- if and .Values.agentSandbox.deploy .Values.security.gvisorPreflight.enabled -}}
{{- if or (eq $mode "require") (and (eq $mode "auto") $realModel) -}}
true
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---- First-party image reference ----
     Render a fully-qualified image ref for a first-party (GHCR) workload,
     preferring an immutable content digest over a mutable tag. Call with a dict:
       repository  the image repo (e.g. ghcr.io/curie-eng/curie-api)
       tag         optional explicit tag; empty falls back to defaultTag
       digest      optional "sha256:..." -- when set, wins and pins by digest
       defaultTag  the fallback tag when `tag` is empty (pass .Chart.AppVersion)
     - digest set -> "<repository>@sha256:..."  (fully immutable + verifiable)
     - else       -> "<repository>:<tag|defaultTag>"
     An empty tag defaulting to the chart appVersion is what makes a given chart
     version render a deterministic image ref (same chart version -> same ref,
     installable and rollback-able) without every install pinning a field.  */}}
{{- define "curie.image" -}}
{{- $repo := required "image.repository is required" .repository -}}
{{- if .digest -}}
{{- printf "%s@%s" $repo .digest -}}
{{- else -}}
{{- printf "%s:%s" $repo (.tag | default .defaultTag | default "latest") -}}
{{- end -}}
{{- end -}}

{{/* ---- Dispatcher gating ----
     The Slack dispatcher only deploys when it has both tokens; without them it
     would crash-loop the reconnect supervisor forever, so a token-less default
     install skips the Deployment entirely (NOTES prints the connect command). */}}
{{- define "curie.dispatcher.enabled" -}}
{{- if and .Values.dispatcher.deploy .Values.dispatcher.slack.appToken .Values.dispatcher.slack.botToken -}}
true
{{- end -}}
{{- end -}}

{{/* ---- Sandbox container hardening (Rail 3) ----
     The identical container-level lockdown applied to the runner and every
     helper container in the sandbox pod (bundle-fetch, bundle-extract, litellm).
     Extracted so the four copies cannot drift (#493); callers keep their own
     `{{- if $runner.hardening.enabled }}` guard and apply `nindent 10`. This is
     the container securityContext only -- the pod-level securityContext
     (runAsUser/fsGroup/seccomp) is a separate, non-duplicated block. */}}
{{- define "curie.sandboxHardening.securityContext" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  capabilities:
    drop: [ALL]
{{- end -}}

{{/* ---- First-party service container securityContext ----
     The `securityContext:` + `toYaml` wrapper the four first-party services (api,
     worker, dispatcher, ui) each render from their own
     `.Values.<svc>.containerSecurityContext`. Extracted so the wrapper lives once
     (#493). Call with the container-security-context VALUE inside the existing
     `{{- with .Values.<svc>.containerSecurityContext }}` guard and apply
     `nindent 10`; the `with` handles the empty case exactly as before. */}}
{{- define "curie.containerSecurityContext" -}}
securityContext:
{{- toYaml . | nindent 2 }}
{{- end -}}
