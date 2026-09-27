/* 公共冰箱格位与清理 — 前端逻辑 */
"use strict";

const state = {
  users: [],
  uid: Number(localStorage.getItem("fridge_uid")) || 1,
  me: null,
  fridges: [],
  tab: null,
  mapFridgeId: null,
  tempFridgeId: null,
  storeFridgeId: null,
  flow: { label: null, item: null, step: 1 },   // 存放三步状态机
  activeBatchId: null,
  expanded: {},                         // 收件箱展开行
};

const ROLE_TABS = {
  employee:     ["store", "map", "appeal"],
  cleaner:      ["clean", "map"],
  tenant_admin: ["appeal", "map"],
  property:     ["ops", "map", "clean"],
};
const TAB_NAMES = { store: "快速存放", map: "格位图", clean: "到期清理", appeal: "申诉复核", ops: "物业运维" };
const ROLE_NAMES = { employee: "员工", cleaner: "保洁", tenant_admin: "租户管理员", property: "物业" };
const STATUS_NAMES = {
  pending_store: "待确认", stored: "在架", expired: "待清理", retention: "保留区",
  disposed: "已处置", claimed_back: "已返还", removed: "已取回",
};
const EXC_NAMES = {
  unlabeled: "无标签物品", wrong_slot: "占错格位", leak: "液体泄漏",
  duplicate_scan: "重复扫码", temp_alert: "温度超限", migration_missing: "迁移缺件",
};

/* ---------------- 基础 ---------------- */
async function api(path, method = "GET", body = null) {
  const opt = { method, headers: { "X-User-Id": state.uid } };
  if (body !== null) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || `请求失败 (${res.status})`);
    err.code = data.error;
    throw err;
  }
  return data;
}

function toast(msg, kind = "") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}
const ok = m => toast(m, "ok");
const err = e => toast(e.message || String(e), "err");

