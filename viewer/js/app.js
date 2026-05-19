// Hash-router + shared utilities for the poker hand viewer.
// Routes:
//   #/sessions                       -> Sessions list
//   #/sessions/:sid                  -> Session detail (hand list)
//   #/sessions/:sid/hands/:hid       -> Hand detail (street columns)

(function () {
  const LOGS_ROOT = "../logs";
  const root = document.getElementById("app");

  const Cache = {
    index: null,        // {sessions: [...]}
    sessions: {},       // sid -> full session JSON (with hands)
  };

  // ── fetch helpers ─────────────────────────────────────────
  async function fetchJSON(path) {
    const res = await fetch(path, { cache: "no-cache" });
    if (!res.ok) throw new Error(`${res.status} ${path}`);
    return res.json();
  }

  async function loadIndex() {
    if (Cache.index) return Cache.index;
    try {
      Cache.index = await fetchJSON(`${LOGS_ROOT}/index.json`);
    } catch (e) {
      Cache.index = { sessions: [], _error: e.message };
    }
    return Cache.index;
  }

  async function loadSession(sid) {
    if (Cache.sessions[sid]) return Cache.sessions[sid];
    // Try sid.json first (writer naming); fall back to looking up file in index
    let file = `${sid}.json`;
    const idx = await loadIndex();
    const entry = (idx.sessions || []).find((s) => s.session_id === sid);
    if (entry && entry.file) file = entry.file;
    const data = await fetchJSON(`${LOGS_ROOT}/${file}`);
    Cache.sessions[sid] = data;
    return data;
  }

  // ── formatting ────────────────────────────────────────────
  const fmt = {
    int(n) { return (n == null) ? "" : Number(n).toLocaleString(); },
    signed(n) {
      if (n == null) return "";
      const v = Number(n);
      const sign = v > 0 ? "+" : v < 0 ? "−" : "";
      return `${sign}${Math.abs(v).toLocaleString()}`;
    },
    isoDate(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return iso.slice(0, 10);
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const dd = String(d.getDate()).padStart(2, "0");
      return `${y}-${m}-${dd}`;
    },
    isoTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      return `${hh}:${mm}`;
    },
    isoTimeSec(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    },
    duration(startIso, endIso) {
      if (!startIso || !endIso) return "";
      const s = new Date(startIso), e = new Date(endIso);
      if (isNaN(s) || isNaN(e)) return "";
      const ms = Math.max(0, e - s);
      const min = Math.round(ms / 60000);
      const h = Math.floor(min / 60);
      const m = min % 60;
      return h > 0 ? `${h}h ${m}m` : `${m}m`;
    },
    weekday(iso) {
      const d = new Date(iso);
      if (isNaN(d)) return "";
      return ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][d.getDay()];
    },
  };

  // ── card rendering ────────────────────────────────────────
  const suitChar = (s) => ({ h: "♥", d: "♦", s: "♠", c: "♣" }[s] || "?");
  const isRed = (s) => s === "h" || s === "d";

  function cardEl(code, size = "sm") {
    const el = document.createElement("span");
    el.className = `card ${size}`;
    if (!code || code === "?" || code === "??") {
      el.classList.add("back");
      el.textContent = "";
      return el;
    }
    const rank = code.slice(0, -1);
    const suit = code.slice(-1);
    if (isRed(suit)) el.classList.add("red");
    el.innerHTML = `<span>${rank}</span><span class="suit">${suitChar(suit)}</span>`;
    return el;
  }

  function emptyCardEl(size = "sm") {
    const el = document.createElement("span");
    el.className = `card ${size} empty`;
    return el;
  }

  function miniBoard(cards, size = "sm") {
    const wrap = document.createElement("span");
    wrap.className = "mini-board";
    for (let i = 0; i < 5; i++) {
      wrap.appendChild(cards[i] ? cardEl(cards[i], size) : emptyCardEl(size));
    }
    return wrap;
  }

  function holePair(cards, size = "sm") {
    const wrap = document.createElement("span");
    wrap.className = "hole-pair";
    const c0 = cards && cards[0];
    const c1 = cards && cards[1];
    wrap.appendChild(c0 ? cardEl(c0, size) : emptyCardEl(size));
    wrap.appendChild(c1 ? cardEl(c1, size) : emptyCardEl(size));
    return wrap;
  }

  // ── action pills / confidence ─────────────────────────────
  const actClass = (action) => {
    if (!action) return "act-check";
    const a = String(action).toLowerCase().replace(/_/g, "");
    if (a === "fold") return "act-fold";
    if (a === "check") return "act-check";
    if (a === "call") return "act-call";
    if (a === "bet") return "act-bet";
    if (a === "raise") return "act-raise";
    if (a === "allin" || a === "all-in") return "act-allin";
    if (a === "sbpost" || a === "bbpost") return "act-post";
    if (a === "showdown") return "act-show";
    if (a === "winner") return "act-win";
    return "act-check";
  };

  function actionPill(action, amount) {
    const span = document.createElement("span");
    span.className = `action-pill ${actClass(action)}`;
    let label = String(action || "").toLowerCase().replace(/_/g, " ");
    span.textContent = label;
    if (amount != null && amount > 0) {
      const amt = document.createElement("span");
      amt.className = "amt";
      amt.textContent = fmt.int(amount);
      span.appendChild(amt);
    }
    return span;
  }

  function confidenceDot(value) {
    const span = document.createElement("span");
    span.className = "confidence-dot " + (value >= 0.75 ? "conf-hi" : value >= 0.5 ? "conf-mid" : "conf-lo");
    span.title = `confidence ${(value == null ? "?" : value.toFixed(2))}`;
    return span;
  }

  // ── position derivation ───────────────────────────────────
  // The real HandSummary doesn't include position_map. Derive it from
  // SB_POST / BB_POST actions and a "seat ring" of active players.
  function derivePositions(hand) {
    const seats = (hand.players || []).map((p) => p.seat).sort((a, b) => a - b);
    if (!seats.length) return {};
    const actions = hand.actions || [];
    const sbAction = actions.find((a) => /sb.?post/i.test(a.action || ""));
    const bbAction = actions.find((a) => /bb.?post/i.test(a.action || ""));
    const sbSeat = sbAction ? sbAction.seat : null;
    const bbSeat = bbAction ? bbAction.seat : null;

    if (sbSeat == null || bbSeat == null) {
      // Fallback: no position info available; label by seat number.
      const out = {};
      seats.forEach((s) => (out[s] = `S${s}`));
      return out;
    }

    // BTN = seat just before SB in the active-seat ring.
    const ring = seats.slice();
    const sbIdx = ring.indexOf(sbSeat);
    const btnSeat = (sbSeat === bbSeat) ? sbSeat : ring[(sbIdx - 1 + ring.length) % ring.length];
    // headsup: BTN == SB
    const isHU = ring.length === 2;
    const map = {};
    if (isHU) {
      map[btnSeat] = "BTN/SB";
      map[bbSeat] = "BB";
      return map;
    }
    map[sbSeat] = "SB";
    map[bbSeat] = "BB";
    map[btnSeat] = "BTN";
    // walk from BB+1 onward: UTG, UTG+1, ..., HJ, CO
    const labels6 = ["UTG", "MP", "CO"];
    const labels7 = ["UTG", "UTG+1", "MP", "CO"];
    const labels8 = ["UTG", "UTG+1", "MP", "HJ", "CO"];
    const labels9 = ["UTG", "UTG+1", "UTG+2", "MP", "HJ", "CO"];
    const labels10 = ["UTG", "UTG+1", "UTG+2", "MP", "MP+1", "HJ", "CO"];
    const remaining = ring.length - 3;
    let labels = labels6;
    if (ring.length === 7) labels = labels7;
    else if (ring.length === 8) labels = labels8;
    else if (ring.length === 9) labels = labels9;
    else if (ring.length >= 10) labels = labels10;
    let idx = ring.indexOf(bbSeat);
    for (let i = 0; i < remaining; i++) {
      idx = (idx + 1) % ring.length;
      const seat = ring[idx];
      map[seat] = labels[i] || `S${seat}`;
    }
    return map;
  }

  // hero seat (configurable, persisted in localStorage)
  const HERO_KEY = "viewer.heroSeat";
  function getHeroSeat() {
    const v = localStorage.getItem(HERO_KEY);
    return v == null || v === "" ? null : Number(v);
  }
  function setHeroSeat(seat) {
    if (seat == null) localStorage.removeItem(HERO_KEY);
    else localStorage.setItem(HERO_KEY, String(seat));
  }

  // ── DOM helpers ───────────────────────────────────────────
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "onclick") node.addEventListener("click", v);
      else if (k === "dataset") {
        for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
      } else if (k.startsWith("on")) {
        node.addEventListener(k.slice(2), v);
      } else if (v === true) {
        node.setAttribute(k, "");
      } else if (v != null && v !== false) {
        node.setAttribute(k, v);
      }
    }
    for (const c of [].concat(children)) {
      if (c == null || c === false) continue;
      if (typeof c === "string" || typeof c === "number") {
        node.appendChild(document.createTextNode(String(c)));
      } else {
        node.appendChild(c);
      }
    }
    return node;
  }

  function clearRoot() {
    while (root.firstChild) root.removeChild(root.firstChild);
  }

  function showError(msg) {
    clearRoot();
    root.appendChild(el("div", { class: "error" }, msg));
  }

  function showLoading() {
    clearRoot();
    root.appendChild(el("div", { class: "loading" }, "Loading…"));
  }

  // ── session aggregate ─────────────────────────────────────
  // Derive session-level stats from a hand-summary list and an optional hero seat.
  function aggregateSession(session, heroSeat) {
    const hands = session.hands || [];
    let startedAt = "", endedAt = "";
    let reviews = 0, biggestPot = 0;
    let heroNet = 0, heroBuyin = null, heroCashout = null;
    let heroWon = 0;
    let vpipCount = 0, pfrCount = 0, heroHands = 0;
    let blinds = {};
    if (hands.length > 0) {
      startedAt = hands[0].started_at || "";
      endedAt = hands[hands.length - 1].ended_at || "";
      blinds = hands[0].blinds || {};
    }
    for (const h of hands) {
      if (h.review_required) reviews += (Array.isArray(h.actions) ? h.actions.filter(a => a.needs_review).length : 1);
      if ((h.pot_total || 0) > biggestPot) biggestPot = h.pot_total;
      if (heroSeat != null) {
        const heroPlayer = (h.players || []).find((p) => p.seat === heroSeat);
        if (heroPlayer) {
          heroHands += 1;
          const start = heroPlayer.stack_start ?? null;
          const end = heroPlayer.stack_end ?? null;
          if (heroBuyin == null && start != null) heroBuyin = start;
          if (end != null) heroCashout = end;
          if (heroPlayer.result != null) heroNet += heroPlayer.result;
          if (h.winner_seat === heroSeat) heroWon += 1;
          // VPIP / PFR via hero's actions in this hand
          const heroActs = (h.actions || []).filter((a) => a.seat === heroSeat);
          const vpipActs = heroActs.filter((a) => /^(call|bet|raise|all.?in)$/i.test(a.action || ""));
          const pfrActs = heroActs.filter((a) => a.street === "preflop" && /^(bet|raise)$/i.test(a.action || ""));
          if (vpipActs.length) vpipCount += 1;
          if (pfrActs.length) pfrCount += 1;
        }
      }
    }
    return {
      hands: hands.length,
      reviews,
      biggestPot,
      startedAt,
      endedAt,
      blinds,
      duration: fmt.duration(startedAt, endedAt),
      heroNet, heroBuyin, heroCashout, heroWon, heroHands,
      vpipPct: heroHands ? Math.round((vpipCount / heroHands) * 100) : null,
      pfrPct: heroHands ? Math.round((pfrCount / heroHands) * 100) : null,
    };
  }

  // ── router ────────────────────────────────────────────────
  function parseHash() {
    const h = location.hash || "#/sessions";
    const parts = h.replace(/^#\/?/, "").split("/").filter(Boolean);
    return parts;
  }

  async function render() {
    const parts = parseHash();
    if (parts.length === 0 || parts[0] === "sessions" && parts.length === 1) {
      await window.ViewerSessions.render(root, ctx);
    } else if (parts[0] === "sessions" && parts.length === 2) {
      await window.ViewerSession.render(root, ctx, parts[1]);
    } else if (parts[0] === "sessions" && parts.length === 4 && parts[2] === "hands") {
      await window.ViewerHand.render(root, ctx, parts[1], parts[3]);
    } else {
      showError(`unknown route: ${parts.join("/")}`);
    }
  }

  function navigate(path) {
    if (location.hash === `#${path}`) {
      render();
    } else {
      location.hash = `#${path}`;
    }
  }

  // ── public context exposed to screen modules ──────────────
  const ctx = {
    fmt, el, clearRoot, showError, showLoading,
    cardEl, emptyCardEl, miniBoard, holePair,
    actClass, actionPill, confidenceDot,
    derivePositions, getHeroSeat, setHeroSeat,
    loadIndex, loadSession, aggregateSession,
    navigate,
  };

  // keyboard shortcuts on hand detail
  window.addEventListener("keydown", (e) => {
    const parts = parseHash();
    if (e.key === "Escape") {
      if (parts[0] === "sessions" && parts.length >= 2) {
        navigate(parts.length === 2 ? "/sessions" : `/sessions/${parts[1]}`);
        e.preventDefault();
      }
    }
    if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && parts[0] === "sessions" && parts.length === 4) {
      window.ViewerHand.navAdjacent(parts[1], parts[3], e.key === "ArrowRight" ? 1 : -1);
      e.preventDefault();
    }
  });

  window.addEventListener("hashchange", () => render());

  window.Viewer = {
    start() {
      if (!location.hash) location.hash = "#/sessions";
      render().catch((e) => showError(String(e)));
    },
    navigate,
    ctx,
  };
})();
