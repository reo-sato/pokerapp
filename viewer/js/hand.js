// Screen 02: Hand detail — street columns
(function () {
  const STREETS = ["preflop", "flop", "turn", "river"];

  function isAutoPost(action) {
    return /sb.?post|bb.?post/i.test(action || "");
  }

  function isMetaAction(action) {
    return /^(new_hand|winner|showdown|amount_only)$/i.test(action || "");
  }

  function boardForStreet(board, street) {
    // board is the *final* community board; we take a prefix per street.
    const arr = Array.isArray(board) ? board : [];
    if (street === "preflop") return [];
    if (street === "flop") return arr.slice(0, 3);
    if (street === "turn") return arr.slice(0, 4);
    if (street === "river") return arr.slice(0, 5);
    return arr.slice();
  }

  function groupActionsByStreet(actions) {
    const out = { preflop: [], flop: [], turn: [], river: [] };
    for (const a of actions || []) {
      if (isAutoPost(a.action)) continue;
      if (isMetaAction(a.action)) continue;
      const s = (a.street || "preflop").toLowerCase();
      if (out[s]) out[s].push(a);
    }
    return out;
  }

  async function render(root, ctx, sid, hidStr) {
    ctx.showLoading();
    let session;
    try {
      session = await ctx.loadSession(sid);
    } catch (e) {
      ctx.showError(`セッション読み込み失敗: ${e.message || e}`);
      return;
    }
    ctx.clearRoot();

    const hid = Number(hidStr);
    const hand = (session.hands || []).find((h) => h.hand_id === hid);
    if (!hand) {
      ctx.showError(`hand #${hid} は session ${sid} にありません`);
      return;
    }
    const allHands = session.hands || [];
    const idx = allHands.findIndex((h) => h.hand_id === hid);
    const prev = idx > 0 ? allHands[idx - 1] : null;
    const next = idx < allHands.length - 1 ? allHands[idx + 1] : null;

    const positions = ctx.derivePositions(hand);
    const heroSeat = ctx.getHeroSeat();

    // Breadcrumb / nav
    root.appendChild(ctx.el("div", { class: "breadcrumb" }, [
      ctx.el("div", {}, [
        ctx.el("button", {
          class: "btn btn-ghost",
          onclick: () => ctx.navigate(`/sessions/${sid}`),
        }, "◀ session"),
      ]),
      ctx.el("div", { style: "display:flex; gap: 8px;" }, [
        ctx.el("button", {
          class: "btn",
          disabled: !prev,
          onclick: () => prev && ctx.navigate(`/sessions/${sid}/hands/${prev.hand_id}`),
        }, prev ? `◀ #${prev.hand_id}` : "◀"),
        ctx.el("button", {
          class: "btn",
          disabled: !next,
          onclick: () => next && ctx.navigate(`/sessions/${sid}/hands/${next.hand_id}`),
        }, next ? `#${next.hand_id} ▶` : "▶"),
      ]),
    ]));

    // Title
    root.appendChild(ctx.el("div", { class: "screen-header" }, [
      ctx.el("span", { class: "eyebrow" }, `SESSION · ${sid}`),
      ctx.el("h1", {}, `Hand #${hid}`),
      ctx.el("div", { class: "subtitle" },
        `${ctx.fmt.isoTimeSec(hand.started_at)} → ${ctx.fmt.isoTimeSec(hand.ended_at)}  ·  ` +
        `SB/BB ${hand.blinds?.sb ?? "—"}/${hand.blinds?.bb ?? "—"}  ·  ` +
        `${hand.resolution_type || "—"}` +
        (hand.review_required ? "  ·  ⚑ review required" : "")
      ),
    ]));

    // Meta strip
    const winnerPlayer = (hand.players || []).find((p) => p.seat === hand.winner_seat);
    const meta = ctx.el("div", { class: "meta-strip" }, [
      ctx.el("div", { class: "field" }, [
        ctx.el("span", { class: "lbl" }, "FINAL POT"),
        ctx.el("span", { class: "val" }, ctx.fmt.int(hand.pot_total)),
      ]),
      ctx.el("div", { class: "field" }, [
        ctx.el("span", { class: "lbl" }, "WINNER"),
        ctx.el("span", { class: "val" },
          winnerPlayer ? `seat ${winnerPlayer.seat} · ${winnerPlayer.name || ""}` : "—"
        ),
      ]),
      ctx.el("div", { class: "field" }, [
        ctx.el("span", { class: "lbl" }, "BOARD"),
        ctx.el("span", {}, ctx.miniBoard(hand.board || [], "md")),
      ]),
      ctx.el("div", { class: "field" }, [
        ctx.el("span", { class: "lbl" }, "BOARD SOURCE"),
        ctx.el("span", { class: "val", style: "font-size: 13px;" }, hand.board_source || "—"),
      ]),
    ]);
    root.appendChild(meta);

    // Seat strip
    const seatStrip = ctx.el("div", { class: "seat-strip", style: "margin-top: 14px;" }, []);
    const seats = (hand.players || []).slice().sort((a, b) => a.seat - b.seat);
    const foldedSet = new Set(hand.folded_seats || []);
    for (const p of seats) {
      const cls = ["seat-pill"];
      if (foldedSet.has(p.seat) || p.folded_street) cls.push("folded");
      if (p.seat === hand.winner_seat) cls.push("winner");
      const heroBadge = (heroSeat === p.seat)
        ? ctx.el("span", { class: "review-flag", style: "color: var(--hero); background: var(--hero-soft);" }, "YOU")
        : null;
      seatStrip.appendChild(ctx.el("div", { class: cls.join(" ") }, [
        ctx.el("div", { class: "row" }, [
          ctx.el("span", { class: "pos-pill" }, positions[p.seat] || `S${p.seat}`),
          ctx.el("span", { class: "name" }, p.name || `seat ${p.seat}`),
          heroBadge,
        ]),
        ctx.el("div", { class: "row" }, [
          ctx.holePair(p.hole_cards || [], "sm"),
        ]),
        ctx.el("div", { class: "stack" },
          `${ctx.fmt.int(p.stack_start)} → ${ctx.fmt.int(p.stack_end)}` +
          (p.result != null ? `  (${ctx.fmt.signed(p.result)})` : "")
        ),
      ]));
    }
    root.appendChild(seatStrip);

    // Street columns
    const byStreet = groupActionsByStreet(hand.actions);
    const grid = ctx.el("div", { class: "streets-grid" }, []);
    let stepCounter = 0;
    const reachedRivers = STREETS.map((s) => byStreet[s].length > 0);
    STREETS.forEach((street, idx) => {
      const acts = byStreet[street];
      const col = ctx.el("div", {
        class: "street-col" + (acts.length === 0 ? " not-played" : ""),
      }, []);
      col.appendChild(ctx.el("h3", {}, [
        ctx.el("span", {}, street.toUpperCase()),
        ctx.el("span", { class: "step" }, `${idx + 1}/4`),
      ]));
      col.appendChild(ctx.el("div", { class: "col-board" }, ctx.miniBoard(boardForStreet(hand.board, street), "sm")));

      if (acts.length === 0) {
        col.appendChild(ctx.el("div", { class: "muted", style: "text-align:center; font-size: 12px;" }, "— not played —"));
        grid.appendChild(col);
        return;
      }

      // pot range
      const potStart = acts[0].pot_after - (acts[0].amount || 0);
      const potEnd = acts[acts.length - 1].pot_after;
      col.appendChild(ctx.el("div", { class: "pot-range" }, [
        ctx.el("span", {}, `${ctx.fmt.int(potStart)} → ${ctx.fmt.int(potEnd)}`),
        ctx.el("span", { class: "pot-delta" }, `+${ctx.fmt.int(potEnd - potStart)}`),
      ]));

      for (const a of acts) {
        stepCounter += 1;
        const cls = ["action-row"];
        if (a.needs_review) cls.push("review");
        if (heroSeat === a.seat) cls.push("hero");
        const row = ctx.el("div", { class: cls.join(" ") }, [
          ctx.el("span", { class: "step-num" }, String(stepCounter).padStart(2, "0")),
          ctx.confidenceDot(a.confidence == null ? 0 : a.confidence),
          ctx.el("div", { class: "body" }, [
            ctx.el("div", { class: "who" }, [
              ctx.el("span", { class: "pos-pill" }, positions[a.seat] || `S${a.seat}`),
              ctx.el("span", { class: "player" }, a.player_name || `seat ${a.seat}`),
            ]),
            ctx.el("div", {}, ctx.actionPill(a.action, a.amount)),
            ctx.el("div", { class: "pot-after" }, `pot ${ctx.fmt.int(a.pot_after)}`),
            a.needs_review
              ? ctx.el("div", { class: "note" }, "⚑ needs review — confidence " + (a.confidence == null ? "?" : a.confidence.toFixed(2)))
              : null,
          ]),
          ctx.el("span", { class: "ts" }, ctx.fmt.isoTimeSec(a.timestamp)),
        ]);
        col.appendChild(row);
      }

      // winner banner on last reached street
      const lastReachedIdx = reachedRivers.lastIndexOf(true);
      if (idx === lastReachedIdx && winnerPlayer) {
        col.appendChild(ctx.el("div", { class: "winner-banner" },
          `🏆 ${positions[winnerPlayer.seat] || `S${winnerPlayer.seat}`} (${winnerPlayer.name || ""}) wins ${ctx.fmt.int(hand.pot_total)}`
        ));
      }

      grid.appendChild(col);
    });
    root.appendChild(grid);

    // Reviews footer
    const reviewActs = (hand.actions || []).filter((a) => a.needs_review && !isAutoPost(a.action) && !isMetaAction(a.action));
    if (reviewActs.length > 0) {
      const footer = ctx.el("div", { class: "review-footer" }, [
        ctx.el("h3", {}, `⚑ NEEDS REVIEW (${reviewActs.length})`),
      ]);
      const tbl = document.createElement("table");
      reviewActs.forEach((a) => {
        const tr = document.createElement("tr");
        tr.appendChild(ctx.el("td", { class: "mono", style: "color: var(--ink-3); font-size: 11px;" }, ctx.fmt.isoTimeSec(a.timestamp)));
        tr.appendChild(ctx.el("td", {}, ctx.el("span", { class: "pos-pill" }, positions[a.seat] || `S${a.seat}`)));
        tr.appendChild(ctx.el("td", {}, a.player_name || ""));
        tr.appendChild(ctx.el("td", {}, ctx.actionPill(a.action, a.amount)));
        tr.appendChild(ctx.el("td", { class: "mono", style: "color: var(--ink-3);" }, `conf ${(a.confidence ?? 0).toFixed(2)}`));
        tbl.appendChild(tr);
      });
      footer.appendChild(tbl);
      root.appendChild(footer);
    }
  }

  // Keyboard prev/next from app.js
  function navAdjacent(sid, hidStr, dir) {
    const session = (window.Viewer.ctx && window.Viewer.ctx.loadSession)
      ? null // we don't have cache directly accessible here; let render do it
      : null;
    // simplest: dispatch via cached fetch
    fetch(`../logs/${sid}.json`).then((r) => r.json()).then((data) => {
      const hands = data.hands || [];
      const idx = hands.findIndex((h) => h.hand_id === Number(hidStr));
      const target = hands[idx + dir];
      if (target) window.Viewer.navigate(`/sessions/${sid}/hands/${target.hand_id}`);
    }).catch(() => {});
  }

  window.ViewerHand = { render, navAdjacent };
})();
