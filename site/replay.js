// Replays the trial run of 17 November 2026 from its board log. The HTML
// already shows the final state, so the page reads the same without this
// script or with reduced motion; the script resets the diagram and plays it.

// [seconds after 14:43:54, kind, actor, text, effect]
const RUN = [
  [0, "verdict", "h01", "[M01] feasible: one small module (count.py + tests + one COMMANDS line), single task, ~10 min"],
  [1, "plan", "h02", "[M02] Single task: implement slugify in textkit/slug.py, one line in cli.py COMMANDS, tests/test_slug.py"],
  [2, "plan", "h03", "[M03] Single task: roman.py gets to_roman, from_roman and a handler; one COMMANDS line in cli.py"],
  [6, "merged", "queue", "L1 T1 by h01 -> 0061867d84", { train: "L1" }],
  [6, "green", "queue", "M01 count: wc-style line/word/char counts for a file or stdin", { green: "M01" }],
  [8, "merged", "queue", "L2 T2 by h02 -> 8c3998f3ed", { train: "L2" }],
  [8, "green", "queue", "M02 slug: URL slug of the argument", { green: "M02" }],
  [8, "free", "h02", "[M02] FREE: can take on any small task", { off: "h02" }],
  [10, "free", "h01", "[M01] FREE: can take on other milestones or review", { off: "h01" }],
  [17, "left", "h03", "h03 left", { off: "h03" }],
  [19, "merged", "queue", "L3 T3 by h03 -> a3b655cee0", { train: "L3" }],
  [19, "green", "queue", "M03 roman: int to Roman numeral and back, rejecting bad input", { green: "M03" }],
  [62, "merged", "queue", "L4 by lead -> 8b50e7d7e7", { train: "L4", red: "M04" }],
  [68, "hired", "lead", "h04, h05 and h06 at .team-build/", { on: ["h04", "h05", "h06"] }],
  [87, "free", "h05", "[M04] FREE: M04 has no tasks yet (captain h04 has not planned)", { off: "h05" }],
  [88, "free", "h06", "[M04] FREE: h06 can take any M04 task once h04 posts the plan", { off: "h06" }],
  [91, "plan", "h04", "[M04] Interface: textkit/words.py has words, top_words, avg_word_len; stats.py has build_report"],
  [106, "merged", "queue", "L5 T4 by h04 -> 8068003f86", { train: "L5" }],
  [119, "merged", "queue", "L6 T5 by h04 -> d8235f0d12", { train: "L6" }],
  [126, "left", "h04", "h04 left", { off: "h04" }],
  [128, "merged", "queue", "L7 T6 by h04 -> 13565ec05e", { train: "L7" }],
  [128, "green", "queue", "M04 stats: JSON report on a file built from count, slug and roman plus word frequency", { green: "M04" }],
];

const MS_PER_SECOND = 90;   // 128 s of the run in about 15 s
const MAX_GAP = 1400;       // long quiet gaps are cut short
const MIN_GAP = 260;
const TRAIN_MS = 1500;

const svg = document.getElementById("yard");
const log = document.getElementById("log");
const replay = document.querySelector(".replay");
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

function clock(t) {
  const s = 14 * 3600 + 43 * 60 + 54 + t;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}

function setAgent(name, lit) {
  const g = svg.querySelector(`[data-agent="${name}"]`);
  g.classList.toggle("lit", lit);
  g.classList.toggle("dim", !lit);
}

function setSignal(id, state) {
  const [top, bottom] = svg.querySelectorAll(`[data-milestone="${id}"] .lamp`);
  top.setAttribute("class", state === "red" ? "lamp red" : "lamp");
  bottom.setAttribute("class", state === "green" ? "lamp green" : "lamp");
}

function setTrain(train, state) {
  train.style.display = state === "hidden" ? "none" : "";
  train.classList.toggle("landed", state === "landed");
}

function addLine(t, kind, actor, text) {
  const li = document.createElement("li");
  li.className = `k-${kind}`;
  li.textContent = `${clock(t)} ${kind} ${actor}: ${text}`;
  log.append(li);
  while (log.children.length > 4) log.firstElementChild.remove();
}

function reset() {
  for (const name of ["lead", "h01", "h02", "h03"]) setAgent(name, true);
  for (const name of ["h04", "h05", "h06"]) setAgent(name, false);
  for (const id of ["M01", "M02", "M03"]) setSignal(id, "red");
  setSignal("M04", "off");
  for (const train of svg.querySelectorAll(".train")) setTrain(train, "hidden");
  document.getElementById("gate-lamp").setAttribute("class", "lamp");
  log.replaceChildren();
}

// Move a train along its helper's path into the queue, through the gate, and
// onto trunk. The gate lamp shows amber while the train is at the gate.
function runTrain(id) {
  const train = document.getElementById(id);
  const path = document.getElementById(`p-${train.dataset.from}`);
  const stopX = Number(train.getAttribute("transform").match(/translate\(([\d.]+)/)[1]);
  const total = path.getTotalLength();
  let stopAt = total;
  for (let lo = 0, hi = total; hi - lo > 0.5;) {
    const mid = (lo + hi) / 2;
    if (path.getPointAtLength(mid).x < stopX || path.getPointAtLength(mid).y < 289) lo = mid; else hi = mid;
    stopAt = hi;
  }
  const lamp = document.getElementById("gate-lamp");
  setTrain(train, "moving");
  return new Promise((done) => {
    const start = performance.now();
    function frame(now) {
      const k = Math.min(1, (now - start) / TRAIN_MS);
      const eased = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      const pt = path.getPointAtLength(eased * stopAt);
      train.setAttribute("transform", `translate(${pt.x.toFixed(1)},${pt.y.toFixed(1)})`);
      lamp.setAttribute("class", pt.x > 420 && pt.x < 540 ? "lamp amber" : "lamp");
      if (k < 1) requestAnimationFrame(frame);
      else {
        train.setAttribute("transform", `translate(${stopX},290)`);
        setTrain(train, "landed");
        lamp.setAttribute("class", "lamp");
        done();
      }
    }
    requestAnimationFrame(frame);
  });
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let playing = false;

async function play() {
  if (playing) return;
  playing = true;
  replay.hidden = true;
  reset();
  let last = 0;
  for (const [t, kind, actor, text, fx = {}] of RUN) {
    const gap = Math.min(MAX_GAP, (t - last) * MS_PER_SECOND);
    if (gap > 0) await wait(Math.max(MIN_GAP, gap));
    last = t;
    if (fx.train) await runTrain(fx.train);
    if (fx.green) setSignal(fx.green, "green");
    if (fx.red) setSignal(fx.red, "red");
    if (fx.off) setAgent(fx.off, false);
    for (const name of fx.on || []) setAgent(name, true);
    addLine(t, kind, actor, text);
  }
  playing = false;
  replay.hidden = false;
}

replay.addEventListener("click", play);

if (reduced.matches || !("IntersectionObserver" in window)) {
  replay.hidden = false;
} else {
  const seen = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) {
      seen.disconnect();
      play();
    }
  }, { threshold: 0.5 });
  seen.observe(svg);
}

// Copy buttons: copy the command and say so on the button.
for (const button of document.querySelectorAll("button.copy")) {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select and copy";
    }
    setTimeout(() => (button.textContent = "Copy"), 2000);
  });
}
