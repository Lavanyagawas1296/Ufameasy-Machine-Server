"use strict";

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const api = (path) => `api/customer/${path}`;
const TZ = "Asia/Kolkata";
const MAX_JOB_ROWS = 15;          // the printed / PDF report shows the 15 newest jobs
const TIMELINE_ROW_DAYS = 7;      // longer periods are drawn as one bar per day
const MAX_PERIOD_DAYS = 92;
const DEBOUNCE_MS = 300;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

const fmtTime = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const fmtIsoDate = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
const fmtNumber = new Intl.NumberFormat("en-GB");

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function setText(id, value) { $(id).textContent = value; }
function calendarDate(iso) { const [y, m, d] = iso.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d, 12)); }
function isoAddDays(iso, days) { const d = calendarDate(iso); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); }
function daysBetween(fromIso, toIso) { return Math.round((calendarDate(toIso) - calendarDate(fromIso)) / 86400000); }
function todayIso() { return fmtIsoDate.format(new Date()); }

// Dates read the same everywhere (page, print, PDF): "17 Nov 2026"; times "14:19".
function fmtDay(iso) { const [y, m, d] = iso.split("-").map(Number); return `${d} ${MONTHS[m - 1]} ${y}`; }
function fmtWeekdayDay(iso) { return `${WEEKDAYS[calendarDate(iso).getUTCDay()]} ${fmtDay(iso)}`; }
function fmtDate(date) { return fmtDay(fmtIsoDate.format(date)); }
function fmtDateTime(date) { return `${fmtDate(date)}, ${fmtTime.format(date)}`; }
function fmtPeriod(period) { return period.from === period.to ? fmtDay(period.from) : `${fmtDay(period.from)} to ${fmtDay(period.to)}`; }
function plural(count, word) { return `${fmtNumber.format(count)} ${word}${count === 1 ? "" : "s"}`; }

function duration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return "—";
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total === 0) return "0 min";
  if (total < 60) return "< 1 min";
  const minutes = Math.round(total / 60), h = Math.floor(minutes / 60), m = minutes % 60;
  if (!h) return `${m} min`;
  return m ? `${h} h ${m} min` : `${h} h`;
}
function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 4000);
}
function request(url, options = {}) { return fetch(url, { credentials: "same-origin", ...options }); }
function needsLogin(response) { if (response.status === 401) { location.assign("login"); return true; } return false; }
function setPressed(groupId, attr, value) {
  $(groupId).querySelectorAll("button").forEach((button) => {
    const on = button.dataset[attr] === value;
    button.classList.toggle("is-active", on);
    button.setAttribute("aria-pressed", String(on));
  });
}

// ---------------------------------------------------------------- report state
// The report follows the period: choosing a period or changing a date reloads it
// after a short pause. Download PDF, Print and Download CSV always use that period.
const R = { period: "week", from: "", to: "", data: null, allJobs: false, timer: null, debounce: null, loading: false, missed: 0, seq: 0, abort: null, printWhenReady: false, valid: true };

function presetRange(period) {
  const today = todayIso();
  if (period === "today") return [today, today];
  if (period === "month") return [`${today.slice(0, 8)}01`, today];
  const weekday = (calendarDate(today).getUTCDay() + 6) % 7;   // Monday = 0
  return [isoAddDays(today, -weekday), today];
}

function reportQuery() { return new URLSearchParams({ from: R.from, to: R.to }).toString(); }

function reportError(message = "") {
  const box = $("reportError");
  box.hidden = !message;
  box.textContent = message;
}
function dateError(message = "") {
  const box = $("dateError");
  box.hidden = !message;
  box.textContent = message;
  for (const id of ["fromDate", "toDate"]) {
    if (message) $(id).setAttribute("aria-invalid", "true"); else $(id).removeAttribute("aria-invalid");
  }
}

function setActionsEnabled(enabled) {
  R.valid = enabled;
  for (const id of ["downloadPdf", "downloadCsv"]) {
    const link = $(id);
    link.classList.toggle("is-disabled", !enabled);
    if (enabled) link.removeAttribute("aria-disabled"); else link.setAttribute("aria-disabled", "true");
  }
  $("printReport").disabled = !enabled;
}

