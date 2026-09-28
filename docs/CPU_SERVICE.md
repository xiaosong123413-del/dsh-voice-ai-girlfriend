# CPU companion service: implementation and operator handoff

Status: core implementation and real cloud model chain verified; performance and phone acceptance are **not complete**. This branch does not replace the original GPU bridge.

## Reuse the existing task environment

Read the device page in the task-system knowledge base for machine-specific roots. Keep code, caches, temporary files and evidence in their approved roots. Do not reinstall DSH or copy its credentials. Model pins and measured failures are recorded in CPU_VALIDATION.md and the task evidence.

1. Install requirements-cpu-service.txt into the existing CPU virtual environment using its uv and approved cache. The WebSocket package is required by both Uvicorn and acceptance clients.
2. Create a task-local configuration from bridge/cpu_config.example.json. Replace every placeholder, set the real HTTPS origin and use a visually checked face box. Set voice_reference_audio to a mono WAV (preferably 3–10 seconds, at most 20 seconds) and voice_reference_text to its exact transcript. The resident TTS worker encodes this voice once and reuses the same prompt for every segment; a missing reference is an error. No implicit ASR or fresh voice design runs per segment. Never put a password or API key in this JSON.
3. Create a task-local Cordis overlay:
   ```yaml
   - insert:
       - id: ai-companion-stream
         name: '<absolute worktree>/bridge/dsh_stream_plugin.mjs'
         inject: [agents, sessionPersistence]
   ```
