"""Process-isolated, resident CPU models; worker calls never touch web state."""
import math
import os
import re
from pathlib import Path

_MODEL = None
_KIND = None
_CONFIG = None
_VADS = {}


def initialize(kind, config):
    global _MODEL, _KIND, _CONFIG
    _KIND, _CONFIG = kind, config
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    if kind == "avatar":
        from cpu_avatar import CpuAvatar
        _MODEL = CpuAvatar(config)
    elif kind == "tts":
        import torch
        from omnivoice import OmniVoice
        torch.set_num_threads(config.get("tts_threads", 8))
        torch.set_num_interop_threads(1)
        _MODEL = OmniVoice.from_pretrained(
            config["tts_model"], device_map="cpu", dtype=torch.float32,
            load_asr=False).float().eval()
        if config.get("tts_precision", "int8") == "int8":
            torch.backends.quantized.engine = "fbgemm"
            _MODEL = torch.ao.quantization.quantize_dynamic(
                _MODEL, {torch.nn.Linear}, dtype=torch.qint8, inplace=True)
    elif kind == "asr":
        import sherpa_onnx
        root = Path(config["asr_models"])
        _MODEL = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(root/"sensevoice-onnx/model.int8.onnx"),
            tokens=str(root/"sensevoice-onnx/tokens.txt"), provider="cpu",
            num_threads=config.get("asr_threads", 4), sample_rate=16000,
            feature_dim=80, language="auto", use_itn=True)
    else:
        raise ValueError("Unknown model worker")


def ready():
    if _MODEL is None:
        raise RuntimeError("Model not initialized")
    return {"kind": _KIND, "device": "CPU", "pid": os.getpid()}


def new_vad():
    import sherpa_onnx
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(Path(_CONFIG["asr_models"])/"silero/silero_vad.onnx")
    config.silero_vad.threshold = 0.5
    config.silero_vad.min_speech_duration = 0.25
    config.silero_vad.min_silence_duration = 0.5
    config.silero_vad.window_size = 512
    config.sample_rate, config.num_threads, config.provider = 16000, 1, "cpu"
    return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=40)


def pcm_array(pcm):
    import numpy as np
    if not pcm or len(pcm) % 2 or len(pcm) > 16000*2*30:
        raise ValueError("Expected PCM16 mono 16kHz, at most 30 seconds")
    return np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768


def transcribe(pcm):
    import numpy as np
    samples = pcm_array(pcm)
    vad = new_vad()
    for start in range(0, len(samples), 512):
        chunk = samples[start:start+512]
        vad.accept_waveform(np.pad(chunk, (0, 512-len(chunk))))
    vad.flush()
    if vad.empty():
        return {"text": "", "speech": False}
    stream = _MODEL.create_stream()
    stream.accept_waveform(16000, samples)
    _MODEL.decode_stream(stream)
    return {"text": re.sub(r"<\|[^|]*\|>", "", stream.result.text).strip(),
            "speech": True, "language": getattr(stream.result, "lang", "")}


def vad_frame(connection, pcm=None):
    if pcm is None:
        _VADS.pop(connection, None)
        return {}
    if len(pcm) != 1024:
        raise ValueError("VAD frame must be 512 PCM16 samples")
    if connection not in _VADS:
        if len(_VADS) >= 4:
            raise ValueError("VAD connection limit")
        _VADS[connection] = new_vad()
    vad = _VADS[connection]
    vad.accept_waveform(pcm_array(pcm))
    ended = not vad.empty()
    while not vad.empty():
        vad.pop()
    return {"speech": bool(vad.is_speech_detected()), "ended": ended}


def synthesize(text, output):
    import soundfile as sf
    from omnivoice import OmniVoiceGenerationConfig
    if not text.strip() or len(text) > 24:
        raise ValueError("Invalid immutable TTS segment")
    audio = _MODEL.generate(text=text, language="Chinese", instruct="female",
        normalize_text=False, generation_config=OmniVoiceGenerationConfig(
            num_step=_CONFIG.get("tts_steps", 16)))[0]
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".partial.wav")
    sf.write(partial, audio, _MODEL.sampling_rate, subtype="PCM_16")
    os.replace(partial, destination)
    duration = len(audio)/_MODEL.sampling_rate
    if not duration or not math.isfinite(duration):
        raise RuntimeError("TTS produced invalid duration")
    return {"path": str(destination), "duration": duration,
            "parts": math.ceil(duration/2)}


def render(audio, part, output):
    return _MODEL.render(audio, part, output)
