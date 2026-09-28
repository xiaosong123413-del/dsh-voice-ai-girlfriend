import hashlib
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import cpu_workers as workers


class VoiceReferenceTests(unittest.TestCase):
    def make_audio(self, root, channels=1):
        path = Path(root)/"reference.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x01\x00"*16000*channels)
        return path

    def test_missing_transcript_never_calls_model_or_asr(self):
        model = Mock()
        with self.assertRaisesRegex(ValueError, "exact transcript"):
            workers.prepare_voice(model, {"voice_reference_audio": "unread.wav"})
        model.create_voice_clone_prompt.assert_not_called()

    def test_stereo_reference_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.make_audio(root, channels=2)
            model = Mock()
            with self.assertRaisesRegex(ValueError, "mono"):
                workers.prepare_voice(model, {
                    "voice_reference_audio": str(path), "voice_reference_text": "你好。"})
            model.create_voice_clone_prompt.assert_not_called()

    def test_same_encoded_prompt_used_for_distinct_segments(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as root:
            path = self.make_audio(root)
            prompt = object()
            model = Mock(sampling_rate=16000)
            model.create_voice_clone_prompt.return_value = prompt
            model.generate.return_value = [np.zeros(16000, dtype="float32")]
            config = {"voice_reference_audio": str(path), "voice_reference_text": "你好。"}
            encoded, digest = workers.prepare_voice(model, config)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            with patch.multiple(workers, _MODEL=model, _CONFIG=config, _VOICE_PROMPT=encoded), patch.dict(
                sys.modules, {"omnivoice": SimpleNamespace(OmniVoiceGenerationConfig=lambda **kw: kw)}
            ):
                workers.synthesize("第一段。", str(Path(root)/"first.wav"))
                workers.synthesize("第二段。", str(Path(root)/"second.wav"))
            model.create_voice_clone_prompt.assert_called_once_with(ref_audio=str(path), ref_text="你好。")
            self.assertEqual(model.generate.call_count, 2)
            for call in model.generate.call_args_list:
                self.assertIs(call.kwargs["voice_clone_prompt"], prompt)
                self.assertNotIn("instruct", call.kwargs)
            self.assertFalse((Path(root)/"first.partial.wav").exists())


if __name__ == "__main__":
    unittest.main()
