{{/* Render extraEnv only after refusing collisions with chart-owned names.
     Each workload with this extension surface has an explicit set in files/.
     Validate names, not values: equal values also break strategic merge patches. */}}
{{- define "curie.extraEnv" -}}
{{- $sets := .root.Files.Get "files/reserved-env.yaml" | fromYaml -}}
{{- $reserved := index $sets .workload -}}
{{- if not $reserved -}}{{- fail (printf "missing reserved env set for %s" .workload) -}}{{- end -}}
{{- $seen := dict -}}
{{- range .env -}}
{{- $name := .name -}}
{{- if hasKey $reserved $name -}}
{{- fail (printf "%s.extraEnv contains chart-owned environment variable %s; remove it from extraEnv and configure %s instead." $.workload $name (index $reserved $name)) -}}
{{- end -}}
{{- if hasKey $seen $name -}}
{{- fail (printf "%s.extraEnv repeats environment variable %s; keep exactly one entry." $.workload $name) -}}
{{- end -}}
{{- $_ := set $seen $name true -}}
{{- end -}}
{{- with .env }}{{ toYaml . }}{{ end -}}
{{- end -}}