const fmtDate = ts => {
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const fmtTime = ts => {
  const d = new Date(ts * 1000);
  return `${fmtDate(ts)} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
const fmtLeft = sec => {
  if (sec <= 0) return "已到期";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h >= 24 ? `剩 ${Math.floor(h / 24)} 天 ${h % 24} 时` : h > 0 ? `剩 ${h} 时 ${m} 分` : `剩 ${m} 分`;
};
const el = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------------- 启动 ---------------- */
async function init() {
  state.users = await api("/api/users");
  const sel = el("userSelect");
  sel.innerHTML = state.users.map(u =>
    `<option value="${u.id}">${esc(u.name)} · ${ROLE_NAMES[u.role]}${u.dept_short ? " · " + u.dept_short : ""}</option>`).join("");
  sel.onchange = () => switchUser(Number(sel.value));
  await switchUser(state.users.find(u => u.id === state.uid) ? state.uid : state.users[0].id);
  setInterval(() => refreshTab(true), 15000);
}

async function switchUser(uid) {
  state.uid = uid;
  localStorage.setItem("fridge_uid", String(uid));
  el("userSelect").value = uid;
  state.me = await api("/api/me");
  el("roleBadge").textContent = ROLE_NAMES[state.me.role] + (state.me.dept_short ? ` · ${state.me.dept_short}` : "");
  state.fridges = await api("/api/fridges");
  state.flow = { label: null, item: null, step: 1 };
  renderTabs();
  const first = ROLE_TABS[state.me.role][0];
  showTab(first);
}

function renderTabs() {
  const tabs = ROLE_TABS[state.me.role];
  el("tabs").innerHTML = tabs.map(t =>
    `<button data-tab="${t}" class="${state.tab === t ? "active" : ""}">${TAB_NAMES[t]}</button>`).join("");
  el("tabs").querySelectorAll("button").forEach(b =>
    b.onclick = () => showTab(b.dataset.tab));
}

async function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach(s => s.classList.add("hidden"));
  el("tab-" + tab).classList.remove("hidden");
  renderTabs();
  await refreshTab();
}

async function refreshTab(quiet = false) {
  try {
    state.fridges = await api("/api/fridges");
    if (state.tab === "store") await renderStore();
    if (state.tab === "map") await renderMap();
    if (state.tab === "clean") await renderClean();
    if (state.tab === "appeal") await renderAppeals();
    if (state.tab === "ops") await renderOps();
  } catch (e) { if (!quiet) err(e); }
}

/* ---------------- 存放三步 ---------------- */
function setStep(n) {
  document.querySelectorAll("#stepper .step").forEach(s => {
    const k = Number(s.dataset.step);
    s.classList.toggle("active", k === n);
    s.classList.toggle("done", k < n);
  });
  el("storeStep1").classList.toggle("hidden", n !== 1);
  el("storeStep2").classList.toggle("hidden", n !== 2);
  el("storeStep3").classList.toggle("hidden", n !== 3);
}

async function renderStore() {
  setStep(state.flow.step);
  el("labelPreview").classList.toggle("hidden", !state.flow.label);
  if (state.flow.label) fillLabelPreview();
  if (state.flow.step === 2) await renderStorePicker();
  if (state.flow.item) await fillConfirmStep();
  await renderMyItems();
}

function fillLabelPreview() {
  const l = state.flow.label;
  el("labelPreview").classList.remove("hidden");
  el("labelImg").src = "/files/" + l.image_path;
  el("labelCode").textContent = l.code;
  el("pickedLabelCode").textContent = l.code;
}

async function fillConfirmStep() {
  const item = state.flow.item;
  el("pendingSlotText").textContent = `${item.fridge_code} · ${item.slot_label}`;
  el("storeDone").classList.toggle("hidden", item.status !== "stored");
  el("btnConfirmStore").classList.toggle("hidden", item.status === "stored");
  el("btnCancelPending").classList.toggle("hidden", item.status === "stored");
  if (item.status === "stored") {
    el("doneDetail").textContent =
      `${item.name} 已放入 ${item.fridge_code} ${item.slot_label}，${fmtDate(item.expires_at)} 到期。`;
  }
  await refreshDoorState(item.fridge_id);
}

async function refreshDoorState(fridgeId) {
  const d = await api(`/api/sim/door/${fridgeId}`);
  const eld = el("doorState");
  eld.textContent = d.open ? "门磁：门未关" : "门磁：已关闭";
  eld.className = "door-state " + (d.open ? "open" : "closed");
  return d;
}

function bindStore() {
  el("btnMakeLabel").onclick = async () => {
    try {
      const name = el("itemName").value.trim();
      const keep = Number(el("keepDays").value);
      state.flow.label = await api("/api/labels", "POST", { item_name: name, keep_days: keep });
      state.flow.step = 1;
      ok("标签已打印，请贴到物品上");
      await renderStore();
    } catch (e) { err(e); }
  };
  el("btnVoidLabel").onclick = async () => {
    try {
      await api(`/api/labels/${state.flow.label.code}/void`, "POST");
      state.flow = { label: null, item: null, step: 1 };
      ok("已撤销：标签作废，本次登记已取消");
      await renderStore();
    } catch (e) { err(e); }
  };
  el("btnToStep2").onclick = async () => {
    const cur = state.fridges.find(f => f.id === state.storeFridgeId);
    if (!cur || cur.status !== "active") {
      state.storeFridgeId = state.fridges.find(f => f.status === "active")?.id;
    }
    state.flow.step = 2;
    await renderStore();
  };
  el("btnBackTo1").onclick = async () => { state.flow.step = 1; await renderStore(); };
  el("btnToggleDoor").onclick = async () => {
    const fid = state.flow.item.fridge_id;
    const d = await api(`/api/sim/door/${fid}`);
    await api("/api/sim/door", "POST", { fridge_id: fid, open: !d.open });
    await refreshDoorState(fid);
  };
  el("btnConfirmStore").onclick = async () => {
    try {
      state.flow.item = await api(`/api/items/${state.flow.item.id}/confirm`, "POST");
      ok("关门确认成功，存放完成");
      await renderStore();
    } catch (e) { err(e); await refreshDoorState(state.flow.item.fridge_id); }
  };
  el("btnCancelPending").onclick = async () => {
    try {
      const r = await api(`/api/items/${state.flow.item.id}/cancel`, "POST");
      state.flow = { label: null, item: null, step: 1 };
      ok(`已撤销存放，标签 ${r.label_code} 恢复可用，格位已释放`);
      await renderStore();
    } catch (e) { err(e); }
  };
  el("btnUndoStore").onclick = async () => {
    try {
      await api(`/api/items/${state.flow.item.id}/take-out`, "POST");
      state.flow = { label: null, item: null, step: 1 };
      ok("已撤销存放：物品取回，格位已释放");
      await renderStore();
    } catch (e) { err(e); }
  };
  el("btnNewStore").onclick = async () => {
    state.flow = { label: null, item: null, step: 1 };
    el("itemName").value = "";
    await renderStore();
  };
}

async function renderStorePicker() {
  const picker = el("storeFridgePicker");
  picker.innerHTML = state.fridges.map(f =>
    `<span class="chip ${f.id === state.storeFridgeId ? "active" : ""} ${f.status !== "active" ? "off" : ""}"
       data-id="${f.id}">${esc(f.code)}${f.status !== "active" ? "（停机）" : ""}</span>`).join("");
  picker.querySelectorAll(".chip").forEach(c => c.onclick = async () => {
    state.storeFridgeId = Number(c.dataset.id);
    await renderStorePicker();
  });
  if (state.storeFridgeId) {
    const map = await api(`/api/fridges/${state.storeFridgeId}/slots`);
    renderFridgeMap(el("storeSlotMap"), map, async slot => {
      if (slot.item_id || slot.blocked) return;
      try {
        state.flow.item = await api("/api/items/store", "POST",
          { label_code: state.flow.label.code, slot_id: slot.id });
        state.flow.step = 3;
        ok(`已暂存到 ${slot.label}，请关门确认`);
        await renderStore();
      } catch (e) {
        err(e);
        await renderStorePicker();   // 竞争失败刷新格位
      }
    });
  }
}

/* ---------------- 格位图 ---------------- */
function renderFridgeMap(container, map, onPick) {
  const f = map.fridge;
  const rows = [];
  for (let r = 1; r <= f.rows; r++) {
    const cells = map.slots.filter(s => s.row === r).map(s => {
      let cls = "free", inner = "";
      if (s.blocked) { cls = "blocked"; }
      else if (s.item_id) {
        cls = s.expired ? "expired" : "used";
        if (s.mine) cls += " mine";
        // 标签只显示部门简称与日期
        inner = `<span class="dept">${esc(s.dept_short)}</span><span class="date">${fmtDate(s.stored_at)}</span>`;
      }
      return `<div class="slot ${cls}" data-id="${s.id}" title="${s.label}">
                <span class="slot-pos">${s.label}</span>${inner}</div>`;
    }).join("");
    rows.push(`<div class="shelf-row" style="grid-template-columns:repeat(${f.cols},1fr)">${cells}</div>`);
    if (r < f.rows) rows.push(`<div class="shelf-plate"></div>`);
  }
  container.innerHTML = `<div class="fridge-shell ${f.status !== "active" ? "shutdown" : ""}">
    <div class="fridge-title"><span>${esc(f.name)}</span><span>${f.status === "active" ? "运行中" : "已停机"}</span></div>
    <div class="shelf">${rows.join("")}</div></div>`;
  container.querySelectorAll(".slot").forEach(div => {
    const slot = map.slots.find(s => s.id === Number(div.dataset.id));
    div.onclick = () => onPick ? onPick(slot) : showSlotDetail(map, slot);
  });
}

async function renderMap() {
  const picker = el("mapFridgePicker");
  if (!state.mapFridgeId || !state.fridges.some(f => f.id === state.mapFridgeId)) {
    state.mapFridgeId = state.fridges[0]?.id;
  }
  picker.innerHTML = state.fridges.map(f =>
    `<span class="chip ${f.id === state.mapFridgeId ? "active" : ""}" data-id="${f.id}">
       ${esc(f.code)} <span class="muted">${f.used_slots}/${f.total_slots}</span>${f.expiring ? ' <span class="dot">●</span>' : ""}</span>`).join("");
  picker.querySelectorAll(".chip").forEach(c => c.onclick = async () => {
    state.mapFridgeId = Number(c.dataset.id);
    el("slotDetail").classList.add("hidden");
    await renderMap();
  });
  if (!state.mapFridgeId) return;
  const map = await api(`/api/fridges/${state.mapFridgeId}/slots`);
  renderFridgeMap(el("slotMap"), map, null);
}

async function showSlotDetail(map, slot) {
  const box = el("slotDetail");
  box.classList.remove("hidden");
  if (!slot.item_id) {
    box.innerHTML = `<b>${slot.label}</b> · ${slot.blocked ? "已封锁（泄漏/维护）" : "空闲格位"}
      <div class="row-actions">
        <button class="btn small" data-exc="leak">报告液体泄漏</button>
        <button class="btn small" data-exc="unlabeled">报告无标签物品</button>
      </div>`;
  } else {
    const mine = slot.mine;
    box.innerHTML = `<b>${slot.label}</b> · ${esc(slot.dept_short)} 部门物品 · 存放于 ${fmtDate(slot.stored_at)}
      ${slot.expired ? ' · <span class="warn">已到期</span>' : ""}
      <div class="row-actions">
        ${mine ? `<button class="btn small" id="btnTakeOut">取回</button>` : ""}
        <button class="btn small" data-exc="wrong_slot">报告占错格位</button>
        <button class="btn small" data-exc="leak">报告液体泄漏</button>
      </div>`;
    const t = box.querySelector("#btnTakeOut");
    if (t) t.onclick = async () => {
      try {
        await api(`/api/items/${slot.item_id}/take-out`, "POST");
        ok("已取回，格位释放");
        box.classList.add("hidden");
        await renderMap();
      } catch (e) { err(e); }
    };
  }
  box.querySelectorAll("[data-exc]").forEach(b => b.onclick = async () => {
    const note = prompt("补充说明（可留空）", "") ?? "";
    try {
      await api("/api/exceptions", "POST", {
        type: b.dataset.exc, fridge_id: map.fridge.id, slot_id: slot.id,
        item_id: slot.item_id || null, note,
      });
      ok("异常已登记，物业/保洁会跟进处理");
      box.classList.add("hidden");
      await renderMap();
    } catch (e) { err(e); }
  });
}

/* ---------------- 本部门物品 ---------------- */
async function renderMyItems() {
  const items = await api("/api/my/items");
  const box = el("myItems");
  if (!items.length) { box.innerHTML = `<div class="empty">本部门暂无在架物品</div>`; return; }
  box.innerHTML = items.map(i => `
    <div class="item-card">
      <span class="status-pill ${i.status}">${STATUS_NAMES[i.status] || i.status}</span>
      <div class="grow"><b>${esc(i.name)}</b>
        <span class="muted"> · ${i.owner_name} · ${i.fridge_code ? i.fridge_code + " " + (i.slot_label || "") : "临时保留区"}
        · ${fmtDate(i.stored_at || i.created_at)} 存 · ${fmtDate(i.expires_at)} 到期</span></div>
      ${i.status === "stored" ? `<button class="btn small" data-act="take" data-id="${i.id}">取回</button>` : ""}
      ${i.status === "retention" ? `<button class="btn small primary" data-act="appeal" data-id="${i.id}">认领申诉</button>` : ""}
    </div>`).join("");
  box.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
    try {
      if (b.dataset.act === "take") {
        await api(`/api/items/${b.dataset.id}/take-out`, "POST");
        ok("已取回，格位释放");
      } else {
        const reason = prompt("申诉理由", "还要食用，请保留");
        if (reason === null) return;
        await api(`/api/items/${b.dataset.id}/appeal`, "POST", { reason });
        ok("申诉已提交，等待租户管理员复核");
      }
      await renderMyItems();
    } catch (e) { err(e); }
  });
}

/* ---------------- 到期清理（收件箱） ---------------- */
async function renderClean() {
  const isCleaner = state.me.role === "cleaner";
  // 批次条
  const batches = await api("/api/batches");
  const open = batches.filter(b => b.status === "open");
  if (!state.activeBatchId || !open.some(b => b.id === state.activeBatchId)) {
    state.activeBatchId = open[0]?.id || null;
  }
  el("batchBar").innerHTML =
    open.map(b => `<span class="chip ${b.id === state.activeBatchId ? "active" : ""}" data-bid="${b.id}">
      批次 #${b.id} · ${esc(b.fridge_code)}</span>`).join("") +
    (isCleaner ? `<button class="btn small primary" id="btnNewBatch">+ 新建批次</button>` : "");
  el("batchBar").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    state.activeBatchId = Number(c.dataset.bid); renderClean();
  });
  const nb = el("btnNewBatch");
  if (nb) nb.onclick = async () => {
    const active = state.fridges.filter(f => f.status === "active");
    const fid = Number(prompt(`输入冰箱编号创建批次：\n${active.map(f => f.id + " = " + f.code).join("  ")}`, active[0]?.id));
    if (!fid) return;
    try {
      const b = await api("/api/batches", "POST", { fridge_id: fid });
      state.activeBatchId = b.id;
      ok(`批次 #${b.id} 已创建`);
      await renderClean();
    } catch (e) { err(e); }
  };

  // 待清理队列
  const queue = await api("/api/cleanup/queue");
  el("cleanupQueue").innerHTML = queue.length ? queue.map(i => inboxRow({
    key: "q" + i.id,
    dept: i.dept_short, title: i.name,
    sub: `${i.fridge_code} ${i.slot_label} · ${i.owner_name} · ${fmtDate(i.expires_at)} 到期`,
    right: `<span class="warn">待清理</span>`,
    expand: `
      <div class="facts">
        <div>标签码：<code>${i.label_code}</code></div>
        <div>存放：${fmtTime(i.stored_at)} · 保留 ${i.keep_days} 天</div>
        ${isCleaner ? `
        <div class="row-actions">
          <input type="file" accept="image/*" id="photo-${i.id}" style="font-size:12px">
          <button class="btn small primary" data-collect="${i.id}"
            ${state.activeBatchId ? "" : "disabled"}>拍照收入保留区（批次 #${state.activeBatchId || "-"}）</button>
        </div>
        ${state.activeBatchId ? "" : '<div class="muted">请先新建清理批次</div>'}` : ""}
      </div>`,
  })).join("") : `<div class="empty">收件箱是空的，没有到期物品</div>`;

  el("cleanupQueue").querySelectorAll("[data-collect]").forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    const itemId = Number(b.dataset.collect);
    const file = el("photo-" + itemId).files[0];
    let photo = null;
    if (file) photo = await fileToBase64(file);
    try {
      await api(`/api/batches/${state.activeBatchId}/collect/${itemId}`, "POST", { photo });
      ok("已拍照并移入临时保留区，申诉期开始计时");
      await renderClean();
    } catch (e) { err(e); }
  });

  // 保留区
  const retention = await api("/api/cleanup/retention");
  el("retentionHint").textContent = retention.length ? `共 ${retention.length} 件，申诉期结束方可处置` : "";
  el("retentionList").innerHTML = retention.length ? retention.map(i => inboxRow({
    key: "r" + i.id,
    dept: i.dept_short, title: i.name,
    sub: `标签 ${i.label_code} · ${i.owner_name}`,
    right: i.open_appeal ? `<span class="warn">申诉复核中</span>`
         : i.seconds_left > 0 ? fmtLeft(i.seconds_left) : `<span class="warn">可处置</span>`,
    expand: `
      ${i.photo_path ? `<img src="/files/${i.photo_path}" alt="清理照片">` : ""}
      <div class="facts">
        <div>标签码：<code>${i.label_code}</code></div>
        <div>申诉截止：${fmtTime(i.retention_until)}（${fmtLeft(i.seconds_left)}）</div>
        ${i.open_appeal ? `<div class="warn">存在未决申诉，需租户管理员复核</div>` : ""}
        ${isCleaner ? `<div class="row-actions">
          <button class="btn small danger" data-dispose="${i.id}"
            ${i.seconds_left > 0 || i.open_appeal ? "disabled" : ""}>申诉期结束，执行处置</button>
        </div>` : ""}
      </div>`,
  })).join("") : `<div class="empty">临时保留区为空</div>`;

  el("retentionList").querySelectorAll("[data-dispose]").forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    try {
      await api(`/api/items/${b.dataset.dispose}/dispose`, "POST");
      ok("已处置并记录");
      await renderClean();
    } catch (e) { err(e); }
  });

  // 批次列表
  el("batchList").innerHTML = batches.length ? batches.map(b => inboxRow({
    key: "b" + b.id,
    dept: b.fridge_code, title: `批次 #${b.id}`,
    sub: `${b.cleaner_name} · ${fmtTime(b.created_at)} · ${b.items.length} 件`,
    right: b.status === "open" ? "进行中" : "已关闭",
    expand: b.items.length ? `<div class="facts">` + b.items.map(i =>
      `<div>${esc(i.name)} · <code>${i.label_code}</code> · ${STATUS_NAMES[i.status]}
       ${i.photo_path ? ` · <a href="/files/${i.photo_path}" target="_blank">照片</a>` : ""}</div>`).join("") + `</div>`
      : `<div class="facts">批次内暂无物品</div>`,
  })).join("") : `<div class="empty">暂无清理批次</div>`;

  bindInboxToggle(el("tab-clean"));
}

