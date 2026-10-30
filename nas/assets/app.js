"use strict";
const byId = id => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const human = bytes => { let n = Number(bytes || 0), units = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return `${n.toFixed(1)} ${units[i]}`; };
const quote = value => "'" + String(value).replaceAll("'", "'\\''") + "'";
const clock = seconds => new Date(seconds * 1000).toLocaleTimeString("zh-CN", { hour12: false });
const MOUNTS = "/srv/omarchy-nas/";
let busy = false, toastTimer, lastGood = null, primaryAction = null;

function toast(message) { byId("toast").textContent = message; byId("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => byId("toast").hidden = true, 2200); }
async function copy(value, done = "已复制") { try { await navigator.clipboard.writeText(value); toast(done); } catch { command("复制内容", "可选中下面的内容手动复制。", value, "内容"); } }
function button(text, action, cls = "text-button") { const b = el("button", cls, text); b.type = "button"; b.addEventListener("click", action); return b; }
function command(title, description, value, label = "在这台电脑的终端里运行") {
  byId("dialog-title").textContent = title; byId("dialog-text").textContent = description;
  document.querySelector("label[for=command]").textContent = label; byId("command").value = value;
  if (!byId("command-dialog").open) byId("command-dialog").showModal();
}
function setPill(text, tone) { const p = byId("overall"); p.className = "pill " + tone; p.textContent = text; }
function setPrimary(label, action) { const b = byId("primary"); primaryAction = action || null; b.hidden = !action; if (action) b.textContent = label; }

// The commands the page hands to a terminal. The wording of each one says what it does and what it leaves alone.
const doctor = data => () => command("诊断设备与依赖", "输出设备识别结果、挂载限制和依赖情况，不会改动磁盘。", data.configured ? "omarchy-nas doctor" : "python3 dist/omarchy-nas.pyz doctor");
const setup = data => () => data.configured
  ? command("添加磁盘", "向导会先显示具体设备和共享模式，再要求确认。默认只读；不会格式化磁盘。", "sudo omarchy-nas add")
  : command("开始设置", "在 omarchy-nas 目录里运行（先用 python3 build.py 生成 dist/omarchy-nas.pyz）。向导会先显示具体设备和共享模式，再要求确认。", "python3 dist/omarchy-nas.pyz setup");
const start = () => command("启动共享服务", "重新启用服务和自动启动，并挂载登记过的卷。", "sudo omarchy-nas start");
const passwd = () => command("修改 NAS 密码", "只改 omarchy-nas 自己的 Samba 账号密码，不改 Linux 登录密码。改完后，各设备下次连接时输入新密码。", "sudo omarchy-nas passwd");

const paused = s => !s.online && (s.state === "paused" || s.enabled === false);
const sourceOf = (s, devices) => devices.find(d => (d.mounts || []).includes(MOUNTS + s.name));

// One share: its name and state, the disk it comes from, its address and what can be done with it.
function shareRow(s, devices) {
  const row = el("div", "share-row"), identity = el("div"), end = el("div", "share-end");
  const [label, tone] = s.online ? ["在线", "positive"] : paused(s) ? ["已暂停", "neutral"] : ["离线", "negative"];
  const name = el("div", "share-name", s.name); name.append(el("span", "pill " + tone, label));
  const src = sourceOf(s, devices), from = src ? [src.label || src.model, src.path].filter(Boolean).join(" · ") : null;
  identity.append(name, el("div", "share-source", [from && "来自 " + from, String(s.fs || "").toUpperCase(), human(s.size), s.mode === "ro" ? "只读" : "可读写"].filter(Boolean).join(" · ")));
  identity.append(s.url ? el("code", "address", s.url) : el("div", "address none", "等待家庭网络地址"));
  if (s.error && !s.online) identity.append(el("div", "share-error", s.error));
  if (s.url) end.append(button("复制地址", () => copy(s.url, `已复制 ${s.name} 的地址`), "button ghost"));
  end.append(button(s.enabled ? "暂停" : "恢复", () => s.enabled
    ? command(`暂停并卸载 ${s.name}`, "请先结束文件传输。此操作会短暂断开 omarchy-nas 的 SMB 连接，再正常卸载这个卷。若仍在占用，会报告错误。", `sudo omarchy-nas pause ${quote(s.name)}`)
    : command(`恢复共享 ${s.name}`, "恢复登记过的磁盘，完成挂载核验后再提供共享。", `sudo omarchy-nas resume ${quote(s.name)}`)));
  row.append(identity, end); return row;
}

function renderShares(data, shares, devices) {
  const online = shares.filter(s => s.online).length, total = shares.reduce((sum, s) => sum + Number(s.size || 0), 0);
  byId("shares-sub").textContent = shares.length ? `已登记 ${shares.length} 个卷 · 共 ${human(total)}（卷容量，不是剩余空间）` : "还没有登记卷";
  byId("online-count").textContent = shares.length ? `${online}/${shares.length}` : "0";
  byId("online-unit").textContent = shares.length ? "在线" : "个共享";
  const list = byId("shares");
  if (!shares.length) { const empty = el("p", "empty", "还没有共享。接入一块数据盘后，这里会列出它的地址。"); empty.append(button(data.configured ? "添加命令" : "设置命令", setup(data))); list.replaceChildren(empty); }
  else list.replaceChildren(...shares.map(s => shareRow(s, devices)));
}

function renderDisks(data, devices) {
  const rows = devices.filter(d => d.fs || d.type === "disk"), body = byId("disks");
  const shared = rows.filter(d => (d.mounts || []).some(m => m.startsWith(MOUNTS))).length;
  byId("disks-sub").textContent = rows.length ? `发现 ${rows.length} 个卷 · ${shared} 个已共享` : "没有发现卷";
  if (!rows.length) { const tr = el("tr"), td = el("td", "", "暂无可显示的磁盘。检查硬盘柜的电源和 USB 连接，或运行诊断。"); td.colSpan = 5; tr.append(td); body.replaceChildren(tr); return; }
  body.replaceChildren(...rows.map(d => {
    const tr = el("tr"), info = el("td"), state = el("td"), fs = (d.fs || "未识别").toUpperCase(), transport = (d.transport || "未知").toUpperCase();
    const detail = el("div", "disk-detail", [d.path, d.label && d.model].filter(Boolean).join(" · "));
    detail.append(el("span", "phone-only", ` · ${fs} · ${transport}`));
    info.append(el("div", "disk-name", d.label || d.model || d.path), detail);
    const mount = (d.mounts || []).find(m => m.startsWith(MOUNTS)), system = (d.reason || "").includes("系统盘");
    if (mount) state.append(el("span", "disk-state", "共享为 " + mount.slice(MOUNTS.length)));
    else if (d.eligible) state.append(button("接入共享", () => command("接入数据盘", "在终端选择共享名并确认。默认只读；不会格式化磁盘。", data.configured ? `sudo omarchy-nas add --device ${quote(d.path)}` : `python3 dist/omarchy-nas.pyz setup --device ${quote(d.path)}`), "button ghost"));
    else { state.append(el("span", "disk-state", system ? "系统盘，已排除" : "不能共享")); if (!system && d.reason) info.append(el("div", "disk-detail", d.reason)); }
    tr.append(info, el("td", "fs", fs), el("td", "transport", transport), el("td", "num", human(d.size)), state);
    return tr;
  }));
}

function renderConnect(data, shares) {
  byId("account").textContent = data.configured && data.user ? data.user : "—";
  byId("network").textContent = data.network ? [data.network, data.interface].filter(Boolean).join(" · ") : "—";
  const first = shares.find(s => s.url), m = first && /^smb:\/\/([^/]+)\/(.+)$/.exec(first.url);
  byId("windows-path").textContent = m ? `\\\\${m[1]}\\${m[2]}` : "\\\\主机IP\\共享名";
}

// The status pill, the one sentence under the title and the one primary action all come from the same fact.
function renderSummary(data, shares, errors) {
  const online = shares.filter(s => s.online), down = shares.filter(s => !s.online && !paused(s)), held = shares.filter(paused);
  const state = !shares.length ? "还没有共享。"
    : online.length === shares.length ? (shares.length === 1 ? `${shares[0].name} 在线，家里的设备可以用下面的地址连接。` : `${shares.length} 个共享都在线，家里的设备可以用下面的地址连接。`)
    : `${online.length ? online.map(s => s.name).join("、") + " 在线" : "没有在线的共享"}${held.length ? "，" + held.map(s => s.name).join("、") + " 已暂停" : ""}。`;
  let pill = ["正常", "positive"], finding = state, primary = null;
  if (!data.configured) { pill = ["未设置", "neutral"]; finding = "还没有设置共享。把硬盘柜接到这台电脑，再运行设置向导。"; primary = ["设置命令", setup(data)]; }
  else if (!data.service_active) { pill = ["服务未运行", "negative"]; finding = "共享服务没有运行，家里的设备暂时连不上。"; primary = ["启动命令", start]; }
  else if (down.length) { pill = ["需要处理", "negative"]; finding = `${down.map(s => s.name).join("、")} 离线${down[0].error ? "：" + down[0].error : "。"}`; primary = ["诊断命令", doctor(data)]; }
  else if (errors.length) { pill = ["需要处理", "negative"]; finding = errors[0]; primary = ["诊断命令", doctor(data)]; }
  else if (!shares.length) { pill = ["未共享", "neutral"]; finding = "已设置，还没有共享。接入一块数据盘后，家里的设备就能连接。"; primary = ["添加命令", setup(data)]; }
  setPill(...pill); byId("finding").textContent = finding; setPrimary(...(primary || []));
  return finding === errors[0];
}

function render(data) {
  lastGood = data;
  const shares = data.shares || [], devices = data.devices || [], errors = data.errors || [];
  byId("demo-banner").hidden = !data.demo;
  byId("host").textContent = data.hostname || "本机";
  byId("version").textContent = "v" + data.version;
  byId("updated").textContent = "更新于 " + clock(data.time);
  renderShares(data, shares, devices); renderDisks(data, devices); renderConnect(data, shares);
  // The first error is not repeated below when it is already the sentence under the title.
  const shown = renderSummary(data, shares, errors);
  byId("errors").replaceChildren(...errors.slice(shown ? 1 : 0).map(e => el("p", "notice error", e)));
}

// A failed read keeps the last good data on screen and says how old it is.
function failed(error) {
  setPill("状态未知", "neutral"); setPrimary();
  const finding = byId("finding"), when = lastGood ? `上次更新于 ${clock(lastGood.time)}，下面的数据可能已经过期。` : "还没有读到任何数据。";
  finding.replaceChildren(document.createTextNode(`读不到本机状态（${error.message}）。${when}`), button("重试", refresh));
}

async function refresh() {
  if (busy) return; busy = true; byId("refresh").disabled = true;
  try { const r = await fetch("/api/status", { cache: "no-store" }); if (!r.ok) throw new Error(`HTTP ${r.status}`); render(await r.json()); }
  catch (e) { failed(e); }
  finally { busy = false; byId("refresh").disabled = false; }
}

byId("refresh").addEventListener("click", refresh);
byId("primary").addEventListener("click", () => primaryAction && primaryAction());
byId("doctor").addEventListener("click", () => doctor(lastGood || {})());
byId("passwd").addEventListener("click", passwd);
byId("copy-command").addEventListener("click", () => copy(byId("command").value));
refresh(); setInterval(() => { if (!document.hidden && !byId("command-dialog").open) refresh(); }, 10000);
