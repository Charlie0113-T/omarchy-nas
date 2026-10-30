// Model.js is a plain QML JavaScript resource (no exports), so it is loaded
// into its own context, the way the QML engine sees it.
// Run: node --test tests/model.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../Model.js', import.meta.url), 'utf8');
const M = vm.createContext({});
vm.runInContext(source, M);

const share = (over = {}) => ({
  name: 'family', fs: 'apfs', size: 12_000_000_000_000, mode: 'ro',
  enabled: true, state: 'online', online: true, url: 'smb://192.168.1.24/family', ...over,
});
const snapshot = (over = {}) => ({
  version: '0.3.0', hostname: 'mac-mini', configured: true, service_active: true,
  errors: [], shares: [share()], ...over,
});

test('glyphs are single Nerd Font code points', () => {
  assert.equal(M.GLYPH.nas.codePointAt(0), 0xf08f3);
  assert.equal([...M.GLYPH.nas].length, 1);
  assert.equal(M.GLYPH.disk.codePointAt(0), 0xf02ca);
});

test('a snapshot that is not a JSON object is no snapshot', () => {
  assert.equal(M.parseSnapshot(''), null);
  assert.equal(M.parseSnapshot('not json'), null);
  assert.equal(M.parseSnapshot('[]'), null);
  assert.equal(M.parseSnapshot('null'), null);
  // Objects from the script's own context: compare by value, not prototype.
  assert.equal(JSON.stringify(M.parseSnapshot('{"configured":true}')), '{"configured":true}');
});

test('everything online is ok, and the bar icon is plain', () => {
  const s = M.summarize(snapshot(), null);
  assert.equal(s.state, 'ok');
  assert.equal(s.level, 'normal');
  assert.equal(s.online, 1);
  assert.equal(s.total, 1);
  assert.equal(M.metaText(s, 'zh-CN'), '1/1 在线');
  assert.equal(M.tooltipText(s, 'en'), 'NAS · 1/1 online');
});

test('an offline share, an error or a stopped service makes the icon urgent', () => {
  const offline = M.summarize(snapshot({ shares: [share(), share({ name: 'photos', state: 'offline', online: false, error: '卷未挂载' })] }), null);
  assert.equal(offline.state, 'degraded');
  assert.equal(offline.level, 'urgent');
  assert.equal(offline.offline, 1);
  assert.equal(offline.shares[1].error, '卷未挂载');
  assert.equal(M.metaText(offline, 'zh-CN'), '1 个离线');

  const errored = M.summarize(snapshot({ errors: ['lsblk failed'] }), null);
  assert.equal(errored.level, 'urgent');
  assert.deepEqual([...errored.errors], ['lsblk failed']);

  const stopped = M.summarize(snapshot({ service_active: false }), null);
  assert.equal(stopped.state, 'stopped');
  assert.equal(stopped.level, 'urgent');
  assert.equal(M.metaText(stopped, 'zh-CN'), '服务未运行');
});

test('a paused share is intentional: not urgent, and not counted as missing', () => {
  const s = M.summarize(snapshot({ shares: [share(), share({ name: 'backup', enabled: false, state: 'paused', online: false, error: 'x' })] }), null);
  assert.equal(s.state, 'ok');
  assert.equal(s.level, 'normal');
  assert.equal(s.paused, 1);
  assert.equal(s.shares[1].state, 'paused');
  assert.equal(s.shares[1].error, '');
  assert.equal(M.metaText(s, 'en'), '1/1 online');

  const allPaused = M.summarize(snapshot({ shares: [share({ enabled: false, state: 'paused', online: false })] }), null);
  assert.equal(M.metaText(allPaused, 'zh-CN'), '已暂停');
});

test('not installed, not set up and not read yet are faded, not alarming', () => {
  const missing = M.summarize(null, { missing: true });
  assert.equal(missing.state, 'missing');
  assert.equal(missing.level, 'dim');
  assert.equal(M.metaText(missing, 'zh-CN'), '未安装 omarchy-nas');

  const unconfigured = M.summarize(snapshot({ configured: false, service_active: false, shares: [] }), null);
  assert.equal(unconfigured.state, 'unconfigured');
  assert.equal(unconfigured.level, 'dim');

  const checking = M.summarize(null, null);
  assert.equal(checking.state, 'checking');
  assert.equal(checking.level, 'dim');
});

test('a status command that failed says why', () => {
  const s = M.summarize(null, { failed: '未完成：配置无法读取' });
  assert.equal(s.state, 'degraded');
  assert.equal(s.level, 'urgent');
  assert.deepEqual([...s.errors], ['未完成：配置无法读取']);
});

test('sizes read the way drives are sold', () => {
  assert.equal(M.formatBytes(12_000_000_000_000), '12 TB');
  assert.equal(M.formatBytes(4_000_787_030_016), '4 TB');
  assert.equal(M.formatBytes(1_500_000_000_000), '1.5 TB');
  assert.equal(M.formatBytes(931_000_000_000), '931 GB');
  assert.equal(M.formatBytes(0), '');
  assert.equal(M.formatBytes('x'), '');
});

test('a share detail is size, file system and mode', () => {
  const ro = M.summarize(snapshot(), null).shares[0];
  assert.equal(M.shareDetail(ro, 'zh-CN'), '12 TB · APFS · 只读');
  const rw = M.summarize(snapshot({ shares: [share({ fs: 'ext4', mode: 'rw', size: 4_000_000_000_000 })] }), null).shares[0];
  assert.equal(M.shareDetail(rw, 'en'), '4 TB · EXT4 · read-write');
});

test('languages: zh-CN, zh-TW, other Chinese codes, and English for the rest', () => {
  assert.equal(M.t('zh-CN', 'stopped'), '服务未运行');
  assert.equal(M.t('zh-TW', 'stopped'), '服務未執行');
  assert.equal(M.t('zh-HK', 'stopped'), '服務未執行');
  assert.equal(M.t('zh', 'stopped'), '服务未运行');
  assert.equal(M.t('de', 'stopped'), 'Service stopped');
  assert.equal(M.t(undefined, 'stopped'), 'Service stopped');
  assert.equal(M.t('en', 'online', [2, 3]), '2/3 online');
  assert.equal(M.t('en', 'no.such.key'), 'no.such.key');
});

test('every catalog has the same keys as English', () => {
  const keys = (lang) => Object.keys(M.STRINGS[lang]).sort();
  assert.deepEqual(keys('zh-CN'), keys('en'));
  assert.deepEqual(keys('zh-TW'), keys('en'));
});

test('pause and resume commands only for names omarchy-nas accepts', () => {
  const online = M.summarize(snapshot(), null).shares[0];
  assert.equal(M.toggleCommand(online), 'sudo omarchy-nas pause family');
  const paused = M.summarize(snapshot({ shares: [share({ enabled: false, state: 'paused' })] }), null).shares[0];
  assert.equal(M.toggleCommand(paused), 'sudo omarchy-nas resume family');
  for (const name of ['Family', 'a;rm -rf ~', '$(id)', 'x y', '', 'a'.repeat(33)]) {
    const item = M.summarize(snapshot({ shares: [share({ name })] }), null).shares[0];
    assert.equal(M.toggleCommand(item), '', name);
  }
  assert.equal(M.toggleCommand(null), '');
});

test('settings are clamped to their range', () => {
  assert.equal(M.clampInt(undefined, 60, 15, 3600), 60);
  assert.equal(M.clampInt('5', 60, 15, 3600), 15);
  assert.equal(M.clampInt(99999, 60, 15, 3600), 3600);
  assert.equal(M.clampInt('120', 60, 15, 3600), 120);
});
