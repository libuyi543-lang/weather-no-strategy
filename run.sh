#!/bin/zsh
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
LOCK="$ROOT/data/run.lock"
LOG="$ROOT/logs/run-$(date +%Y-%m-%d).log"
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir "$LOCK" 2>/dev/null || exit 0
set -a
source "$ROOT/.env"
source "$ROOT/.beeapi.env"
set +a

notify_failure() {
  local exit_code=$?
  rmdir "$LOCK" 2>/dev/null || true
  local text="【天气 NO 策略运行失败】\n时间：$(date '+%Y-%m-%d %H:%M:%S %Z')\n退出码：$exit_code\n日志：$LOG\n本次未生成推荐，也未执行任何交易。"
  /usr/bin/curl -sS -X POST \
    -H 'Content-Type: application/json' \
    -d "$(/usr/bin/jq -n --arg text "$text" '{msg_type:"text",content:{text:$text}}')" \
    "$FEISHU_WEATHER_NO_WEBHOOK" >/dev/null 2>&1 || true
  exit "$exit_code"
}
trap notify_failure ERR
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

exec >>"$LOG" 2>&1
echo "[$(date -Iseconds)] starting"
node "$ROOT/prepare.mjs" "$@"

if jq -e '.candidates | length == 0' "$ROOT/data/snapshot.json" >/dev/null; then
  cat > "$ROOT/data/analysis.json" <<JSON
{"targetDate":"$(jq -r .targetDate "$ROOT/data/snapshot.json")","generatedAt":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","marketSummary":"No eligible NO asks in configured range.","assessments":[]}
JSON
else
  /usr/bin/perl -e 'alarm shift; exec @ARGV' 1800 codex exec \
    --profile weather-no-strategy \
    -c model_reasoning_effort="medium" \
    --ephemeral \
    --skip-git-repo-check \
    --sandbox danger-full-access \
    -C "$ROOT" \
    --output-schema "$ROOT/analysis.schema.json" \
    --output-last-message "$ROOT/data/analysis.json" \
    - < "$ROOT/data/prompt.txt"
fi

node "$ROOT/finalize.mjs"
cp "$ROOT/data/report.json" "$ROOT/data/report-$(date +%Y-%m-%d-%H%M%S).json"
echo "[$(date -Iseconds)] completed"
