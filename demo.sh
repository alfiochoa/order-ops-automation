#!/usr/bin/env bash
# ~2 min demo: flaky webhook -> traffic -> dead letters -> webhook recovers -> replay.
# Uses a temp data dir and short retry delays; nothing is written to your real data/.
set -euo pipefail
PY=${PY:-python}; D=$(mktemp -d); trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
sed 's/base_delay: 0.5/base_delay: 0.05/' config/rules.yaml > "$D/rules.yaml"
export OPS_DB="$D/ops.db" OPS_EXPORT_CSV="$D/export.csv" OPS_RULES="$D/rules.yaml" OPS_LOG_LEVEL=ERROR

FAIL_RATE=0.7 $PY -m tools.mock_webhook & MOCK=$!
$PY -m uvicorn opsauto.app:app_factory --factory --port 8000 --log-level error & sleep 3
echo "== 1. traffic while the webhook is 70% flaky"; $PY -m tools.simulate -n 150 --seed 7
echo "== 2. health";  curl -s localhost:8000/health; echo
echo "== 3. dead-letter queue"; $PY -m opsauto.cli dlq | head -5; $PY -m opsauto.cli dlq | wc -l | sed 's/^/open dead letters: /'
kill $MOCK; sleep 1; $PY -m tools.mock_webhook & sleep 2   # webhook recovers
echo "== 4. replay"; $PY -m opsauto.cli replay
echo "== 5. dlq after replay"; $PY -m opsauto.cli dlq | wc -l | sed 's/^/open dead letters: /'
echo "== 6. audit tail"; $PY -m opsauto.cli audit -n 3
if [ -n "${SHOT:-}" ]; then $PY -m tools.screenshot http://127.0.0.1:8000 "$SHOT"; fi
