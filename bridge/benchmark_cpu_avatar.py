"""Measure the actual configured CPU avatar; never activate an alternative model."""
import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from cpu_avatar import CpuAvatar


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.output.exists() or args.repeats < 1 or not args.audio.is_file():
        parser.error("Use an existing WAV, positive repeats, and a new evidence directory")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True)
    report = {"status": "RUNNING", "pid": os.getpid(), "cases": [],
              "visual_accepted": False, "phone_accepted": False,
              "timing_scope": "render and encode only; excludes TTS, LLM and phone"}
    def save():
        (args.output/"summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    save()
    try:
        report["input_sha256"] = hashlib.sha256(args.audio.read_bytes()).hexdigest()
        model_path = Path(config["avatar_ir"])
        report["ir_xml_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
        report["ir_bin_sha256"] = hashlib.sha256(model_path.with_suffix(".bin").read_bytes()).hexdigest()
        report["image_sha256"] = hashlib.sha256(Path(config["avatar_image"]).read_bytes()).hexdigest()
        start = time.perf_counter()
        avatar = CpuAvatar(config)
        report.update(load_seconds=time.perf_counter()-start, metadata=avatar.metadata)
        waveform, rate, _ = avatar.audio_features(str(args.audio))
        duration = len(waveform)/rate
        for repeat in range(args.repeats):
            parts = []
            for part in range(math.ceil(duration/2)):
                parts.append(avatar.render(str(args.audio), part,
                    str(args.output/f"repeat-{repeat}-part-{part}.mp4")))
                report["active_repeat"] = repeat
                save()
            seconds = sum(item["seconds"] for item in parts)
            report["cases"].append({"repeat": repeat, "parts": parts,
                "audio_seconds": duration, "seconds": seconds, "rtf": seconds/duration})
            save()
        report["generation_pass"] = True
        report["necessary_throughput_pass"] = all(item["rtf"] <= 1 for item in report["cases"][1:] or report["cases"])
        report["status"] = "PASS" if report["necessary_throughput_pass"] else "FAIL"
        return 0 if report["status"] == "PASS" else 1
    except Exception as exc:
        report.update(status="FAIL", error_type=type(exc).__name__, error=str(exc))
        return 1
    finally:
        save()


if __name__ == "__main__":
    raise SystemExit(main())