function inboxRow({ key, dept, title, sub, right, expand }) {
  const open = state.expanded[key];
  return `
    <div class="inbox-row" data-key="${key}">
      <span class="chip-dept">${esc(dept)}</span>
      <div class="main"><b>${esc(title)}</b><div class="sub">${esc(sub)}</div></div>
      <div class="right">${right}</div>
    </div>
    ${open ? `<div class="inbox-expand" data-key="${key}">${expand}</div>` : ""}`;
}

function bindInboxToggle(root) {
  root.querySelectorAll(".inbox-row").forEach(r => r.onclick = () => {
    state.expanded[r.dataset.key] = !state.expanded[r.dataset.key];
    refreshTab(true);
  });
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const rd = new FileReader();
    rd.onload = () => resolve(String(rd.result).split(",")[1]);
    rd.onerror = reject;
    rd.readAsDataURL(file);
  });
}

/* ---------------- 申诉复核 ---------------- */
async function renderAppeals() {
  const list = await api("/api/appeals");
  const isAdmin = state.me.role === "tenant_admin";
  el("appealHint").textContent = isAdmin
    ? "您是租户管理员：请复核本部门的认领申诉，批准后物品登记返还。"
    : "员工可在「快速存放 → 本部门物品」对保留区物品发起认领申诉。";
  el("appealList").innerHTML = list.length ? list.map(a => inboxRow({
    key: "a" + a.id,
    dept: a.dept_short,
    title: `${a.item_name} · ${a.user_name} 申诉`,
    sub: `${fmtTime(a.created_at)} 提交 · ${esc(a.reason || "无理由")}`,
    right: a.status === "open" ? `<span class="warn">待复核</span>`
         : a.status === "approved" ? "已批准返还" : "已驳回",
    expand: `
      ${a.photo_path ? `<img src="/files/${a.photo_path}" alt="清理照片">` : ""}
      <div class="facts">
        <div>申诉理由：${esc(a.reason || "—")}</div>
        <div>申诉截止：${fmtTime(a.retention_until)}</div>
        ${isAdmin && a.status === "open" ? `
        <div class="row-actions">
          <button class="btn small primary" data-approve="${a.id}">批准返还</button>
          <button class="btn small danger" data-reject="${a.id}">驳回</button>
        </div>` : ""}
      </div>`,
  })).join("") : `<div class="empty">暂无申诉</div>`;

  el("appealList").querySelectorAll("[data-approve],[data-reject]").forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    const id = b.dataset.approve || b.dataset.reject;
    try {
      await api(`/api/appeals/${id}/resolve`, "POST", { approve: Boolean(b.dataset.approve) });
      ok(b.dataset.approve ? "已批准，物品登记返还" : "已驳回");
      await renderAppeals();
    } catch (e) { err(e); }
  });
  bindInboxToggle(el("tab-appeal"));
}

