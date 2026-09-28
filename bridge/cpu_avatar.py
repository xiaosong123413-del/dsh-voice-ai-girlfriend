"""Resident original Wav2Lip/OpenVINO CPU renderer. Publishes only complete MP4."""
import functools
import math
import os
import subprocess
import sys
import time
from pathlib import Path


class CpuAvatar:
    def __init__(self, config):
        import cv2
        import numpy as np
        import openvino as ov
        import imageio_ffmpeg
        self.cv2, self.np = cv2, np
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        source = Path(config["wav2lip_source"])
        sys.path.insert(0, str(source))
        import audio
        self.audio = audio
        model_path = Path(config["avatar_ir"])
        if not model_path.is_file():
            if config.get("avatar_precision", "fp32") != "fp32":
                raise ValueError("Requested quantized avatar IR is missing; no automatic precision replacement")
            import torch
            from models import Wav2Lip
            torch.set_num_threads(config.get("avatar_threads", 4))
            model = Wav2Lip().eval()
            checkpoint = torch.load(config["wav2lip_weights"], map_location="cpu", weights_only=True)
            weights = {key.removeprefix("module."): value for key, value in checkpoint["state_dict"].items()}
            model.load_state_dict(weights)
            graph = ov.convert_model(model, example_input=(
                torch.zeros(1, 1, 80, 16), torch.zeros(1, 6, 96, 96)))
            model_path.parent.mkdir(parents=True, exist_ok=True)
            ov.save_model(graph, model_path, compress_to_fp16=False)
        self.core = ov.Core()
        self.batch = config.get("avatar_batch", 8)
        graph = self.core.read_model(str(model_path))
        quantized = any(op.get_type_name() == "FakeQuantize" for op in graph.get_ops())
        if config.get("avatar_precision") == "int8" and not quantized:
            raise ValueError("Configured INT8 avatar IR has no activation quantizers")
        graph.reshape({graph.inputs[0]: [self.batch, 1, 80, 16], graph.inputs[1]: [self.batch, 6, 96, 96]})
        frame = cv2.imread(config["avatar_image"])
        if frame is None:
            raise ValueError("Cannot read configured avatar image")
        height, width = frame.shape[:2]
        scale = 480 / min(height, width)
        self.size = (int(width * scale) // 2 * 2, int(height * scale) // 2 * 2)
        self.frame = cv2.resize(frame, self.size)
        if "avatar_box" in config:
            x1, y1, x2, y2 = config["avatar_box"]
        else:
            detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            boxes = detector.detectMultiScale(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 1.1, 5)
            if len(boxes) != 1:
                raise ValueError("Avatar must have one detected face or an explicit verified avatar_box")
            x, y, w, h = map(int, boxes[0])
            x1, y1, x2, y2 = x, y, x+w, min(height, y+h+10)
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError("Avatar box is outside image")
        self.box = tuple(round(v * scale) for v in (x1, y1, x2, y2))
        x1, y1, x2, y2 = self.box
        face = cv2.resize(self.frame[y1:y2, x1:x2], (96, 96))
        masked = face.copy()
        masked[48:] = 0
        self.face_input = np.concatenate((masked, face), axis=2).transpose(2, 0, 1)[None].astype("float32") / 255
        # The avatar is fixed for this resident worker. Bind it as a constant so
        # OpenVINO can precompute the face encoder without changing FP32 values.
        from openvino import opset13
        face_parameter = graph.inputs[1].get_node()
        fixed_face = opset13.constant(np.repeat(self.face_input, self.batch, axis=0))
        face_parameter.output(0).replace(fixed_face.output(0))
        graph.remove_parameter(face_parameter)
        graph.validate_nodes_and_infer_types()
        self.model = self.core.compile_model(graph, "CPU", {
            "INFERENCE_NUM_THREADS": config.get("avatar_threads", 4), "NUM_STREAMS": 1,
            "PERFORMANCE_HINT": "LATENCY", "INFERENCE_PRECISION_HINT": "f32"})
        self.metadata = {"backend": "original Wav2Lip/OpenVINO", "device": "CPU",
                         "fixed_face": True,
                         "size": self.size, "fps": 25, "face_box": self.box, "batch": self.batch,
                         "graph_precision": "int8" if quantized else "fp32"}

    @functools.lru_cache(maxsize=2)
    def audio_features(self, audio_path):
        import soundfile as sf
        waveform, rate = sf.read(audio_path, dtype="float32")
        if waveform.ndim != 1 or not len(waveform):
            raise ValueError("Expected nonempty mono TTS audio")
        mel = self.audio.melspectrogram(self.audio.load_wav(audio_path, 16000))
        if not self.np.isfinite(mel).all():
            raise ValueError("Invalid mel spectrogram")
        if mel.shape[1] < 16:
            mel = self.np.pad(mel, ((0, 0), (0, 16-mel.shape[1])), mode="edge")
        return waveform, rate, mel

    def render(self, audio_path, part_index, output_path):
        import soundfile as sf
        started = time.perf_counter()
        waveform, rate, mel = self.audio_features(audio_path)
        start = part_index * 2 * rate
        chunk = waveform[start:min(len(waveform), start+2*rate)]
        if not len(chunk):
            raise ValueError("Empty media tail")
        duration = len(chunk) / rate
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(".partial.mp4")
        audio_chunk = destination.with_suffix(".part.wav")
        sf.write(audio_chunk, chunk, rate, subtype="PCM_16")
        process = subprocess.Popen([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "bgr24", "-video_size",
            f"{self.size[0]}x{self.size[1]}", "-framerate", "25", "-i", "pipe:0",
            "-i", str(audio_chunk), "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
            "-t", str(duration), str(partial)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
        try:
            x1, y1, x2, y2 = self.box
            frame_count = math.ceil(duration * 25)
            for offset in range(0, frame_count, self.batch):
                count = min(self.batch, frame_count-offset)
                mel_frames = []
                for item in range(self.batch):
                    index = offset + min(item, count-1)
                    mel_start = min(int((part_index*2 + index/25)*80), mel.shape[1]-16)
                    mel_frames.append(mel[:, mel_start:mel_start+16])
                audio_input = self.np.asarray(mel_frames, dtype="float32")[:, None]
                prediction = self.model([audio_input])[self.model.output(0)]
                for item in range(count):
                    pixels = self.np.clip(prediction[item].transpose(1, 2, 0)*255, 0, 255).astype("uint8")
                    frame = self.frame.copy()
                    frame[y1:y2, x1:x2] = self.cv2.resize(pixels, (x2-x1, y2-y1))
                    process.stdin.write(frame.tobytes())
            process.stdin.close()
            process.stdin = None
            _, error = process.communicate(timeout=60)
            if process.returncode:
                raise RuntimeError("FFmpeg failed: " + error.decode(errors="replace")[-2000:])
            if not partial.is_file() or partial.stat().st_size < 1024:
                raise RuntimeError("Encoder produced no valid media")
            os.replace(partial, destination)
            return {"duration": duration, "path": str(destination),
                    "seconds": time.perf_counter()-started, "frames": math.ceil(duration*25)}
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.communicate()
            partial.unlink(missing_ok=True)
            raise
        finally:
            audio_chunk.unlink(missing_ok=True)
