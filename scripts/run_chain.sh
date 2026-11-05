#!/usr/bin/env bash
# Measurement chain for P4, run detached (scripts/start_chain.ps1 from P3's pattern):
#   bash scripts/run_chain.sh dev      -> baselines + v1 + ablations on the dev split
#   bash scripts/run_chain.sh test     -> test runs x3 (v1 haiku), x1 or x3 for the others
# Each step is committed when it lands; progress in evals/results/chain.log; chain.done at the end.
cd "$(dirname "$0")/.." || exit 1
unset VIRTUAL_ENV
LOG=evals/results/chain.log
STAGE="${1:-dev}"
log() { echo "[$(date +%H:%M)] $*" >>"$LOG"; }
commit() {
  (cd .. && git add -A -- p4-ops-action-agent/evals && git commit -q -m "P4: $1

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" >/dev/null 2>&1)
  log "committed: $1"
}
run() {
  log "######## $*"
  timeout 7200 uv run python scripts/run_eval.py "$@" 2>&1 | grep -v "it/s\]\|s/it\]\|^\s*$\|^v[01]" >>"$LOG"
  log "exit=${PIPESTATUS[0]}"
}
log "chain start ($STAGE)"
if [ "$STAGE" = "dev" ]; then
  for sys in v0 v0+ v1; do run --system $sys --model haiku --split dev --max-cost 4; commit "dev run $sys haiku"; done
  for sys in v1-noengine v1-nogating v1-noapproval v1-nountrusted v1-nochecker v1-noverify; do
    run --system $sys --model haiku --split dev --max-cost 4; commit "ablation $sys on dev"
  done
  timeout 7200 uv run python scripts/guard_mutation.py --split dev --per-template 2 --max-cost 4 2>&1 | grep -v "it/s\]\|s/it\]\|^\s*$\|^v1" >>"$LOG"; commit "guard mutation test on dev"
else
  for t in r1 r2 r3; do run --system v1 --model haiku --split test --tag $t --max-cost 4; commit "test run v1 haiku $t"; done
  for t in r1 r2 r3; do run --system v0+ --model haiku --split test --tag $t --max-cost 4; commit "test run v0+ haiku $t"; done
  for t in r1 r2 r3; do run --system v0 --model haiku --split test --tag $t --max-cost 4; commit "test run v0 haiku $t"; done
  run --system v1 --model sonnet --split test --tag r1 --max-cost 8; commit "test run v1 sonnet r1"
  run --system v0+ --model opus --split test --tag r1 --max-cost 12; commit "test run v0+ opus r1"
  uv run python scripts/final_table.py >>"$LOG" 2>&1; commit "final tables"
fi
log "chain done"
touch evals/results/chain.done
