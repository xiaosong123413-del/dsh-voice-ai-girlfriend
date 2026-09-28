"""First readable message streams; only the committed final candidate follows it."""
class SpeechRewrite(ValueError):
    pass


class SpeechSelection:
    def __init__(self):
        self.first = None
        self.first_text = ""
        self.spoken = ""
        self.first_committed = False
        self.last = None
        self.closed = False

    def _drain(self, final=False):
        pending = self.first_text[len(self.spoken):]
        chunks = []
        while pending:
            boundary = next((i+1 for i, char in enumerate(pending[:24])
                             if char in "。！？!?\n"), None)
            size = boundary or (24 if len(pending) >= 24 else len(pending) if final else 0)
            if not size:
                break
            text, pending = pending[:size], pending[size:]
            self.spoken += text
            if text.strip():
                chunks.append((self.first, text))
        return chunks

    def delta(self, message, text):
        if self.closed or not text:
            return []
        if self.first is None:
            self.first = message
        if message != self.first or self.first_committed:
            return []
        self.first_text += text
        return self._drain()

    def retry(self, message, cumulative):
        if self.closed or self.first_committed:
            return []
        if len(cumulative) < len(self.spoken):
            if not self.spoken.startswith(cumulative):
                raise SpeechRewrite("Retry rewrote an already submitted prefix")
            return []
        if not cumulative.startswith(self.spoken):
            raise SpeechRewrite("Retry rewrote an already submitted prefix")
        self.first, self.first_text = message, cumulative
        return self._drain()

    def commit(self, message, text):
        if self.closed or not text.strip():
            return []
        if self.first is None:
            self.first, self.first_text = message, text
        if message == self.first:
            if not text.startswith(self.spoken):
                raise SpeechRewrite("Committed message rewrote submitted speech")
            if self.first_committed and text != self.first_text:
                raise SpeechRewrite("Committed message changed")
            self.first_text = text
            self.first_committed = True
            self.last = (message, text)
            return self._drain(final=True)
        self.last = (message, text)
        return []

    def finish(self):
        if self.closed:
            return []
        self.closed = True
        chunks = self._drain(final=True)
        if self.last and self.last[0] != self.first:
            message, text = self.last
            # Split final candidate without changing the first-message identity.
            tail = SpeechSelection()
            chunks += tail.delta(message, text)
            chunks += tail.commit(message, text)
        return chunks
