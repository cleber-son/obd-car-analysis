/* window.obdVoice — fala em português no modo direção (docs/GPS-ARQUITETURA.md, seção 8.3).
   Mesma técnica do tests.js (que não muda): o iPhone só fala depois de um toque (unlock), descarta a fala se o objeto
   for coletado (guardamos as falas) e descarta speak() logo depois de cancel() (cancela e fala 150 ms depois).
   speak(texto, {key, cooldown_s, interrupt}): a mesma 'key' não fala de novo antes de cooldown_s (padrão: nunca mais
   nesta página). A escolha 🔊/🔇 fica neste aparelho (a mesma do Testes guiados). Limite conhecido: o Safari só fala
   com a página aberta e a tela acesa; o modo direção já mantém a tela ligada. */
(() => {
  "use strict";
  let on = true; try { on = localStorage.getItem("obdVoice") !== "0"; } catch { }
  let unlocked = false, ptVoice = null;
  const said = new Map();                 // key -> instante da última fala
  const talking = [];
  const plain = (t) => String(t ?? "").replace(/<[^>]+>/g, " ").replace(/[\u{1F300}-\u{1FAFF}☀-➿️]/gu, "").replace(/\s+/g, " ").trim();
  const ss = () => window.speechSynthesis;
  function pick() { const vs = ss()?.getVoices() || []; ptVoice = vs.find((v) => /pt[-_]BR/i.test(v.lang)) || vs.find((v) => /^pt/i.test(v.lang)) || null; }
  try { pick(); ss()?.addEventListener?.("voiceschanged", pick); } catch { }

  function unlock() {                     // chamar dentro de um toque
    if (unlocked || !ss()) return; unlocked = true;
    try { const u = new SpeechSynthesisUtterance("ok"); u.lang = "pt-BR"; u.volume = 0.01; talking.push(u); ss().speak(u); } catch { }
  }
  function speak(text, o = {}) {
    const t = plain(text), s = ss(); if (!on || !t || !s) return false;
    if (o.key != null) {
      const last = said.get(o.key);
      if (last != null && (o.cooldown_s == null || Date.now() - last < o.cooldown_s * 1000)) return false;
      said.set(o.key, Date.now());
    }
    try {
      if (!ptVoice) pick();
      const u = new SpeechSynthesisUtterance(t.slice(0, 260)); u.lang = "pt-BR"; u.rate = 1.05; u.volume = 1; if (ptVoice) u.voice = ptVoice;
      talking.push(u); if (talking.length > 12) talking.shift();
      u.onend = u.onerror = () => { const i = talking.indexOf(u); if (i >= 0) talking.splice(i, 1); };
      s.resume?.();
      if (o.interrupt && (s.speaking || s.pending)) { s.cancel(); setTimeout(() => { s.resume?.(); s.speak(u); }, 150); }
      else s.speak(u);
      return true;
    } catch { return false; }
  }
  function set(v) { on = !!v; try { localStorage.setItem("obdVoice", on ? "1" : "0"); } catch { } if (!on) { try { ss()?.cancel(); } catch { } } }
  const enabled = () => on && !!ss();
  const supported = () => !!ss();
  window.obdVoice = { unlock, speak, set, enabled, supported, plain };
})();
