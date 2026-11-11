"use strict";

const byId = (id) => document.getElementById(id);
const form = byId("reservation-form");
const settings = byId("settings-form");
const query = byId("availability-form");
const pages = {reservation: "座位预约", availability: "空闲时段", settings: "配置管理"};
const labels = {
  idle: "待启动", running: "运行中", stopping: "正在停止", stopped: "已停止",
  finished: "已结束", failed: "任务失败",
};
const results = {verified: "登录成功", booked: "预约成功", no_booking: "未完成预约"};
const pageSize = 40;
const autoRefreshInterval = 1000;
let state;
let connected = false;
let configured = false;
let configurationError = "";
let busy = false;
let saving = false;
let generation = 0;
let activePage = "reservation";
let availability;
let cachedAvailability;
let availabilityVersion = -1;
let fetchingAvailability = false;
let resultPage = 0;
let seatListSignature;
let lastAvailabilityQuery;
let nextAutoRefreshAt = Infinity;

for (const target of [form, settings]) {
  target.querySelector("[data-options]").append(byId("reservation-options").content.cloneNode(true));
  const select = target.elements.start;
  select.append(new Option("现在开始", ""));
  for (const [label, first, end] of [["上午", 450, 720], ["下午", 720, 1080], ["晚上", 1080, 1320]]) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (let minutes = first; minutes < end; minutes += 30) {
      const time = timeLabel(minutes);
      group.append(new Option(time, time));
    }
    select.append(group);
  }
  select.append(new Option("自定义时间", "custom"));
  target.addEventListener("change", () => updateForm(target));
}
for (const dates of document.querySelectorAll("[data-date-options]")) {
  dates.append(byId("date-options").content.cloneNode(true));
}

function updateForm(target) {
  const fields = target.elements;
  const custom = fields.duration_mode.value === "custom";
  const customStart = fields.start.value === "custom";
  fields.start.options[0].textContent = fields.date.value === "tomorrow" ? "最早可预约" : "现在开始";
  target.querySelector("[data-custom-start]").hidden = !customStart;
  fields.custom_start.disabled = !customStart;
  fields.custom_start.required = customStart;
  target.querySelector("[data-duration-field]").hidden = !custom;
  fields.duration.disabled = !custom;
  fields.duration.required = custom;
  // Keep the saved count even while one-shot mode overrides it.
  fields.max_attempts.disabled = fields.once.checked;
  if (target === form) {
    byId("start-reservation").textContent = fields.manual_confirm.checked ? "查找座位" : "开始预约";
  }
}

function readOptions(target) {
  const fields = target.elements;
  return {
    floor: Number(fields.floor.value),
    date: fields.date.value,
    start: fields.start.value === "custom" ? fields.custom_start.value : fields.start.value,
    duration: Number(fields.duration_mode.value === "custom" ? fields.duration.value : fields.duration_mode.value),
    interval: Number(fields.interval.value),
    max_attempts: Number(fields.max_attempts.value),
    auto_confirm: !fields.manual_confirm.checked,
    once: fields.once.checked,
    session: fields.session.value || "whut",
  };
}

function setOptions(target, options) {
  const fields = target.elements;
  for (const name of ["floor", "interval", "max_attempts", "session"]) {
    fields[name].value = options[name] ?? "";
  }
  fields.date.value = options.date === "tomorrow" ? "tomorrow" : "today";
  const start = options.start || "";
  const preset = [...fields.start.options].some((option) => option.value === start);
  fields.start.value = preset ? start : "custom";
  fields.custom_start.value = preset ? "" : start;
  fields.duration_mode.value = options.duration > 0 ? "custom" : options.duration < 0 ? "-1" : "0";
  fields.duration.value = options.duration > 0 ? options.duration : 120;
  fields.manual_confirm.checked = !options.auto_confirm;
  fields.once.checked = options.once;
  updateForm(target);
}

function showError(message) {
  const error = byId("error");
  error.textContent = message;
  error.hidden = !message;
}

function applyConfig(config) {
  setOptions(form, config.defaults);
  setOptions(settings, config.defaults);
  query.elements.date.value = config.defaults.date === "tomorrow" ? "tomorrow" : "today";
  query.elements.floor.value = config.defaults.floor;
  settings.elements.username.value = config.username;
  settings.elements.password.value = "";
  settings.elements.password.placeholder = config.password_configured ? "已保存" : "";
  settings.elements.clear_password.checked = false;
  byId("clear-password").hidden = !config.password_configured;
  configured = true;
  if (config.invalid.length) showError(`配置值无效：${config.invalid.join("、")}`);
  render();
}

