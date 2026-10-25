// Снимает сырой звук с микрофона и отдаёт его в основной поток кусочками по 128 сэмплов.
class AikoTap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}

registerProcessor("aiko-tap", AikoTap);
