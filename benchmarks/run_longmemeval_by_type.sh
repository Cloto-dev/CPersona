#!/usr/bin/env bash
# LongMemEval under two recall regimes, for the per-question-type reader.
#
#   OUTPUT_DIR=<arm dir> [CPERSONA_REPO=<checkout>] [REGIMES="full limit10"] \
#       benchmarks/run_longmemeval_by_type.sh [harness args]
#
# Writes <arm dir>/full and <arm dir>/limit10, each holding the harness's
# LongMemEval.json and a rankings.jsonl dump. Read them with
# benchmarks/longmemeval_by_type.py NAME=<arm dir> ...
#
# The two regimes differ in exactly two things, and the dump header records
# both so the reader can refuse a mislabelled directory:
#
#   full     the pooled corpus, --recall_limit 0 (limit = corpus size), autocut
#            and the fused gate off — the Track B regime run_trackb.sh pins.
#            Comparable to the shipped Track B record.
#   limit10  --isolate_scenes (one scene's history is the haystack, stored as
#            its own channel), --recall_limit 10, autocut and the fused gate
#            at their shipped defaults (on) — what a caller of the MCP `recall`
#            tool receives over their own memory. The gate variables are unset
#            here rather than set to true, so the regime is the build's own
#            default and not this script's opinion. Pooled, the top ten of the
#            237k-session corpus is 0.3% own-scene rows, so limit=10 over the
#            pool would measure nothing.
#
# Everything else mirrors run_trackb.sh: bge-m3, float16, budget batching,
# rrf, live calibration. The full regime adds --fast (behaviour-invariant
# acceleration of the pooled scan); the isolated regime does not need it — a
# scene is a few hundred rows — and the accelerator's fallback for a channel
# filter calls the original search with the current signature, which an older
# build does not have. Per-query latency numbers are not representative in
# either regime and are not what this measures. Extra arguments pass through
# to the harness, e.g. --subtasks.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

: "${OUTPUT_DIR:?set OUTPUT_DIR to the arm directory}"
MODEL_PATH="${MODEL_PATH:-BAAI/bge-m3}"
LMEB_DIR="${LMEB_DIR:-$HOME/lmeb}"
EMB_CACHE_DIR="${EMB_CACHE_DIR:-$HOME/lmeb/embcache}"
EMB_CACHE_MODEL="${EMB_CACHE_MODEL:-$MODEL_PATH}"
PYTHON_BIN="${PYTHON_BIN:-python}"
DEVICE="${DEVICE:-cpu}"
CPERSONA_REPO="${CPERSONA_REPO:-$REPO_ROOT}"

# Regimes to run, space-separated. Default both; REGIMES=limit10 reruns one.
REGIMES="${REGIMES:-full limit10}"

run_regime() {
  local regime="$1"; shift
  local out="$OUTPUT_DIR/$regime"
  case " $REGIMES " in *" $regime "*) ;; *) echo "== regime $regime skipped (REGIMES=$REGIMES)"; return 0;; esac
  if [ -e "$out/LongMemEval.json" ]; then
    # The harness would skip the cached task but still truncate the dump it
    # is told to write, leaving a record with no rankings behind it.
    echo "== regime $regime already measured at $out; remove the directory to re-run" >&2
    return 1
  fi
  mkdir -p "$out"
  echo "== regime $regime -> $out (CPERSONA_REPO=$CPERSONA_REPO)"
  # `env` takes its -u options before any assignment, so the regime's
  # arguments go first.
  env "$@" \
      CPERSONA_REPO="$CPERSONA_REPO" \
      LMEB_DIR="$LMEB_DIR" \
      PYTHONIOENCODING=utf-8 \
      EMB_CACHE_DIR="$EMB_CACHE_DIR" \
      EMB_CACHE_MODEL="$EMB_CACHE_MODEL" \
      "$PYTHON_BIN" "$SCRIPT_DIR/benchmark_trackb_lmeb.py" \
        --model_path "$MODEL_PATH" \
        --device "$DEVICE" --dtype float16 --budget_encode \
        --recall_mode rrf --auto_calibrate \
        --tasks LongMemEval \
        --output_dir "$out" \
        --dump_rankings "$out/rankings.jsonl" \
        "${REGIME_ARGS[@]}" \
        "${EXTRA_ARGS[@]}"
}

EXTRA_ARGS=("$@")

# --unclamp_limit is a no-op on 2.5.0+ and lifts the bug-032 limit=100 clamp
# on a v2.4.38..v2.4.41 checkout, so the same command line is full-ranking on
# either; the harness logs which of the two it did.
REGIME_ARGS=(--recall_limit 0 --unclamp_limit --fast)
run_regime full CPERSONA_AUTOCUT_ENABLED=false CPERSONA_FUSED_GATE_ENABLED=false

REGIME_ARGS=(--recall_limit 10 --isolate_scenes --skip_latency_pass)
run_regime limit10 -u CPERSONA_AUTOCUT_ENABLED -u CPERSONA_FUSED_GATE_ENABLED
