"use strict";
const byId = id => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const human = bytes => { let n = Number(bytes || 0), units = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return `${n.toFixed(1)} ${units[i]}`; };
const quote = value => "'" + String(value).replaceAll("'", "'\\''") + "'";
let busy = false, toastTimer;
function toast(message) { byId("toast").textContent = message; byId("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => byId("toast").hidden = true, 2200); }
async function copy(value) { try { await navigator.clipboard.writeText(value); toast("已复制"); } catch { command("复制内容", "可选中下面的内容手动复制。", value); } }
function button(text, action, cls="text-button") { const b = el("button", cls, text); b.addEventListener("click", action); return b; }
function command(title, description, value) { byId("dialog-title").textContent = title; byId("dialog-text").textContent = description; byId("command").value = value; if (!byId("command-dialog").open) byId("command-dialog").showModal(); }
function shareCard(s) {
  const card = el("article", "share"), top = el("div", "share-top");
  top.append(el("span", "drive-icon", "•••"), el("span", "pill" + (s.online ? "" : " warn"), s.online ? "已连接" : s.state === "paused" ? "已暂停" : "未就绪"));
  card.append(top, el("h3", "", s.name), el("div", "meta", `${String(s.fs).toUpperCase()} · ${human(s.size)} · ${s.mode === "ro" ? "只读" : "可读写"}`));
  card.append(el("code", "share-url", s.url || "等待家庭网络地址"));
  if (s.error) card.append(el("p", "share-error", s.error));
  const actions = el("div", "actions");
  if (s.url) actions.append(button("复制连接地址 ↗", () => copy(s.url)));
  actions.append(button(s.enabled ? "暂停共享" : "恢复共享", () => command(s.enabled ? "暂停并卸载" : "恢复共享", s.enabled ? "请先结束文件传输。此操作会短暂断开本插件的 SMB 连接，再正常卸载这个卷。若仍在占用，会报告错误。" : "恢复登记过的磁盘，完成挂载核验后再提供共享。", `sudo omarchy-nas ${s.enabled ? "pause" : "resume"} ${quote(s.name)}`)));
  card.append(actions); return card;
}
function render(data) {
  byId("demo-banner").hidden = !data.demo;
  byId("host").textContent = `${data.hostname} / ${data.configured ? `访问账号 ${data.user}` : "尚未初始化"}`;
  byId("version").textContent = "v" + data.version;
  byId("count").textContent = `${data.shares.filter(s=>s.online).length} / ${data.shares.length}`;
  byId("capacity").textContent = human(data.shares.reduce((sum,s)=>sum+s.size,0));
  byId("service-state").textContent = data.service_active ? "共享服务运行中" : "共享服务未启动";
  byId("errors").replaceChildren(...data.errors.map(e=>el("div", "notice", e)));
  const shares = byId("shares"); shares.replaceChildren();
  if (!data.shares.length) { const empty = el("div", "empty"); empty.append(el("strong", "", "接入第一块数据盘"), el("p", "", "将 USB 硬盘柜接到主机，然后运行设置向导。选择磁盘、家庭网络和访问密码。"), button("查看设置命令 →", ()=>command("开始设置", "在下载文件的目录运行。向导会先显示具体设备和共享模式，再要求确认。", data.configured ? "sudo omarchy-nas add" : "python3 ~/Downloads/omarchy-nas.pyz setup"))); shares.append(empty); }
  else data.shares.forEach(s=>shares.append(shareCard(s)));
  const devices = byId("devices"); devices.replaceChildren();
  const rows = data.devices.filter(d=>d.fs || d.type === "disk");
  if (!rows.length) devices.append(el("div", "empty", "暂无可显示的磁盘。检查硬盘柜的电源和 USB 连接，或运行诊断。"));
  for (const d of rows) {
    const row = el("div", "device"), info = el("div");
    info.append(el("div", "device-name", d.label || d.model || d.path), el("div", "device-detail", `${d.path} · ${(d.fs || "未识别文件系统").toUpperCase()} · ${(d.transport || "未知接口").toUpperCase()}`));
    row.append(info, el("div", "device-size", human(d.size)));
    const state = el("div", "device-state");
    if (d.mounts.some(m=>m.startsWith("/srv/omarchy-nas/"))) state.append(el("span", "pill", "本插件已挂载"));
    else if (d.eligible) state.append(button("接入共享 →", ()=>command("接入数据盘", "在终端选择共享名并确认。默认只读；不会格式化磁盘。", data.configured ? `sudo omarchy-nas add --device ${quote(d.path)}` : `python3 ~/Downloads/omarchy-nas.pyz setup --device ${quote(d.path)}`)));
    else { const tag=el("span", "pill neutral", d.reason.includes("系统盘") ? "系统盘 · 已排除" : "查看原因"); state.append(tag); if (!d.reason.includes("系统盘")) info.append(el("div", "device-detail", d.reason)); }
    row.append(state); devices.append(row);
  }
  byId("updated").textContent = "更新于 " + new Date(data.time*1000).toLocaleTimeString("zh-CN");
}
async function refresh() { if (busy) return; busy = true; byId("refresh").disabled = true; try { const r = await fetch("/api/status", {cache:"no-store"}); if (!r.ok) throw new Error(`HTTP ${r.status}`); render(await r.json()); } catch (e) { byId("errors").replaceChildren(el("div", "notice", `状态读取失败：${e.message}。上次数据可能已过期。`)); byId("service-state").textContent="状态未知"; } finally { busy=false; byId("refresh").disabled=false; } }
byId("refresh").addEventListener("click", refresh);
byId("doctor").addEventListener("click", ()=>command("诊断设备与依赖", "输出设备识别结果、挂载限制和依赖情况，不会改动磁盘。", "omarchy-nas doctor"));
byId("copy-command").addEventListener("click", ()=>copy(byId("command").value));
refresh(); setInterval(()=>{ if (!document.hidden && !byId("command-dialog").open) refresh(); }, 10000);