function render() {
  const running = state?.status === "running";
  const active = running || state?.status === "stopping";
  const availabilityJob = state?.kind === "availability";
  const waiting = running && state.pending;
  const status = byId("status");
  const statusText = connected ? (waiting ? "等待确认" : labels[state.status]) : "连接断开";
  if (status.textContent !== statusText) status.textContent = statusText;
  status.dataset.state = connected ? state.status : "offline";
  for (const id of ["start-reservation", "login", "query", "resume-query"]) {
    byId(id).disabled = !connected || !configured || busy || active;
  }
  byId("save-settings").disabled = !configured || saving;
  for (const id of ["accept", "skip", "stop", "stop-query"]) {
    byId(id).disabled = !connected || busy;
  }
  byId("stop").hidden = !running || availabilityJob;
  byId("stop-query").hidden = !availabilityJob || (!running && !byId("auto-refresh").checked);
  byId("pending").hidden = !waiting;
  if (waiting) {
    const {room, label, date, start, end} = state.pending.seat;
    byId("seat-details").textContent = [room, label ? `${label} 号座位` : "", date, start && `${start}–${end}`]
      .filter(Boolean).join(" / ");
  }
  const note = byId("task-note");
  note.textContent = results[state?.result] || "";
  note.hidden = !note.textContent;
  const logs = byId("logs");
  const output = state?.logs.join("\n") || "";
  if (logs.textContent !== output) {
    const follow = logs.scrollHeight - logs.clientHeight - logs.scrollTop < 28;
    logs.textContent = output;
    logs.hidden = !output;
    if (follow) logs.scrollTop = logs.scrollHeight;
  }
}

