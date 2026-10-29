/**
 * 面板数据路由：/bili-live/api/*
 *
 * 读接口（GET /data）只读文件，不起 Python —— 面板每 2 秒轮询一次，
 * 起进程太贵。所有文件都是后端原子写的（.tmp + rename），不会读到半个 JSON。
 *
 * 写接口（POST /action）分两种：
 *   - 短任务（加 UP 主、改设置、丢弃/重试）同步调 Python CLI，等结果返回；
 *   - 长任务（投稿上传）用 detached 子进程跑，立刻返回，进度写 uploads/<id>.json，
 *     面板靠轮询看进度。
 */
import { spawn } from 'node:child_process';
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  readdirSync,
  renameSync,
  statSync,
  statfsSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';

const API_PREFIX = '/bili-live/api';
const MAX_BODY = 512 * 1024;

// 与后端 store.DEFAULT_SETTINGS 保持一致（面板显示用；真正生效的是后端 settings.json）
export const DEFAULT_SETTINGS = {
  pollSeconds: 30,
  maxQn: 10000,
  preferCodec: 'avc',
  partSeconds: 7200,
  danmaku: true,
  makeCover: true,
  keepRawFlv: false,
  minFreeGb: 8,
  titleTemplate: '【录播】{date} {uname} {title}',
  descTemplate: '{uname} 的直播录播（{date}）。\n直播间：{roomUrl}\n本录播由 DSH「直播小助手」自动录制。',
  tid: 21,
  tidName: '日常',
  tags: ['录播', '{uname}'],
  copyright: 1,
  source: '',
  visibility: 'private',
  dynamic: '',
  noReprint: 1,
  openElec: 1,
  closeDanmaku: 0,
  closeReply: 0,
  dtime: 0,
  autoPublish: false,
  deleteLocalAfterPublish: true,
};

// 常用分区（与后端 upload.TID_LIST 对齐的常用项）
export const TIDS = [
  { tid: 21, name: '日常' },
  { tid: 160, name: '生活' },
  { tid: 257, name: '虚拟UP主' },
  { tid: 4, name: '游戏' },
  { tid: 17, name: '单机游戏' },
  { tid: 171, name: '电子竞技' },
  { tid: 3, name: '音乐' },
  { tid: 31, name: '翻唱' },
  { tid: 1, name: '动画' },
  { tid: 129, name: '舞蹈' },
  { tid: 5, name: '娱乐' },
  { tid: 71, name: '综艺' },
  { tid: 36, name: '知识' },
  { tid: 188, name: '科技' },
  { tid: 181, name: '影视' },
  { tid: 211, name: '美食' },
  { tid: 217, name: '动物圈' },
  { tid: 234, name: '运动' },
  { tid: 223, name: '汽车' },
  { tid: 155, name: '时尚' },
  { tid: 202, name: '资讯' },
];

// ---------------------------------------------------------------- 基础

function readJson(path, fallback = null) {
  try {
    if (!existsSync(path)) return fallback;
    return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    return fallback;
  }
}

function writeJsonAtomic(path, value) {
  const tmp = `${path}.tmp`;
  try {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(tmp, JSON.stringify(value, null, 1), 'utf8');
    renameSync(tmp, path);
    return true;
  } catch {
    return false;
  }
}

function tailFile(path, maxBytes = 12000) {
  try {
    if (!existsSync(path)) return '';
    const size = statSync(path).size;
    const start = Math.max(0, size - maxBytes);
    const fd = openSync(path, 'r');
    try {
      const buf = Buffer.alloc(size - start);
      if (!buf.length) return '';
      readSync(fd, buf, 0, buf.length, start);
      return buf.toString('utf8');
    } finally {
      closeSync(fd);
    }
  } catch {
    return '';
  }
}

function freeGb(path) {
  try {
    if (typeof statfsSync !== 'function') return null;
    const st = statfsSync(path || '.');
    return Math.round(((st.bavail * st.bsize) / 1024 ** 3) * 10) / 10;
  } catch {
    return null;
  }
}

