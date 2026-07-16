"""LMEB (Long-horizon Memory Embedding Benchmark) Runner.

Evaluates embedding models on 22 memory retrieval tasks using the LMEB framework.
Supports HuggingFace sentence-transformers and OpenAI API models.

Usage:
  # Smoke test (LoCoMo only, fast)
  python benchmarks/benchmark_lmeb.py --model_path sentence-transformers/all-MiniLM-L6-v2 --tasks LoCoMo

  # Full LMEB (all 22 tasks)
  python benchmarks/benchmark_lmeb.py --model_path sentence-transformers/all-MiniLM-L6-v2

  # OpenAI model
  python benchmarks/benchmark_lmeb.py --model_type openai --openai_model text-embedding-3-small

  # Specific tasks
  python benchmarks/benchmark_lmeb.py --model_path BAAI/bge-m3 --tasks LoCoMo,LongMemEval --batch_size 16
"""

import argparse
import json
import logging
import os
import sys
import time

import numpy as np

# --- LMEB framework setup ---
LMEB_DIR = os.environ.get(
    "LMEB_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp", "lmeb"),
)
sys.path.insert(0, LMEB_DIR)
os.environ["LOCAL_DATA_PREFIX"] = os.path.join(LMEB_DIR, "eval_data")

# Register LMEB custom tasks before importing mteb tasks
import src  # noqa: E402, F401 — side-effect import: registers tasks into _TASKS_REGISTRY
import mteb  # noqa: E402

