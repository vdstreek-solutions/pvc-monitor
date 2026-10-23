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
