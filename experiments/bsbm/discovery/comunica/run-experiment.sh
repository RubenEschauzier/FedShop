#!/usr/bin/env bash
# Start the benchmark's services and documents if they are not running, then run the workload with
# Comunica. Results go to experiments/bsbm/benchmark/discovery/runs/<name>/ (summary.md, results.jsonl);
# an interrupted run resumes where it stopped when started again with the same name.
#
# Usage: run-experiment.sh [name] [run-comunica options...]
#   run-experiment.sh                                   plain queries, catalog endpoints, no bind joins, 180 s
#   run-experiment.sh plain-default --comunica-config ""  the same with Comunica's own config (bind joins on)
#   run-experiment.sh service-no-bind-join --form service the SERVICE form, seeds only
# Leaves the services running; stop them with: pkill -f "fedshop-discovery serve"; docker stop fedshop-discovery-virtuoso
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$ROOT"
CONFIG=experiments/bsbm/snakefile/config.yaml
RUNS=experiments/bsbm/benchmark/discovery/runs
NAME="${1:-plain-no-bind-join}"
shift || true

ARGS=(--name "$NAME" --timeout 180)
case " $* " in
  *" --comunica-config "*) ;;
  *) ARGS+=(--comunica-config experiments/bsbm/discovery/comunica/config-no-bind-join.json) ;;
esac
# An empty --comunica-config means the engine's own config.
EXTRA=()
skip=false
for a in "$@"; do
  if $skip; then skip=false; [ -n "$a" ] && EXTRA+=(--comunica-config "$a"); continue; fi
  if [ "$a" = "--comunica-config" ]; then skip=true; continue; fi
  EXTRA+=("$a")
done

up() { curl -sf -o /dev/null http://localhost:3000/vendor0/Product0 && curl -sf -o /dev/null "http://localhost:8891/sparql?query=ASK%7B%7D"; }
if ! up; then
  mkdir -p "$RUNS"
  echo "Starting Virtuoso and CSS (log: $RUNS/serve.log)..."
  nohup uv run fedshop-discovery serve "$CONFIG" > "$RUNS/serve.log" 2>&1 &
  for _ in $(seq 120); do up && break; sleep 5; done
  up || { echo "Services did not come up, see $RUNS/serve.log" >&2; exit 1; }
fi
echo "Running $NAME..."
uv run fedshop-discovery run-comunica "$CONFIG" "${ARGS[@]}" "${EXTRA[@]}"