function updateDownloadLinks() {
  const query = reportQuery();
  $("downloadPdf").href = `${api("report/usage.pdf")}?${query}`;
  $("downloadCsv").href = `${api("report/jobs.csv")}?${query}`;
}

function setLoading(on) {
  $("reportBody").classList.toggle("is-loading", on);
  $("reportBody").setAttribute("aria-busy", String(on));
  setText("reportLoading", on ? "Updating report…" : "");
}

function choosePeriod(period) {
  R.period = period;
  setPressed("periodButtons", "period", period);
  const custom = period === "custom";
  $("customDates").hidden = !custom;
  $("fromDate").disabled = $("toDate").disabled = !custom;
  if (custom) {
    $("fromDate").value = R.from;
    $("toDate").value = R.to;
    $("fromDate").focus();
    return;                                // same dates as now: nothing to reload until one changes
  }
  dateError();
  setActionsEnabled(true);
  queueReport(...presetRange(period));
}

function customRange() {
  const from = $("fromDate").value, to = $("toDate").value;
  let message = "";
  if (!from || !to) message = "Choose both a From and a To date.";
  else if (to < from) message = "The To date must be on or after the From date.";
  else if (to > todayIso()) message = "The To date cannot be in the future.";
  else if (daysBetween(from, to) + 1 > MAX_PERIOD_DAYS) message = `Choose a period of ${MAX_PERIOD_DAYS} days or less.`;
  dateError(message);
  setActionsEnabled(!message);
  return message ? null : [from, to];
}

function onDateChange() {
  const range = customRange();
  if (range) { queueReport(...range); return; }
  clearTimeout(R.debounce);               // an invalid range never replaces the report on screen
  R.debounce = null;
  R.printWhenReady = false;
  if (!R.loading) setLoading(false);
}

function queueReport(from, to) {
  if (from === R.from && to === R.to && R.data && !R.loading && !R.debounce) return;
  R.from = from;
  R.to = to;
  R.allJobs = false;
  updateDownloadLinks();
  setLoading(true);
  clearTimeout(R.debounce);
  R.debounce = setTimeout(() => { R.debounce = null; loadReport(); }, DEBOUNCE_MS);
}

async function loadReport({ quiet = false } = {}) {
  if (currentPage() !== "report") return;
  if (quiet && (R.loading || R.debounce)) return;   // a background refresh never replaces a user's request
  if (R.abort) R.abort.abort();                    // the newest period always wins
  const seq = ++R.seq, controller = new AbortController();
  R.abort = controller;
  R.loading = true;
  if (!quiet) { reportError(); setLoading(true); }
  updateDownloadLinks();
  try {
    const response = await request(`${api("report")}?${reportQuery()}`, { signal: controller.signal });
    if (seq !== R.seq || needsLogin(response)) return;
    if (response.status === 400) {
      reportError(`This period cannot be shown. Choose up to ${MAX_PERIOD_DAYS} days, ending today or earlier.`);
      R.printWhenReady = false;
      return;
    }
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    if (seq !== R.seq) return;
    R.data = data;
    R.missed = 0;
    $("offlineNotice").hidden = true;
    renderReport(R.data);
    if (R.printWhenReady) { R.printWhenReady = false; setTimeout(() => window.print(), 0); }
  } catch {
    if (seq !== R.seq) return;             // replaced by a newer request
    R.missed += 1;
    R.printWhenReady = false;
    if (R.data) $("offlineNotice").hidden = R.missed < 2;
    else reportError("Could not load the report. Try again.");
  } finally {
    if (seq === R.seq) {
      R.loading = false;
      R.abort = null;
      if (!R.debounce) setLoading(false);
      scheduleRefresh();
    }
  }
}

function scheduleRefresh() {
  clearTimeout(R.timer);
  // Only a period that includes today changes while you watch it.
  if (R.to >= todayIso()) R.timer = setTimeout(() => loadReport({ quiet: true }), R.missed ? 60000 : 30000);
}