4. In the process environment, set AI_COMPANION_PASSWORD interactively to a private value of at least 16 characters. Do not log, commit or paste it into task records. Set HF_HOME, TEMP/TMP and UV_CACHE_DIR to the approved task roots; HF_HUB_OFFLINE=1 prevents accidental downloads while serving.
5. Start the CPU environment's Python with `bridge/cpu_server.py --config <task config>`. The service binds only 127.0.0.1:8765. Wait for health status ready before interaction. The measured cold model start was about 105 seconds.
6. Forward only /voice and /voice/* through the existing HTTPS tunnel to this loopback service, retaining the full path. Do not expose management ports or the DSH SDK control endpoint. The configured public_origin must match exactly. Forward WebSocket upgrades, SSE without buffering, and byte-range requests.
7. Open /voice/ on the phone, authenticate, then click Start. If autoplay is rejected, the page exposes an explicit play button. End and backgrounding stop local capture/playback immediately. Microphone off leaves video playback running.
8. Stop the owned service with its normal console interrupt. Lifespan shutdown closes the owned DSH child and resident models. Do not kill or restart unrelated DSH processes.

The DSH plugin opens an ephemeral authenticated loopback cancel socket; its credential exists only in the child environment and parent memory. The native SDK initializes the user's existing DSH provider configuration. The owned plugin uses native agents.create/resume and createUserMessage APIs for prompts, because this installed SDK's session/prompt only creates new sessions. A prompt-start marker arms media only after resume/cancellation has settled; old log/turn events cannot be spoken. The tested API model is deepseek-v4-pro; deepseek-official is the provider ID and is not an API model name.

## Components and contracts

- reply_protocol.py: reply CAS, immutable Unicode text segments, distinct text/media sequences, atomic subscription snapshot, cancellation, closed-state checks and generation-ready.
- speech_selection.py: streams the first readable assistant message; retains only the final committed candidate; rejects rewritten spoken prefixes; flushes the short final tail.
- dsh_bridge.py and dsh_stream_plugin.mjs: real SDK requests, live assistant-stream frames, durable assistant/message and turn/end, owned-session cancellation.
- cpu_workers.py/cpu_runtime.py: one resident process per model, explicit CPU thread budgets, one TTS generation per immutable segment, bounded overlap with rendering, cancellation checks before/after atomic inference.
- cpu_avatar.py: original non-GAN Wav2Lip compiled once with OpenVINO CPU; a constant FP32 face input (encoder work can be precomputed), batched inference, 25 fps H.264/AAC MP4, two-second parts and intact tails, atomic publish.
- cpu_server.py: cookie authentication, same-origin mutation/WS checks, protected API/SSE/media/Range; no public docs or management routes.
- phone/: actual recording worklet, 16 kHz PCM, voice endpointing, bounded preroll, shared media queue, user-gesture start, mic/end separation, disconnection/background stop.

## Reproducible checks

From bridge with the CPU Python:
```text
python -m unittest test_reply_protocol test_speech_selection test_cpu_server test_cpu_runtime test_dsh_bridge test_evaluate_acceptance test_cpu_voice -v
node --test phone/test_capture.cjs phone/test_interaction.cjs
node --check phone/app.js
```

32 Python tests, 3 worklet sample-rate tests and 7 simulated-browser interaction tests passed on 2026-09-28. Interaction tests include interrupted startup, old playback promises, duplicate media, End and backgrounding; they are not a real browser or phone test. FakeRuntime in unit tests is explicitly a test double; it is never a production fallback. Real evidence is separate:
- dsh-sdk-smoke-02.json: native API stream and completed durable turn.
- dsh-adapter-01.json: one actual response submitted once to the reply protocol.
- runtime-smoke-01.json: real ASR/VAD plus DSH→OmniVoice→Wav2Lip, 2.00+0.04 second media, first media 28.962 seconds, total 30.768 seconds, orderly shutdown.
- http-runtime-smoke-01.json: first real HTTP probe failed because websockets was absent. Failure retained.
- http-runtime-smoke-02.json: PASS after installing pinned websockets; real HTTP auth, loading state, VAD WS, silence STT, idempotent real STT, actual DSH/model media via SSE, byte Range, ready cancellation and cancelled-media denial. Generation total 31.713 seconds; phone/performance flags remain false.
- verify_cpu_service.py --config <task config> --sample <real 16k mono WAV> --output <new evidence directory>: repeatable repository version of the real HTTP probe. It starts an isolated local service with an in-memory test credential, exercises real models/API, then shuts down. Port 8765 must be free; do not stop unrelated listeners.
- evaluate_acceptance.py <phone-run.json> --output <new report.json>: nearest-rank twenty-turn latency evaluation; missing audible evidence, failed turns, duplicate IDs and non-phone cohorts fail. This evaluator does not replace human review of the evidence.

- http-service-repo-03/summary.json: repository probe PASS, all ten real HTTP/WS/SSE/model checks, 32.446 seconds to complete, owned service closed.
- http-service-repo-04/summary.json: latest fixed-reference voice plus FP32 constant-face implementation passed all ten real HTTP checks and closed normally. Reply audio was 3.16 seconds; full generation took 49.027 seconds. This is a functional PASS and a performance FAIL, not a phone latency measurement.
- voice-reference-01.json: two real OmniVoice segments reused one encoded generated-demo voice reference; 30.011/32.213 seconds for 2.16/2.32 seconds of audio. Voice consistency/listening are not human-accepted.
- avatar-frozen-equivalence-07.json: fixed-face FP32 output equals baseline element-for-element on six batches from two new audio samples (maximum absolute error zero).
- benchmark_cpu_avatar.py --config <task config> --audio <WAV> --output <new evidence directory>: real rendering/encoding and warm throughput checks. avatar-repo-08 generated all parts, but its throughput gate failed: 3.988–4.119 seconds per 2.16 seconds of audio. The script keeps generation and performance results separate.
- dsh-resume-01.json: actual native session resume across two separate DSH subprocesses; the second process correctly recalled the flower name supplied in the first, without speaking historical turns.

These tests do not prove a five-to-ten-second audible phone response. CPU throughput remains below playback rate. INT8 and 16 steps require listening-quality acceptance. Real phone permissions, 20-turn P95, long playback gaps/AV drift, external-speaker echo, five interruptions and 20 human ASR recordings remain unverified.

## Optional offline optimization experiments

quantize_cpu_avatar.py uses the [official NNCF calibration flow](https://docs.openvino.ai/2024/openvino-workflow/model-optimization-guide/quantizing-models-post-training/basic-quantization-flow.html) on the fixed original Wav2Lip graph. Install requirements-cpu-optimization.txt only for this experiment. Supply a new output directory and representative WAV inputs; preserve the FP32 IR. Calibration-set MAE is not held-out accuracy. The script does not change the runtime configuration. Set avatar_precision=int8 only after validating a generated IR; a missing INT8 IR is an explicit error, never a silent FP32 replacement.

The TTS CLI also accepts experimental eight-step sampling. The measured eight-step cases took 10.029/10.778 seconds for 2.16 seconds of audio. Listening and sustained-throughput gates remain unpassed; serving still defaults to sixteen steps. The first NNCF experiment failed on Windows GBK progress output; UTF-8 output was fixed and the failed evidence retained. NNCF graph-statistics compilation then crashed in native OpenVINO (0xc0000005); --statistics python used the public Python-statistics option and successfully produced an experimental IR. Three renders passed technically, with warm two-second parts taking 2.125/2.293 seconds, but visual comparison showed color blocks and neck artifacts. This INT8 avatar is rejected and NOT activated. Serving retains FP32 plus the numerically verified fixed-face optimization.

## Public HTTPS verification

Run `verify_public_service.py --origin https://<hostname> --output <new report.json>` with AI_COMPANION_PASSWORD privately supplied in its process environment. It tests the existing service and leaves it running. Run before user handoff: logging in revokes the previous app cookie. The probe generates one real DSH/model reply, then cancels it; it never prints the password or cookie.

The dedicated Cloudflare deployment passed twelve public checks in public-https-01.json: login, private endpoints, secure cookie, same-origin enforcement, WebSocket, unbuffered SSE with real media, byte ranges, no caching, cancellation and denied cancelled media. First file notification was 38.205 seconds; this is not actual audible phone timing. A separate desktop HTTPS login/health check returned 303/200 and ready using the encrypted credential without displaying it.

Deployment-specific hostname, task paths and encrypted-credential ownership live on the task-system device page. Windows logon tasks were registered for the existing Administrator profile; a reboot without that profile logging in is not a verified unattended startup. Never run another connector with a different ingress configuration under the same tunnel ID. The AI companion uses its own named tunnel.

## Detailed phone acceptance

Use the project acceptance matrix/spec as the normative thresholds. For each run record a fresh run ID, commit, model hashes, config excluding credentials, phone/browser/OS, network, asset, timestamps and raw observed outcome.

1. Without authentication: UI redirects to login; API/SSE/WS/media/Range are inaccessible. Log in; re-test exact /voice prefix and MIME types. Expire auth during capture and playback: both must stop without interpreting HTML as JSON.
2. Cold launch: UI must not say ready before all models and DSH initialize. Start once: microphone permission and playback authorization behave explicitly. Denied microphone still allows typed input.
3. Five spoken turns: one capture ID, one DSH prompt, ordered video exactly once. Include a short final tail. Compare recognized text, final spoken text and actual video audio.
4. Long replies of 30/45/60 seconds: start playback before all generation finishes; measure every buffering gap and AV offset. Required gap/offset thresholds are in SPEC. Retain failed runs.
5. First/last: trigger tools and retries; only first and final readable messages are spoken. Test 100 characters without punctuation, duplicate ACK delivery, changed prefix, stale epoch and a restarted owned bridge.
6. Cancel at queued TTS, running TTS, running renderer, playback and generated-ready. New speech must stop old playback within the formal one-second barge-in requirement; no late old result may reappear.
7. Mic off during generation/playback leaves playback running. End stops both. Background/lock, permission revocation and ten-second network loss require a new user gesture; no automatic re-recording, re-submission or history replay.
8. Phone external speaker at 50%/80% volume: three 20-second silent-human trials per volume; zero self-triggered prompts. Headphones and desktop mobile viewport do not substitute.
9. Twenty ordinary warm real-API turns: measure last valid microphone speech sample to first *actually audible* video on the phone's own monotonic clock. Retain all cases; nearest-rank P95 is the nineteenth sorted latency and must be <=10 seconds. Tool-wait cases are functional tests, outside this latency cohort.
10. Keep task-system states as AI进行中 or 待验收 as appropriate. Only user acceptance closes tasks. Cloud functional PASS and phone/performance PASS are separate.

## Limits and provenance

Personal/non-commercial use: upstream Wav2Lip licensing restricts commercial use. The technical demo reuses an existing project avatar frame; it is not evidence of the user's final avatar/voice selection. Production never falls back to a static avatar, canned animation, audio-only output, hosted speech or GPU.
