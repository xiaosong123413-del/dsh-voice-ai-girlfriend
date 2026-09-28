import unittest
from speech_selection import SpeechSelection, SpeechRewrite

class SpeechSelectionTests(unittest.TestCase):
    def test_no_punctuation_and_unicode_are_bounded(self):
        choice = SpeechSelection()
        chunks = choice.delta("first", "🙂"*100)
        chunks += choice.commit("first", "🙂"*100)
        chunks += choice.finish()
        self.assertEqual("".join(x[1] for x in chunks), "🙂"*100)
        self.assertTrue(all(len(x[1]) <= 24 for x in chunks))
    def test_only_first_and_final_message(self):
        choice = SpeechSelection()
        chunks = choice.delta("first", "你好。")
        chunks += choice.commit("first", "你好。")
        chunks += choice.commit("middle", "正在查找工具。")
        chunks += choice.commit("last", "最终答案。")
        chunks += choice.finish()
        self.assertEqual("".join(x[1] for x in chunks), "你好。最终答案。")
    def test_same_first_last_flushes_tail_once(self):
        choice = SpeechSelection()
        chunks = choice.delta("first", "你好")
        chunks += choice.commit("first", "你好")
        chunks += choice.commit("first", "你好")
        chunks += choice.finish()
        chunks += choice.finish()
        self.assertEqual(chunks, [("first", "你好")])
    def test_rewritten_spoken_prefix_fails(self):
        choice = SpeechSelection()
        choice.delta("first", "不能撤回的话。")
        with self.assertRaises(SpeechRewrite):
            choice.commit("first", "修改后的内容。")
    def test_empty_reasoning_message_is_not_first(self):
        choice = SpeechSelection()
        choice.commit("reasoning", "")
        self.assertEqual(choice.delta("answer", "答案。"), [("answer", "答案。")])
    def test_retry_same_prefix_does_not_repeat(self):
        choice = SpeechSelection()
        out = choice.delta("attempt1", "回答。")
        out += choice.retry("attempt2", "回答。后半句")
        out += choice.commit("attempt2", "回答。后半句")
        out += choice.finish()
        self.assertEqual("".join(x[1] for x in out), "回答。后半句")

if __name__ == "__main__": unittest.main()
