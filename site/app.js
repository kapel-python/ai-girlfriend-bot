/* =====================================================================
   Aria — landing page interactions
   Modules: reveal, nav, scroll progress, counters, chat demo,
            personas, code tabs, copy, spotlight, misc
   ===================================================================== */
(function () {
  'use strict';

  var $  = function (sel, ctx) { return (ctx || document).querySelector(sel); };
  var $$ = function (sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); };

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- Reveal on scroll ---------- */
  function initReveal() {
    var items = $$('[data-reveal]');
    if (!items.length) return;

    if (reduceMotion || !('IntersectionObserver' in window)) {
      items.forEach(function (el) { el.classList.add('is-visible'); });
      return;
    }

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var el = entry.target;
        el.style.setProperty('--reveal-delay', (el.dataset.revealDelay || 0) + 'ms');
        el.classList.add('is-visible');
        io.unobserve(el);
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -8% 0px' });

    items.forEach(function (el) { io.observe(el); });
  }

  /* ---------- Nav: sticky, burger, active link ---------- */
  function initNav() {
    var nav = $('#nav');
    var burger = $('#navBurger');
    var links = $('#navLinks');
    if (!nav) return;

    var onScroll = function () {
      nav.classList.toggle('is-stuck', window.scrollY > 24);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });

    if (burger && links) {
      var close = function () {
        links.classList.remove('is-open');
        nav.classList.remove('is-menu-open');
        burger.setAttribute('aria-expanded', 'false');
      };
      burger.addEventListener('click', function () {
        var open = links.classList.toggle('is-open');
        nav.classList.toggle('is-menu-open', open);
        burger.setAttribute('aria-expanded', String(open));
      });
      links.addEventListener('click', function (e) {
        if (e.target.tagName === 'A') close();
      });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') close();
      });
      window.addEventListener('resize', function () {
        if (window.innerWidth > 1024) close();
      });
    }

    // Active section highlighting
    var anchors = $$('#navLinks a[href^="#"]');
    var sections = anchors
      .map(function (a) { return document.getElementById(a.getAttribute('href').slice(1)); })
      .filter(Boolean);

    if (sections.length && 'IntersectionObserver' in window) {
      var spy = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          anchors.forEach(function (a) {
            a.classList.toggle('is-active', a.getAttribute('href') === '#' + entry.target.id);
          });
        });
      }, { rootMargin: '-45% 0px -50% 0px' });
      sections.forEach(function (s) { spy.observe(s); });

      window.addEventListener('scroll', function () {
        if (window.scrollY < 200) {
          anchors.forEach(function (a) { a.classList.remove('is-active'); });
        }
      }, { passive: true });
    }
  }

  /* ---------- Scroll progress bar ---------- */
  function initProgress() {
    var bar = $('#scrollProgress');
    if (!bar) return;
    var ticking = false;

    var update = function () {
      var max = document.documentElement.scrollHeight - window.innerHeight;
      var ratio = max > 0 ? Math.min(window.scrollY / max, 1) : 0;
      bar.style.transform = 'scaleX(' + ratio + ')';
      ticking = false;
    };

    window.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(update);
    }, { passive: true });
    update();
  }

  /* ---------- Animated counters ---------- */
  function initCounters() {
    var nodes = $$('.count');
    if (!nodes.length) return;

    var run = function (el) {
      var target = parseFloat(el.dataset.count) || 0;
      var decimals = parseInt(el.dataset.decimals, 10) || 0;
      var format = function (v) {
        return v.toFixed(decimals).replace('.', ',');
      };
      if (reduceMotion) { el.textContent = format(target); return; }

      var duration = 1400;
      var start = null;
      var tick = function (ts) {
        if (start === null) start = ts;
        var p = Math.min((ts - start) / duration, 1);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = format(target * eased);
        if (p < 1) window.requestAnimationFrame(tick);
      };
      window.requestAnimationFrame(tick);
    };

    if (!('IntersectionObserver' in window)) { nodes.forEach(run); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        run(entry.target);
        io.unobserve(entry.target);
      });
    }, { threshold: 0.6 });
    nodes.forEach(function (el) { io.observe(el); });
  }

  /* ---------- Hero chat simulation ---------- */
  // Rendered instantly so the demo opens on an ongoing conversation, not a blank screen.
  var CHAT_HISTORY = [
    { side: 'in',  text: 'кстати я нашла тот сериал, про который ты говорил' },
    { side: 'out', text: 'и как?' },
    { side: 'in',  text: 'две серии подряд, не смогла оторваться' }
  ];

  var CHAT_SCRIPT = [
    { side: 'out', text: 'привет) чем занимаешься?' },
    { side: 'out', text: 'я только с работы' },
    { side: 'in',  text: 'оо привет', typing: 1500 },
    { side: 'in',  text: 'валяюсь, сериал фоном', typing: 1900 },
    { side: 'in',  text: 'а ты чего так поздно сегодня', typing: 2100 },
    { side: 'out', text: 'дедлайн, что поделать' },
    { side: 'in',  text: 'опять этот твой проект', typing: 1700 },
    { side: 'in',  text: 'ты хоть поел нормально?', typing: 1600 },
    { side: 'out', text: 'кофе считается?' },
    { side: 'in',  text: 'нет конечно', typing: 1200 },
    { side: 'in',  text: 'иди поешь, я подожду', typing: 1800 }
  ];

  var MOODS = [
    'спокойное, тёплое',
    'тёплое, игривое',
    'заинтересованное',
    'заботливое',
    'чуть ворчливое'
  ];

  function initHeroChat() {
    var chat = $('#chat');
    var status = $('#chatStatus');
    var moodBox = $('.floaty--1 span');
    var typingBox = $('.floaty--2 span');
    if (!chat) return;

    var MAX_BUBBLES = 7;

    var addBubble = function (side, text) {
      var el = document.createElement('div');
      el.className = 'bubble bubble--' + (side === 'in' ? 'in' : 'out');
      el.textContent = text;
      chat.appendChild(el);
      trim();
      return el;
    };

    var trim = function () {
      while (chat.children.length > MAX_BUBBLES) chat.removeChild(chat.firstChild);
    };

    var clearTyping = function () {
      $$('.bubble--typing', chat).forEach(function (el) {
        if (el.parentNode) el.parentNode.removeChild(el);
      });
    };

    var showTyping = function () {
      var el = document.createElement('div');
      el.className = 'bubble bubble--in bubble--typing';
      el.innerHTML = '<i></i><i></i><i></i>';
      chat.appendChild(el);
      trim();
      if (status) status.textContent = 'печатает…';
      return el;
    };

    var seed = function () {
      chat.innerHTML = '';
      CHAT_HISTORY.forEach(function (m) { addBubble(m.side, m.text); });
    };

    if (reduceMotion) {
      seed();
      CHAT_SCRIPT.slice(0, 4).forEach(function (m) { addBubble(m.side, m.text); });
      return;
    }

    var index = 0;
    var timer = null;
    var wait = function (ms, fn) { timer = window.setTimeout(fn, ms); };

    var next = function () {
      if (index >= CHAT_SCRIPT.length) {
        // restart the loop after a pause
        wait(3200, function () {
          seed();
          index = 0;
          if (status) status.textContent = 'в сети';
          next();
        });
        return;
      }

      var msg = CHAT_SCRIPT[index++];

      if (msg.side === 'out') {
        addBubble('out', msg.text);
        if (status) status.textContent = 'в сети';
        wait(900, next);
        return;
      }

      clearTyping();
      var dots = showTyping();
      if (typingBox) {
        typingBox.textContent = (msg.typing / 1000).toFixed(1) + ' с · ' + msg.text.length + ' симв.';
      }

      wait(msg.typing, function () {
        if (dots.parentNode) dots.parentNode.removeChild(dots);
        addBubble('in', msg.text);
        if (status) status.textContent = 'в сети';
        if (moodBox) moodBox.textContent = MOODS[index % MOODS.length];
        wait(1100, next);
      });
    };

    // Pause the demo when it is off-screen (saves cycles, feels intentional)
    seed();

    if ('IntersectionObserver' in window) {
      var running = false;
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            if (running) return;
            running = true;
            if (!chat.children.length) seed();
            clearTyping();
            if (status) status.textContent = 'в сети';
            next();
          } else if (running) {
            running = false;
            window.clearTimeout(timer);
            timer = null;
          }
        });
      }, { threshold: 0.2 });
      io.observe(chat);
    } else {
      next();
    }
  }

  /* ---------- Personas ---------- */
  var PERSONAS = [
    {
      id: 'realistic', tab: '🎧 реалистичная',
      title: 'Реалистичная',
      desc: 'Универсальный характер по умолчанию: живая, не идеальная, с собственными делами и планами на вечер. Именно её слышно в примере выше.',
      meters: [78, 45, 60],
      chat: [
        { side: 'out', text: 'как день прошёл?' },
        { side: 'in',  text: 'нормально, только устала жутко' },
        { side: 'in',  text: 'весь день на созвонах, голова гудит' },
        { side: 'out', text: 'сочувствую' },
        { side: 'in',  text: 'ладно, зато завтра пятница' }
      ]
    },
    {
      id: 'mila', tab: '💬 милая и живая',
      title: 'Милая и живая',
      desc: 'Тёплая, эмоциональная, легко вовлекается в разговор и сама подкидывает темы. Отвечает быстрее и охотнее остальных.',
      meters: [95, 30, 85],
      chat: [
        { side: 'out', text: 'как день прошёл?' },
        { side: 'in',  text: 'ооо ну наконец-то ты написал!' },
        { side: 'in',  text: 'день был странный, расскажу' },
        { side: 'out', text: 'давай' },
        { side: 'in',  text: 'но сначала ты) как сам?' }
      ]
    },
    {
      id: 'shy', tab: '🌸 скромная',
      title: 'Скромная',
      desc: 'Короткие ответы, осторожные формулировки, редко пишет первой. Раскрывается постепенно — и тем ценнее становится каждая её реплика.',
      meters: [70, 15, 25],
      chat: [
        { side: 'out', text: 'как день прошёл?' },
        { side: 'in',  text: 'нормально' },
        { side: 'in',  text: 'ничего особенного, правда' },
        { side: 'out', text: 'а если подробнее?' },
        { side: 'in',  text: 'ну… было немного грустно, но уже лучше' }
      ]
    },
    {
      id: 'sassy', tab: '😏 дерзкая',
      title: 'Дерзкая',
      desc: 'Ирония, подколы и полное отсутствие желания соглашаться со всем подряд. Если сказать глупость — услышите об этом сразу.',
      meters: [50, 95, 70],
      chat: [
        { side: 'out', text: 'как день прошёл?' },
        { side: 'in',  text: 'лучше, чем твой, судя по времени' },
        { side: 'out', text: 'обидно' },
        { side: 'in',  text: 'зато честно' },
        { side: 'in',  text: 'ладно, рассказывай, что там у тебя' }
      ]
    },
    {
      id: 'calm', tab: '🌙 спокойная',
      title: 'Спокойная',
      desc: 'Размеренный тон, длинные паузы, ощущение вечернего разговора. Никакой суеты — с ней легко молчать и легко думать вслух.',
      meters: [80, 20, 40],
      chat: [
        { side: 'out', text: 'как день прошёл?' },
        { side: 'in',  text: 'тихо. я почти весь вечер у окна просидела' },
        { side: 'out', text: 'звучит хорошо' },
        { side: 'in',  text: 'да, редкое ощущение' },
        { side: 'in',  text: 'расскажи, как у тебя. я никуда не спешу' }
      ]
    },
    {
      id: 'manipulator', tab: '🎭 манипулятор',
      title: 'Манипулятор',
      desc: 'Сложный вымышленный характер: обиды между строк, проверки, игра на внимании. Перед применением бот показывает предупреждение о эмоционально давящем стиле; 18+ и манипулятор остаются только вымышленными персонажами.',
      meters: [55, 80, 90],
      chat: [
        { side: 'out', text: 'как день прошёл?' },
        { side: 'in',  text: 'а тебе правда интересно?' },
        { side: 'out', text: 'ну конечно' },
        { side: 'in',  text: 'просто ты вчера тоже спрашивал и пропал' },
        { side: 'in',  text: 'ладно. день был так себе' }
      ]
    },
    {
      id: '18plus', tab: '🔞 18+',
      title: '18+',
      desc: 'Строго совершеннолетний вымышленный персонаж: смелый романтический флирт и чувственные намёки, но с явным подтверждением возраста и уважением к отказу собеседника.',
      meters: [90, 85, 70],
      chat: [
        { side: 'out', text: 'подтверди, что тебе есть 18' },
        { side: 'in',  text: 'есть, продолжай' },
        { side: 'in',  text: 'тогда я могу быть чуть смелее' },
        { side: 'out', text: 'и не забывай уважать мои границы' }
      ]
    },
    {
      id: 'custom', tab: '✍️ свой характер',
      title: 'Свой промт',
      desc: 'Поверх любого пресета можно дописать собственный слой: имя, биографию, привычки, манеру речи. Системная часть при этом остаётся защищённой.',
      meters: [60, 60, 60],
      chat: [
        { side: 'out', text: '/start → 🎭 настройки характера → свой характер' },
        { side: 'in',  text: 'её зовут так, как вы напишете' },
        { side: 'in',  text: 'у неё будет ваша биография и ваши привычки' },
        { side: 'in',  text: 'а формат ответа всё равно останется валидным' }
      ]
    }
  ];

  function initPersonas() {
    var tabsBox = $('#personaTabs');
    var chatBox = $('#personaChat');
    var titleEl = $('#personaTitle');
    var descEl  = $('#personaDesc');
    var meters  = [$('#mWarm'), $('#mSass'), $('#mInit')];
    if (!tabsBox || !chatBox) return;

    var timers = [];
    var clearTimers = function () {
      timers.forEach(window.clearTimeout);
      timers = [];
    };

    var render = function (persona) {
      clearTimers();
      chatBox.innerHTML = '';
      if (titleEl) titleEl.textContent = persona.title;
      if (descEl)  descEl.textContent = persona.desc;

      meters.forEach(function (m, i) {
        if (m) m.style.width = persona.meters[i] + '%';
      });

      persona.chat.forEach(function (msg, i) {
        var draw = function () {
          var el = document.createElement('div');
          el.className = 'bubble bubble--' + (msg.side === 'in' ? 'in' : 'out');
          el.textContent = msg.text;
          chatBox.appendChild(el);
        };
        if (reduceMotion) draw();
        else timers.push(window.setTimeout(draw, i * 320));
      });
    };

    PERSONAS.forEach(function (persona, i) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'persona-tab' + (i === 0 ? ' is-active' : '');
      btn.textContent = persona.tab;
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', i === 0 ? 'true' : 'false');
      btn.addEventListener('click', function () {
        $$('.persona-tab', tabsBox).forEach(function (b) {
          b.classList.remove('is-active');
          b.setAttribute('aria-selected', 'false');
        });
        btn.classList.add('is-active');
        btn.setAttribute('aria-selected', 'true');
        render(persona);
      });
      tabsBox.appendChild(btn);
    });

    // Render the first persona as soon as any part of the section approaches the
    // viewport, so the panel is never caught mid-scroll in its empty state.
    var first = PERSONAS[0];
    var section = document.getElementById('personas');
    if (section && 'IntersectionObserver' in window && !reduceMotion) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          render(first);
          io.disconnect();
        });
      }, { rootMargin: '0px 0px 25% 0px' });
      io.observe(section);
    } else {
      render(first);
    }
  }

  /* ---------- Code tabs ---------- */
  var SNIPPETS = {
    tree:
      '<span class="d">app/</span>\n' +
      '├── <span class="k">bot/</span>\n' +
      '│   ├── handlers/       <span class="d"># menu.py, chat.py — тонкие</span>\n' +
      '│   ├── keyboards/      <span class="d"># inline-меню</span>\n' +
      '│   ├── middlewares/    <span class="d"># логирование ошибок</span>\n' +
      '│   └── states/         <span class="d"># FSM aiogram</span>\n' +
      '├── <span class="k">ai/</span>\n' +
      '│   ├── client.py       <span class="d"># retry: 429 / 5xx / timeout</span>\n' +
      '│   ├── prompts.py      <span class="d"># SYSTEM / PERSONALITY / CUSTOM</span>\n' +
      '│   ├── models.py       <span class="d"># список моделей + кэш</span>\n' +
      '│   └── response_parser.py <span class="d"># устойчивый JSON-парсер</span>\n' +
      '├── <span class="k">conversation/</span>\n' +
      '│   ├── manager.py      <span class="d"># debounce, generation_id, scheduler</span>\n' +
      '│   ├── memory.py       <span class="d"># краткосрочная + долгосрочная</span>\n' +
      '│   ├── typing_simulator.py <span class="d"># модель набора текста</span>\n' +
      '│   └── sender.py       <span class="d"># typing keep-alive, нарезка</span>\n' +
      '├── <span class="k">database/</span>\n' +
      '│   ├── database.py     <span class="d"># SQLite (aiosqlite)</span>\n' +
      '│   ├── models.py       <span class="d"># dataclass-модели</span>\n' +
      '│   └── repository.py   <span class="d"># весь SQL здесь</span>\n' +
      '├── config.py           <span class="d"># конфигурация из .env</span>\n' +
      '├── logging_config.py   <span class="d"># логи без ключей</span>\n' +
      '└── main.py             <span class="d"># сборка и polling</span>',

    flow:
      '<span class="s">Telegram message</span>\n' +
      '      ↓\n' +
      '<span class="k">Handler</span> <span class="d">(тонкий)</span>\n' +
      '      ↓\n' +
      '<span class="k">ConversationManager</span> <span class="d">── буфер + generation_id</span>\n' +
      '      ↓\n' +
      '<span class="k">Debounce</span> <span class="d">── по умолчанию</span> <span class="n">2.0</span> <span class="d">сек</span>\n' +
      '      ↓\n' +
      '<span class="k">AI Service</span> <span class="d">── OpenAI-совместимый API</span>\n' +
      '      ↓\n' +
      '<span class="k">Response Decision</span> <span class="d">── JSON {should_reply, messages, mood}</span>\n' +
      '      ↓\n' +
      '<span class="k">Typing Simulator</span> <span class="d">── реалистичное время набора</span>\n' +
      '      ↓\n' +
      '<span class="k">Telegram Sender</span> <span class="d">── typing keep-alive, отправка</span>\n' +
      '      ↓\n' +
      '<span class="s">SQLite</span> <span class="d">── история, факты, настроение</span>\n\n' +
      'Proactive: <span class="k">DECISION</span> → <span class="k">TIMING</span> → 1 сообщение\n' +
      'Legacy morning/stage settings: <span class="d">deprecated</span>',

    env:
      '<span class="d"># --- Telegram ---</span>\n' +
      '<span class="k">BOT_TOKEN</span>=<span class="s">токен от @BotFather</span>\n\n' +
      '<span class="d"># --- модель ---</span>\n' +
      '<span class="k">AI_API_KEY</span>=<span class="s">ваш ключ</span>\n' +
      '<span class="k">AI_BASE_URL</span>=<span class="s">https://gptunnel.ru/v1</span>\n' +
      '<span class="k">DEFAULT_MODEL</span>=<span class="s">deepseek-v4-flash</span>\n\n' +
      '<span class="d"># --- поведение ---</span>\n' +
      '<span class="k">MESSAGE_DEBOUNCE</span>=<span class="n">2.0</span>      <span class="d"># пауза перед ответом, сек</span>\n' +
      '<span class="k">TYPING_SIMULATION</span>=<span class="n">true</span>     <span class="d"># человеческая скорость набора</span>\n' +
      '<span class="k">SHORT_MEMORY_LIMIT</span>=<span class="n">100</span>    <span class="d"># сообщений в контексте</span>\n\n' +
      '<span class="k">PROACTIVE_ENABLED</span>=<span class="n">true</span>     <span class="d"># DECISION → TIMING</span>\n' +
      '<span class="k">PROACTIVE_MIN_DELAY_MINUTES</span>=<span class="n">3</span>\n' +
      '<span class="k">PROACTIVE_MAX_DELAY_MINUTES</span>=<span class="n">720</span>\n' +
      '<span class="k">PROACTIVE_MAX_MESSAGES</span>=<span class="n">4</span>\n' +
      '<span class="k">PROACTIVE_COOLDOWN_MINUTES</span>=<span class="n">20</span>\n\n' +
      '<span class="d"># morning/stage settings — deprecated</span>\n\n' +
      '<span class="d"># --- хранилище ---</span>\n' +
      '<span class="k">DATABASE_PATH</span>=<span class="s">bot.db</span>'
  };

  function initCodeTabs() {
    var body = $('#codeBody');
    var tabs = $$('.code__tab');
    if (!body || !tabs.length) return;

    var show = function (key) {
      var code = body.querySelector('code') || body;
      code.innerHTML = SNIPPETS[key] || '';
      body.style.animation = 'none';
      // force reflow so the fade-in animation replays
      void body.offsetWidth;
      body.style.animation = '';
    };

    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) {
          t.classList.remove('is-active');
          t.setAttribute('aria-selected', 'false');
        });
        tab.classList.add('is-active');
        tab.setAttribute('aria-selected', 'true');
        show(tab.dataset.code);
      });
    });

    show('tree');
  }

  /* ---------- Copy to clipboard ---------- */
  function initCopy() {
    var btn = $('#copyBtn');
    var label = $('#copyLabel');
    if (!btn) return;

    var fallback = function (text) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (e) { /* ignore */ }
      document.body.removeChild(ta);
    };

    var done = function () {
      btn.classList.add('is-done');
      if (label) label.textContent = 'Скопировано';
      window.setTimeout(function () {
        btn.classList.remove('is-done');
        if (label) label.textContent = 'Копировать';
      }, 2000);
    };

    btn.addEventListener('click', function () {
      var text = btn.dataset.copy || '';
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () { fallback(text); done(); });
      } else {
        fallback(text);
        done();
      }
    });
  }

  /* ---------- Card spotlight (pointer-follow highlight) ---------- */
  function initSpotlight() {
    if (reduceMotion) return;
    var zones = $$('[data-spotlight]');
    zones.forEach(function (zone) {
      zone.addEventListener('pointermove', function (e) {
        var card = e.target.closest ? e.target.closest('.card') : null;
        if (!card) return;
        var r = card.getBoundingClientRect();
        card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
        card.style.setProperty('--my', (e.clientY - r.top) + 'px');
      });
    });
  }

  /* ---------- FAQ: single item open at a time ---------- */
  function initFaq() {
    var items = $$('#faqList .faq__item');
    items.forEach(function (item) {
      item.addEventListener('toggle', function () {
        if (!item.open) return;
        items.forEach(function (other) {
          if (other !== item) other.open = false;
        });
      });
    });
  }

  /* ---------- Misc ---------- */
  function initMisc() {
    var year = $('#year');
    if (year) year.textContent = String(new Date().getFullYear());
  }

  /* ---------- Boot ---------- */
  function boot() {
    initReveal();
    initNav();
    initProgress();
    initCounters();
    initHeroChat();
    initPersonas();
    initCodeTabs();
    initCopy();
    initSpotlight();
    initFaq();
    initMisc();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
