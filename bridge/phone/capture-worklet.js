class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.sum = 0; this.weight = 0; this.phase = 0;
    this.frame = new Int16Array(512); this.count = 0;
  }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    const ratio = sampleRate / 16000;
    for (const sample of channel) {
      let remaining = 1;
      while (remaining > 1e-8) {
        const take = Math.min(remaining, ratio - this.phase);
        this.sum += sample * take; this.weight += take; this.phase += take; remaining -= take;
        if (this.phase >= ratio - 1e-8) {
          this.frame[this.count++] = Math.round(Math.max(-1, Math.min(1, this.sum / this.weight)) * 32767);
          this.sum = 0; this.weight = 0; this.phase = 0;
          if (this.count === 512) {
            this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
            this.frame = new Int16Array(512); this.count = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);
