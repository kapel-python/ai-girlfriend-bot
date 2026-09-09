/* Демо-конвейер: буфер → debounce → решение модели → симулятор набора → отправка.
   Тайминги набора считаются так же, как в app/conversation/typing_simulator.py:
   от длины текста, а не фиксированной паузой. */

(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var PERSONAS = [
    {
      key: "realistic",
      chip: "🎧 реалистичный",
      name: "она",
      mood: "ровная",
      temp: "warm",
      desc: "Универсальный fallback-характер. Никакого сленга и эмодзи ради «естественности»: «ага», «пон», «хз» — тоже полноценный ответ. Не задаёт вопрос после каждого сообщения и не вытягивает разговор искусственно.",
      dialog: [
        { who: "me", text: "слушай" },
        { who: "me", text: "ты вообще спишь когда-нибудь" },
        { who: "her", texts: ["сплю конечно", "просто не сейчас"] },
        { who: "me", text: "а чего не сейчас" },
        { who: "her", texts: ["да так"] }
      ]
    },
    {
      key: "mila",
      chip: "💬 милая и живая",
      name: "Мила",
      mood: "тёплая",
      temp: "warm",
      desc: "Мила, 23 года. Тёплая, остроумная, немного саркастичная. Любит подколоть без зла. У неё бывает лень, плохое настроение и свои дела — она не сидит и не ждёт сообщений.",
      dialog: [
        { who: "me", text: "привет" },
        { who: "me", text: "как день прошёл" },
        { who: "her", texts: ["привет)", "да как обычно, ничего интересного", "у тебя как?"] },
        { who: "me", text: "тоже никак, работал весь день" },
        { who: "her", texts: ["ну ты трудяга конечно", "хоть поел нормально?"] }
      ]
    },
    {
      key: "shy",
      chip: "🌸 скромная",
      name: "она",
      mood: "смущённая",
      temp: "warm",
      desc: "Отвечает коротко и осторожно, не перехватывает инициативу. Раскрывается медленно — и это видно по длине сообщений, а не по прямым признаниям.",
      dialog: [
        { who: "me", text: "ты сегодня какая-то тихая" },
        { who: "her", texts: ["нет всё нормально"] },
        { who: "me", text: "точно?" },
        { who: "her", texts: ["просто не знаю что писать", "ты не подумай ничего"] }
      ]
    },
    {
      key: "bold",
      chip: "😏 дерзкая",
      name: "она",
      mood: "на кураже",
      temp: "warm",
      desc: "Подкалывает первой, не боится спорить и не спешит соглашаться. Если шутка не смешная — не изображает восторг.",
      dialog: [
        { who: "me", text: "я вообще-то занят был" },
        { who: "her", texts: ["ага, конечно"] },
        { who: "me", text: "серьёзно" },
        { who: "her", texts: ["верю-верю", "два часа очень занят был, я поняла"] }
      ]
    },
    {
      key: "calm",
      chip: "🌙 спокойная",
      name: "она",
      mood: "сонная",
      temp: "cold",
      desc: "Размеренная и негромкая. Ночью в промт подставляется московское время — и отвечает она соответственно.",
      dialog: [
        { who: "me", text: "не спишь?" },
        { who: "her", texts: ["почти уже"] },
        { who: "me", text: "давай тогда завтра" },
        { who: "her", texts: ["давай", "спокойной ночи"] }
      ]
    },
    {
      key: "manipulator",
      chip: "🎭 манипулятор",
      name: "она",
      mood: "недовольная",
      temp: "cold",
      desc: "Пресет для тех, кто хочет сложного собеседника: обиды с подтекстом, «всё нормально» вместо ответа, холод после игнора. Именно здесь настроение из БД слышно сильнее всего.",
      dialog: [
        { who: "me", text: "прости, закрутился" },
        { who: "her", texts: ["ничего страшного"] },
        { who: "me", text: "ты обиделась?" },
        { who: "her", texts: ["нет", "с чего бы"] }
      ]
    },
    {
      key: "adult",
      chip: "🔞 18+",
      name: "она",
      mood: "в настроении",
      temp: "warm",
      desc: "Отдельный пресет со снятыми ограничениями тона. Включается только вручную в меню и хранится, как и остальные, отдельно от системного промта.",
      dialog: [
        { who: "me", text: "чем занята" },
        { who: "her", texts: ["лежу", "скучаю немного"] },
        { who: "me", text: "по мне?" },
        { who: "her", texts: ["не наглей", "но да"] }
      ]
    }
  ];

  var chat = document.getElementById("chat");
  var traceList = document.getElementById("trace-list");
  var statusEl = document.getElementById("chat-status");
  var nameEl = document.getElementById("chat-name");
  var moodEl = document.getElementById("chat-mood");
  var chipsBox = document.getElementById("chips");
  var chipDesc = document.getElementById("chip-desc");
  var replayBtn = document.getElementById("replay");

  var run = 0;

  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, reduced ? 0 : ms); });
  }

  function trim() {
    while (chat.children.length > 9) { chat.removeChild(chat.firstChild); }
  }

  function bubble(text, mine, cold) {
    var el = document.createElement("div");
    el.className = "bubble " + (mine ? "me" : "her" + (cold ? " cold" : ""));
    el.textContent = text;
    chat.appendChild(el);
    trim();
  }

  function showDots() {
    var el = document.createElement("div");
    el.className = "dots-bubble";
    el.id = "dots";
    el.innerHTML = "<i></i><i></i><i></i>";
    chat.appendChild(el);
    trim();
    return el;
  }

  function hideDots() {
    var d = document.getElementById("dots");
    if (d && d.parentNode) { d.parentNode.removeChild(d); }
  }

  function trace(html, hot) {
    var li = document.createElement("li");
    li.innerHTML = html;
    if (hot) { li.className = "hot"; }
    traceList.appendChild(li);
    while (traceList.children.length > 7) { traceList.removeChild(traceList.firstChild); }
  }

  function setStatus(text, typing) {
    statusEl.textContent = text;
    statusEl.className = typing ? "typing" : "";
  }

  /* время набора: как у человека — от объёма текста */
  function typingSeconds(texts) {
    var chars = texts.join(" ").length;
    return Math.min(4.2, 0.9 + chars / 13);
  }

  async function play(persona) {
    var me = ++run;
    chat.innerHTML = "";
    traceList.innerHTML = "";
    nameEl.textContent = persona.name;
    moodEl.textContent = persona.mood;
    moodEl.dataset.temp = persona.temp;
    setStatus("была недавно", false);

    var cold = persona.temp === "cold";
    var i = 0;

    while (i < persona.dialog.length) {
      if (run !== me) { return; }
      var turn = persona.dialog[i];

      if (turn.who === "me") {
        bubble(turn.text, true, false);
        trace("входящее → буфер");
        await sleep(700);
        i++;
        continue;
      }

      trace("debounce <b>2.0 с</b> — серия дочитана");
      await sleep(900);
      if (run !== me) { return; }

      setStatus("печатает…", true);
      var dots = showDots();
      trace("запрос к модели · json_object");
      await sleep(750);
      if (run !== me) { hideDots(); return; }

      trace("should_reply: <b>true</b> · mood: " + persona.mood);
      var secs = typingSeconds(turn.texts);
      trace("набор <b>" + secs.toFixed(1) + " с</b> · сообщений: " + turn.texts.length, true);
      await sleep(secs * 1000);
      if (run !== me) { hideDots(); return; }

      hideDots();
      void dots;
      for (var k = 0; k < turn.texts.length; k++) {
        bubble(turn.texts[k], false, cold);
        if (k < turn.texts.length - 1) { await sleep(620); }
        if (run !== me) { return; }
      }
      setStatus("была только что", false);
      trace("сохранено в историю · SQLite");
      await sleep(1400);
      i++;
    }

    if (run !== me) { return; }
    trace("факты в долгую память (раз в 3 обмена)");
  }

  /* ---- чипы характеров ---- */

  var current = 1; /* Мила — она же в первом кадре */

  function select(index) {
    var buttons = chipsBox.querySelectorAll(".chip");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute("aria-selected", i === index ? "true" : "false");
    }
    current = index;
    chipDesc.textContent = PERSONAS[index].desc;
    play(PERSONAS[index]);
  }

  PERSONAS.forEach(function (p, index) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", "false");
    b.textContent = p.chip;
    b.addEventListener("click", function () {
      select(index);
      document.getElementById("phone").scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
    });
    chipsBox.appendChild(b);
  });

  replayBtn.addEventListener("click", function () { play(PERSONAS[current]); });

  /* первый кадр не пустой: сообщение уже в чате до старта анимации */
  bubble(PERSONAS[current].dialog[0].text, true, false);
  chipsBox.querySelectorAll(".chip")[current].setAttribute("aria-selected", "true");
  chipDesc.textContent = PERSONAS[current].desc;
  moodEl.textContent = PERSONAS[current].mood;
  nameEl.textContent = PERSONAS[current].name;
  play(PERSONAS[current]);
})();