// ---------------------------------------------------------------- report rendering
// Wording and numbers match the PDF (server/customer_report_pdf.py).
function renderReport(d) {
  const generated = new Date(d.generated_at);
  setText("machineName", d.machine_label || "—");
  setText("freshness", `· Updated ${fmtTime.format(generated)} IST`);
  setText("printMachine", d.machine_label || "—");
  setText("printPeriod", fmtPeriod(d.period));
  setText("printGenerated", `${fmtDateTime(generated)} IST`);
  const shift = `${d.shift.start}–${d.shift.end}`;
  setText("aboutTimeline", d.days.length <= TIMELINE_ROW_DAYS
    ? `The timeline shows shift hours (${shift}); the notes on the right cover the whole day.`
    : `Each bar shows one day's shift hours (${shift}).`);
  renderRunning(d);
  renderKpis(d);
  renderTimeline(d);
  renderJobs(d);
}

function operatorName(name) { return typeof name === "string" && name && name !== "Unknown" ? name : null; }

function renderRunning(d) {
  const run = d.running_now, card = $("runningNow");
  card.hidden = !run;
  if (!run) return;
  const paused = run.state === "paused";
  card.classList.toggle("is-paused", paused);
  const pill = $("runningState");
  pill.className = `pill ${paused ? "pill-paused" : "pill-running"}`;
  pill.textContent = paused ? "Paused now" : "Running now";
  setText("runningFile", run.file_name ? run.display_name : "File not recorded");
  setText("runningSince", `Started ${fmtTime.format(new Date(run.started_at))} · ${duration(run.seconds_so_far)} so far`);
  const hasProgress = typeof run.progress_pct === "number";
  $("runningProgressWrap").hidden = !hasProgress;
  if (hasProgress) $("runningProgress").style.width = `${Math.min(100, run.progress_pct)}%`;
  let line = "";
  if (run.approx_line && run.total_lines) line = `Line ≈ ${fmtNumber.format(run.approx_line)} of ${fmtNumber.format(run.total_lines)}`;
  else if (hasProgress) line = `${Math.round(run.progress_pct)}% done`;
  else if (run.total_lines) line = `${fmtNumber.format(run.total_lines)} lines`;
  setText("runningLine", line);
  const op = $("runningOperator"), name = operatorName(run.operator);
  clear(op);
  op.append("Operator: ", el("span", name ? { text: name } : { class: "not-recorded", text: "Not recorded" }));
}

// Jobs started = completed + stopped early + machine faults + not finished.
// jobs_summary.stopped_early counts every early end, faults included.
function jobCounts(d) {
  const s = d.jobs_summary;
  const running = d.jobs.filter((job) => job.result === "running").length;
  return {
    started: s.count + s.not_closed,
    completed: s.completed,
    stopped: Math.max(0, s.stopped_early - s.faults),
    faults: s.faults,
    running,
    noEnd: Math.max(0, s.not_closed - running),
  };
}

function renderKpis(d) {
  const t = d.totals, c = jobCounts(d);
  setText("kpiRun", duration(t.running_seconds));
  setText("kpiRunSub", `Paused ${duration(t.paused_seconds)} · idle ${duration(t.idle_seconds)}`);
  setText("kpiJobs", fmtNumber.format(c.started));
  const notFinished = [];
  if (c.running) notFinished.push(`${c.running} still running`);
  if (c.noEnd) notFinished.push(`${c.noEnd} with no end recorded`);
  setText("kpiJobsSub", notFinished.length ? `incl. ${notFinished.join(" and ")}` : "");
  setText("kpiDone", fmtNumber.format(c.completed));
  setText("kpiDoneSub", c.started ? `${Math.round((100 * c.completed) / c.started)}% of jobs started` : "");
  setText("kpiStopped", fmtNumber.format(c.stopped));
  setText("kpiFaults", fmtNumber.format(c.faults));
}

const STATE_LABELS = { running: "Running", paused: "Paused", idle: "Idle", off: "Off" };

function dayNote(day) {
  if (day.is_future) return "";
  const run = day.running_seconds, paused = day.paused_seconds, idle = day.idle_seconds;
  if (!run && !paused && !idle) return "No activity";
  const parts = [`Run ${duration(run)}`];
  if (paused) parts.push(`paused ${duration(paused)}`);
  parts.push(`idle ${duration(idle)}`);
  return parts.join(", ");
}

function faultLabel(count) { return count ? `, ${plural(count, "machine fault")}` : ""; }

