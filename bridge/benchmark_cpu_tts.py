"""Reproducible CPU-only OmniVoice benchmark; does not start the voice service.

Exit 0: technical generation and necessary timing gate pass.
Exit 1: generation or necessary timing gate fails.
Exit 2: invalid/missing prerequisites. A PASS is not phone or listening acceptance.
"""
import argparse
import cProfile
import hashlib
import importlib.metadata
import json
import os
import platform
import pstats
import subprocess
import sys
import time
import traceback
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New run directory; never overwrite a run.")
    parser.add_argument("--threads", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--precision", choices=["fp32", "int8"], default="fp32")
    parser.add_argument("--steps", type=int, choices=[8, 16, 32], default=32)
    parser.add_argument("--text", default="你好，我已经准备好了。")
    args = parser.parse_args()
    if not args.text.strip() or args.repeats < 1 or any(n < 1 for n in args.threads):
        parser.error("text, repeats and thread counts must be nonempty/positive")
    files = [args.model / "model.safetensors", args.model / "audio_tokenizer/model.safetensors"]
    if any(not path.is_file() for path in files) or args.output.exists():
        parser.error("model weights must exist and output directory must be new")
    args.output.mkdir(parents=True)
    report = {"status": "RUNNING", "stage": "imports", "pid": os.getpid(), "cases": [],
              "listening_verified": False, "phone_verified": False,
              "timing_boundary": "model.generate only, excluding load and WAV write",
              "performance_gate": "necessary only: generation <=10s and RTF<=1; not end-to-end acceptance"}
    manifest = {"run_id": args.output.name, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "host": platform.node(), "platform": platform.platform(), "logical_cpus": os.cpu_count(),
                "python": sys.version, "model": str(args.model.resolve()),
                "model_files": [{"name": str(path.relative_to(args.model)), "sha256": sha256(path)} for path in files],
                "threads": args.threads, "repeats": args.repeats, "text": args.text,
                "text_sha256": hashlib.sha256(args.text.encode()).hexdigest(),
                "steps": args.steps, "seed": 42, "precision": args.precision, "device": "cpu",
                "asr_loading": False, "script_sha256": sha256(Path(__file__))}
    try:
        manifest["source_commit"] = subprocess.check_output(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"], text=True).strip()
        manifest["source_status"] = subprocess.check_output(
            ["git", "-C", str(Path(__file__).resolve().parent), "status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        manifest["source_commit"] = None
    def save():
        (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")
        (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        print(json.dumps({"status": report["status"], "stage": report["stage"],
                          "completed_cases": len(report["cases"])}), flush=True)
    save()
    try:
        import torch
        import numpy as np
        import soundfile as sf
        from omnivoice import OmniVoice, OmniVoiceGenerationConfig
        manifest["packages"] = {name: importlib.metadata.version(name) for name in
                                ("torch", "torchaudio", "omnivoice", "transformers", "soundfile")}
        torch.set_num_threads(args.threads[0])
        torch.set_num_interop_threads(1)
        if torch.version.cuda is not None:
            raise ValueError("A CPU-only torch build is required")
        report["stage"] = "load_model"
        save()
        started = time.perf_counter()
        model = OmniVoice.from_pretrained(str(args.model), device_map="cpu",
                                           dtype=torch.float32, load_asr=False).float().eval()
        report["load_seconds"] = time.perf_counter() - started
        if args.precision == "int8":
            torch.backends.quantized.engine = "fbgemm"
            quant_start = time.perf_counter()
            model = torch.ao.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8, inplace=True)
            report["quantize_seconds"] = time.perf_counter() - quant_start
            manifest["quantized_linear_count"] = sum(isinstance(m, torch.ao.nn.quantized.dynamic.Linear) for m in model.modules())
            if not manifest["quantized_linear_count"]:
                raise ValueError("No linear layers quantized")
        devices = sorted({str(p.device) for p in model.parameters()})
        dtypes = sorted({str(p.dtype) for p in model.parameters() if p.is_floating_point()})
        if devices != ["cpu"] or dtypes != ["torch.float32"] or getattr(model, "_asr_pipe", None) is not None:
            raise ValueError(f"Unexpected runtime: {devices}, {dtypes}")
        manifest.update(actual_devices=devices, actual_dtypes=dtypes, sample_rate=model.sampling_rate)
        for threads in args.threads:
            torch.set_num_threads(threads)
            for repeat in range(args.repeats):
                report.update(stage="generate", current_threads=threads, current_repeat=repeat)
                save()
                torch.manual_seed(42)
                profiler = cProfile.Profile()
                started = time.perf_counter()
                with torch.inference_mode(), profiler:
                    wave = model.generate(text=args.text, language="Chinese", instruct="female",
                        normalize_text=False, generation_config=OmniVoiceGenerationConfig(num_step=args.steps))[0]
                seconds = time.perf_counter() - started
                if isinstance(wave, torch.Tensor):
                    wave = wave.detach().cpu().numpy()
                wave = np.asarray(wave).squeeze()
                if wave.ndim != 1 or not wave.size or not np.isfinite(wave).all() or np.max(np.abs(wave)) <= .001:
                    raise ValueError("Invalid, empty or silent generated waveform")
                name = f"threads-{threads}-repeat-{repeat}"
                wav = args.output / (name + ".wav")
                sf.write(wav, wave, model.sampling_rate, subtype="PCM_16")
                with (args.output / (name + ".profile.txt")).open("w", encoding="utf-8") as log:
                    pstats.Stats(profiler, stream=log).strip_dirs().sort_stats("cumulative").print_stats(25)
                duration = wave.size / model.sampling_rate
                report["cases"].append({"threads": threads, "repeat": repeat, "seconds": seconds,
                    "audio_seconds": duration, "rtf": seconds / duration, "file": wav.name,
                    "sha256": sha256(wav), "timing_pass": seconds <= 10 and seconds <= duration})
                save()
        successful = any(all(c["timing_pass"] for c in report["cases"] if c["threads"] == n) for n in args.threads)
        report.update(status="PASS" if successful else "FAIL", stage="complete",
                      generation_pass=True, necessary_timing_gate_pass=successful)
        return 0 if successful else 1
    except ImportError as exc:
        report.update(status="BLOCKED", stage="prerequisites", error=str(exc), traceback=traceback.format_exc())
        return 2
    except Exception as exc:
        report.update(status="FAIL", error=str(exc), traceback=traceback.format_exc())
        return 1
    finally:
        save()


if __name__ == "__main__":
    sys.exit(main())