/* ---------------- 物业运维 ---------------- */
async function renderOps() {
  // 冰箱卡片
  el("fridgeCards").innerHTML = state.fridges.map(f => `
    <div class="fridge-card ${f.status !== "active" ? "shutdown" : ""}">
      <h3>${esc(f.code)} <span class="tag ${f.status === "active" ? "ok" : "bad"}">${f.status === "active" ? "运行中" : "已停机"}</span></h3>
      <div class="muted">${esc(f.name)}</div>
      <div class="stat"><span>格位占用</span><b>${f.used_slots}/${f.total_slots}</b></div>
      <div class="stat"><span>最新温度</span><b>${f.last_temp == null ? "—" : f.last_temp + " ℃"}</b></div>
      <div class="stat"><span>门磁</span><b>${f.door_open ? "未关" : "关闭"}</b></div>
      ${f.status === "active" ? `<div class="row-actions"><button class="btn small danger" data-shutdown="${f.id}">停机检修</button></div>` : ""}
    </div>`).join("");
  el("fridgeCards").querySelectorAll("[data-shutdown]").forEach(b => b.onclick = async () => {
    const reason = prompt("停机原因", "设备检修");
    if (reason === null) return;
    try {
      await api(`/api/fridges/${b.dataset.shutdown}/shutdown`, "POST", { reason });
      ok("已停机，请尽快迁移未到期物品");
      await renderOps();
    } catch (e) { err(e); }
  });

  // 停机与迁移
  const shutdowns = await api("/api/shutdowns");
  el("shutdownList").innerHTML = shutdowns.length ? `<table>
    <tr><th>冰箱</th><th>原因</th><th>开始</th><th>剩余在架</th><th>状态</th><th></th></tr>
    ${shutdowns.map(s => `<tr>
      <td>${esc(s.fridge_code)}</td><td>${esc(s.reason || "—")}</td><td>${fmtTime(s.started_at)}</td>
      <td>${s.remaining}</td><td>${s.status === "open" ? "停机中" : "已恢复"}</td>
      <td>${s.status === "open" ? `
        <select id="mig-target-${s.id}">
          ${state.fridges.filter(f => f.status === "active").map(f =>
            `<option value="${f.id}">迁入 ${esc(f.code)}</option>`).join("")}
        </select>
        <button class="btn small primary" data-migrate="${s.id}">批量迁移</button>
        <button class="btn small" data-end="${s.id}">结束停机</button>` : ""}</td>
    </tr>`).join("")}</table>` : `<div class="empty">暂无停机记录</div>`;
  el("shutdownList").querySelectorAll("[data-migrate]").forEach(b => b.onclick = async () => {
    const to = Number(el("mig-target-" + b.dataset.migrate).value);
    try {
      const m = await api(`/api/shutdowns/${b.dataset.migrate}/migrate`, "POST", { to_fridge_id: to });
      ok(`迁移完成：${m.moved} 件迁入 ${m.to_code}（保留原标签）${m.missing ? `，${m.missing} 件缺件已登记异常` : ""}`);
      await renderOps();
    } catch (e) { err(e); }
  });
  el("shutdownList").querySelectorAll("[data-end]").forEach(b => b.onclick = async () => {
    try {
      await api(`/api/shutdowns/${b.dataset.end}/end`, "POST");
      ok("冰箱已恢复运行");
      await renderOps();
    } catch (e) { err(e); }
  });

  const migrations = await api("/api/migrations");
  el("migrationList").innerHTML = migrations.length ? `<table>
    <tr><th>时间</th><th>从</th><th>到</th><th>迁移</th><th>缺件</th><th>操作人</th></tr>
    ${migrations.map(m => `<tr><td>${fmtTime(m.at)}</td><td>${esc(m.from_code)}</td>
      <td>${esc(m.to_code)}</td><td>${m.moved}</td>
      <td>${m.missing ? `<span class="warn">${m.missing}</span>` : 0}</td>
      <td>${esc(m.operator_name)}</td></tr>`).join("")}</table>` : "";

  await renderTempSection();
  await renderExceptions();
  await renderInspections();
}

