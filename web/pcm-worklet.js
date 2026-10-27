// Captures microphone audio and posts 16 kHz 16-bit PCM chunks to the main thread.

class PCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Int16Array(1024); // 64 ms at 16 kHz
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0][0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this.chunk[this.filled++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;

      if (this.filled === this.chunk.length) {
        const out = this.chunk.slice(0);
        this.port.postMessage(out.buffer, [out.buffer]);
        this.filled = 0;
      }
    }

    return true;
  }
}

registerProcessor("pcm-recorder", PCMRecorder);
