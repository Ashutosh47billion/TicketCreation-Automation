{{/*
Expand the name of the chart.
*/}}
{{- define "ticket-automation.name" -}}
{{- .Values.nameOverride | default .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "ticket-automation.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := .Values.nameOverride | default .Chart.Name }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "ticket-automation.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{ include "ticket-automation.selectorLabels" . }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "ticket-automation.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ticket-automation.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
