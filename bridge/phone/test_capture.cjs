const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
for (const rate of [16000, 44100, 48000]) {
  test("PCM capture resamples " + rate + " Hz continuously", () => {
    let Processor;
    const output = [];
    const sandbox = {
      sampleRate: rate, Int16Array, Math,
      AudioWorkletProcessor: class { constructor() { this.port = { postMessage: b => output.push(new Int16Array(b)) }; } },
      registerProcessor: (_, value) => { Processor = value; }
    };
    vm.runInNewContext(fs.readFileSync(__dirname + "/capture-worklet.js", "utf8"), sandbox);
    const processor = new Processor();
    for (let pos = 0; pos < rate; pos += 128) {
      processor.process([[new Float32Array(Math.min(128, rate-pos)).fill(0.5)]]);
    }
    assert.equal(output.length, 31);
    assert.equal(output[0].length, 512);
    for (const frame of output) for (const value of frame) assert.equal(value, 16384);
  });
}