function isoOf(ts) {
  if (!ts) return '';
  try {
    const d = new Date(Number(ts) * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------- 读文件

function loadSettings(paths) {
  const saved = readJson(paths.settingsFile, {}) || {};
  const out = { ...DEFAULT_SETTINGS };
  for (const [k, v] of Object.entries(saved)) {
    if (v !== undefined && v !== null) out[k] = v;
  }
  return out;
}

function sessionsRoot(paths) {
  const settings = loadSettings(paths);
  return settings.recordRoot && String(settings.recordRoot).trim()
    ? String(settings.recordRoot)
    : join(paths.workRoot, 'sessions');
}

function listSessions(paths, limit = 60) {
  const root = sessionsRoot(paths);
  const out = [];
  try {
    if (!existsSync(root)) return out;
    const names = readdirSync(root).sort().reverse();
    for (const name of names.slice(0, limit)) {
      const dir = join(root, name);
      try {
        if (!statSync(dir).isDirectory()) continue;
      } catch {
        continue;
      }
      const meta = readJson(join(dir, 'meta.json'), null);
      if (!meta) continue;
      const parts = meta.parts || [];
      out.push({
        id: meta.id || name,
        mid: String(meta.mid || ''),
        uname: meta.uname || '',
        title: meta.title || '',
        roomId: meta.roomId || 0,
        status: meta.status || '',
        startText: isoOf(meta.recStart),
        recStart: meta.recStart || 0,
        recEnd: meta.recEnd || 0,
        parts: parts.length,
        durationSec:
          Math.round(
            (parts.reduce((a, p) => a + (p.durationMs || 0), 0) / 1000) * 10,
          ) / 10,
        bytes: parts.reduce((a, p) => a + (p.bytes || 0), 0),
        danmaku: meta.danmaku || 0,
        cover: meta.cover || '',
        error: meta.error || '',
      });
    }
  } catch {
    /* 目录不存在 */
  }
  return out;
}

function loadQueue(paths) {
  const data = readJson(join(paths.workRoot, 'queue.json'), { items: [] }) || { items: [] };
  const items = (data.items || []).map((it) => {
    const up = readJson(join(paths.workRoot, 'uploads', `${it.id}.json`), null);
    const terminal = ['done', 'discarded'].includes(it.status);
    return {
      id: it.id,
      sessionId: it.sessionId || '',
      mid: String(it.mid || ''),
      uname: it.uname || '',
      partIndex: it.partIndex || 1,
      title: it.title || '',
      desc: it.desc || '',
      tag: it.tag || '',
      tid: it.tid || 21,
      visibility: it.visibility || 'private',
      dtime: it.dtime || 0,
      bytes: it.bytes || 0,
      durationSec: it.durationSec || 0,
      width: it.width || 0,
      height: it.height || 0,
      cover: it.cover || '',
      file: it.file || '',
      status: it.status || 'pending',
      error: it.error || '',
      aid: it.aid || null,
      bvid: it.bvid || null,
      createdAt: it.createdAt || 0,
      submittedAt: it.submittedAt || 0,
      fileDeleted: Boolean(it.fileDeleted),
      visibilityApplied: it.visibilityApplied || '',
      visibilityNote: it.visibilityNote || '',
      progress: !up || terminal ? null : {
        phase: up.phase || '',
        percent: up.percent || 0,
        note: up.note || '',
      },
    };
  });
  const counts = {};
  for (const it of items) counts[it.status] = (counts[it.status] || 0) + 1;
  return { items, counts };
}

function buildData(paths, deps) {
  const settings = loadSettings(paths);
  const daemon = readJson(join(paths.workRoot, 'daemon.json'), {}) || {};
  const alive = Boolean(daemon.pid) && deps.isAlive(daemon.pid);
  const recording = alive ? daemon.recording || {} : {};
  const upsRaw = (readJson(join(paths.workRoot, 'ups.json'), { items: [] }) || {}).items || [];
  const ups = upsRaw.map((u) => ({
    mid: String(u.mid || ''),
    uname: u.uname || '',
    face: u.face || '',
    roomId: u.roomId || 0,
    enabled: u.enabled !== false,
    live: Boolean(u.live),
    title: u.title || '',
    cover: u.cover || '',
    online: u.online || 0,
    checkedAt: u.checkedAt || 0,
    recording: recording[String(u.mid)] || null,
  }));
  const { items: queue, counts } = loadQueue(paths);
  const account = readJson(join(paths.workRoot, 'account.json'), null);
  const cookieText = deps.readCookie() || '';

  return {
    ok: true,
    now: new Date().toISOString(),
    daemon: {
      alive,
      pid: alive ? daemon.pid : 0,
      heartbeat: alive ? isoOf(daemon.heartbeatAt || daemon.updatedAt) : '',
      version: daemon.version || '',
      uptimeSec: alive ? daemon.uptimeSec || 0 : 0,
    },
    cookie: {
      present: cookieText.length > 0,
      bytes: cookieText.length,
      file: paths.cookieFile,
      account: account || null,
    },
    disk: { freeGb: freeGb(paths.workRoot) },
    settings,
    ups,
    sessions: listSessions(paths),
    queue,
    queueCounts: counts,
    publishOptions: { tids: TIDS },
    host: paths.workRoot,
    logTail: tailFile(join(paths.workRoot, 'daemon.log'), deps.maxLogTail || 12000),
  };
}

// ---------------------------------------------------------------- 请求体

function readBody(req) {
  return new Promise((done) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        done(null);
        try {
          req.destroy();
        } catch {
          /* ignore */
        }
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      if (!chunks.length) {
        done({});
        return;
      }
      try {
        const v = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        done(v && typeof v === 'object' ? v : {});
      } catch {
        done(null);
      }
    });
    req.on('error', () => done(null));
  });
}

