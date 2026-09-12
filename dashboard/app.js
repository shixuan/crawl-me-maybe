/* crawl me maybe — dashboard.

   Deliberately free of any vocabulary this crawler does not define.
   What a goal is looking for is the goal's business, not this page's:
   field names come from the run's extraction spec and are rendered as
   they were declared, so a run about shops and a run about papers look
   the same here and neither needed a line of code.

   Filtering runs over rows already in memory: a run is a few dozen
   results, so every keystroke can re-render without a round trip, and
   the reader narrows things down instead of the crawler guessing. */

const $ = (sel) => document.querySelector(sel);

const TITLE_KEY = " title"; // not a legal field name, so it cannot collide
const ANY_FIELD = " any"; // same trick: cannot collide with a real field

const state = {
  runs: [],
  run: null,
  goalId: null,
  rows: [],
  fields: [],
  classes: new Set(), // empty means every class
  whens: new Set(), // same, over the four ways a result sits in time
  during: "", // days ahead that still count as open; "" is no window
  query: "",
  // "" = no requirement, ANY_FIELD = at least one field, otherwise the
  // name of the one field a result has to carry.
  hasField: "",
  headline: TITLE_KEY,
  sort: "relevance",
};

async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error((await r.json()).error || r.statusText);
  return r.json();
}

