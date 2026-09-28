"""Technical SenseVoice + Silero CPU smoke, not recognition accuracy acceptance."""
import argparse
import hashlib
import json
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
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = args.models / "sensevoice-onnx/model.int8.onnx"
    tokens = args.models / "sensevoice-onnx/tokens.txt"
    silero = args.models / "silero/silero_vad.onnx"
    paths = [model, tokens, silero, args.samples / "zh.wav", args.samples / "en.wav"]
    if args.output.exists() or any(not p.is_file() for p in paths):
        parser.error("output must be new and models/samples must exist")
    args.output.mkdir(parents=True)
    report = {"status": "RUNNING", "cases": [],
              "scope": "CPU VAD + ASR technical smoke; CER, entities and phone acceptance not evaluated",
              "asr_threads": 4, "vad_threads": 1, "provider": "cpu", "sample_rate": 16000,
              "script_sha256": sha256(Path(__file__)),
              "inputs": [{"path": str(p), "sha256": sha256(p)} for p in paths]}
    def save():
        (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    save()
    try:
        import numpy as np
        import sherpa_onnx
        import soundfile as sf
        report["sherpa_onnx_version"] = sherpa_onnx.__version__
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(silero)
        config.silero_vad.threshold = 0.5
        config.silero_vad.min_speech_duration = 0.25
        config.silero_vad.min_silence_duration = 0.5
        config.silero_vad.window_size = 512
        config.sample_rate, config.num_threads, config.provider = 16000, 1, "cpu"
        vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=40)
        started = time.perf_counter()
        asr = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), provider="cpu", num_threads=4,
            sample_rate=16000, feature_dim=80, language="auto", use_itn=True)
        report["asr_load_seconds"] = time.perf_counter() - started
        for name in ("zh", "en", "silence"):
            if name == "silence":
                audio = np.zeros(32000, dtype=np.float32)
            else:
                audio, rate = sf.read(args.samples / (name + ".wav"), dtype="float32")
                if rate != 16000 or audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all():
                    raise ValueError("Expected finite, nonempty 16kHz mono samples")
            for repeat in range(3):
                started = time.perf_counter()
                vad.reset()
                for start in range(0, len(audio), 512):
                    chunk = audio[start:start+512]
                    if len(chunk) < 512:
                        chunk = np.pad(chunk, (0, 512-len(chunk)))
                    vad.accept_waveform(chunk)
                vad.flush()
                speech = not vad.empty()
                vad_seconds = time.perf_counter() - started
                text, asr_seconds = "", 0.0
                if speech:
                    stream = asr.create_stream()
                    stream.accept_waveform(16000, audio)
                    started = time.perf_counter()
                    asr.decode_stream(stream)
                    asr_seconds = time.perf_counter() - started
                    text = stream.result.text
                passed = (not speech and not text) if name == "silence" else (speech and bool(text))
                report["cases"].append({"sample": name, "repeat": repeat,
                    "audio_seconds": len(audio)/16000, "speech": speech, "text": text,
                    "vad_seconds": vad_seconds, "asr_seconds": asr_seconds, "passed": passed})
                save()
        passed = all(case["passed"] for case in report["cases"])
        report["status"] = "PASS" if passed else "FAIL"
        return 0 if passed else 1
    except ImportError as exc:
        report.update(status="BLOCKED", error=str(exc))
        return 2
    except Exception as exc:
        report.update(status="FAIL", error=str(exc), traceback=traceback.format_exc())
        return 1
    finally:
        save()
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    sys.exit(main())
