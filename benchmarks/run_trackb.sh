#!/bin/bash
# Track B launcher — CPersona Cascading Recall on LMEB (all 22 tasks).
#
# Runs benchmark_trackb_lmeb.py with the official measurement regime
# (see benchmarks/README.md). Every knob is an environment variable so the
# same launcher covers any embedding model and any cpersona checkout.
#
# Usage (defaults shown):
#   MODEL_PATH=BAAI/bge-m3 \
#   LMEB_DIR=~/lmeb \
#   EMB_CACHE_DIR=~/lmeb/embcache \
#   OUTPUT_DIR=trackb_results \
#   bash benchmarks/run_trackb.sh [extra benchmark_trackb_lmeb.py args...]
#
# Long runs: launch fully detached, e.g.
#   nohup bash benchmarks/run_trackb.sh > trackb_run.log 2>&1 &
#
# IMPORTANT: never share EMB_CACHE_DIR between embedding models — use one
# cache directory per model (see README, "Embedding cache").
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

MODEL_PATH="${MODEL_PATH:-BAAI/bge-m3}"
LMEB_DIR="${LMEB_DIR:-$HOME/lmeb}"
EMB_CACHE_DIR="${EMB_CACHE_DIR:-$HOME/lmeb/embcache}"
EMB_CACHE_MODEL="${EMB_CACHE_MODEL:-$MODEL_PATH}"
OUTPUT_DIR="${OUTPUT_DIR:-trackb_results}"
PYTHON_BIN="${PYTHON_BIN:-python}"
DEVICE="${DEVICE:-mps}"

# Benchmark doctrine: full-precision ranking benchmarks run with the
# contamination-prevention truncation layers disabled. Those layers exist to
# protect precision in live use; they can only ever remove items from an
# already-ranked list, so on a pure ranking metric (NDCG) they are neutral at
# best and lossy at worst. Setting them explicitly keeps the regime
# self-documenting and independent of per-version defaults.
export CPERSONA_AUTOCUT_ENABLED=false
export CPERSONA_FUSED_GATE_ENABLED=false

env CPERSONA_REPO="${CPERSONA_REPO:-$REPO_ROOT}" \
    LMEB_DIR="$LMEB_DIR" \
    PYTHONIOENCODING=utf-8 \
    EMB_CACHE_DIR="$EMB_CACHE_DIR" \
    EMB_CACHE_MODEL="$EMB_CACHE_MODEL" \
    "$PYTHON_BIN" "$SCRIPT_DIR/benchmark_trackb_lmeb.py" \
      --model_path "$MODEL_PATH" \
      --device "$DEVICE" --dtype float16 --budget_encode \
      --recall_mode rrf --auto_calibrate \
      --output_dir "$OUTPUT_DIR" \
      "$@"