function renderTimeline(d) {
  const box = $("timeline");
  clear(box);
  setText("timelineHint", `Shift hours ${d.shift.start}–${d.shift.end}`);
  if (d.days.length <= TIMELINE_ROW_DAYS) renderTimelineRows(box, d);
  else renderTimelineBars(box, d);
}

function renderTimelineRows(box, d) {
  box.className = "timeline timeline-rows";
  for (const day of d.days) {
    const note = dayNote(day);
    const track = el("div", { class: `track${day.is_future ? " is-future" : ""}`, role: "img" });
    for (const seg of day.segments) {
      if (seg.state === "off") continue;   // the empty track means "off"
      const node = el("div", { class: `seg st-${seg.state}`, title: `${STATE_LABELS[seg.state]} ${fmtTime.format(new Date(seg.from))}–${fmtTime.format(new Date(seg.to))}` });
      node.style.left = `${seg.left * 100}%`;
      node.style.width = `${seg.width * 100}%`;
      track.append(node);
    }
    for (const at of day.faults) {
      const mark = el("div", { class: "fault", title: "Machine fault: emergency stop or lost connection" });
      mark.style.left = `${at * 100}%`;
      track.append(mark);
    }
    track.setAttribute("aria-label", `${fmtWeekdayDay(day.date)}: ${note || "not reached yet"}${faultLabel(day.faults.length)}`);
    box.append(el("div", { class: "timeline-row" }, [
      el("span", { class: "day-label", text: fmtWeekdayDay(day.date) }),
      track,
      el("span", { class: "day-note", text: note }),
    ]));
  }
  const axis = el("div", { class: "axis" });
  const toMin = (v) => { const [h, m] = v.split(":").map(Number); return h * 60 + m; };
  const start = toMin(d.shift.start), end = toMin(d.shift.end), span = end - start;
  const step = span > 12 * 60 ? 120 : 60;
  for (let t = Math.ceil(start / 60) * 60; t <= end; t += step) {
    const pos = (t - start) / span;
    const label = el("span", { text: `${String(Math.floor(t / 60) % 24).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}` });
    if (pos <= 0.001) label.className = "first";
    else if (pos >= 0.999) label.className = "last";
    label.style.left = `${pos * 100}%`;
    axis.append(label);
  }
  box.append(el("div", { class: "timeline-row timeline-axis", "aria-hidden": "true" }, [el("span"), axis, el("span")]));
}

function dayFractions(day) {
  const f = { running: 0, paused: 0, idle: 0 };
  for (const seg of day.segments) if (seg.state in f) f[seg.state] += seg.width;
  return f;
}

function barLabels(days) {
  if (days.length <= 31) {
    return days.map((day, i) => {
      const [y, m, dd] = day.date.split("-").map(Number);
      return [String(dd), i === 0 || dd === 1 ? `${MONTHS[m - 1]} ${y}` : ""];
    });
  }
  return days.map((day, i) => {
    const [, m, dd] = day.date.split("-").map(Number);
    return [i % 7 === 0 && i < days.length - 3 ? `${dd} ${MONTHS[m - 1]}` : "", ""];   // no label clipped at the right edge
  });
}

function renderTimelineBars(box, d) {
  box.className = "timeline timeline-bars";
  const [sh, sm] = d.shift.start.split(":").map(Number), [eh, em] = d.shift.end.split(":").map(Number);
  const shiftSeconds = ((eh * 60 + em) - (sh * 60 + sm)) * 60;
  const columns = `repeat(${d.days.length}, minmax(0, 1fr))`;
  const plot = el("div", { class: "bars-plot" });
  const labels = el("div", { class: "bars-labels", "aria-hidden": "true" });
  const months = el("div", { class: "bars-months", "aria-hidden": "true" });
  plot.style.gridTemplateColumns = labels.style.gridTemplateColumns = months.style.gridTemplateColumns = columns;
  const names = barLabels(d.days);
  d.days.forEach((day, i) => {
    const note = dayNote(day), name = fmtWeekdayDay(day.date);
    const col = el("div", {
      class: `bar-col${day.is_future ? " is-future" : ""}`, role: "img",
      "aria-label": `${name}: ${note || "not reached yet"}${faultLabel(day.faults.length)}`,
      title: `${name}${note ? `\n${note}` : ""}${faultLabel(day.faults.length)}`,
    });
    const bar = el("div", { class: "bar" });
    if (!day.is_future) {
      const f = dayFractions(day);
      for (const state of ["running", "paused", "idle"]) {
        if (f[state] <= 0) continue;
        const part = el("div", { class: `bar-part st-${state}` });
        part.style.height = `${Math.min(1, f[state]) * 100}%`;
        bar.append(part);
      }
    }
    col.append(day.faults.length ? el("span", { class: "bar-fault" }) : el("span", { class: "bar-fault-space" }), bar);
    plot.append(col);
    labels.append(el("span", { text: names[i][0] }));
    months.append(el("span", { text: names[i][1] }));
  });
  const yAxis = el("div", { class: "bars-y", "aria-hidden": "true" }, [el("span", { text: duration(shiftSeconds) }), el("span", { text: "0 h" })]);
  const parts = [yAxis, plot, el("span"), labels];
  if (names.some((n) => n[1])) parts.push(el("span"), months);
  box.append(el("div", { class: "bars" }, parts));
}

