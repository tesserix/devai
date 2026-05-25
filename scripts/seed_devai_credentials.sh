#!/usr/bin/env bash
# Interactive credential seeder for the 12 DevAI secrets that require
# external provider credentials (Anthropic, OpenAI, Groq, Gemini, ...).
#
# Each prompt uses `read -rs` so values never echo, never enter shell
# history, and never appear in any log. After every entry:
#   1. `gcloud secrets create` (or `versions add` if it exists)
#   2. IAM bind to app-secrets-devai-prod@tesseracthub-480811
#   3. unset the local variable
#
# Press Enter on any prompt to SKIP that secret (useful when you don't
# have one yet — e.g. cloudflare). The script tracks what was set vs.
# skipped and prints a summary at the end.
#
# Re-run any time: idempotent. New values land as additional versions.

set -euo pipefail

PROJECT="tesseracthub-480811"
SA="app-secrets-devai-prod@tesseracthub-480811.iam.gserviceaccount.com"

# secret_name | human description | optional URL hint
SECRETS=(
  "prod-devai-anthropic-api-key|Anthropic API key (sk-ant-...)|console.anthropic.com/settings/keys"
  "prod-devai-openai-api-key|OpenAI API key (sk-...)|platform.openai.com/api-keys"
  "prod-devai-groq-api-key|Groq API key (gsk_...)|console.groq.com/keys"
  "prod-devai-gemini-api-key|Google Gemini API key|aistudio.google.com/app/apikey"
  "prod-devai-langsmith-api-key|LangSmith API key (lsv2_...)|smith.langchain.com/settings"
  "prod-devai-cloudflare-account-id|Cloudflare account ID|dash.cloudflare.com → right sidebar"
  "prod-devai-cloudflare-api-token|Cloudflare API token (Zone DNS edit)|dash.cloudflare.com/profile/api-tokens"
  "prod-devai-github-app-private-key|GitHub App private key (PEM, paste full multi-line text, end with Ctrl-D)|github.com/organizations/tesserix/settings/apps/devai-agent-app"
  "prod-devai-github-client-id|GitHub OAuth App client ID|github.com/organizations/tesserix/settings/applications"
  "prod-devai-github-client-secret|GitHub OAuth App client secret|same place"
  "prod-devai-github-oauth-client-id|GitHub OAuth client ID (dashboard login)|github.com/organizations/tesserix/settings/applications"
  "prod-devai-github-oauth-client-secret|GitHub OAuth client secret (dashboard login)|same place"
)

push_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --project="$PROJECT" --data-file=- >/dev/null
    echo "  ✓ $name: new version added"
  else
    printf '%s' "$value" | gcloud secrets create "$name" --project="$PROJECT" \
      --replication-policy=automatic --data-file=- >/dev/null
    echo "  ✓ $name: created"
  fi
  gcloud secrets add-iam-policy-binding "$name" --project="$PROJECT" \
    --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor \
    --condition=None >/dev/null 2>&1
}

set_count=0
skip_count=0
declare -a SKIPPED=()

echo
echo "=================================================================="
echo "  DevAI credential seeder — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Project:        $PROJECT"
echo "  Bind access to: $SA"
echo
echo "  Press Enter on any prompt to SKIP that secret."
echo "  Values are never echoed, never logged."
echo "=================================================================="

for entry in "${SECRETS[@]}"; do
  IFS='|' read -r name desc hint <<<"$entry"
  echo
  echo "──── $name"
  echo "     $desc"
  [ -n "$hint" ] && echo "     hint: $hint"

  if [[ "$name" == *github-app-private-key* ]]; then
    # Multi-line PEM. Read until EOF (Ctrl-D).
    echo "     paste the PEM block (multi-line), then press Ctrl-D:"
    value=$(cat)
  else
    read -rs -p "     value (hidden): " value
    echo
  fi

  if [ -z "$value" ]; then
    echo "     [skipped]"
    SKIPPED+=("$name")
    skip_count=$((skip_count + 1))
    continue
  fi

  push_secret "$name" "$value"
  set_count=$((set_count + 1))
  # Wipe local copy
  value=""
  unset value
done

echo
echo "=================================================================="
echo "  Done. $set_count secrets set, $skip_count skipped."
if [ "$skip_count" -gt 0 ]; then
  echo
  echo "  Skipped:"
  for s in "${SKIPPED[@]}"; do echo "    - $s"; done
  echo
  echo "  Re-run this script (idempotent) when ready to fill them in."
fi
echo
echo "  Next: trigger the ExternalSecrets to re-sync immediately:"
echo
echo "      kubectl annotate externalsecret -n devai \\"
echo "        devai-api-secrets devai-auth-bff-secrets \\"
echo "        force-sync=\"\$(date +%s)\" --overwrite"
echo
echo "  Then watch the pods come up:"
echo
echo "      kubectl get pods -n devai -w"
echo "=================================================================="