function sendJson(res, status, payload) {
  let body;
  try {
    body = Buffer.from(JSON.stringify(payload), 'utf8');
  } catch {
    body = Buffer.from('{"ok":false,"error":"序列化失败"}', 'utf8');
    status = 500;
  }
  try {
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': String(body.length),
      'cache-control': 'no-store',
    });
    res.end(body);
  } catch {
    /* 连接已断开 */
  }
}

function queryOf(rawUrl) {
  const out = {};
  const q = String(rawUrl || '').indexOf('?');
  if (q < 0) return out;
  for (const part of String(rawUrl).slice(q + 1).split('&')) {
    if (!part) continue;
    const eq = part.indexOf('=');
    const k = eq < 0 ? part : part.slice(0, eq);
    const v = eq < 0 ? '' : part.slice(eq + 1);
    try {
      out[decodeURIComponent(k)] = decodeURIComponent(v.replace(/\+/g, ' '));
    } catch {
      out[k] = v;
    }
  }
  return out;
}

// ---------------------------------------------------------------- 动作

function normalizeCookie(raw) {
  const parts = [];
  for (const line of String(raw || '').replace(/\r/g, '\n').split('\n')) {
    const t = line.split('#')[0].trim();
    if (t) parts.push(t);
  }
  return parts.join('; ');
}

async function doAction(body, paths, deps) {
  const action = String((body && body.action) || '');
  const backend = deps.backend;   // async (args, opts) => { json, log }
  const needMid = ['addUp', 'removeUp', 'toggleUp', 'startRec', 'stopRec'];
  const mid = body && body.mid !== undefined ? String(body.mid) : '';
  if (needMid.includes(action) && !mid) {
    return { ok: false, error: '缺少 UID / 房间号 / 直播间链接' };
  }

  switch (action) {
    case 'addUp': {
      const r = await backend(['ups', 'add', '--mid', mid]);
      return r.json || { ok: false, error: r.log || '添加失败' };
    }
    case 'removeUp':
      return (await backend(['ups', 'remove', '--mid', mid])).json || { ok: false, error: '移除失败' };
    case 'toggleUp':
      return (
        await backend(['ups', 'toggle', '--mid', mid, '--enabled', body.enabled === false ? '0' : '1'])
      ).json || { ok: false, error: '切换失败' };
    case 'startRec':
      return (await backend(['record', 'start', '--mid', mid])).json || { ok: false, error: '开始失败' };
    case 'stopRec':
      return (await backend(['record', 'stop', '--mid', mid])).json || { ok: false, error: '停止失败' };
    case 'daemonStart': {
      const r = await deps.startDaemon();
      return r;
    }
    case 'daemonStop': {
      const r = await backend(['daemon', 'stop']);
      return r.json || { ok: false, error: '停止失败' };
    }
    case 'saveCookie': {
      const cookie = normalizeCookie(body.cookie);
      if (!cookie || !/SESSDATA=/.test(cookie)) {
        return { ok: false, error: 'cookie 里必须有 SESSDATA（整行粘贴，含 bili_jct）' };
      }
      if (!/bili_jct=/.test(cookie)) {
        return { ok: false, error: 'cookie 里还缺 bili_jct（投稿要用它做 csrf）' };
      }
      try {
        mkdirSync(dirname(paths.cookieFile), { recursive: true });
        writeFileSync(paths.cookieFile, cookie, 'utf8');
      } catch (e) {
        return { ok: false, error: `写 cookie 文件失败：${e.message}` };
      }
      return { ok: true, bytes: cookie.length, note: '已保存，新的录制与投稿立即使用新凭据' };
    }
    case 'checkAuth': {
      const r = await backend(['auth']);
      const json = r.json || {};
      if (json.ok && json.loggedIn) {
        writeJsonAtomic(join(paths.workRoot, 'account.json'), {
          mid: json.account && json.account.mid,
          uname: json.account && json.account.uname,
          checkedAt: Math.floor(Date.now() / 1000),
        });
      } else {
        writeJsonAtomic(join(paths.workRoot, 'account.json'), null);
      }
      return json.ok ? { ...json } : { ok: false, error: json.error || '检查失败' };
    }
    case 'saveSettings': {
      const patch = (body && body.settings) || {};
      const r = await backend(['settings', 'set', '--json', JSON.stringify(patch)]);
      return r.json || { ok: false, error: '保存失败' };
    }
    case 'publish': {
      const ids = Array.isArray(body.ids) ? body.ids.map(String) : [];
      if (!ids.length) return { ok: false, error: '没勾选要发布的条目' };
      const r = await deps.startPublish(ids);
      return r;
    }
    case 'discard':
    case 'retry': {
      const ids = Array.isArray(body.ids) ? body.ids.map(String) : [];
      if (!ids.length) return { ok: false, error: '没选中条目' };
      return (await backend(['queue', action, '--ids', ids.join(',')])).json || { ok: false, error: '操作失败' };
    }
    case 'updateItem': {
      const id = String(body.id || '');
      if (!id) return { ok: false, error: '缺少条目 id' };
      return (
        await backend(['queue', 'update', '--id', id, '--json', JSON.stringify(body.patch || {})])
      ).json || { ok: false, error: '保存失败' };
    }
    case 'finalizeSession': {
      const id = String(body.id || '');
      if (!id) return { ok: false, error: '缺少会话 id' };
      return (await backend(['sessions', 'finalize', '--id', id])).json || { ok: false, error: '收尾失败' };
    }
    case 'deleteSession': {
      const id = String(body.id || '');
      if (!id) return { ok: false, error: '缺少会话 id' };
      return (await backend(['sessions', 'delete', '--id', id])).json || { ok: false, error: '删除失败' };
    }
    case 'openPath':
      return { ok: false, error: '本插件不提供打开目录（沙箱限制），请用文件管理器打开 host 路径' };
    default:
      return { ok: false, error: `未知动作：${action}` };
  }
}