const RESULT_VIEW = {
  completed: ["Completed", "result-completed"],
  stopped: ["Stopped early", "result-stopped"],
  aborted: ["Stopped early", "result-stopped"],
  failed: ["Machine fault", "result-failed"],
  running: ["Running", "result-running"],
  unrecorded: ["No end recorded", "result-unrecorded"],
  open: ["No end recorded", "result-unrecorded"],
};

// Why a job ended: "" when nothing applies (completed or running), null when not recorded.
function reasonText(job) {
  if (job.result === "stopped" || job.result === "aborted") return "Stopped by operator";
  if (job.result === "failed") {
    const reported = job.reason || "";
    return reported && !reported.includes("[redacted]") && !reported.startsWith("Fault (reason not reported") ? reported : "Machine fault";
  }
  if (job.result === "unrecorded" || job.result === "open") return null;
  return "";
}

function mutedCell(text, extra = "") { return el("td", { class: `not-recorded ${extra}`.trim(), text }); }

function fileCell(job) {
  if (!job.file_name) return mutedCell("Not recorded", "file-cell");
  const td = el("td", { class: "file-cell" });
  if (job.available) {
    const button = el("button", { class: "file-button", type: "button", text: job.display_name, title: "Preview this G-code file" });
    button.addEventListener("click", () => viewFile(job.file_name, button));
    td.append(button);
  } else {
    td.append(el("span", { text: job.display_name, title: job.display_name }));
  }
  return td;
}

function renderJobs(d) {
  const body = $("jobsBody"), foot = $("jobsFoot"), jobs = d.jobs;
  clear(body);
  clear(foot);
  const shown = R.allJobs ? jobs : jobs.slice(0, MAX_JOB_ROWS);
  shown.forEach((job, index) => {
    const [label, cls] = RESULT_VIEW[job.result] || [job.result, "result-unrecorded"];
    const start = new Date(job.started_at), operator = operatorName(job.operator), reason = reasonText(job);
    body.append(el("tr", { class: index >= MAX_JOB_ROWS ? "over-cap" : null }, [
      el("td", { class: "nowrap", text: fmtDate(start) }),
      operator ? el("td", { class: "cell-clip", text: operator, title: operator }) : mutedCell("Not recorded"),
      fileCell(job),
      el("td", { class: "nowrap", text: fmtTime.format(start) }),
      job.ended_at ? el("td", { class: "nowrap", text: fmtTime.format(new Date(job.ended_at)) }) : mutedCell("—", "nowrap"),
      job.result === "running" ? mutedCell("—", "nowrap") : el("td", { class: "nowrap", text: duration(job.run_seconds) }),
      el("td", {}, el("span", { class: `result ${cls}`, text: label })),
      reason ? el("td", { class: "cell-clip", text: reason, title: reason }) : mutedCell(reason === null ? "Not recorded" : "—"),
    ]));
  });
  const hidden = jobs.length - MAX_JOB_ROWS;
  if (hidden > 0) {
    foot.append(el("tr", { class: "more-row print-only" }, el("td", { colspan: "8", text: `+ ${fmtNumber.format(hidden)} more ${hidden === 1 ? "job" : "jobs"} in the CSV download` })));
  }
  if (jobs.length) {
    const total = jobs.reduce((sum, job) => sum + (job.result === "running" ? 0 : Number(job.run_seconds) || 0), 0);
    foot.append(el("tr", { class: "total-row" }, [
      el("td", { colspan: "3", text: `Total: ${plural(jobs.length, "job")}` }),
      el("td", { colspan: "2" }),
      el("td", { class: "nowrap", colspan: "3", text: duration(total) }),
    ]));
  }
  $("jobsEmpty").hidden = jobs.length > 0;
  const more = $("moreJobs");
  more.hidden = jobs.length <= MAX_JOB_ROWS;
  more.textContent = R.allJobs ? "Show fewer jobs" : `Show all ${plural(jobs.length, "job")}`;
  setText("jobsHint", d.jobs_truncated ? `Latest ${fmtNumber.format(jobs.length)} jobs, newest first` : "Newest first");
}

