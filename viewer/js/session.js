// Screen 01: Session detail — hand list
(function () {
  const State = {
    resultFilter: "all",   // all | won | lost
    streetFilter: "any",   // any | preflop | flop+ | turn+ | river
    reviewOnly: false,
    sortKey: "id",         // id | pot | result
    sortDir: 1,            // default chronological asc
  };

  function streetReachedRank(street) {
    return ({ preflop: 1, flop: 2, turn: 3, river: 4, showdown: 4 })[street] || 0;
  }

  function lastStreet(hand) {
    let last = 0;
    for (const a of hand.actions || []) {
      const r = streetReachedRank(a.street);
      if (r > last) last = r;
    }
    return ["preflop","flop","turn","river"][last - 1] || "preflop";
  }

  function streetsBar(lastName) {
    const want = streetReachedRank(lastName);
    const bar = document.createElement("span");
    bar.className = "streets-bar";
    for (let i = 1; i <= 4; i++) {
      const t = document.createElement("span");
      t.className = "tick" + (i <= want ? " on" : "");
      bar.appendChild(t);
    }
    return bar;
  }

  async function render(root, ctx, sid) {
    ctx.showLoading();
    let session;
    try {
      session = await ctx.loadSession(sid);
    } catch (e) {
      ctx.showError(`セッション読み込み失敗: ${e.message || e}`);
      return;
    }
    ctx.clearRoot();

    const hands = session.hands || [];
    const heroSeat = ctx.getHeroSeat();
    const agg = ctx.aggregateSession(session, heroSeat);

    // Breadcrumb
    root.appendChild(ctx.el("div", { class: "breadcrumb" }, [
      ctx.el("button", {
        class: "btn btn-ghost",
        onclick: () => ctx.navigate("/sessions"),
      }, "◀ all sessions"),
      ctx.el("span", { class: "muted mono" }, sid),
    ]));

    // Header
    root.appendChild(ctx.el("div", { class: "screen-header" }, [
      ctx.el("span", { class: "eyebrow" }, "SESSION"),
      ctx.el("h1", {}, ctx.fmt.isoDate(agg.startedAt) || sid),
      ctx.el("div", { class: "subtitle" },
        `${ctx.fmt.isoTime(agg.startedAt)} → ${ctx.fmt.isoTime(agg.endedAt)} (${agg.duration || "—"})  ·  ` +
        `${hands.length} hands  ·  SB/BB ${agg.blinds.sb ?? "—"}/${agg.blinds.bb ?? "—"}`
      ),
    ]));

    // KPI strip
    const kpi = ctx.el("div", { class: "kpi-row" }, []);
    if (heroSeat != null && agg.heroHands > 0) {
      const netClass = agg.heroNet >= 0 ? "amount-pos" : "amount-neg";
      kpi.appendChild(ctx.el("div", { class: "kpi" }, [
        ctx.el("div", { class: "label" }, `YOUR RESULT · SEAT ${heroSeat}`),
        ctx.el("div", { class: `value ${netClass}` }, ctx.fmt.signed(agg.heroNet)),
        ctx.el("div", { class: "sub" }, `buyin ${ctx.fmt.int(agg.heroBuyin)} → ${ctx.fmt.int(agg.heroCashout)}`),
      ]));
    } else {
      kpi.appendChild(ctx.el("div", { class: "kpi" }, [
        ctx.el("div", { class: "label" }, "HERO SEAT"),
        ctx.el("div", { class: "value muted", style: "font-size: 14px;" }, "未設定"),
        ctx.el("div", { class: "sub" }, "下の hero seat ピッカーで指定すると net/VPIP/PFR が出ます"),
      ]));
    }
    kpi.appendChild(ctx.el("div", { class: "kpi" }, [
      ctx.el("div", { class: "label" }, "HANDS"),
      ctx.el("div", { class: "value" }, ctx.fmt.int(hands.length)),
      ctx.el("div", { class: "sub" },
        heroSeat != null
          ? `VPIP ${agg.vpipPct ?? "—"}% · PFR ${agg.pfrPct ?? "—"}%`
          : `${agg.duration || "—"}`
      ),
    ]));
    kpi.appendChild(ctx.el("div", { class: "kpi" }, [
      ctx.el("div", { class: "label" }, "BIGGEST POT"),
      ctx.el("div", { class: "value" }, ctx.fmt.int(agg.biggestPot)),
      ctx.el("div", { class: "sub" }, "max pot_total"),
    ]));
    kpi.appendChild(ctx.el("div", { class: agg.reviews > 0 ? "kpi accent" : "kpi" }, [
      ctx.el("div", { class: "label" }, "NEEDS REVIEW"),
      ctx.el("div", { class: "value" }, ctx.fmt.int(agg.reviews)),
      ctx.el("div", { class: "sub" }, "flagged actions"),
    ]));
    root.appendChild(kpi);

    // Hero seat picker
    const heroBar = ctx.el("div", { class: "filter-bar" }, [
      ctx.el("span", { class: "group-label" }, "HERO SEAT"),
    ]);
    const seats = Array.from(new Set(
      hands.flatMap((h) => (h.players || []).map((p) => p.seat))
    )).sort((a, b) => a - b);
    const allChip = ctx.el("span", {
      class: "chip" + (heroSeat == null ? " active" : ""),
      onclick: () => { ctx.setHeroSeat(null); render(root, ctx, sid); },
    }, "なし");
    heroBar.appendChild(allChip);
    for (const s of seats) {
      heroBar.appendChild(ctx.el("span", {
        class: "chip" + (heroSeat === s ? " active" : ""),
        onclick: () => { ctx.setHeroSeat(s); render(root, ctx, sid); },
      }, `seat ${s}`));
    }
    root.appendChild(heroBar);

    // Filter bar
    const fb = ctx.el("div", { class: "filter-bar" }, []);
    const fChip = (label, key, group, value) => ctx.el("span", {
      class: "chip" + (State[group] === value || State[group] === true && value === true ? " active" : ""),
      onclick: () => { State[group] = value; render(root, ctx, sid); },
    }, label);

    if (heroSeat != null) {
      const fg = ctx.el("div", { class: "filter-group" }, [
        ctx.el("span", { class: "group-label" }, "RESULT"),
        fChip("all", null, "resultFilter", "all"),
        fChip("won", null, "resultFilter", "won"),
        fChip("lost", null, "resultFilter", "lost"),
      ]);
      fb.appendChild(fg);
    }
    const streetGroup = ctx.el("div", { class: "filter-group" }, [
      ctx.el("span", { class: "group-label" }, "STREET"),
      fChip("any", null, "streetFilter", "any"),
      fChip("flop+", null, "streetFilter", "flop+"),
      fChip("turn+", null, "streetFilter", "turn+"),
      fChip("river", null, "streetFilter", "river"),
    ]);
    fb.appendChild(streetGroup);

    fb.appendChild(ctx.el("span", {
      class: "chip review" + (State.reviewOnly ? " active" : ""),
      onclick: () => { State.reviewOnly = !State.reviewOnly; render(root, ctx, sid); },
    }, "⚑ review only"));

    // sort
    const sortGroup = ctx.el("div", { class: "filter-group", style: "margin-left: auto;" }, [
      ctx.el("span", { class: "group-label" }, "SORT"),
    ]);
    const sortChip = (label, key) => {
      const active = State.sortKey === key;
      return ctx.el("span", {
        class: "chip" + (active ? " active" : ""),
        onclick: () => {
          if (State.sortKey === key) State.sortDir = -State.sortDir;
          else { State.sortKey = key; State.sortDir = key === "id" ? 1 : -1; }
          render(root, ctx, sid);
        },
      }, `${label}${active ? (State.sortDir === -1 ? " ↓" : " ↑") : ""}`);
    };
    sortGroup.appendChild(sortChip("hand #", "id"));
    sortGroup.appendChild(sortChip("pot", "pot"));
    if (heroSeat != null) sortGroup.appendChild(sortChip("|result|", "result"));
    fb.appendChild(sortGroup);
    root.appendChild(fb);

    // Filter + sort hands
    let rows = hands.slice();
    rows = rows.filter((h) => {
      // street filter
      const ls = lastStreet(h);
      const rank = streetReachedRank(ls);
      if (State.streetFilter === "flop+" && rank < 2) return false;
      if (State.streetFilter === "turn+" && rank < 3) return false;
      if (State.streetFilter === "river" && rank < 4) return false;
      if (State.reviewOnly && !h.review_required) return false;
      if (heroSeat != null && State.resultFilter && State.resultFilter !== "all") {
        const heroPlayer = (h.players || []).find((p) => p.seat === heroSeat);
        const result = heroPlayer ? (heroPlayer.result || 0) : 0;
        if (State.resultFilter === "won" && result <= 0) return false;
        if (State.resultFilter === "lost" && result >= 0) return false;
      }
      return true;
    });
    rows.sort((a, b) => {
      const key = State.sortKey;
      const dir = State.sortDir;
      let av, bv;
      if (key === "id") { av = a.hand_id; bv = b.hand_id; }
      else if (key === "pot") { av = a.pot_total || 0; bv = b.pot_total || 0; }
      else if (key === "result" && heroSeat != null) {
        const ah = (a.players || []).find((p) => p.seat === heroSeat);
        const bh = (b.players || []).find((p) => p.seat === heroSeat);
        av = Math.abs(ah ? (ah.result || 0) : 0);
        bv = Math.abs(bh ? (bh.result || 0) : 0);
      }
      if (av < bv) return -dir;
      if (av > bv) return dir;
      return 0;
    });

    if (!rows.length) {
      root.appendChild(ctx.el("div", { class: "empty" }, "条件に一致するハンドがありません"));
      return;
    }

    // Hand table
    const table = ctx.el("table", { class: "list-table" }, [
      ctx.el("thead", {}, ctx.el("tr", {}, [
        ctx.el("th", { class: "col-num" }, "#"),
        ctx.el("th", {}, "time"),
        ctx.el("th", {}, "hero pos"),
        ctx.el("th", {}, "hero cards"),
        ctx.el("th", {}, "streets · board"),
        ctx.el("th", {}, "winner"),
        ctx.el("th", { class: "col-num" }, "pot"),
        ctx.el("th", { class: "col-num" }, "result"),
        ctx.el("th", { class: "col-chev" }, ""),
      ])),
    ]);
    const tbody = ctx.el("tbody", {});

    for (const h of rows) {
      const positions = ctx.derivePositions(h);
      const heroPlayer = heroSeat != null ? (h.players || []).find((p) => p.seat === heroSeat) : null;
      const heroResult = heroPlayer ? (heroPlayer.result || 0) : null;
      const winner = (h.players || []).find((p) => p.seat === h.winner_seat);
      const ls = lastStreet(h);
      const trCls = [];
      if (h.review_required) trCls.push("has-review");
      const tr = ctx.el("tr", {
        class: trCls.join(" "),
        onclick: () => ctx.navigate(`/sessions/${sid}/hands/${h.hand_id}`),
      }, [
        ctx.el("td", { class: "col-num" }, `#${h.hand_id}`),
        ctx.el("td", { class: "mono" }, ctx.fmt.isoTimeSec(h.started_at)),
        ctx.el("td", {},
          heroPlayer
            ? ctx.el("span", { class: "pos-pill" }, positions[heroSeat] || `S${heroSeat}`)
            : ctx.el("span", { class: "muted" }, "—")
        ),
        ctx.el("td", {},
          heroPlayer && heroPlayer.hole_cards && heroPlayer.hole_cards.length
            ? ctx.holePair(heroPlayer.hole_cards, "sm")
            : ctx.el("span", { class: "muted" }, "—")
        ),
        ctx.el("td", {}, [
          streetsBar(ls),
          ctx.el("span", { class: "muted", style: "margin-left: 8px;" }, ls),
          ctx.el("div", { style: "margin-top: 4px;" }, ctx.miniBoard(h.board || [], "sm")),
        ]),
        ctx.el("td", {}, [
          ctx.el("div", {}, winner ? winner.name || `seat ${winner.seat}` : "—"),
          ctx.el("div", { class: "muted", style: "font-size: 11px;" }, positions[h.winner_seat] || ""),
        ]),
        ctx.el("td", { class: "col-num mono" }, ctx.fmt.int(h.pot_total)),
        ctx.el("td", { class: "col-num" },
          heroResult == null
            ? ctx.el("span", { class: "muted" }, "—")
            : ctx.el("span", {
                class: heroResult >= 0 ? "amount-pos mono" : "amount-neg mono",
              }, ctx.fmt.signed(heroResult)),
        ),
        ctx.el("td", { class: "col-chev" }, "›"),
      ]);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    root.appendChild(table);
  }

  window.ViewerSession = { render };
})();