function escape(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(0, 16).replace("T", " ");
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* -- when a result runs --------------------------------------------

   The server already said whether each row is undated, over or open.
   All that is left here is where the reader draws the line ahead of
   them, which is a knob and needs no round trip. */

const WHEN_LABELS = { open: "still open", later: "starts later", undated: "no date", over: "already over" };

function horizonISO() {
  if (!state.during) return "";
  const d = new Date();
  d.setDate(d.getDate() + Number(state.during));
  // Built from the local parts on purpose. toISOString would convert to
  // UTC first, which moves the line a day for anyone west of it.
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function day(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function runsFor(r) {
  // Said the way the page said it, one end or two.
  if (r.starts_on && r.ends_on) {
    return r.starts_on === r.ends_on ? day(r.ends_on) : `${day(r.starts_on)} to ${day(r.ends_on)}`;
  }
  if (r.ends_on) return `until ${day(r.ends_on)}`;
  if (r.starts_on) return `from ${day(r.starts_on)}`;
  return "";
}

function whenOf(r, horizon) {
  return r.when === "open" && r.starts_on && horizon && r.starts_on > horizon ? "later" : r.when;
}

function valueOf(field) {
  // A field is {value, evidence}; older rows may hold the bare value.
  return field && typeof field === "object" ? field.value : field;
}

/* -- runs ----------------------------------------------------------- */

function renderRuns() {
  const list = $("#runs");
  const onlyWith = $("#only-with-results").checked;
  const runs = state.runs.filter((r) => !onlyWith || r.analyses > 0);
  list.innerHTML = "";
  if (!runs.length) {
    list.innerHTML = '<li class="empty">no runs</li>';
    return;
  }
  for (const r of runs) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.className = "run";
    b.type = "button";
    b.setAttribute("aria-current", String(r.run === state.run));
    b.innerHTML = `<span class="run-when">${when(r.started)}</span>
      <span class="run-sub">${r.analyses} analysed &middot; ${r.pages} pages</span>`;
    b.title = r.prompt || r.run;
    b.onclick = () => loadRun(r.run);
    li.append(b);
    list.append(li);
  }
}

/* -- filters -------------------------------------------------------- */

function renderChips() {
  const box = $("#chips");
  box.innerHTML = "";
  // Taken from the rows rather than a list kept here: the analyzer's
  // vocabulary has grown before, and a class nobody predicted would
  // otherwise be invisible and unfilterable.
  const counts = new Map();
  for (const r of state.rows) counts.set(r.classification, (counts.get(r.classification) || 0) + 1);
  const present = [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a));
  for (const c of present) {
    const b = document.createElement("button");
    b.className = "chip";
    b.type = "button";
    b.innerHTML = `${escape(c.toLowerCase())} &middot; ${counts.get(c)}`;
    b.setAttribute("aria-pressed", String(state.classes.has(c)));
    b.onclick = () => {
      state.classes.has(c) ? state.classes.delete(c) : state.classes.add(c);
      renderChips();
      renderCards();
    };
    box.append(b);
  }
}

function renderWhenChips() {
  const box = $("#when-chips");
  box.innerHTML = "";
  const horizon = horizonISO();
  const counts = new Map();
  for (const r of state.rows) {
    const w = whenOf(r, horizon);
    counts.set(w, (counts.get(w) || 0) + 1);
  }
  // Nothing to filter by when no run has read a date yet.
  box.hidden = counts.size <= 1 && counts.has("undated");
  for (const w of ["open", "later", "undated", "over"]) {
    if (!counts.has(w)) continue;
    const b = document.createElement("button");
    b.className = "chip";
    b.type = "button";
    b.innerHTML = `${escape(WHEN_LABELS[w])} &middot; ${counts.get(w)}`;
    b.setAttribute("aria-pressed", String(state.whens.has(w)));
    b.onclick = () => {
      state.whens.has(w) ? state.whens.delete(w) : state.whens.add(w);
      renderWhenChips();
      renderCards();
    };
    box.append(b);
  }
}

function renderFieldChoices() {
  // Two selects over the same list of declared fields, answering two
  // different questions: which field leads a card, and which field a
  // result has to carry to be shown at all.
  fillFieldSelect($("#headline"), [{ key: TITLE_KEY, label: "page title" }], state.headline);
  fillFieldSelect(
    $("#has-field"),
    [
      { key: "", label: "anything" },
      { key: ANY_FIELD, label: "any field" },
    ],
    state.hasField,
  );
}

function fillFieldSelect(sel, leading, current) {
  const options = [...leading];
  for (const f of state.fields) options.push({ key: f, label: f.replace(/_/g, " ") });
  sel.innerHTML = options
    .map((o) => {
      const on = o.key === current ? " selected" : "";
      return `<option value="${escape(o.key)}"${on}>${escape(o.label)}</option>`;
    })
    .join("");
  // Nothing to choose between when the goal declared no fields.
  sel.closest(".field").hidden = options.length <= leading.length;
}

function visible() {
  const q = state.query.trim().toLowerCase();
  const horizon = horizonISO();
  let rows = state.rows;
  if (state.classes.size) rows = rows.filter((r) => state.classes.has(r.classification));
  if (state.whens.size) rows = rows.filter((r) => state.whens.has(whenOf(r, horizon)));
  if (state.hasField === ANY_FIELD) {
    rows = rows.filter((r) => Object.keys(r.extracted || {}).length);
  } else if (state.hasField) {
    // A field is present when it survived the evidence check, which is
    // what makes "only the ones with a launch date" a claim about the
    // pages rather than about the model's willingness to guess.
    rows = rows.filter((r) => valueOf((r.extracted || {})[state.hasField]));
  }
  if (q) {
    rows = rows.filter((r) => {
      const hay = [
        r.title, r.summary, r.url, r.text,
        ...(r.tags || []),
        ...Object.entries(r.extracted || {}).flatMap(([k, v]) => [k, valueOf(v), v && v.evidence]),
      ];
      return hay.some((s) => typeof s === "string" && s.toLowerCase().includes(q));
    });
  }
  const by = {
    relevance: (a, b) => Number(b.relevance) - Number(a.relevance),
    published: (a, b) => String(b.published_at || "").localeCompare(String(a.published_at || "")),
    title: (a, b) => String(headlineOf(a)).localeCompare(String(headlineOf(b))),
    // Soonest first, and anything with no end after everything that has one.
    ends: (a, b) => (a.ends_on || "9999").localeCompare(b.ends_on || "9999"),
  }[state.sort];
  return [...rows].sort(by);
}

/* -- result cards --------------------------------------------------- */

function headlineOf(r) {
  const chosen = state.headline === TITLE_KEY ? null : valueOf((r.extracted || {})[state.headline]);
  return chosen || r.title || r.url || r.url_key;
}

function card(r) {
  const el = document.createElement("article");
  el.className = "result";

  // Every field keeps its row even when its value is also the headline:
  // the headline is for scanning, the row carries the evidence, and the
  // evidence is the only reason to trust either of them.
  const fields = Object.entries(r.extracted || {})
    .map(([name, v]) => {
      const evidence = v && v.evidence ? `<div class="evidence">${escape(v.evidence)}</div>` : "";
      return `<div class="field-row">
        <div class="field-name">${escape(name.replace(/_/g, " "))}</div>
        <div class="field-value">${escape(valueOf(v))}</div>
        ${evidence}
      </div>`;
    })
    .join("");

  const tags = (r.tags || []).map((t) => `<span class="tag">${escape(t)}</span>`).join("");
  const headline = headlineOf(r);
  // Only the two that need explaining are marked. Still open is the
  // expected case and needs no badge.
  const group = whenOf(r, horizonISO());
  const mark = group === "later" || group === "over" ? WHEN_LABELS[group] : "";
  const runs = runsFor(r);
  const subtitle = r.title && r.title !== headline ? r.title : "";

  el.innerHTML = `
    <div class="result-head">
      <h3 class="result-title"><a href="${escape(r.url)}" target="_blank" rel="noopener">${escape(headline)}</a></h3>
      <span class="score" title="relevance">${Number(r.relevance).toFixed(2)}</span>
      <span class="class-tag">${escape(r.classification.toLowerCase())}</span>
      ${mark ? `<span class="when-tag">${escape(mark)}</span>` : ""}
    </div>
    <p class="result-sub">
      <a class="open" href="${escape(r.url)}" target="_blank" rel="noopener" title="${escape(r.url)}">open page</a>
      ${subtitle ? `<span>${escape(subtitle)}</span>` : ""}
      ${r.published_at ? `<span>${escape(when(r.published_at))}</span>` : ""}
      ${runs ? `<span class="when-range">${escape(runs)}</span>` : ""}
    </p>
    ${r.summary ? `<p class="summary">${escape(r.summary)}</p>` : ""}
    ${fields ? `<div class="fields">${fields}</div>` : ""}
    ${tags ? `<div class="tags">${tags}</div>` : ""}`;
  return el;
}

function renderCards() {
  const rows = visible();
  const box = $("#cards");
  box.innerHTML = "";
  rows.forEach((r) => box.append(card(r)));
  $("#empty").hidden = rows.length > 0;
  const withFields = rows.filter((r) => Object.keys(r.extracted || {}).length).length;
  const extra = withFields ? ` &middot; ${withFields} with extracted fields` : "";
  $("#tally").innerHTML = `${rows.length} of ${state.rows.length}${extra}`;
}

/* -- loading -------------------------------------------------------- */

function adopt(data) {
  state.goalId = data.goal_id;
  state.rows = data.rows;
  state.fields = data.fields;
  // The spec's first field leads unless the reader says otherwise; with
  // no spec at all there is nothing to lead with but the page's title.
  state.headline = data.fields.length ? data.fields[0] : TITLE_KEY;
  const goal = data.goals.find((g) => g.goal_id === data.goal_id) || {};
  $("#goal-prompt").textContent = goal.prompt || "";
  $("#goal-block").hidden = !goal.prompt;
  state.classes.clear();
  state.whens.clear();
  renderFieldChoices();
  renderChips();
  renderWhenChips();
  renderCards();
}

async function loadRun(run, goalId) {
  state.run = run;
  $("#cards").innerHTML = '<p class="loading">reading...</p>';
  renderRuns();
  try {
    const base = `/api/run/${encodeURIComponent(run)}`;
    const data = await api(goalId ? `${base}/${encodeURIComponent(goalId)}` : base);
    adopt(data);

    // A replay analyses the same pages under a new goal, so one run can
    // hold several. Offered only when there is a choice to make.
    const goals = $("#goal");
    goals.hidden = data.goals.length < 2;
    goals.innerHTML = data.goals
      .map((g) => {
        const on = g.goal_id === data.goal_id ? " selected" : "";
        return `<option value="${escape(g.goal_id)}"${on}>${escape((g.prompt || g.goal_id).slice(0, 48))}</option>`;
      })
      .join("");
    goals.onchange = () => loadRun(run, goals.value);
  } catch (e) {
    $("#cards").innerHTML = `<p class="empty">could not read this run: ${escape(e.message)}</p>`;
  }
}

async function boot() {
  $("#q").oninput = (e) => { state.query = e.target.value; renderCards(); };
  $("#has-field").onchange = (e) => { state.hasField = e.target.value; renderCards(); };
  $("#sort").onchange = (e) => { state.sort = e.target.value; renderCards(); };
  // Moving the line only re-groups what is already here.
  $("#during").onchange = (e) => { state.during = e.target.value; renderWhenChips(); renderCards(); };
  $("#headline").onchange = (e) => { state.headline = e.target.value; renderCards(); };
  $("#only-with-results").onchange = renderRuns;

  const { runs } = await api("/api/runs");
  state.runs = runs;
  renderRuns();
  // Open the newest run that analysed anything: an empty page on arrival
  // reads as a broken dashboard rather than an idle one.
  const first = runs.find((r) => r.analyses > 0) || runs[0];
  if (first) loadRun(first.run);
}

boot();
