set -euo pipefail
if [ -z "${TG_TOKEN:-}" ] || [ -z "${GLOBAL_TELEGRAM_CHAT_ID:-}" ]; then
  echo "Telegram workflow failure notification skipped: target is not configured."
  exit 0
fi

REPORT_FILE="${GITHUB_WORKSPACE}/reports/execution_report.json"
if [ -f "$REPORT_FILE" ]; then
  echo "Execution report exists; strategy-level runtime handling should already have recorded this failure."
  exit 0
fi

case "${NOTIFY_LANG:-zh}" in
  zh*) TITLE="⚠️ Binance 启动失败，尚未生成执行报告。请查看运行日志。" ;;
  *) TITLE="⚠️ Binance startup failed before an execution report was written. Check the run log." ;;
esac
MESSAGE=$(printf '%s\n' \
  "$TITLE" \
  "run: #${GITHUB_RUN_NUMBER} attempt ${GITHUB_RUN_ATTEMPT}" \
  "url: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}")
RESPONSE="$(curl -fsS -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${GLOBAL_TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${MESSAGE}")"
python3 - "${RESPONSE}" <<'PY'
import json
import sys

try:
    payload = json.loads(sys.argv[1])
except json.JSONDecodeError as exc:
    raise SystemExit("Telegram returned invalid JSON") from exc
if not isinstance(payload, dict) or payload.get("ok") is not True:
    raise SystemExit("Telegram did not acknowledge workflow failure notification")
PY