async function renderTempSection() {
  const picker = el("tempFridgePicker");
  if (!state.tempFridgeId) state.tempFridgeId = state.fridges[0]?.id;
  picker.innerHTML = state.fridges.map(f =>
    `<span class="chip ${f.id === state.tempFridgeId ? "active" : ""}" data-id="${f.id}">${esc(f.code)}</span>`).join("") +
    ` <button class="btn small" id="btnTick">立即巡检</button>`;
  picker.querySelectorAll(".chip").forEach(c => c.onclick = async () => {
    state.tempFridgeId = Number(c.dataset.id); await renderTempSection();
  });
  el("btnTick").onclick = async () => {
    const r = await api("/api/sim/tick", "POST");
    ok(`巡检完成：新到期 ${r.expired} 件，温度告警 ${r.alerts} 条`);
    await renderOps();
  };
  if (!state.tempFridgeId) return;
  const temps = await api(`/api/fridges/${state.tempFridgeId}/temps`);
  const f = state.fridges.find(x => x.id === state.tempFridgeId);
  if (temps.length > 1) {
    const w = 600, h = 120, pad = 8;
    const vals = temps.map(t => t.value);
    const min = Math.min(...vals, f.temp_min) - 0.5, max = Math.max(...vals, f.temp_max) + 0.5;
    const x = i => pad + i * (w - 2 * pad) / (temps.length - 1);
    const y = v => h - pad - (v - min) * (h - 2 * pad) / (max - min);
    const pts = temps.map((t, i) => `${x(i)},${y(t.value)}`).join(" ");
    el("tempChart").innerHTML = `<svg viewBox="0 0 ${w} ${h}">
      <line x1="0" x2="${w}" y1="${y(f.temp_max)}" y2="${y(f.temp_max)}" stroke="#d9634f" stroke-dasharray="4 3"/>
      <line x1="0" x2="${w}" y1="${y(f.temp_min)}" y2="${y(f.temp_min)}" stroke="#8a9299" stroke-dasharray="4 3"/>
      <polyline points="${pts}" fill="none" stroke="#46c99a" stroke-width="2"/>
      ${temps.map((t, i) => t.value > f.temp_max || t.value < f.temp_min
        ? `<circle cx="${x(i)}" cy="${y(t.value)}" r="3.5" fill="#d9634f"/>` : "").join("")}
    </svg>`;
  } else {
    el("tempChart").innerHTML = `<div class="empty">暂无温度数据</div>`;
  }
  el("tempTable").innerHTML = `<table><tr><th>时间</th><th>温度</th><th>状态</th></tr>
    ${temps.slice(-8).reverse().map(t => `<tr><td>${fmtTime(t.at)}</td><td>${t.value} ℃</td>
      <td>${t.value > f.temp_max || t.value < f.temp_min
        ? '<span class="tag bad">超限</span>' : '<span class="tag ok">正常</span>'}</td></tr>`).join("")}
    </table>`;
}

