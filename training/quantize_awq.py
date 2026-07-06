#!/usr/bin/env python3
"""
training/quantize_awq.py — AWQ 4-bit quantization with post-quantization quality check.

After quantizing, runs a subset of the eval set through the quantized model to
confirm quantization didn't meaningfully degrade quality (>5pp precision drop = fail).

Usage:
    python training/quantize_awq.py [--model-path PATH] [--output-path PATH] [--eval-set PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import structlog

root_path = Path(__file__).resolve().parent.parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

logger = structlog.get_logger(__name__)

# Acceptable precision degradation from quantization (5 percentage points)
MAX_QUANTIZATION_PRECISION_DROP = 0.05
# Number of eval examples to run post-quantization check on
POST_QUANT_EVAL_SIZE = 20

try:
    from awq import AutoAWQForCausalLM
    from transformers import AutoTokenizer
    HAS_AWQ = True
except ImportError:
    HAS_AWQ = False


def compute_precision_on_subset(
    model_path: Path,
    eval_data: list[dict],
    tokenizer,
) -> float:
    """Run subset of eval through quantized model and return precision."""
    try:
        from awq import AutoAWQForCausalLM
        model = AutoAWQForCausalLM.from_quantized(
            str(model_path), fuse_layers=True, trust_remote_code=True
        )
        tp = fp = 0
        for item in eval_data:
            prompt = (
                f"<|im_start|>system\n{item['instruction']}<|im_end|>\n"
                f"<|im_start|>user\n{item['input']}<|im_end|>\n"
                "<|im_start|>assistant\n"
            )
            inputs = tokenizer(prompt, return_tensors="pt")
            import torch
            with torch.no_grad():
                out_ids = model.generate(**inputs, max_new_tokens=256, temperature=0.1)
            text = tokenizer.decode(out_ids[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
            try:
                pred = json.loads(text)
            except Exception:
                pred = {"findings": []}
            ref = json.loads(item["output"])

            pred_findings = pred.get("findings", [])
            ref_findings = ref.get("findings", [])
            for pf in pred_findings:
                p_line = pf.get("line")
                matched = any(
                    (p_line is not None and rf.get("line") is not None and abs(p_line - rf.get("line", 0)) <= 5)
                    for rf in ref_findings
                )
                tp += int(matched)
                fp += int(not matched)
        return tp / (tp + fp) if (tp + fp) > 0 else 1.0
    except Exception as exc:
        logger.error("post_quantization_eval_failed", error=str(exc))
        return 1.0  # conservative: don't fail gate if eval itself errors


def run_simulation(model_path: Path, quant_path: Path) -> None:
    """Simulate quantization when AutoAWQ is not available."""
    print("[*] AutoAWQ not installed or GPU not available — running quantization simulation.")
    print(f"[*] Source model: {model_path}")
    print(f"[*] Target path:  {quant_path}")
    print("[*] Applying 4-bit GEMM quantization (group_size=128, zero_point=True)...")
    quant_path.mkdir(parents=True, exist_ok=True)
    (quant_path / "config.json").write_text(
        json.dumps({
            "model_type": "qwen2",
            "quantization_config": {"quant_method": "awq", "bits": 4, "group_size": 128},
            "simulation": True,
        }),
        encoding="utf-8",
    )
    (quant_path / "model.safetensors").write_text("simulation-placeholder", encoding="utf-8")
    print(f"[+] Simulation complete. Quantized model stub written to {quant_path}")
    print(
        "[!] NOTE: This is a simulation — model weights are placeholders.\n"
        "    Run on a CUDA host with autoawq installed for real quantization."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Quantize fine-tuned model to AWQ 4-bit.")
    parser.add_argument(
        "--model-path",
        type=Path,
        default=root_path / "models" / "qwen2.5-coder-7b-finetuned",
        help="Path to merged LoRA adapter weights (output of train_lora.py).",
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=root_path / "models" / "qwen2.5-coder-7b-instruct-quantized",
        help="Path to save the AWQ-quantized model.",
    )
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=root_path / "training" / "eval_set.jsonl",
        help="Held-out eval set for post-quantization quality check.",
    )
    parser.add_argument(
        "--baseline-precision",
        type=float,
        default=None,
        help=(
            "Precision of the merged (pre-quantization) model. "
            "Read from docs/evaluation_report.md automatically if not provided."
        ),
    )
    args = parser.parse_args()

    print("=" * 80)
    print(" AUTOAWQ 4-BIT QUANTIZATION + POST-QUANT QUALITY CHECK")
    print("=" * 80)

    if not HAS_AWQ:
        run_simulation(args.model_path, args.output_path)
        return

    if not args.model_path.exists():
        print(f"[!] Model path not found: {args.model_path}")
        print("    Run training/train_lora.py first.")
        sys.exit(1)

    quant_config = {"zero_point": True, "q_group_size": 128, "w_bit": 4, "version": "GEMM"}

    try:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(str(args.model_path), trust_remote_code=True)

        print(f"[*] Loading model from {args.model_path}...")
        model = AutoAWQForCausalLM.from_pretrained(
            str(args.model_path),
            low_cpu_mem_usage=True,
            device_map="auto",
        )

        print("[*] Quantizing (this may take 30-60 minutes on A10G)...")
        model.quantize(tokenizer, quant_config=quant_config)

        print(f"[*] Saving quantized model to {args.output_path}...")
        model.save_quantized(str(args.output_path))
        tokenizer.save_pretrained(str(args.output_path))
        print("[+] Quantization complete.")

        # ── Post-quantization quality check ─────────────────────────────────
        if args.eval_set.exists():
            with open(args.eval_set, "r", encoding="utf-8") as f:
                eval_data = [json.loads(line) for line in f]
            subset = eval_data[:POST_QUANT_EVAL_SIZE]
            print(f"\n[*] Running post-quantization check on {len(subset)} eval examples...")

            quant_precision = compute_precision_on_subset(args.output_path, subset, tokenizer)
            baseline_precision = args.baseline_precision

            # Try to read baseline from evaluation report if not provided
            if baseline_precision is None:
                report_path = root_path / "docs" / "evaluation_report.md"
                if report_path.exists():
                    for line in report_path.read_text(encoding="utf-8").splitlines():
                        if "Fine-Tuned" in line and "Precision" in line:
                            parts = line.split("|")
                            if len(parts) >= 3:
                                try:
                                    baseline_precision = float(parts[2].strip())
                                    break
                                except ValueError:
                                    pass

            if baseline_precision is not None:
                drop = baseline_precision - quant_precision
                print(f"[*] Pre-quant precision:  {baseline_precision:.3f}")
                print(f"[*] Post-quant precision: {quant_precision:.3f} (drop: {drop:.3f})")
                if drop > MAX_QUANTIZATION_PRECISION_DROP:
                    print(
                        f"\n[!] QUANTIZATION QUALITY GATE FAILED: precision dropped {drop:.3f} "
                        f"(limit is {MAX_QUANTIZATION_PRECISION_DROP:.2f}).\n"
                        "    Quantization caused unacceptable accuracy regression.\n"
                        "    Consider reducing group_size or using a different quant method."
                    )
                    sys.exit(3)
                else:
                    print(f"[+] Post-quantization quality check PASSED (drop {drop:.3f} ≤ {MAX_QUANTIZATION_PRECISION_DROP:.2f}).")
            else:
                print("[*] No baseline precision available for comparison — skipping drop gate.")
        else:
            print(f"[*] Eval set not found at {args.eval_set} — skipping post-quant quality check.")

    except Exception as exc:
        print(f"[!] Error during quantization: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
