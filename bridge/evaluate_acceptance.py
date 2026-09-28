"""Evaluate actual phone timing evidence. Missing/failed cases never become PASS."""
import argparse
import json
import math
from pathlib import Path


def evaluate(data):
    errors = []
    if data.get("source") != "real-phone-real-api":
        errors.append("Requires real phone and real API evidence")
    for key in ("run_id", "commit", "phone", "browser", "network", "model_manifest"):
        if not data.get(key):
            errors.append("Missing " + key)
    turns = data.get("turns", [])
    if len(turns) != 20:
        errors.append("Exactly 20 ordinary warm turns required")
    if len({turn.get("id") for turn in turns}) != len(turns):
        errors.append("Duplicate turn IDs")
    times = []
    for turn in turns:
        if turn.get("cold") or turn.get("tool_wait"):
            errors.append("Cold/tool-wait turn included in ordinary cohort")
        if turn.get("outcome") != "pass":
            errors.append("Failed turn retained: " + str(turn.get("id")))
        start, end = turn.get("last_speech_sample_ms"), turn.get("first_audible_video_ms")
        if not turn.get("audible_evidence") or turn.get("clock") != "phone-monotonic":
            errors.append("Missing actual audible evidence or common phone clock")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end) or end < start:
            errors.append("Invalid timing: " + str(turn.get("id")))
        else:
            times.append((end-start)/1000)
    times.sort()
    p95 = times[math.ceil(.95*len(times))-1] if times else None
    if p95 is not None and p95 > 10:
        errors.append("P95 exceeds ten seconds")
    return {"status": "FAIL" if errors else "PASS", "count": len(turns),
            "p50_seconds": times[math.ceil(.5*len(times))-1] if times else None,
            "p95_seconds": p95, "max_seconds": max(times) if times else None,
            "sorted_seconds": times, "errors": errors,
            "scope": "ordinary warm phone latency only; other acceptance gates remain separate"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output; retain prior results")
    result = evaluate(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "p95_seconds": result["p95_seconds"]}))
    return 0 if result["status"] == "PASS" else 1

if __name__ == "__main__": raise SystemExit(main())