logging.basicConfig(
    format="%(levelname)s|%(asctime)s|%(name)s: %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("benchmark_lmeb")

LMEB_TASK_NAMES = [
    "EPBench", "KnowMeBench", "LoCoMo", "LongMemEval", "REALTALK", "TMD",
    "MemBench", "ConvoMem", "QASPER", "NovelQA", "PeerQA", "CovidQA",
    "ESGReports", "MLDR", "LooGLE", "LMEB_SciFact", "Gorilla", "ToolBench",
    "ReMe", "Proced_mem_bench", "MemGovern", "DeepPlanning",
]

MEMORY_TYPES = {
    "EPBench": "Episodic", "KnowMeBench": "Episodic", "ReMe": "Episodic",
    "LoCoMo": "Dialogue", "LongMemEval": "Dialogue", "REALTALK": "Dialogue",
    "TMD": "Dialogue", "MemBench": "Dialogue", "ConvoMem": "Dialogue",
    "QASPER": "Semantic", "NovelQA": "Semantic", "PeerQA": "Semantic",
    "CovidQA": "Semantic", "ESGReports": "Semantic", "MLDR": "Semantic",
    "LooGLE": "Semantic", "LMEB_SciFact": "Semantic",
    "Gorilla": "Procedural", "ToolBench": "Procedural",
    "Proced_mem_bench": "Procedural", "MemGovern": "Procedural",
    "DeepPlanning": "Procedural",
}


# --- Token-budget dynamic batching (Apple Silicon / MPS) ---

from budget_batching import install_budget_batching  # noqa: E402

# --- OpenAI Encoder ---

class OpenAIEncoder:
    """MTEB-compatible encoder that uses OpenAI embeddings API."""

    def __init__(self, model: str = "text-embedding-3-small", api_key: str | None = None):
        import httpx
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.api_url = os.environ.get(
            "OPENAI_API_URL", "https://api.openai.com/v1/embeddings"
        )
        self._client = httpx.Client(timeout=120)
        self._dimensions: int | None = None

        # MTEB model metadata
        from mteb.models.model_meta import ModelMeta
        self.mteb_model_meta = ModelMeta(
            name=f"openai/{model}",
            revision="api",
            languages=None,
            open_weights=False,
            framework=[],
        )

    def encode(self, sentences: list[str], *, batch_size: int = 2048, **kwargs) -> np.ndarray:
        all_embeddings = []
        for i in range(0, len(sentences), batch_size):
            batch = sentences[i : i + batch_size]
            resp = self._client.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = [d["embedding"] for d in data["data"]]
            all_embeddings.extend(embeddings)

        arr = np.array(all_embeddings, dtype=np.float32)
        # L2 normalize
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1
        arr = arr / norms
        if self._dimensions is None:
            self._dimensions = arr.shape[1]
        return arr


# --- Summary ---

def find_result_files(output_dir: str) -> dict[str, str]:
    """Find MTEB result JSON files in nested directory structure."""
    result_map = {}
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            if f.endswith(".json") and f != "model_meta.json" and not f.startswith("_"):
                task_name = f.replace(".json", "")
                result_map[task_name] = os.path.join(root, f)
    return result_map


def summarize_results(output_dir: str, task_names: list[str], metric: str = "main_score"):
    """Summarize results from MTEB output JSON files."""
    results = {}
    type_scores: dict[str, list[float]] = {}
    result_files = find_result_files(output_dir)

    for name in task_names:
        if name not in result_files:
            logger.warning(f"Missing result for {name}")
            continue
        json_path = result_files[name]

        with open(json_path) as f:
            data = json.load(f)

        eval_split = list(data["scores"].keys())[0]
        subsets = data["scores"][eval_split]
        score = sum(s[metric] for s in subsets) / len(subsets)
        results[name] = score

        mem_type = MEMORY_TYPES.get(name, "Unknown")
        type_scores.setdefault(mem_type, []).append(score)

    if not results:
        logger.warning("No results found.")
        return

    # Print summary
    print("\n" + "=" * 70)
    print(f"LMEB Benchmark Results — {metric}")
    print("=" * 70)

    mean_dataset = sum(results.values()) / len(results)
    print(f"\nMean (Dataset): {len(results)} tasks — {mean_dataset * 100:.2f}")

    type_means = []
    for mem_type in ["Dialogue", "Episodic", "Semantic", "Procedural"]:
        if mem_type in type_scores:
            scores = type_scores[mem_type]
            m = sum(scores) / len(scores)
            type_means.append(m)
            print(f"  {mem_type}: {len(scores)} tasks — {m * 100:.2f}")

    if type_means:
        print(f"Mean (Type): {sum(type_means) / len(type_means) * 100:.2f}")

    print("\nPer-task scores:")
    for name in task_names:
        if name in results:
            print(f"  {name}: {results[name] * 100:.2f}")

    # Save summary JSON
    summary = {
        "metric": metric,
        "mean_dataset": mean_dataset,
        "type_means": {k: sum(v) / len(v) for k, v in type_scores.items()},
        "per_task": results,
    }
    summary_path = os.path.join(output_dir, "_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSummary saved to {summary_path}")


def _load_st_model(args, model_kwargs):
    """Load a sentence-transformer through mteb, with a direct fallback.

    Some models exist in the mteb registry as metadata-only entries (no
    loader — e.g. jinaai/jina-embeddings-v5-text-nano), where mteb.get_model
    raises NotImplementedError. Those are loaded directly and wrapped; the
    wrapper picks up the model's built-in prompts (query/document), so
    prompt handling matches registry-loaded models.
    """
    try:
        return mteb.get_model(args.model_path, device=args.device, model_kwargs=model_kwargs)
    except NotImplementedError:
        from sentence_transformers import SentenceTransformer
        from mteb.models.sentence_transformer_wrapper import (
            SentenceTransformerEncoderWrapper,
        )
        logger.info(f"mteb registry has no loader for {args.model_path} — loading directly")
        st = SentenceTransformer(
            args.model_path,
            device=args.device,
            trust_remote_code=args.trust_remote_code,
            model_kwargs=model_kwargs,
        )
        return SentenceTransformerEncoderWrapper(st)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="LMEB Benchmark Runner")
    parser.add_argument(
        "--model_path",
        default="sentence-transformers/all-MiniLM-L6-v2",
        help="HuggingFace model path (for sentence-transformer type)",
    )
    parser.add_argument(
        "--model_type",
        default="sentence-transformer",
        choices=["sentence-transformer", "openai"],
        help="Model type",
    )
    parser.add_argument(
        "--tasks",
        default=None,
        help="Comma-separated task names (default: all 22 LMEB tasks)",
    )
    parser.add_argument(
        "--output_dir",
        default="lmeb_results",
        help="Output directory for results",
    )
    parser.add_argument("--batch_size", type=int, default=64, help="Encoding batch size")
    parser.add_argument("--openai_model", default="text-embedding-3-small")
    parser.add_argument("--openai_key", default=None)
    parser.add_argument(
        "--budget_encode",
        action="store_true",
        help="Use token-budget dynamic batching (STBudgetEncoder) — required for "
        "long-context tasks on MPS/Apple Silicon",
    )
    parser.add_argument("--device", default=None, help="torch device for --budget_encode (e.g. mps)")
    parser.add_argument(
        "--dtype",
        default="float16",
        choices=["float16", "float32"],
        help="model dtype for --budget_encode",
    )
    parser.add_argument(
        "--trust_remote_code",
        action="store_true",
        help="pass trust_remote_code=True when the model needs custom modeling "
        "code (e.g. jinaai/jina-embeddings-v5-text-nano)",
    )
    parser.add_argument(
        "--default_task",
        default=None,
        help="default_task model kwarg for task-LoRA models "
        "(jina v5: 'retrieval' — required, encoding fails without a task)",
    )
    args = parser.parse_args()

    # Resolve task list
    if args.tasks:
        task_names = [t.strip() for t in args.tasks.split(",")]
    else:
        task_names = LMEB_TASK_NAMES

    tasks = mteb.get_tasks(tasks=task_names)
    logger.info(f"Selected {len(tasks)} tasks")

    # Load model
    if args.model_type == "openai":
        model = OpenAIEncoder(model=args.openai_model, api_key=args.openai_key)
        model_name = args.openai_model
    elif args.budget_encode:
        import torch
        model_kwargs = {}
        if args.dtype == "float16":
            model_kwargs["torch_dtype"] = torch.float16
        if args.default_task:
            model_kwargs["default_task"] = args.default_task
        model = _load_st_model(args, model_kwargs)
        install_budget_batching(model.model)
        model_name = args.model_path.split("/")[-1]
    else:
        model_kwargs = {"default_task": args.default_task} if args.default_task else {}
        model = _load_st_model(args, model_kwargs)
        model_name = args.model_path.split("/")[-1]

    # Create output directory: lmeb_results/{model_name}/
    result_dir = os.path.join(args.output_dir, model_name)
    os.makedirs(result_dir, exist_ok=True)

    logger.info(f"Model: {model_name} ({args.model_type})")
    logger.info(f"Output: {result_dir}")

    # Run evaluation
    total_start = time.time()
    for i, task in enumerate(tasks, 1):
        name = task.metadata.name
        existing = find_result_files(result_dir)
        if name in existing:
            logger.info(f"[{i}/{len(tasks)}] {name} — cached, skipping")
            continue

        logger.info(f"[{i}/{len(tasks)}] {name} — starting...")
        task_start = time.time()

        try:
            evaluation = mteb.MTEB(tasks=[task])
            evaluation.run(
                model,
                output_folder=result_dir,
                encode_kwargs={"batch_size": args.batch_size},
            )
            elapsed = time.time() - task_start
            logger.info(f"[{i}/{len(tasks)}] {name} — done in {elapsed:.1f}s")
        except Exception as e:
            logger.error(f"[{i}/{len(tasks)}] {name} — FAILED: {e}")
            continue

    total_elapsed = time.time() - total_start
    logger.info(f"Total time: {total_elapsed:.1f}s ({total_elapsed / 60:.1f}min)")

    # Summarize
    summarize_results(result_dir, task_names)


if __name__ == "__main__":
    main()
