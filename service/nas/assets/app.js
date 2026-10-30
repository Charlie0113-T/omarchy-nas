"use strict";
const byId = id => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const human = bytes => { let n = Number(bytes || 0), units = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return `${n.toFixed(1)} ${units[i]}`; };
const quote = value => "'" + String(value).replaceAll("'", "'\\''") + "'";
const MOUNTS = "/srv/omarchy-nas/";
const LANGUAGES = ["zh-CN", "en"];
const LANGUAGE_KEY = "omarchy-nas-language";

// Every word on the page, in both languages. A function takes the values the sentence needs.
const STRINGS = {
  "zh-CN": {
    pageTitle: "Omarchy NAS · 存储总览", location: "位置", thisComputer: "本机", title: "存储总览",
    loading: "读取中", reading: "正在读取本机状态…", notReadYet: "等待首次读取", updated: time => `更新于 ${time}`, refresh: "刷新",
    demo: "演示模式：页面上的设备、容量和地址都是示例数据，不代表这台电脑已经接通。",
    shares: "共享", connectTitle: "在其他设备上连接", account: "账号", password: "密码", passwordValue: "设置时的 NAS 密码", change: "修改", homeNetwork: "家庭网络",
    stepMac: "Finder → 前往 → 连接服务器", stepIpad: "文件 → ⋯ → 连接服务器 → 注册用户", stepWindows: "地址栏输入", windowsPlaceholder: "\\\\主机IP\\共享名",
    disks: "磁盘", diagnostics: "诊断命令", colVolume: "卷", colFs: "文件系统", colTransport: "接口", colSize: "容量", colState: "状态",
    footer: "独立社区工具，与 Omarchy 官方无关。管理页只在这台电脑上打开。",
    runInTerminal: "在这台电脑的终端里运行", close: "关闭", copyCommand: "复制命令", dialogNote: "磁盘和服务操作在终端里确认，状态页本身不申请管理员权限。",
    copied: "已复制", copiedAddress: name => `已复制 ${name} 的地址`, copyTitle: "复制内容", copyText: "可选中下面的内容手动复制。", copyLabel: "内容",
    online: "在线", paused: "已暂停", offline: "离线", fine: "正常", notSetUp: "未设置", serviceStopped: "服务未运行", attention: "需要处理", noShares: "未共享", unknown: "状态未知",
    from: disk => `来自 ${disk}`, readOnly: "只读", readWrite: "可读写", noAddress: "等待家庭网络地址", copyAddress: "复制地址", pause: "暂停", resume: "恢复",
    sharesSub: (n, size) => `已登记 ${n} 个卷 · 共 ${size}（卷容量，不是剩余空间）`, noVolumes: "还没有登记卷", onlineUnit: "在线", sharesUnit: "个共享",
    sharesEmpty: "还没有共享。接入一块数据盘后，这里会列出它的地址。", setupCommand: "设置命令", addCommand: "添加命令", startCommand: "启动命令",
    disksSub: (n, shared) => `发现 ${n} 个卷 · ${shared} 个已共享`, noDisksFound: "没有发现卷", disksEmpty: "暂无可显示的磁盘。检查硬盘柜的电源和 USB 连接，或运行诊断。",
    unrecognised: "未识别", unknownTransport: "未知", sharedAs: name => `共享为 ${name}`, systemDisk: "系统盘，已排除", cannotShare: "不能共享", shareDisk: "接入共享",
    list: names => names.join("、"), nothingShared: "还没有共享。",
    oneOnline: name => `${name} 在线，家里的设备可以用下面的地址连接。`, allOnline: n => `${n} 个共享都在线，家里的设备可以用下面的地址连接。`,
    mixed: (online, held) => `${online ? online + " 在线" : "没有在线的共享"}${held ? "，" + held + " 已暂停" : ""}。`,
    notConfigured: "还没有设置共享。把硬盘柜接到这台电脑，再运行设置向导。", serviceDown: "共享服务没有运行，家里的设备暂时连不上。",
    downFinding: (names, count, error) => `${names} 离线${error ? "：" + error : "。"}`, setUpNoShares: "已设置，还没有共享。接入一块数据盘后，家里的设备就能连接。",
    readFailed: (message, time) => `读不到本机状态（${message}）。` + (time ? `上次更新于 ${time}，下面的数据可能已经过期。` : "还没有读到任何数据。"), retry: "重试",
    doctorTitle: "诊断设备与依赖", doctorText: "输出设备识别结果、挂载限制和依赖情况，不会改动磁盘。",
    addTitle: "添加磁盘", addText: "向导会先显示具体设备和共享模式，再要求确认。默认只读；不会格式化磁盘。",
    setupTitle: "开始设置", setupText: "在 omarchy-nas 的 service 目录里运行（先用 python3 build.py 生成 dist/omarchy-nas.pyz）。向导会先显示具体设备和共享模式，再要求确认。",
    startTitle: "启动共享服务", startText: "重新启用服务和自动启动，并挂载登记过的卷。",
    passwdTitle: "修改 NAS 密码", passwdText: "只改 omarchy-nas 自己的 Samba 账号密码，不改 Linux 登录密码。改完后，各设备下次连接时输入新密码。",
    pauseTitle: name => `暂停并卸载 ${name}`, pauseText: "请先结束文件传输。此操作会短暂断开 omarchy-nas 的 SMB 连接，再正常卸载这个卷。若仍在占用，会报告错误。",
    resumeTitle: name => `恢复共享 ${name}`, resumeText: "恢复登记过的磁盘，完成挂载核验后再提供共享。",
    shareDiskTitle: "接入数据盘", shareDiskText: "在终端选择共享名并确认。默认只读；不会格式化磁盘。"
  },
  en: {
    pageTitle: "Omarchy NAS · Storage", location: "Location", thisComputer: "This computer", title: "Storage",
    loading: "Loading", reading: "Reading this computer's status…", notReadYet: "Not read yet", updated: time => `Updated ${time}`, refresh: "Refresh",
    demo: "Demo mode: the disks, sizes and addresses on this page are made up; this computer isn't sharing anything.",
    shares: "Shares", connectTitle: "Connect from other devices", account: "Account", password: "Password", passwordValue: "Set during setup", change: "Change", homeNetwork: "Home network",
    stepMac: "Finder → Go → Connect to Server", stepIpad: "Files → ⋯ → Connect to Server → Registered User", stepWindows: "In the address bar:", windowsPlaceholder: "\\\\host-IP\\share-name",
    disks: "Disks", diagnostics: "Diagnostics", colVolume: "Volume", colFs: "File system", colTransport: "Connection", colSize: "Size", colState: "Status",
    footer: "A community tool, not affiliated with Omarchy. This page only opens on this computer.",
    runInTerminal: "Run in a terminal on this computer", close: "Close", copyCommand: "Copy command", dialogNote: "Disk and service changes are confirmed in the terminal; this page never asks for administrator rights.",
    copied: "Copied", copiedAddress: name => `Copied the address of ${name}`, copyTitle: "Copy", copyText: "Select the text below and copy it by hand.", copyLabel: "Text",
    online: "Online", paused: "Paused", offline: "Offline", fine: "All good", notSetUp: "Not set up", serviceStopped: "Service stopped", attention: "Needs attention", noShares: "No shares", unknown: "Unknown",
    from: disk => `from ${disk}`, readOnly: "read-only", readWrite: "read-write", noAddress: "Waiting for a home network address", copyAddress: "Copy address", pause: "Pause", resume: "Resume",
    sharesSub: (n, size) => `${n} registered ${n === 1 ? "volume" : "volumes"} · ${size} in total (volume size, not free space)`, noVolumes: "No registered volumes yet", onlineUnit: "online", sharesUnit: "shares",
    sharesEmpty: "No shares yet. Once you add a data disk, its address shows here.", setupCommand: "Setup command", addCommand: "Add command", startCommand: "Start command",
    disksSub: (n, shared) => `${n} ${n === 1 ? "volume" : "volumes"} found · ${shared} shared`, noDisksFound: "No volumes found", disksEmpty: "No disks to show. Check the enclosure's power and USB cable, or run diagnostics.",
    unrecognised: "unknown", unknownTransport: "unknown", sharedAs: name => `shared as ${name}`, systemDisk: "system disk, excluded", cannotShare: "can't be shared", shareDisk: "Share it",
    list: names => names.join(", "), nothingShared: "No shares yet.",
    oneOnline: name => `${name} is online; your devices can connect with the address below.`, allOnline: n => `All ${n} shares are online; your devices can connect with the addresses below.`,
    mixed: (online, held) => `${online ? online + " online" : "No share is online"}${held ? ", " + held + " paused" : ""}.`,
    notConfigured: "Nothing is set up yet. Connect the drive enclosure to this computer, then run the setup wizard.", serviceDown: "The sharing service isn't running, so other devices can't connect right now.",
    downFinding: (names, count, error) => `${names} ${count === 1 ? "is" : "are"} offline${error ? ": " + error : "."}`, setUpNoShares: "Set up, but nothing is shared yet. Add a data disk and your devices can connect.",
    readFailed: (message, time) => `Couldn't read this computer's status (${message}). ` + (time ? `Last updated ${time}; the data below may be out of date.` : "No data yet."), retry: "Retry",
    doctorTitle: "Check disks and dependencies", doctorText: "Prints what was detected, the mount limits and any missing dependencies. It doesn't change any disk.",
    addTitle: "Add a disk", addText: "The wizard shows the exact device and share mode before asking you to confirm. Read-only by default; it never formats a disk.",
    setupTitle: "Set up", setupText: "Run it in omarchy-nas's service folder (build dist/omarchy-nas.pyz first with python3 build.py). The wizard shows the exact device and share mode before asking you to confirm.",
    startTitle: "Start sharing", startText: "Turns the services and autostart back on and mounts the registered volumes.",
    passwdTitle: "Change the NAS password", passwdText: "Changes only omarchy-nas's own Samba password, not your Linux login. Devices ask for the new one the next time they connect.",
    pauseTitle: name => `Pause and unmount ${name}`, pauseText: "Finish any file transfers first. This briefly drops omarchy-nas's SMB connections, then unmounts the volume cleanly. If it's still in use, it reports an error.",
    resumeTitle: name => `Resume ${name}`, resumeText: "Checks and mounts the registered disk again before sharing it.",
    shareDiskTitle: "Share this disk", shareDiskText: "Choose a share name in the terminal and confirm. Read-only by default; it never formats a disk."
  }
};

