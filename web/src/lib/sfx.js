// WebAudio blips — square/triangle only, because the kitchen should sound
// like an 8-bit kitchen and not like a synthesizer demo. Master mute lives in
// localStorage and defaults to OFF: no page gets to make noise at a human
// without being asked, and autoplay policy agrees with us.

let ctx = null;

export function isMuted() {
  const v = localStorage.getItem('cooker-sfx');
  return v !== 'on';  // default off
}

export function setMuted(m) {
  localStorage.setItem('cooker-sfx', m ? 'off' : 'on');
}

function audio() {
  if (!ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) ctx = new AC();
  }
  if (ctx && ctx.state === 'suspended') ctx.resume();
  return ctx;
}

export function unlock() {
  // Called on the first click, which is also the only moment the browser
  // will let us start making sound at all.
  const a = audio();
  return !!a;
}

function blip({ type = 'square', f0 = 440, f1 = f0, dur = 0.12, gain = 0.06, when = 0 }) {
  if (isMuted()) return;
  const a = audio();
  if (!a) return;
  const t0 = a.currentTime + when;
  const osc = a.createOscillator();
  const g = a.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(f0, t0);
  if (f1 !== f0) osc.frequency.exponentialRampToValueAtTime(Math.max(20, f1), t0 + dur);
  g.gain.setValueAtTime(gain, t0);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(g).connect(a.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

function noiseBurst({ dur = 0.25, gain = 0.03, freq = 900, q = 1.2, when = 0 }) {
  if (isMuted()) return;
  const a = audio();
  if (!a) return;
  const t0 = a.currentTime + when;
  const len = Math.max(1, Math.floor(a.sampleRate * dur));
  const buf = a.createBuffer(1, len, a.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / len);
  const src = a.createBufferSource();
  src.buffer = buf;
  const bp = a.createBiquadFilter();
  bp.type = 'bandpass';
  bp.frequency.value = freq;
  bp.Q.value = q;
  const g = a.createGain();
  g.gain.value = gain;
  src.connect(bp).connect(g).connect(a.destination);
  src.start(t0);
}

export function sfxFor(type) {
  switch (type) {
    case 'runner.stage': // a fresh stage hit the flame: klunk
      blip({ type: 'triangle', f0: 180, f1: 90, dur: 0.14, gain: 0.08 });
      noiseBurst({ dur: 0.08, freq: 500, gain: 0.02 });
      break;
    case 'runner.published':
    case 'chain.complete': // plated: little arpeggio
      blip({ type: 'square', f0: 523, dur: 0.09, gain: 0.05 });
      blip({ type: 'square', f0: 659, dur: 0.09, gain: 0.05, when: 0.09 });
      blip({ type: 'square', f0: 784, dur: 0.14, gain: 0.05, when: 0.18 });
      break;
    case 'runner.rejected': // swept: descending buzz
      blip({ type: 'sawtooth', f0: 220, f1: 110, dur: 0.3, gain: 0.05 });
      break;
    case 'llm.abort':
    case 'runner.cancelled':
    case 'task.paused': // preempted: someone else needs the stove
      blip({ type: 'sawtooth', f0: 330, f1: 140, dur: 0.22, gain: 0.06 });
      break;
    case 'digest.written': // morning paper: soft double ding
      blip({ type: 'triangle', f0: 880, dur: 0.12, gain: 0.05 });
      blip({ type: 'triangle', f0: 1175, dur: 0.18, gain: 0.05, when: 0.13 });
      break;
    default:
      break;
  }
}

// bubbling: called on a slow interval while anything is boiling
export function bubble() {
  noiseBurst({ dur: 0.09, freq: 300 + Math.random() * 500, q: 3, gain: 0.018 });
}