async function renderExceptions() {
  const list = await api("/api/exceptions");
  const canResolve = ["property", "cleaner"].includes(state.me.role);
  el("exceptionList").innerHTML = list.length ? list.map(x => inboxRow({
    key: "x" + x.id,
    dept: EXC_NAMES[x.type] || x.type,
    title: `${x.fridge_code || "—"} ${x.slot_label || ""}`,
    sub: `${fmtTime(x.created_at)} · ${esc(x.note || "—")} · 登记人 ${esc(x.reporter || "系统")}`,
    right: x.status === "open" ? `<span class="warn">待处理</span>` : "已处理",
    expand: `<div class="facts">
        <div>类型：${EXC_NAMES[x.type] || x.type}</div>
        <div>说明：${esc(x.note || "—")}</div>
        ${canResolve && x.status === "open" ? `<div class="row-actions">
          <button class="btn small primary" data-resolve="${x.id}">标记已处理</button></div>` : ""}
      </div>`,
  })).join("") : `<div class="empty">暂无异常</div>`;
  el("exceptionList").querySelectorAll("[data-resolve]").forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    try {
      await api(`/api/exceptions/${b.dataset.resolve}/resolve`, "POST");
      ok("异常已处理");
      await renderExceptions();
    } catch (e) { err(e); }
  });
  bindInboxToggle(el("tab-ops"));

  const opts = state.fridges.map(f => `<option value="${f.id}">${esc(f.code)}</option>`).join("");
  for (const id of ["excFridge", "inspFridge"]) {
    const sel = el(id), cur = sel.value;
    sel.innerHTML = opts;
    if (cur) sel.value = cur;   // 刷新时保留已选项
  }
}