// omarchy-nas words its fixed reasons in Chinese; these are their English forms. Anything else it
// reports (a mount error with its path, say) is shown as written. tests/test_nas.py checks that
// every fixed phrase in core.py and web.py is here.
const BACKEND_EN = {
  "系统盘或含活动系统/交换分区": "system disk, or holds the running system or swap",
  "首版仅接入 USB / 可移除磁盘": "only USB or removable disks are supported",
  "不是可直接接入的独立文件系统": "not a standalone file system that can be shared directly",
  "文件系统暂不支持": "file system not supported yet",
  "文件系统暂不支持或尚未识别": "file system not supported or not recognised yet",
  "没有稳定 UUID / PARTUUID": "no stable UUID / PARTUUID",
  "已由其他程序挂载，请先正常卸载": "mounted by another program; unmount it there first",
  "设备有多个父节点，需人工检查": "the device has several parents and needs a manual check",
  "共享服务未运行": "the sharing service isn't running",
  "等待后台核验 / 状态采样过期": "waiting for the background check, or the status sample is out of date"
};

function chosenLanguage() {
  const asked = decodeURIComponent(location.hash.slice(1));
  if (LANGUAGES.includes(asked)) return asked;
  try { const saved = localStorage.getItem(LANGUAGE_KEY); if (LANGUAGES.includes(saved)) return saved; } catch {}
  const first = (navigator.languages && navigator.languages[0]) || navigator.language || "en";
  return /^zh\b/i.test(first) ? "zh-CN" : "en";
}
let language = chosenLanguage();
const tr = (key, ...args) => { const v = STRINGS[language][key] ?? STRINGS["zh-CN"][key]; return typeof v === "function" ? v(...args) : v; };
const backend = text => !text || language !== "en" ? text : String(text).split("；")
  .map(part => BACKEND_EN[part] ?? (part.startsWith("文件系统暂不支持：") ? `${BACKEND_EN["文件系统暂不支持"]}: ${part.slice(9)}` : part)).join("; ");
