# CPU validation checkpoint

For the implemented service, current integration evidence, startup and phone acceptance steps, read [CPU_SERVICE.md](CPU_SERVICE.md). The FP32 measurements below remain historical baselines; they are not the current implementation status.

Task and acceptance truth: the knowledge-base project `06-program/AI女友/AI女友-index.md`.
Read its implementation/continuation page and Tianyi device page before another run.
This repository's original GPU/DUIX performance claims do not apply to the CPU branch.

## Reproduce the OmniVoice gate

Use the dedicated CPU Python environment and the already downloaded local model.
Set OMP_NUM_THREADS, MKL_NUM_THREADS and OPENBLAS_NUM_THREADS to 1 before Python starts.
Use HF_HUB_OFFLINE=1. Ensure Git is on PATH for the source manifest.
All arguments are device-specific locations, obtained from the device page.

```powershell
& $cpuPython bridge/benchmark_cpu_tts.py --model $modelDirectory --output $newRunDirectory --threads 1 4 --repeats 2
```

The output directory must be new. Never overwrite a failed run. The command records:

- Model weight hashes, source commit/status, script hash, package versions, input hash and CPU thread counts.
- Actual CPU/FP32 parameters, model loading time, generation time, audio duration and RTF.
- Every WAV and its hash, plus a cumulative CPU profile for every generation.
- Independent generation, necessary timing, listening and phone acceptance states.

Exit 0 means technical generation and a necessary timing gate pass for at least one tested thread setting.
Exit 1 means failure, including valid audio generated too slowly.
Exit 2 means invalid or missing prerequisites.
Generation must be <=10 seconds AND RTF<=1 for this necessary gate. Passing does not prove the
20-turn end-to-end phone P95 or 30/45/60-second continuous-video criteria.
No reference-voice quality acceptance is implied by the female voice-design smoke.

## Verified on 2026-09-28

- Cloud source checkout recovered at 480bbabada7335735cad591eaa55f32fe54a4214.
  103 archive file blobs matched the commit's Git hashes before checkout/worktree creation.
- CPU import check passed; OmniVoice weights and embedded audio tokenizer are fully downloaded.
- Silero + existing SenseVoice passed nine technical cases (Chinese/English/silence, three each).
  The failed direct-ASR silence test is retained. Recognition accuracy is not accepted.
- Initial 8-thread FP32/32-step TTS generated 1.9 seconds of audio in 46.367, 41.073 and 39.169 seconds.
  Generation succeeded; real-time throughput and the response-latency prerequisite failed.
- One-thread generation took 180.814 seconds for 1.9 seconds of audio; remaining low-thread cases were stopped and the interruption recorded separately. 16 threads took 45.009 seconds. The 32-thread case was stopped after at least 158.195 seconds; 64 threads were not reached and are unmeasured. Wav2Lip, browser integration and phone acceptance are unverified.
- FP32 CPU performance gate is blocked. The original run summaries are preserved; interruption sidecars record stopped processes so historical RUNNING states cannot be mistaken for live work.
- A possible CPU INT8 experiment requires a decision because it changes the recorded FP32 design; no quantization has been applied.

## Reproduce ASR technical smoke

```powershell
& $cpuPython bridge/verify_cpu_asr.py --models $existingSenseVoiceRoot --samples $officialSampleDirectory --output $newAsrRunDirectory
```

This reuses existing SenseVoice and Silero weights. Chinese, English and two-second silence each run three times. VAD gates ASR; silence must not be decoded. The output records file hashes and each timing/transcript. A nonempty transcript only passes the technical smoke, never the CER/entity acceptance gate. Exit codes are 0 (technical pass), 1 (failure), 2 (missing/invalid prerequisites).

Do not proceed to UI expansion or claim a usable phone conversation while the CPU media gate fails.
Do not silently switch models, enable GPU, use prerecorded talking video or replace video with audio.