async function renderInspections() {
  const list = await api("/api/inspections");
  el("inspectionList").innerHTML = list.length ? `<table>
    <tr><th>时间</th><th>冰箱</th><th>结果</th><th>备注</th><th>检查人</th></tr>
    ${list.map(i => `<tr><td>${fmtTime(i.at)}</td><td>${esc(i.fridge_code)}</td>
      <td>${i.result === "pass" ? '<span class="tag ok">合格</span>' : '<span class="tag bad">不合格</span>'}</td>
      <td>${esc(i.notes || "—")}</td><td>${esc(i.inspector_name)}</td></tr>`).join("")}</table>`
    : `<div class="empty">暂无检查记录</div>`;
}

function bindOps() {
  el("btnReportExc").onclick = async () => {
    try {
      await api("/api/exceptions", "POST", {
        type: el("excType").value, fridge_id: Number(el("excFridge").value),
        note: el("excNote").value,
      });
      el("excNote").value = "";
      ok("异常已登记");
      await renderExceptions();
    } catch (e) { err(e); }
  };
  el("btnInspect").onclick = async () => {
    try {
      await api(`/api/fridges/${el("inspFridge").value}/inspect`, "POST", {
        result: el("inspResult").value, notes: el("inspNotes").value,
      });
      el("inspNotes").value = "";
      ok("卫生检查已登记");
      await renderInspections();
    } catch (e) { err(e); }
  };
}

/* ---------------- 启动 ---------------- */
bindStore();
bindOps();
init().catch(err);