// ---------------------------------------------------------------- print
// Print uses the A4 portrait print stylesheet in app.css; Download PDF downloads
// the same page made by the server (server/customer_report_pdf.py).
let printTitle = null;
addEventListener("beforeprint", () => {
  if (currentPage() !== "report" || !R.data) return;
  printTitle = document.title;
  document.title = `Usage report ${fmtPeriod(R.data.period)}`;
});
addEventListener("afterprint", () => {
  if (printTitle !== null) { document.title = printTitle; printTitle = null; }
});

function printReport() {
  if (!R.valid) return;
  if (R.loading || R.debounce || !R.data) { R.printWhenReady = true; return; }   // print once the new period is shown
  window.print();
}

// ---------------------------------------------------------------- file preview
async function viewFile(name, trigger) {
  const modal = $("fileModal");
  setText("fileModalTitle", name);
  setText("filePreview", "Loading preview…");
  setText("filePreviewMeta", "");
  $("downloadPreviewFile").href = `${api("files/download")}?name=${encodeURIComponent(name)}`;
  $("downloadPreviewFile").setAttribute("download", name);
  $("closeFileModal").onclick = () => { modal.close(); trigger.focus(); };
  modal.showModal();
  try {
    const response = await request(`${api("files/view")}?name=${encodeURIComponent(name)}&lines=500`);
    if (needsLogin(response)) return;
    if (!response.ok) throw new Error();
    const data = await response.json();
    setText("filePreview", data.lines.join("\n"));
    setText("filePreviewMeta", `${fmtNumber.format(data.total_lines)} lines${data.total_lines > data.lines.length ? ` · first ${data.lines.length} shown` : ""}`);
  } catch {
    setText("filePreview", "Preview is not available.");
  }
}

// ---------------------------------------------------------------- logs page
const L = { range: "7d", severity: "", cursor: null };
const fmtLogTime = { format: fmtDateTime };   // "17 Nov 2026, 14:19"

function istValue(value) { if (!value) return ""; return `${value.length === 16 ? `${value}:00` : value}+05:30`; }
function logBounds() {
  if (L.range === "custom") return { from: istValue($("logsFrom").value), to: istValue($("logsTo").value) };
  const end = new Date(), start = new Date(end.getTime() - ({ "24h": 24, "7d": 168, "30d": 720 }[L.range] || 168) * 3600 * 1000);
  return { from: start.toISOString(), to: end.toISOString() };
}
function logParams(cursor = "") {
  const b = logBounds(), p = new URLSearchParams({ limit: "50", from: b.from, to: b.to });
  if (L.severity) p.set("severity", L.severity);
  if (cursor) p.set("cursor", cursor);
  return p;
}
function logsError(message = "") { $("logsError").hidden = !message; $("logsError").textContent = message; }
function updateLogsDownload() { $("downloadLogs").href = `${api("logs/download")}?${logParams()}`; }
function customLogsValid() { const f = $("logsFrom").value, t = $("logsTo").value; return f && t && new Date(f) <= new Date(t); }

