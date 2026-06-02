#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
workflow_file="$repo_dir/.github/workflows/main.yml"

grep -Fq 'TG_TOKEN: ${{ secrets.TG_TOKEN }}' "$workflow_file"
grep -Fq 'GLOBAL_TELEGRAM_CHAT_ID: ${{ vars.GLOBAL_TELEGRAM_CHAT_ID }}' "$workflow_file"
grep -Fq 'NOTIFY_LANG: ${{ vars.NOTIFY_LANG }}' "$workflow_file"
grep -Fq "STRATEGY_PROFILE: \${{ vars.STRATEGY_PROFILE || 'crypto_leader_rotation' }}" "$workflow_file"
grep -Fq 'id-token: write' "$workflow_file"
grep -Fq 'workload_identity_provider: ${{ env.GCP_WORKLOAD_IDENTITY_PROVIDER }}' "$workflow_file"
grep -Fq 'service_account: ${{ env.GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT }}' "$workflow_file"
grep -Fq '7. Notify Telegram on runtime workflow failure' "$workflow_file"
grep -Fq "if: \${{ failure() && github.event.inputs.validate_only != 'true' }}" "$workflow_file"
grep -Fq 'reports/execution_report.json' "$workflow_file"
grep -Fq 'https://api.telegram.org/bot${TG_TOKEN}/sendMessage' "$workflow_file"
if grep -Fq 'TG_CHAT_ID:' "$workflow_file"; then
  echo "workflow should not pass TG_CHAT_ID anymore" >&2
  exit 1
fi
if grep -Fq 'GCP_SA_KEY:' "$workflow_file"; then
  echo "workflow should not pass GCP_SA_KEY anymore" >&2
  exit 1
fi
