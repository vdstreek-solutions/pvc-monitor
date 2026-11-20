{{- define "pvc-monitor.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pvc-monitor.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "pvc-monitor.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "pvc-monitor.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "pvc-monitor.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pvc-monitor.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "pvc-monitor.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "pvc-monitor.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "pvc-monitor.secretName" -}}
{{- default (include "pvc-monitor.fullname" .) .Values.secret.existingSecret }}
{{- end }}

{{- define "pvc-monitor.tlsSecretName" -}}
{{- default .Values.gateway.hostname .Values.gateway.listenerSet.tlsSecretName }}
{{- end }}

{{- define "pvc-monitor.listenerSetName" -}}
{{- default (include "pvc-monitor.fullname" .) .Values.gateway.listenerSet.name }}
{{- end }}

{{- define "pvc-monitor.image" -}}
{{- if .Values.image.digest -}}
{{- if not (hasPrefix "sha256:" .Values.image.digest) -}}
{{- fail "image.digest must look like sha256:<hex>" -}}
{{- end -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end }}

{{/*
Render-time safety checks. Included at the top of every template so the same
(first) failure is reported whichever template helm renders first.
*/}}
{{- define "pvc-monitor.validate" -}}
{{- $d := .Values.secret.data -}}
{{- $exposed := or .Values.gateway.enabled (ne .Values.service.type "ClusterIP") -}}
{{- if not .Values.secret.existingSecret -}}
{{- /* The same spellings the application reads as false (settings.env_bool). */ -}}
{{- if has (toString $d.AUTH_ENABLED | trim | lower) (list "false" "0" "no" "off") -}}
{{- if $exposed -}}
{{- fail (printf "Authentication is off (secret.data.AUTH_ENABLED=%q) while the app is exposed (gateway.enabled=true or service.type is not ClusterIP). Configure OIDC, or disable gateway and keep service.type=ClusterIP." (toString $d.AUTH_ENABLED)) -}}
{{- end -}}
{{- else -}}
{{- range $k := list "OIDC_ISSUER" "OIDC_CLIENT_ID" "OIDC_CLIENT_SECRET" "SESSION_SECRET" "ALLOWED_GROUPS" -}}
{{- if not (index $d $k) -}}
{{- fail (printf "Authentication is enabled (secret.data.AUTH_ENABLED is not false) but secret.data.%s is empty. Set OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, SESSION_SECRET and ALLOWED_GROUPS (group names, or \"*\" for every authenticated user), or use secret.existingSecret. Running without login requires AUTH_ENABLED=\"false\" with gateway.enabled=false." $k) -}}
{{- end -}}
{{- end -}}
{{- if lt (len (toString $d.SESSION_SECRET)) 32 -}}
{{- fail "secret.data.SESSION_SECRET must be at least 32 characters (e.g. openssl rand -hex 32)." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and (not .Values.serviceAccount.create) (not .Values.serviceAccount.name) -}}
{{- fail "serviceAccount.name is required when serviceAccount.create=false; otherwise the cluster-wide read binding would go to the namespace's default ServiceAccount." -}}
{{- end -}}
{{- if and .Values.vcenterCA.configMapName .Values.vcenterCA.secretName -}}
{{- fail "Set only one of vcenterCA.configMapName and vcenterCA.secretName." -}}
{{- end -}}
{{- if and .Values.networkPolicy.enabled .Values.gateway.enabled (not .Values.networkPolicy.ingressFrom) -}}
{{- fail "networkPolicy.ingressFrom is empty while gateway.enabled=true; the Gateway could not reach the pod. Set it to the Gateway data-plane namespace/pod selector, or set networkPolicy.enabled=false." -}}
{{- end -}}
{{- end }}