function showPage() {
  const name = location.hash.slice(1);
  activePage = Object.hasOwn(pages, name) ? name : "reservation";
  byId("page-title").textContent = pages[activePage];
  document.querySelectorAll("[data-page]").forEach((page) => {
    page.hidden = page.dataset.page !== activePage;
  });
  document.querySelectorAll("nav a").forEach((link) => {
    if (link.hash === `#${activePage}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  showError(configurationError);
  refreshAvailability();
}

async function request(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  };
  const response = await fetch(`/api/${path}`, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}

async function poll() {
  const current = generation;
  try {
    const next = await request("status");
    if (current === generation && !busy) {
      connected = true;
      state = next;
      render();
      await refreshAvailability();
      maybeAutoRefresh();
    }
  } catch {
    if (current === generation && !busy) {
      connected = false;
      render();
    }
  } finally {
    setTimeout(poll, 850);
  }
}

async function act(path, body, automatic = false) {
  if (busy || !connected) return;
  busy = true;
  generation += 1;
  showError("");
  render();
  try {
    state = await request(path, body);
    if (path === "availability") {
      const continuing = body.resume && lastAvailabilityQuery &&
        ["date", "floor", "time_step", "session"].every((key) => body[key] === lastAvailabilityQuery[key]);
      lastAvailabilityQuery = body;
      nextAutoRefreshAt = Infinity;
      cachedAvailability = automatic || continuing ? availability : null;
      if (!automatic && !continuing) {
        resultPage = 0;
        byId("room-filter").value = "";
        byId("seat-filter").value = "";
      }
      await refreshAvailability();
    }
  } catch (failure) {
    showError(failure.message);
  } finally {
    busy = false;
    generation += 1;
    render();
  }
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function timeLabel(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function seatTrack(slots) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const firstMinute = 360;
  const width = 1080;
  svg.setAttribute("viewBox", `0 0 ${width} 20`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  for (const [start, end] of slots || []) {
    const left = Math.max(0, start - firstMinute);
    const right = Math.min(width, end - firstMinute);
    if (right <= left) continue;
    const bar = document.createElementNS(svg.namespaceURI, "rect");
    bar.setAttribute("x", left);
    bar.setAttribute("y", "4");
    bar.setAttribute("width", right - left);
    bar.setAttribute("height", "12");
    svg.append(bar);
  }
  return svg;
}

function seatRow(seat) {
  const row = element("div", "", "seat-row");
  const label = element("strong", seat.label, "seat-number");
  const track = element("div", "", "seat-track");
  track.append(seatTrack(seat.slots));
  const times = element("div", "", "seat-times");
  if (seat.slots?.length) {
    for (const [start, end] of seat.slots) {
      times.append(element("span", `${timeLabel(start)}–${timeLabel(end)}`, "time-chip"));
    }
  } else {
    const descriptions = {
      locked: "停用",
      onsite: "仅现场可选",
      pending: seat.overview_free ? "有空闲 · 待查时段" : "待查时段",
      sampling: "采样中",
    };
    times.textContent = descriptions[seat.state] ||
      (seat.slots === null ? "读取失败" : seat.time_step ? "未找到完整空闲段" : "无空闲");
    times.classList.add("seat-unavailable");
  }
  row.append(label, track, times);
  return row;
}

function visibleRooms() {
  if (!cachedAvailability || (availability.date && availability.date !== cachedAvailability.date)) {
    return availability.rooms;
  }
  const rooms = new Map(cachedAvailability.rooms.map((room) => [room.id, room]));
  for (const room of availability.rooms) {
    const old = rooms.get(room.id);
    const rows = new Map((old?.seats || []).map((row) => [row.id ?? row.label, row]));
    for (const row of room.seats) {
      const id = row.id ?? row.label;
      if (!["pending", "sampling"].includes(row.state) || !rows.has(id)) rows.set(id, row);
    }
    rooms.set(room.id, {...room, seats: [...rows.values()]});
  }
  return [...rooms.values()];
}

function renderSeatList(rooms = visibleRooms()) {
  const list = byId("seat-list");
  const floorRoom = byId("room-filter").value;
  const search = byId("seat-filter").value.trim().toLowerCase();
  const free = byId("free-only").checked;
  const groups = rooms
    .filter((room) => !floorRoom || room.id === floorRoom)
    .map((room) => ({
      ...room,
      seats: room.seats.filter((seat) => (!free || seat.slots?.length || seat.overview_free) &&
          seat.label.toLowerCase().includes(search))
        .sort((a, b) => (a.position ?? 0) - (b.position ?? 0)),
    }));
  const total = groups.reduce((sum, room) => sum + room.seats.length, 0);
  const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1);
  resultPage = Math.min(resultPage, lastPage);
  const first = resultPage * pageSize;
  const last = Math.min(total, first + pageSize);
  const visible = [];
  let cursor = 0;
  for (const room of groups) {
    const start = Math.max(0, first - cursor);
    const end = Math.min(room.seats.length, last - cursor);
    cursor += room.seats.length;
    if (end > start) visible.push({id: room.id, name: room.name, floor: room.floor, seats: room.seats.slice(start, end)});
  }
  const signature = JSON.stringify([total, resultPage, visible, total ? null : availability.status, availability.error]);
  if (signature === seatListSignature) return;
  seatListSignature = signature;
  const fragment = document.createDocumentFragment();
  let offset = 0;
  for (const room of groups) {
    const start = Math.max(0, first - offset);
    const end = Math.min(room.seats.length, last - offset);
    offset += room.seats.length;
    if (end <= start) continue;
    const group = element("section", "", "seat-group");
    group.append(element("h3", `${room.floor} / ${room.name}`));
    const axis = element("div", "", "seat-axis");
    const hours = element("div", "", "seat-hours");
    for (const hour of [6, 9, 12, 15, 18, 21, 24]) hours.append(element("span", timeLabel(hour * 60)));
    axis.append(element("span", "座位"), hours, element("span", "可预约时段"));
    group.append(axis);
    room.seats.slice(start, end).forEach((seat) => group.append(seatRow(seat)));
    fragment.append(group);
  }
  if (!total && !availability.error) {
    fragment.append(element("p", availability.status === "running" ? "正在读取座位" : "没有匹配的座位", "empty"));
  }
  list.replaceChildren(fragment);
  byId("paging").hidden = lastPage === 0;
  byId("page-count").textContent = `${first + 1}–${last} / ${total}`;
  byId("previous").disabled = resultPage === 0;
  byId("next").disabled = resultPage === lastPage;
}

function renderAvailability() {
  const status = byId("availability-status");
  status.textContent = availability ? ({
    running: byId("auto-refresh").checked ? "自动刷新中" : cachedAvailability ? "刷新中" :
      (availability.stage === "inventory" ? "读取座位清单" : "读取空闲时段"),
    stopped: "已停止", failed: "查询失败",
    finished: byId("auto-refresh").checked ? "自动刷新中" : "查询完成",
  })[availability.status] : "尚未查询";
  status.dataset.state = availability?.status || "idle";
  byId("availability-output").hidden = !availability;
  if (!availability) return;
  byId("query-date").textContent = availability.date || cachedAvailability?.date || "";
  const rooms = visibleRooms();
  const loaded = availability.rooms.reduce((sum, room) => sum + room.seats.length, 0);
  const scanned = availability.rooms.reduce((sum, room) =>
    sum + room.seats.filter((seat) => !["pending", "sampling"].includes(seat.state)).length, 0);
  const total = availability.rooms.reduce((sum, room) => sum + room.total, 0);
  const available = rooms.reduce((sum, room) => sum + room.seats.filter((seat) => seat.slots?.length).length, 0);
  const count = byId("query-count");
  count.hidden = availability.status === "failed" && rooms.length === 0;
  const progress = `时段 ${scanned} / ${loaded}`;
  count.textContent = availability.stage === "inventory" ? `座位 ${loaded} / ${total}` :
    `${progress} · ${available} 个有空闲`;
  const warning = byId("availability-error");
  warning.textContent = availability.error || (availability.rooms.some((room) => room.partial) ? "部分阅览室的座位清单不完整" : "");
  warning.hidden = !warning.textContent;
  const select = byId("room-filter");
  const current = select.value;
  if (select.options.length !== rooms.length + 1 ||
      rooms.some((room, index) => select.options[index + 1].value !== room.id)) {
    const options = rooms.map((room) => new Option(`${room.floor} / ${room.name}`, room.id));
    select.replaceChildren(new Option("全部阅览室", ""), ...options);
    select.value = rooms.some((room) => room.id === current) ? current : "";
  }
  byId("availability-filters").hidden = rooms.length === 0;
  renderSeatList(rooms);
}

async function refreshAvailability() {
  const version = state?.availability_version;
  if (activePage !== "availability" || fetchingAvailability || version === undefined ||
      version === availabilityVersion) return;
  fetchingAvailability = true;
  try {
    availability = await request("availability");
    availabilityVersion = version;
    if (availability?.status === "finished") {
      cachedAvailability = null;
      nextAutoRefreshAt = Date.now() + autoRefreshInterval;
    }
    if (availability?.status === "failed" || availability?.closed) {
      byId("auto-refresh").checked = false;
    }
    renderAvailability();
  } catch (failure) {
    if (activePage === "availability") showError(failure.message);
  } finally {
    fetchingAvailability = false;
  }
}

function maybeAutoRefresh() {
  if (activePage !== "availability" || document.hidden || !byId("auto-refresh").checked ||
      !lastAvailabilityQuery || availability?.status !== "finished" || busy ||
      ["running", "stopping"].includes(state?.status) || Date.now() < nextAutoRefreshAt) return;
  if (query.elements.date.value !== lastAvailabilityQuery.date ||
      Number(query.elements.floor.value) !== lastAvailabilityQuery.floor ||
      Number(query.elements.time_step.value) !== lastAvailabilityQuery.time_step) return;
  act("availability", {...lastAvailabilityQuery, resume: false, refresh: true}, true);
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  act("start", readOptions(form));
});
settings.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (saving) return;
  const feedback = byId("save-status");
  saving = true;
  render();
  feedback.textContent = "";
  showError("");
  try {
    applyConfig(await request("config", {
      defaults: readOptions(settings),
      username: settings.elements.username.value,
      password: settings.elements.password.value,
      clear_password: settings.elements.clear_password.checked,
    }));
    feedback.textContent = "已保存";
  } catch (failure) {
    showError(failure.message);
  } finally {
    saving = false;
    render();
  }
});
query.addEventListener("submit", (event) => {
  event.preventDefault();
  startAvailability(false);
});
function startAvailability(resume) {
  act("availability", {
    date: query.elements.date.value,
    floor: Number(query.elements.floor.value),
    time_step: Number(query.elements.time_step.value),
    session: form.elements.session.value || "whut",
    resume,
  });
}
byId("resume-query").addEventListener("click", () => startAvailability(true));
byId("auto-refresh").addEventListener("change", () => {
  if (!byId("auto-refresh").checked) {
    if (availability) renderAvailability();
    render();
    return;
  }
  if (!connected || !configured || busy || ["running", "stopping"].includes(state?.status)) return;
  const matching = lastAvailabilityQuery &&
    query.elements.date.value === lastAvailabilityQuery.date &&
    Number(query.elements.floor.value) === lastAvailabilityQuery.floor &&
    Number(query.elements.time_step.value) === lastAvailabilityQuery.time_step;
  if (matching) {
    nextAutoRefreshAt = Date.now();
    maybeAutoRefresh();
  } else {
    startAvailability(false);
  }
});
byId("login").addEventListener("click", () => act("login", {session: form.elements.session.value || "whut"}));
for (const id of ["stop", "stop-query"]) {
  byId(id).addEventListener("click", () => {
    if (id === "stop-query") {
      byId("auto-refresh").checked = false;
      if (state.status !== "running") {
        render();
        renderAvailability();
        return;
      }
    }
    act("stop", {id: state.id});
  });
}
for (const decision of ["accept", "skip"]) {
  byId(decision).addEventListener("click", () => act("confirm", {id: state.pending.id, decision}));
}
for (const id of ["room-filter", "seat-filter", "free-only"]) {
  byId(id).addEventListener("input", () => {
    resultPage = 0;
    renderSeatList();
  });
}
byId("previous").addEventListener("click", () => {
  resultPage -= 1;
  renderSeatList();
});
byId("next").addEventListener("click", () => {
  resultPage += 1;
  renderSeatList();
});

window.addEventListener("hashchange", showPage);
showPage();
request("config").then(applyConfig).catch((failure) => {
  configurationError = failure.message;
  showError(configurationError);
});
poll();
