// Screen 00: All sessions list
(function () {
  // Tiny SVG sparkline of cumulative net-or-pots across sessions
  function sparkline(values, width = 240, height = 36) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "sparkline");
    if (!values.length) return svg;
    const min = Math.min(0, ...values);
    const max = Math.max(0, ...values);
    const range = (max - min) || 1;
    const pts = values.map((v, i) => {
      const x = (i / Math.max(1, values.length - 1)) * (width - 4) + 2;
      const y = height - 2 - ((v - min) / range) * (height - 4);
      return [x, y];
    });
    // zero line
    if (min < 0 && max > 0) {
      const zy = height - 2 - ((0 - min) / range) * (height - 4);
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", 0); line.setAttribute("x2", width);
      line.setAttribute("y1", zy); line.setAttribute("y2", zy);
      line.setAttribute("stroke", "#3a4259");
      line.setAttribute("stroke-dasharray", "2 3");
      svg.appendChild(line);
    }
    const path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    path.setAttribute("points", pts.map((p) => p.join(",")).join(" "));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#aab2c5");
    path.setAttribute("stroke-width", "1.5");
    svg.appendChild(path);
    // dots
    pts.forEach(([x, y], i) => {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", 2);
      c.setAttribute("fill", values[i] >= 0 ? "#4ade80" : "#ff5b6f");
      svg.appendChild(c);
    });
    return svg;
  }

  const State = {
    sortKey: "date",      // "date" | "pot" | "reviews"
    sortDir: -1,          // -1 desc, 1 asc
    filterBlind: null,    // string like "100/200"
    reviewOnly: false,
  };

  async function render(root, ctx) {
    ctx.showLoading();
    const idx = await ctx.loadIndex();
    ctx.clearRoot();

    const sessions = idx.sessions || [];

    // Header
    root.appendChild(ctx.el("div", { class: "screen-header" }, [
      ctx.el("span", { class: "eyebrow" }, "SESSIONS"),
      ctx.el("h1", {}, "All sessions"),
      ctx.el("div", { class: "subtitle" },
        `${sessions.length} sessions · ${sessions.reduce((s, x) => s + (x.hands || 0), 0)} hands`
      ),
    ]));

    if (idx._error) {
      root.appendChild(ctx.el("div", { class: "error" },
        `index.json を読み込めませんでした (${idx._error}). ` +
        `pokerapp を 1 ハンドでも動かすと logs/index.json が生成されます。`
      ));
      return;
    }

    if (!sessions.length) {
      root.appendChild(ctx.el("div", { class: "empty" },
        "セッションがまだありません — python main.py --cli で記録を開始してください"
      ));
      return;
    }

    // KPI strip
    const totalHands = sessions.reduce((s, x) => s + (x.hands || 0), 0);
    const totalReviews = sessions.reduce((s, x) => s + (x.reviews || 0), 0);
    const biggest = Math.max(0, ...sessions.map((x) => x.biggest_pot || 0));
    const reviewSessions = sessions.filter((s) => (s.reviews || 0) > 0).length;

    const kpi = ctx.el("div", { class: "kpi-row" }, [
      ctx.el("div", { class: "kpi" }, [
        ctx.el("div", { class: "label" }, "TOTAL HANDS"),
        ctx.el("div", { class: "value" }, ctx.fmt.int(totalHands)),
        ctx.el("div", { class: "sub" }, `across ${sessions.length} sessions`),
      ]),
      ctx.el("div", { class: "kpi" }, [
        ctx.el("div", { class: "label" }, "BIGGEST POT"),
        ctx.el("div", { class: "value" }, ctx.fmt.int(biggest)),
        ctx.el("div", { class: "sub" }, "max pot recorded"),
      ]),
      ctx.el("div", { class: ((totalReviews > 0) ? "kpi accent" : "kpi") }, [
        ctx.el("div", { class: "label" }, "NEEDS REVIEW"),
        ctx.el("div", { class: "value" }, ctx.fmt.int(totalReviews)),
        ctx.el("div", { class: "sub" }, `flags across ${reviewSessions} sessions`),
      ]),
      ctx.el("div", { class: "kpi" }, [
        ctx.el("div", { class: "label" }, "LATEST"),
        ctx.el("div", { class: "value" }, ctx.fmt.isoDate(sessions[0].started_at) || "—"),
        ctx.el("div", { class: "sub" }, sessions[0].session_id || ""),
      ]),
    ]);
    root.appendChild(kpi);

    // Filter bar
    const distinctBlinds = Array.from(new Set(
      sessions.map((s) => (s.blinds && s.blinds.sb != null) ? `${s.blinds.sb}/${s.blinds.bb}` : null)
              .filter(Boolean)
    ));

    const filterBar = ctx.el("div", { class: "filter-bar" }, []);

    const stakesGroup = ctx.el("div", { class: "filter-group" }, [
      ctx.el("span", { class: "group-label" }, "STAKES"),
    ]);
    const stakeChip = (label, value) => {
      const chip = ctx.el("span", {
        class: "chip" + (State.filterBlind === value ? " active" : ""),
        onclick: () => { State.filterBlind = (State.filterBlind === value ? null : value); render(root, ctx); },
      }, label);
      return chip;
    };
    stakesGroup.appendChild(stakeChip("all", null));
    for (const b of distinctBlinds) stakesGroup.appendChild(stakeChip(b, b));
    filterBar.appendChild(stakesGroup);

    const reviewChip = ctx.el("span", {
      class: "chip review" + (State.reviewOnly ? " active" : ""),
      onclick: () => { State.reviewOnly = !State.reviewOnly; render(root, ctx); },
    }, "⚑ review only");
    filterBar.appendChild(reviewChip);

    const sortGroup = ctx.el("div", { class: "filter-group", style: "margin-left: auto;" }, [
      ctx.el("span", { class: "group-label" }, "SORT"),
    ]);
    const sortChip = (label, key) => {
      const isActive = State.sortKey === key;
      return ctx.el("span", {
        class: "chip" + (isActive ? " active" : ""),
        onclick: () => {
          if (State.sortKey === key) State.sortDir = -State.sortDir;
          else { State.sortKey = key; State.sortDir = -1; }
          render(root, ctx);
        },
      }, `${label}${isActive ? (State.sortDir === -1 ? " ↓" : " ↑") : ""}`);
    };
    sortGroup.appendChild(sortChip("date", "date"));
    sortGroup.appendChild(sortChip("biggest pot", "pot"));
    sortGroup.appendChild(sortChip("reviews", "reviews"));
    filterBar.appendChild(sortGroup);
    root.appendChild(filterBar);

    // Apply filters
    let rows = sessions.slice();
    if (State.filterBlind) {
      rows = rows.filter((s) => s.blinds && `${s.blinds.sb}/${s.blinds.bb}` === State.filterBlind);
    }
    if (State.reviewOnly) {
      rows = rows.filter((s) => (s.reviews || 0) > 0);
    }
    rows.sort((a, b) => {
      const key = State.sortKey;
      let av, bv;
      if (key === "date") { av = a.started_at || ""; bv = b.started_at || ""; }
      else if (key === "pot") { av = a.biggest_pot || 0; bv = b.biggest_pot || 0; }
      else if (key === "reviews") { av = a.reviews || 0; bv = b.reviews || 0; }
      if (av < bv) return -State.sortDir;
      if (av > bv) return State.sortDir;
      return 0;
    });

    // Table
    const table = ctx.el("table", { class: "list-table" }, [
      ctx.el("thead", {}, ctx.el("tr", {}, [
        ctx.el("th", {}, "Date"),
        ctx.el("th", {}, "Session ID"),
        ctx.el("th", {}, "Time · duration"),
        ctx.el("th", { class: "col-num" }, "Hands"),
        ctx.el("th", {}, "Stakes"),
        ctx.el("th", { class: "col-num" }, "Biggest pot"),
        ctx.el("th", { class: "col-num" }, "Reviews"),
        ctx.el("th", { class: "col-chev" }, ""),
      ])),
    ]);
    const tbody = ctx.el("tbody", {});
    for (const s of rows) {
      const sb = s.blinds && s.blinds.sb;
      const bb = s.blinds && s.blinds.bb;
      const stakes = (sb != null && bb != null) ? `${sb}/${bb}` : "—";
      const tr = ctx.el("tr", {
        class: (s.reviews > 0 ? "has-review" : ""),
        onclick: () => ctx.navigate(`/sessions/${s.session_id}`),
      }, [
        ctx.el("td", {}, [
          ctx.el("div", {}, ctx.fmt.isoDate(s.started_at) || "—"),
          ctx.el("div", { class: "muted" }, ctx.fmt.weekday(s.started_at)),
        ]),
        ctx.el("td", { class: "mono" }, s.session_id),
        ctx.el("td", {}, [
          ctx.el("div", { class: "mono" }, `${ctx.fmt.isoTime(s.started_at)} → ${ctx.fmt.isoTime(s.ended_at)}`),
          ctx.el("div", { class: "muted" }, ctx.fmt.duration(s.started_at, s.ended_at)),
        ]),
        ctx.el("td", { class: "col-num" }, ctx.fmt.int(s.hands)),
        ctx.el("td", { class: "mono" }, stakes),
        ctx.el("td", { class: "col-num" }, ctx.fmt.int(s.biggest_pot)),
        ctx.el("td", { class: "col-num" },
          s.reviews > 0
            ? ctx.el("span", { class: "review-flag" }, `⚑ ${s.reviews}`)
            : ctx.el("span", { class: "muted" }, "—")
        ),
        ctx.el("td", { class: "col-chev" }, "›"),
      ]);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    root.appendChild(table);

    // Sparkline (biggest pot trajectory by date asc)
    const trajectory = sessions.slice().sort((a, b) => (a.started_at || "").localeCompare(b.started_at || ""));
    const vals = trajectory.map((s) => s.biggest_pot || 0);
    if (vals.length > 1) {
      const spark = ctx.el("div", { style: "margin-top: 18px; display: flex; align-items: center; gap: 10px;" }, [
        ctx.el("span", { class: "eyebrow" }, "BIGGEST POT TRAJECTORY"),
        sparkline(vals, 360, 40),
      ]);
      root.appendChild(spark);
    }
  }

  window.ViewerSessions = { render };
})();
