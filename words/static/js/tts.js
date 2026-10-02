// ===== TTS 发音引擎（全局单例）=====
// 单词/短语：优先有道真人发音，失败回退系统合成（慢速清晰）
// 例句：系统合成，优先神经语音，按标点分句播放并加入自然停顿
(function () {
  var synth = window.speechSynthesis;
  var voices = [];

  // 用户发音偏好（与设置页同步写入 localStorage），默认美音
  function getVoiceType() {
    return localStorage.getItem('vocab-voice') === 'uk' ? 'uk' : 'us';
  }
  // 用户自定语速（例句使用），默认 1.0
  function getUserRate() {
    var r = parseFloat(localStorage.getItem('vocab-rate'));
    return (!isNaN(r) && r > 0) ? r : 1.0;
  }

  function enVoices() {
    return voices.filter(function (v) {
      return v.lang && v.lang.toLowerCase().indexOf('en') === 0;
    });
  }

  // 按语言前缀选最优语音：优先神经/在线语音（Natural、Online 等）
  function pickVoice(prefix) {
    var pool = voices.filter(function (v) {
      return v.lang && v.lang.toLowerCase().indexOf(prefix) === 0;
    });
    if (!pool.length) pool = enVoices();
    if (!pool.length) return null;
    var neuralRe = /natural|online|neural|jenny|aria|guy|sonia|ryan|davis|sara|michelle|google/i;
    for (var i = 0; i < pool.length; i++) {
      if (neuralRe.test(pool[i].name)) return pool[i];
    }
    return pool[0];
  }

  function loadVoices() {
    if (synth) voices = synth.getVoices() || [];
  }
  if (synth) {
    loadVoices();
    synth.addEventListener('voiceschanged', loadVoices);
    setTimeout(loadVoices, 300);
    setTimeout(loadVoices, 1200); // 部分内核语音列表加载较晚
  }

  // ----- 系统合成：分句队列播放，句间停顿 -----
  function splitSentences(text) {
    var matches = text.match(/[^.!?。！？,;，；\n]+[.!?。！？,;，；]?/g) || [text];
    return matches.map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function speakSystem(text, opts) {
    if (!synth) return;
    opts = opts || {};
    synth.cancel();
    var type = getVoiceType();
    var v = pickVoice(type === 'uk' ? 'en-gb' : 'en-us');
    var parts = splitSentences(text);
    var baseRate = opts.rate || getUserRate();
    var pauseMs = opts.pauseMs != null ? opts.pauseMs : 180;
    var idx = 0;

    function playNext() {
      if (idx >= parts.length) return;
      var seg = parts[idx++];
      // 逗号等短停顿处多停一点：通过延迟下一句实现，这里先排队当前句
      var u = new SpeechSynthesisUtterance(seg);
      u.lang = type === 'uk' ? 'en-GB' : 'en-US';
      // 片段结尾是逗号类短停顿时语速不变，停顿由下一句前延迟控制
      var isCommaPause = /[,;，；]$/.test(seg);
      u.rate = baseRate;
      u.pitch = 1;
      if (v) u.voice = v;
      u.onend = function () {
        setTimeout(playNext, isCommaPause ? pauseMs : Math.round(pauseMs * 1.6));
      };
      u.onerror = function () { setTimeout(playNext, pauseMs); };
      synth.speak(u);
    }
    playNext()
  }

  // ----- 有道真人发音 -----
  var currentAudio = null;
  function speakHuman(text) {
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    if (synth) synth.cancel();
    var type = getVoiceType() === 'uk' ? 2 : 1; // 1=美音 2=英音
    var url = 'https://dict.youdao.com/dictvoice?audio=' +
              encodeURIComponent(text) + '&type=' + type;
    var audio = new Audio(url);
    currentAudio = audio;
    var fallbackUsed = false;
    function fallback() {
      if (fallbackUsed) return;
      fallbackUsed = true;
      // 拿不到真人发音（无网络/无该词条）：系统合成慢速朗读
      speakSystem(text, { rate: 0.85, pauseMs: 0 });
    }
    audio.addEventListener('error', fallback);
    audio.play().catch(fallback);
  }

  function isWordOrPhrase(t) {
    if (t.length > 40) return false;
    // 不含句末标点，且不超过 3 个词 → 视为单词/短语
    if (/[.!?。！？]/.test(t)) return false;
    return t.split(/\s+/).length <= 3;
  }

  window.VOCAB_SPEAK = function (text) {
    if (!text) return;
    var t = String(text).trim();
    if (!t) return;
    if (isWordOrPhrase(t)) {
      speakHuman(t);
    } else {
      speakSystem(t); // 例句：自定语速 + 分句停顿
    }
  };
})();