async function loadLogs(append = false) {
  if (currentPage() !== "logs") return;
  logsError();
  updateLogsDownload();
  try {
    const response = await request(`${api("logs")}?${logParams(append ? L.cursor : "")}`);
    if (needsLogin(response)) return;
    if (!response.ok) throw new Error();
    const rows = await response.json();
    L.cursor = response.headers.get("X-Next-Cursor");
    const body = $("logsBody");
    if (!append) clear(body);
    for (const row of rows) {
      body.append(el("tr", {}, [
        el("td", { class: "nowrap", text: row.time ? fmtLogTime.format(new Date(row.time)) : "—" }),
        el("td", { text: row.severity }),
        el("td", { text: row.message || "" }),
      ]));
    }
    $("logsEmpty").hidden = body.children.length > 0;
    setText("logsState", `${body.children.length} events${L.cursor ? "" : " · No more events"}`);
    $("loadMore").hidden = !L.cursor;
  } catch {
    logsError("Could not load logs. Try again.");
  }
}

// ---------------------------------------------------------------- routing and live updates
function currentPage() { return location.hash.slice(1) === "logs" ? "logs" : "report"; }
function route() {
  const page = currentPage();
  document.querySelectorAll("[data-page]").forEach((node) => { node.hidden = node.dataset.page !== page; });
  document.querySelectorAll("[data-route-link]").forEach((link) => {
    const on = link.dataset.routeLink === page;
    link.classList.toggle("is-active", on);
    if (on) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  document.title = page === "logs" ? "Logs" : "Usage report";
  if (page === "logs") { clearTimeout(R.timer); loadLogs(); } else loadReport();
}

let lastLive = 0, liveTimer = null;
function onTelemetryUpdate() {
  // Debounced: a busy machine writes many events; refresh at most every 10 s.
  if (liveTimer) return;
  const wait = Math.max(0, 10000 - (Date.now() - lastLive));
  liveTimer = setTimeout(() => {
    liveTimer = null;
    lastLive = Date.now();
    if (currentPage() === "report" && R.to >= todayIso()) loadReport({ quiet: true });
  }, wait);
}
function connectLive() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  try {
    const socket = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.onmessage = (event) => { try { if (JSON.parse(event.data).type === "customer_telemetry_update") onTelemetryUpdate(); } catch { /* ignore */ } };
    socket.onclose = () => setTimeout(connectLive, 5000);
    socket.onerror = () => socket.close();
  } catch {
    setTimeout(connectLive, 5000);
  }
}

// ---------------------------------------------------------------- wire up
document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => choosePeriod(button.dataset.period)));
for (const id of ["fromDate", "toDate"]) $(id).addEventListener("change", onDateChange);
function guardDownload(event) {
  if (!R.valid) { event.preventDefault(); return; }
  toast("Download started");
}
$("downloadPdf").addEventListener("click", guardDownload);
$("downloadCsv").addEventListener("click", guardDownload);
$("printReport").addEventListener("click", printReport);
$("moreJobs").addEventListener("click", () => { R.allJobs = !R.allJobs; if (R.data) renderJobs(R.data); });

document.querySelectorAll("[data-log-range]").forEach((button) => button.addEventListener("click", () => {
  L.range = button.dataset.logRange;
  setPressed("rangeButtons", "logRange", L.range);
  const custom = L.range === "custom";
  $("logsFrom").disabled = $("logsTo").disabled = $("applyLogs").disabled = !custom;
  if (custom) { $("logsFrom").focus(); updateLogsDownload(); } else { L.cursor = null; loadLogs(); }
}));
document.querySelectorAll("[data-severity]").forEach((button) => button.addEventListener("click", () => {
  L.severity = button.dataset.severity;
  setPressed("eventButtons", "severity", L.severity);
  L.cursor = null;
  loadLogs();
}));
$("applyLogs").addEventListener("click", () => {
  if (!customLogsValid()) { logsError("Choose a From time before the To time."); return; }
  L.cursor = null;
  loadLogs();
});
$("refreshLogs").addEventListener("click", () => { L.cursor = null; loadLogs(); });
$("loadMore").addEventListener("click", () => loadLogs(true));
$("downloadLogs").addEventListener("click", (event) => {
  if (L.range === "custom" && !customLogsValid()) { event.preventDefault(); logsError("Choose a From time before the To time."); return; }
  toast("Download started");
});

[R.from, R.to] = presetRange(R.period);
updateDownloadLinks();
$("fromDate").max = $("toDate").max = todayIso();
setPressed("periodButtons", "period", R.period);
setPressed("rangeButtons", "logRange", L.range);
setPressed("eventButtons", "severity", L.severity);
addEventListener("hashchange", route);
connectLive();
route();
