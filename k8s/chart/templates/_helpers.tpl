{{- define "devai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "devai.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "devai.labels" -}}
app.kubernetes.io/name: {{ include "devai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "devai.selectorLabels" -}}
app.kubernetes.io/name: {{ include "devai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "devai.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "devai.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "devai.postgresHost" -}}
{{- printf "%s-postgres" (include "devai.fullname" .) -}}
{{- end -}}

{{- define "devai.redisHost" -}}
{{- printf "%s-redis" (include "devai.fullname" .) -}}
{{- end -}}