// ---------------------------------------------------------------- 注册

export function registerPanelRoutes(ctx, deps) {
  const paths = deps.paths;
  const guarded = async (fn) => {
    try {
      return await fn();
    } catch (e) {
      return { status: 500, json: { ok: false, error: (e && e.message) || String(e) } };
    }
  };

  const pathnameOf = (rawUrl) => {
    try {
      return new URL(rawUrl || '/', 'http://127.0.0.1').pathname;
    } catch {
      return String(rawUrl || '/').split('?')[0];
    }
  };

  const buildReply = async ({ pathname, method, body }) => {
    if (method === 'GET' && pathname === `${API_PREFIX}/data`) {
      return { status: 200, json: buildData(paths, deps) };
    }
    if (method === 'GET' && pathname === `${API_PREFIX}/log`) {
      return {
        status: 200,
        json: {
          ok: true,
          text: tailFile(join(paths.workRoot, 'daemon.log'), 40000),
          publish: tailFile(join(paths.workRoot, 'publish.log'), 20000),
        },
      };
    }
    if (method === 'POST' && pathname === `${API_PREFIX}/action`) {
      if (body === null) return { status: 400, json: { ok: false, error: '请求体不是合法 JSON' } };
      const result = await doAction(body, paths, deps);
      return { status: result && result.ok === false ? 400 : 200, json: result || { ok: false, error: '空结果' } };
    }
    return { status: 404, json: { ok: false, error: 'not found' } };
  };

  ctx.inject(['connection'], (hostCtx) => {
    const conn = hostCtx.connection || hostCtx.get?.('connection');
    const reg = conn && conn.fetch && typeof conn.fetch.register === 'function' ? conn.fetch : null;
    if (!reg) return;
    hostCtx.effect(
      () =>
        reg.register({
          path: API_PREFIX,
          methods: ['GET', 'POST'],
          requestBody: 'buffered',
          fetch: async (request) => {
            const url = (request && request.url) || '/';
            const method = String((request && request.method) || 'GET').toUpperCase();
            let body = null;
            if (method === 'POST') {
              try {
                body = await request.json();
              } catch {
                body = null;
              }
            }
            const reply = await guarded(() =>
              buildReply({ pathname: pathnameOf(url), method, body, query: queryOf(url) }),
            );
            return new Response(JSON.stringify(reply.json), {
              status: reply.status,
              headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
            });
          },
        }),
      'bili-live: panel api route (connection)',
    );
  });

  ctx.inject(['webServer'], (hostCtx) => {
    const webServer = hostCtx.webServer || hostCtx.get?.('webServer');
    if (!webServer || typeof webServer.register !== 'function') return;
    hostCtx.effect(
      () =>
        webServer.register({
          kind: 'prefix',
          path: API_PREFIX,
          async handler(req, res) {
            const method = String(req.method || 'GET').toUpperCase();
            const body = method === 'POST' ? await readBody(req) : undefined;
            const reply = await guarded(() =>
              buildReply({ pathname: pathnameOf(req.url), method, body, query: queryOf(req.url) }),
            );
            sendJson(res, reply.status, reply.json);
          },
        }),
      'bili-live: panel api route (webserver)',
    );
  });
}
