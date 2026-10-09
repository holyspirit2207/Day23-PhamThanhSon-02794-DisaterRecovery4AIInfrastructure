#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "1. Khoi dong ingest va replicate..."
python3 state/ingest.py --region a --rate 0.5 --duration 150 > run/ingest.log 2>&1 &
INGEST_PID=$!
python3 state/replicate.py --every 30 --duration 150 --backend fs > run/replicate.log 2>&1 &
REPL_PID=$!

sleep 5

echo "2. Khoi dong loadgen traffic va health checker..."
python3 loadgen/traffic.py --duration 70 --rps 2 --out reports/drill-2-withdr.jsonl &
TRAFFIC_PID=$!
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 70 --out reports/health-events.jsonl > run/health.log 2>&1 &
HC_PID=$!

sleep 12

echo "3. Danh sap Region A..."
python3 chaos/kill_region.py --region a --mode netblock --mock

echo "4. Chay runbook failover..."
python3 dr/runbook.py --primary a --target b --backend fs --auto

echo "5. Cho traffic generator hoan tat toan bo requests..."
wait "$TRAFFIC_PID"

kill "$INGEST_PID" "$REPL_PID" "$HC_PID" 2>/dev/null || true
echo "DRILL 2 HOAN TAT THANH CONG"