const clock = seconds => new Date(seconds * 1000).toLocaleTimeString(language === "en" ? "en-GB" : "zh-CN", { hour12: false });

// The words written into the page itself, in the chosen language.
function applyLanguage() {
  document.documentElement.lang = language;
  document.title = tr("pageTitle");
  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = tr(node.dataset.i18n);
  for (const node of document.querySelectorAll("[data-i18n-label]")) node.setAttribute("aria-label", tr(node.dataset.i18nLabel));
  byId("windows-path").textContent = tr("windowsPlaceholder");
  const other = language === "en" ? "zh-CN" : "en", toggle = byId("language");
  toggle.textContent = other === "en" ? "English" : "中文"; toggle.lang = other;
}

let busy = false, toastTimer, lastGood = null, lastError = null, primaryAction = null;
function toast(message) { byId("toast").textContent = message; byId("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => byId("toast").hidden = true, 2200); }
async function copy(value, done = tr("copied")) { try { await navigator.clipboard.writeText(value); toast(done); } catch { command(tr("copyTitle"), tr("copyText"), value, tr("copyLabel")); } }
function button(text, action, cls = "text-button") { const b = el("button", cls, text); b.type = "button"; b.addEventListener("click", action); return b; }
function command(title, description, value, label = tr("runInTerminal")) {
  byId("dialog-title").textContent = title; byId("dialog-text").textContent = description;
  document.querySelector("label[for=command]").textContent = label; byId("command").value = value;
  if (!byId("command-dialog").open) byId("command-dialog").showModal();
}
function setPill(text, tone) { const p = byId("overall"); p.className = "pill " + tone; p.textContent = text; }
function setPrimary(label, action) { const b = byId("primary"); primaryAction = action || null; b.hidden = !action; if (action) b.textContent = label; }

// The commands the page hands to a terminal. The wording of each one says what it does and what it leaves alone.
const doctor = data => () => command(tr("doctorTitle"), tr("doctorText"), data.configured ? "omarchy-nas doctor" : "python3 dist/omarchy-nas.pyz doctor");
const setup = data => () => data.configured
  ? command(tr("addTitle"), tr("addText"), "sudo omarchy-nas add")
  : command(tr("setupTitle"), tr("setupText"), "python3 dist/omarchy-nas.pyz setup");
const start = () => command(tr("startTitle"), tr("startText"), "sudo omarchy-nas start");
const passwd = () => command(tr("passwdTitle"), tr("passwdText"), "sudo omarchy-nas passwd");

const paused = s => !s.online && (s.state === "paused" || s.enabled === false);
const sourceOf = (s, devices) => devices.find(d => (d.mounts || []).includes(MOUNTS + s.name));

// One share: its name and state, the disk it comes from, its address and what can be done with it.
function shareRow(s, devices) {
  const row = el("div", "share-row"), identity = el("div"), end = el("div", "share-end");
  const [label, tone] = s.online ? [tr("online"), "positive"] : paused(s) ? [tr("paused"), "neutral"] : [tr("offline"), "negative"];
  const name = el("div", "share-name", s.name); name.append(el("span", "pill " + tone, label));
  const src = sourceOf(s, devices), from = src ? [src.label || src.model, src.path].filter(Boolean).join(" · ") : null;
  identity.append(name, el("div", "share-source", [from && tr("from", from), String(s.fs || "").toUpperCase(), human(s.size), s.mode === "ro" ? tr("readOnly") : tr("readWrite")].filter(Boolean).join(" · ")));
  identity.append(s.url ? el("code", "address", s.url) : el("div", "address none", tr("noAddress")));
  if (s.error && !s.online) identity.append(el("div", "share-error", backend(s.error)));
  if (s.url) end.append(button(tr("copyAddress"), () => copy(s.url, tr("copiedAddress", s.name)), "button ghost"));
  end.append(button(s.enabled ? tr("pause") : tr("resume"), () => s.enabled
    ? command(tr("pauseTitle", s.name), tr("pauseText"), `sudo omarchy-nas pause ${quote(s.name)}`)
    : command(tr("resumeTitle", s.name), tr("resumeText"), `sudo omarchy-nas resume ${quote(s.name)}`)));
  row.append(identity, end); return row;
}

function renderShares(data, shares, devices) {
  const online = shares.filter(s => s.online).length, total = shares.reduce((sum, s) => sum + Number(s.size || 0), 0);
  byId("shares-sub").textContent = shares.length ? tr("sharesSub", shares.length, human(total)) : tr("noVolumes");
  byId("online-count").textContent = shares.length ? `${online}/${shares.length}` : "0";
  byId("online-unit").textContent = shares.length ? tr("onlineUnit") : tr("sharesUnit");
  const list = byId("shares");
  if (!shares.length) { const empty = el("p", "empty", tr("sharesEmpty")); empty.append(button(data.configured ? tr("addCommand") : tr("setupCommand"), setup(data))); list.replaceChildren(empty); }
  else list.replaceChildren(...shares.map(s => shareRow(s, devices)));
}

function renderDisks(data, devices) {
  const rows = devices.filter(d => d.fs || d.type === "disk"), body = byId("disks");
  const shared = rows.filter(d => (d.mounts || []).some(m => m.startsWith(MOUNTS))).length;
  byId("disks-sub").textContent = rows.length ? tr("disksSub", rows.length, shared) : tr("noDisksFound");
  if (!rows.length) { const row = el("tr"), td = el("td", "", tr("disksEmpty")); td.colSpan = 5; row.append(td); body.replaceChildren(row); return; }
  body.replaceChildren(...rows.map(d => {
    const row = el("tr"), info = el("td"), state = el("td"), fs = (d.fs || tr("unrecognised")).toUpperCase(), transport = (d.transport || tr("unknownTransport")).toUpperCase();
    const detail = el("div", "disk-detail", [d.path, d.label && d.model].filter(Boolean).join(" · "));
    detail.append(el("span", "phone-only", ` · ${fs} · ${transport}`));
    info.append(el("div", "disk-name", d.label || d.model || d.path), detail);
    const mount = (d.mounts || []).find(m => m.startsWith(MOUNTS)), system = (d.reason || "").includes("系统盘");
    if (mount) state.append(el("span", "disk-state", tr("sharedAs", mount.slice(MOUNTS.length))));
    else if (d.eligible) state.append(button(tr("shareDisk"), () => command(tr("shareDiskTitle"), tr("shareDiskText"), data.configured ? `sudo omarchy-nas add --device ${quote(d.path)}` : `python3 dist/omarchy-nas.pyz setup --device ${quote(d.path)}`), "button ghost"));
    else { state.append(el("span", "disk-state", system ? tr("systemDisk") : tr("cannotShare"))); if (!system && d.reason) info.append(el("div", "disk-detail", backend(d.reason))); }
    row.append(info, el("td", "fs", fs), el("td", "transport", transport), el("td", "num", human(d.size)), state);
    return row;
  }));
}

function renderConnect(data, shares) {
  byId("account").textContent = data.configured && data.user ? data.user : "—";
  byId("network").textContent = data.network ? [data.network, data.interface].filter(Boolean).join(" · ") : "—";
  const first = shares.find(s => s.url), m = first && /^smb:\/\/([^/]+)\/(.+)$/.exec(first.url);
  byId("windows-path").textContent = m ? `\\\\${m[1]}\\${m[2]}` : tr("windowsPlaceholder");
}

// The status pill, the one sentence under the title and the one primary action all come from the same fact.
// Returns whether that sentence is omarchy-nas's first error, so it isn't repeated below.
function renderSummary(data, shares, errors) {
  const online = shares.filter(s => s.online), down = shares.filter(s => !s.online && !paused(s)), held = shares.filter(paused);
  const names = list => tr("list", list.map(s => s.name));
  const state = !shares.length ? tr("nothingShared")
    : online.length === shares.length ? (shares.length === 1 ? tr("oneOnline", shares[0].name) : tr("allOnline", shares.length))
    : tr("mixed", online.length ? names(online) : "", held.length ? names(held) : "");
  let pill = [tr("fine"), "positive"], finding = state, primary = null, usedError = false;
  if (!data.configured) { pill = [tr("notSetUp"), "neutral"]; finding = tr("notConfigured"); primary = [tr("setupCommand"), setup(data)]; }
  else if (!data.service_active) { pill = [tr("serviceStopped"), "negative"]; finding = tr("serviceDown"); primary = [tr("startCommand"), start]; }
  else if (down.length) { pill = [tr("attention"), "negative"]; finding = tr("downFinding", names(down), down.length, backend(down[0].error)); primary = [tr("diagnostics"), doctor(data)]; }
  else if (errors.length) { pill = [tr("attention"), "negative"]; finding = backend(errors[0]); usedError = true; primary = [tr("diagnostics"), doctor(data)]; }
  else if (!shares.length) { pill = [tr("noShares"), "neutral"]; finding = tr("setUpNoShares"); primary = [tr("addCommand"), setup(data)]; }
  setPill(...pill); byId("finding").textContent = finding; setPrimary(...(primary || []));
  return usedError;
}

function render(data) {
  lastGood = data; lastError = null;
  const shares = data.shares || [], devices = data.devices || [], errors = data.errors || [];
  byId("demo-banner").hidden = !data.demo;
  byId("host").textContent = data.hostname || tr("thisComputer");
  byId("version").textContent = "v" + data.version;
  byId("updated").textContent = tr("updated", clock(data.time));
  renderShares(data, shares, devices); renderDisks(data, devices); renderConnect(data, shares);
  const shown = renderSummary(data, shares, errors);
  byId("errors").replaceChildren(...errors.slice(shown ? 1 : 0).map(e => el("p", "notice error", backend(e))));
}

// A failed read keeps the last good data on screen and says how old it is.
function failed(error) {
  lastError = error;
  setPill(tr("unknown"), "neutral"); setPrimary();
  byId("finding").replaceChildren(document.createTextNode(tr("readFailed", error.message, lastGood && clock(lastGood.time))), button(tr("retry"), refresh));
}

async function refresh() {
  if (busy) return; busy = true; byId("refresh").disabled = true;
  try { const r = await fetch("/api/status", { cache: "no-store" }); if (!r.ok) throw new Error(`HTTP ${r.status}`); render(await r.json()); }
  catch (e) { failed(e); }
  finally { busy = false; byId("refresh").disabled = false; }
}

// Switching language redraws the page from the data already on screen, without a new read.
function switchLanguage() {
  language = language === "en" ? "zh-CN" : "en";
  try { localStorage.setItem(LANGUAGE_KEY, language); } catch {}
  if (location.hash) history.replaceState(null, "", location.pathname);
  applyLanguage();
  if (lastGood) render(lastGood);
  if (lastError) failed(lastError);
}

applyLanguage();
byId("refresh").addEventListener("click", refresh);
byId("primary").addEventListener("click", () => primaryAction && primaryAction());
byId("doctor").addEventListener("click", () => doctor(lastGood || {})());
byId("passwd").addEventListener("click", passwd);
byId("copy-command").addEventListener("click", () => copy(byId("command").value));
byId("language").addEventListener("click", switchLanguage);
refresh(); setInterval(() => { if (!document.hidden && !byId("command-dialog").open) refresh(); }, 10000);
