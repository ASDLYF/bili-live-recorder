/**
 * B站录播姬（Host 侧）
 *
 * 设计：插件只负责「拉起与控制」，真正干活的在 Python 后端（bili_rec 包）：
 *   轮询开播 → 抓FLV(自改时间戳/按关键帧切分P) → 转 mp4 + 封面 + 弹幕 → 投稿队列
 *
 * 三条硬约束（都是本机实测出来的）：
 *   1. 宿主沙箱禁止用管道捕获子进程输出（spawn 管道 EPERM），所以结果一律
 *      `--json-out <文件>` 落盘后再读，子进程日志走文件描述符。
 *   2. 命令行程序建不了 HTTPS（schannel），因此抓流与投稿全在 Python 里做，
 *      ffmpeg 只处理本地文件。
 *   3. 录制要脱离 DSH 生命周期：守护进程用 detached 方式拉起，DSH 退出后继续录。
 */
import { spawn } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  statSync,
} from 'node:fs';
import { dirname, join, resolve as pathResolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { DEFAULT_SETTINGS, registerPanelRoutes } from './api.js';
import { isAlive, killTree, loadSchemastery, resolvePythonCmd, resolveUserPath, workerEnv } from './platform.js';

const CONFIG_DEFAULTS = {
  scriptDir: '',
  workRoot: '',
  pythonPath: '',
  ffmpegPath: '',
  ffprobePath: '',
  cookieFile: '',
  autoStartDaemon: true,
  startGraceMs: 2500,
  backendTimeoutMs: 180000,
  maxLogTail: 12000,
};

// ---------------------------------------------------------------- 小工具

function readJson(path, fallback = null) {
  try {
    if (!existsSync(path)) return fallback;
    return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    return fallback;
  }
}

function openLog(path) {
  try {
    mkdirSync(dirname(path), { recursive: true });
    return openSync(path, 'a');
  } catch {
    return null;
  }
}

function tailFile(path, maxBytes = 8000) {
  try {
    if (!existsSync(path)) return '';
    const raw = readFileSync(path, 'utf8');
    return raw.length > maxBytes ? raw.slice(-maxBytes) : raw;
  } catch {
    return '';
  }
}

function toLosslessJson(value, depth = 0) {
  if (value === null) return null;
  const t = typeof value;
  if (t === 'string' || t === 'boolean') return value;
  if (t === 'number') return Number.isFinite(value) ? value : null;
  if (t === 'undefined' || t === 'function' || t === 'symbol') return undefined;
  if (depth > 6) return null;
  if (Array.isArray(value)) {
    return value.map((x) => {
      const v = toLosslessJson(x, depth + 1);
      return v === undefined ? null : v;
    });
  }
  if (t === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      const s = toLosslessJson(v, depth + 1);
      if (s !== undefined) out[k] = s;
    }
    return out;
  }
  return null;
}

function previewArgs(argv) {
  return argv.map((a) => (/[\s"]/.test(String(a)) ? JSON.stringify(String(a)) : String(a))).join(' ');
}

function text(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function block(value) {
  return [{ type: 'text', text: typeof value === 'string' ? value : text(value) }];
}

/** 所有工具输出都是「后端 JSON 透传」，所以共用一个宽松 schema + 各自的中文渲染。 */
function genericOutput(render) {
  return {
    schema: {
      type: 'object',
      additionalProperties: true,
      properties: { ok: { type: 'boolean' }, error: { type: 'string' } },
    },
    render,
  };
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)}GB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(1)}MB`;
  if (v >= 1024) return `${(v / 1024).toFixed(0)}KB`;
  return `${v}B`;
}

function fmtDur(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}` : `${m}:${String(ss).padStart(2, '0')}`;
}

// ---------------------------------------------------------------- 路径

function looksLikeBackend(dir) {
  try {
    return Boolean(dir) && existsSync(join(dir, 'bili_rec', 'cli.py'));
  } catch {
    return false;
  }
}

function discoverBackend(cfg, scriptDir, workRoot) {
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    scriptDir,
    join(here, 'backend'),
    join(here, '..', 'bili_live_backend'),
    join(workRoot || '', '..', 'bili_live_backend'),
    'D:\\1\\摄影\\bili_live_backend',
    join(process.env.USERPROFILE || process.env.HOME || '', 'bili_live_backend'),
  ];
  const seen = new Set();
  for (const c of candidates) {
    if (!c) continue;
    const norm = pathResolve(String(c));
    if (seen.has(norm)) continue;
    seen.add(norm);
    if (looksLikeBackend(norm)) return norm;
  }
  return scriptDir;
}

function resolvePaths(cfg) {
  const configured = resolveUserPath(cfg.scriptDir || '');
  const workRootCfg = resolveUserPath(cfg.workRoot || '');
  const scriptDir = looksLikeBackend(configured)
    ? configured
    : discoverBackend(cfg, configured, workRootCfg);
  const workRoot = workRootCfg || (scriptDir ? join(scriptDir, '_bili_live_work') : '');
  const cookieCfg = resolveUserPath(cfg.cookieFile || '');
  const cookieFile = cookieCfg || (scriptDir ? join(scriptDir, 'bili_cookie.txt') : '');
  return { scriptDir, workRoot, cookieFile };
}

// ---------------------------------------------------------------- 子进程

function execFd(cmd, args, { cwd, timeoutMs = 120000, logFile, env } = {}) {
  return new Promise((resolve) => {
    const fd = logFile ? openLog(logFile) : null;
    const stdio = fd === null ? ['ignore', 'ignore', 'ignore'] : ['ignore', fd, fd];
    let child;
    try {
      child = spawn(cmd, args, { cwd, env, stdio, windowsHide: true });
    } catch (e) {
      resolve({ code: -1, error: String(e && e.message), log: '' });
      return;
    }
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      killTree(child, child.pid);
      resolve({ code: -2, error: `超时（${Math.round(timeoutMs / 1000)}s）`, log: '' });
    }, timeoutMs);

    child.on('error', (e) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ code: -1, error: String(e && e.message), log: '' });
    });
    child.on('exit', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ code: code === null ? -1 : code, log: '' });
    });
  });
}

export function apply(ctx, config = {}) {
  const cfg = { ...CONFIG_DEFAULTS, ...(config || {}) };
  // Config 里 schema 的默认值可能被削掉，这里再兜一次
  for (const [k, v] of Object.entries(CONFIG_DEFAULTS)) {
    if (cfg[k] === undefined || cfg[k] === null) cfg[k] = v;
  }

  const paths = resolvePaths(cfg);
  if (paths.workRoot) {
    try {
      mkdirSync(paths.workRoot, { recursive: true });
    } catch {
      /* 读不到目录时后端会自己报错 */
    }
  }

  const toolDefs = new Map();
  const define = (def) => {
    const raw = def.execute;
    const wrapped = Object.assign({}, def, {
      async execute(...args) {
        try {
          const result = await raw.apply(def, args);
          const clean = toLosslessJson(result);
          return clean === undefined ? null : clean;
        } catch (e) {
          return { ok: false, error: (e && e.message) || String(e), kind: 'bili_live' };
        }
      },
    });
    toolDefs.set(wrapped.name, wrapped);
    return ctx.effect(() => ctx.tools.register(wrapped));
  };

  // ------------------------------------------------------------ 后端调用

  const resolvePython = async () => {
    const probe = (cmd, a) => execFd(cmd, a, { timeoutMs: 20000, env: workerEnv({}) });
    const r = await resolvePythonCmd(cfg.pythonPath, probe);
    if (!r.ok) {
      throw new Error(
        `找不到可用的 Python。请在插件配置里填 pythonPath。尝试过：${(r.failures || []).join('；')}`,
      );
    }
    return r;
  };

  const backend = async (argv, opts = {}) => {
    const { scriptDir, workRoot, cookieFile } = resolvePaths(cfg);
    if (!looksLikeBackend(scriptDir)) {
      return { json: { ok: false, error: `找不到 Python 后端目录（需要 bili_rec/cli.py）。当前：${scriptDir || '(空)'}` }, log: '' };
    }
    const stamp = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    const outFile = join(workRoot || scriptDir, `_result-${stamp}.json`);
    const logFile = join(workRoot || scriptDir, '_logs', `cli-${stamp}.log`);
    const py = await resolvePython();
    const args = [...py.pre, '-m', 'bili_rec.cli', ...argv, '--json-out', outFile];
    if (!argv.includes('--workdir')) args.push('--workdir', workRoot);
    if (!argv.includes('--cookie-file') && cookieFile) args.push('--cookie-file', cookieFile);
    await execFd(py.cmd, args, {
      cwd: scriptDir,
      timeoutMs: opts.timeoutMs || cfg.backendTimeoutMs,
      logFile,
      env: workerEnv({
        ffmpegPath: resolveUserPath(cfg.ffmpegPath || ''),
        ffprobePath: resolveUserPath(cfg.ffprobePath || ''),
        extra: { BILI_REC_WORK: workRoot || '' },
      }),
    });
    const json = readJson(outFile, null);
    if (!json) {
      return {
        json: {
          ok: false,
          error: `后端没有返回结果（日志：${logFile}）`,
          argv: previewArgs(argv),
          log: tailText(logFile, 40),
        },
        log: '',
      };
    }
    return { json, log: '' };
  };

  const daemonAlive = () => {
    const state = readJson(join(paths.workRoot, 'daemon.json'), {}) || {};
    return { alive: Boolean(state.pid) && isAlive(state.pid), pid: state.pid || 0, state };
  };

  const startDaemon = async () => {
    const cur = daemonAlive();
    if (cur.alive) return { ok: true, already: true, pid: cur.pid };
    const { scriptDir, workRoot, cookieFile } = resolvePaths(cfg);
    if (!looksLikeBackend(scriptDir)) {
      return { ok: false, error: `找不到后端目录（需要 bili_rec/cli.py）：${scriptDir || '(空)'}` };
    }
    const py = await resolvePython();
    const logFile = join(workRoot, 'daemon.out.log');
    const fd = openLog(logFile);
    const args = [...py.pre, '-m', 'bili_rec.daemon', '--workdir', workRoot];
    if (cookieFile) args.push('--cookie-file', cookieFile);
    let child;
    try {
      child = spawn(py.cmd, args, {
        cwd: scriptDir,
        detached: true,
        windowsHide: true,
        stdio: fd === null ? 'ignore' : ['ignore', fd, fd],
        env: workerEnv({
          ffmpegPath: resolveUserPath(cfg.ffmpegPath || ''),
          ffprobePath: resolveUserPath(cfg.ffprobePath || ''),
          extra: { BILI_REC_WORK: workRoot || '' },
        }),
      });
    } catch (e) {
      return { ok: false, error: `拉起守护进程失败：${e && e.message}` };
    }
    try {
      child.unref();
    } catch {
      /* ignore */
    }
    await new Promise((r) => setTimeout(r, Math.max(800, cfg.startGraceMs || 2500)));
    const after = daemonAlive();
    if (!after.alive) {
      return {
        ok: false,
        error: '守护进程没起来，检查 Python 与后端路径',
        log: tailText(logFile, 60),
      };
    }
    return { ok: true, pid: after.pid, started: true };
  };

  const startPublish = async (ids) => {
    const { scriptDir, workRoot, cookieFile } = resolvePaths(cfg);
    if (!looksLikeBackend(scriptDir)) {
      return { ok: false, error: `找不到后端目录：${scriptDir || '(空)'}` };
    }
    const py = await resolvePython();
    const logFile = join(workRoot, 'publish.log');
    const fd = openLog(logFile);
    const args = [...py.pre, '-m', 'bili_rec.cli', 'queue', 'publish',
      '--ids', ids.join(','), '--workdir', workRoot];
    if (cookieFile) args.push('--cookie-file', cookieFile);
    let child;
    try {
      child = spawn(py.cmd, args, {
        cwd: scriptDir,
        detached: true,
        windowsHide: true,
        stdio: fd === null ? 'ignore' : ['ignore', fd, fd],
        env: workerEnv({ extra: { BILI_REC_WORK: workRoot || '' } }),
      });
    } catch (e) {
      return { ok: false, error: `启动投稿失败：${e && e.message}` };
    }
    try {
      child.unref();
    } catch {
      /* ignore */
    }
    return { ok: true, started: true, ids, note: '投稿已开始，进度看队列里的百分比' };
  };

  function tailText(path, lines = 80) {
    try {
      if (!existsSync(path)) return '';
      const raw = readFileSync(path, 'utf8');
      const arr = raw.replace(/\r\n/g, '\n').split('\n').filter(Boolean);
      return arr.slice(-Math.max(1, lines)).join('\n');
    } catch {
      return '';
    }
  }

  const readCookie = () => {
    const { cookieFile } = resolvePaths(cfg);
    try {
      if (!cookieFile || !existsSync(cookieFile)) return '';
      return readFileSync(cookieFile, 'utf8').trim();
    } catch {
      return '';
    }
  };

  // ------------------------------------------------------------ 面板路由

  registerPanelRoutes(ctx, {
    paths: (() => {
      const p = resolvePaths(cfg);
      return {
        ...p,
        settingsFile: join(p.workRoot, 'settings.json'),
      };
    })(),
    backend,
    isAlive,
    startDaemon,
    startPublish,
    readCookie,
    maxLogTail: cfg.maxLogTail,
  });

  // ------------------------------------------------------------ 工具

  define({
    name: 'bili_live_status',
    description:
      'B站录播姬总览：守护进程是否在跑、监控的 UP 主及其开播/录制状态、已录会话、待发布队列统计、磁盘剩余。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: genericOutput((_a, v) => {
      if (!v || v.ok === false) return block(`读取状态失败：${(v && v.error) || '未知错误'}`);
      const d = v.daemon || {};
      const lines = [
        `录播姬：守护进程${d.alive ? `运行中（PID ${d.pid}，已运行 ${fmtDur(d.uptimeSec)}）` : '未运行（不会自动录制）'}`,
        `  工作目录：${v.workRoot || '(未配置)'}　磁盘剩余：${v.disk && v.disk.freeGb != null ? `${v.disk.freeGb}GB` : '未知'}`,
        `  凭据：${v.cookie && v.cookie.present ? '已保存' : '未设置（录制与投稿都要 B 站 cookie）'}`,
        `  UP 主 ${(v.ups || []).length} 个：`,
        ...(v.ups || []).map((u) => {
          const rec = u.recording;
          const state = rec
            ? `录制中 第${rec.parts || 1}段 已录 ${fmtDur((rec.curPartMs || 0) / 1000)} / ${fmtBytes(rec.bytes)} / 弹幕 ${rec.danmaku || 0} 条`
            : u.live
              ? '直播中（未在录）'
              : '未开播';
          return `    ${u.uname || '?'}（${u.mid}，房间 ${u.roomId || '?'}）${state}`;
        }),
        `  待发布队列：${Object.entries(v.queueCounts || {}).map(([k, c]) => `${k} ${c}`).join(' / ') || '空'}`,
      ];
      return block(lines.join('\n'));
    }),
    async execute() {
      const r = await backend(['status']);
      const json = r.json || {};
      return {
        ok: json.ok !== false,
        workRoot: paths.workRoot,
        daemon: json.daemon,
        disk: json.disk,
        cookie: json.cookie,
        ups: (json.ups || []).map((u) => ({
          mid: u.mid,
          uname: u.uname,
          roomId: u.roomId,
          live: u.live,
          title: u.title,
          recording: u.recording || null,
        })),
        sessions: (json.sessions || []).slice(0, 10),
        queueCounts: json.queueCounts,
        settings: json.settings,
        error: json.error,
      };
    },
  });

  define({
    name: 'bili_live_ups',
    description:
      '管理要监控的 UP 主：list 列出 / add 添加（只填 UID，插件自动解析直播间）/ remove 移除 / toggle 开关监控。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['list', 'add', 'remove', 'toggle'] },
        mid: { type: 'string', description: 'B 站 UID（数字）或主页链接' },
        enabled: { type: 'boolean', description: 'toggle 时用：true 开始监控，false 暂停' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: genericOutput((_a, v) => {
      if (!v || v.ok === false) return block(`操作失败：${(v && v.error) || '未知错误'}`);
      if (v.ups) {
        if (!v.ups.length) return block('还没有监控任何 UP 主，用 bili_live_ups { action:"add", mid } 添加');
        return block(
          `监控列表（${v.ups.length} 个）：\n` +
            v.ups
              .map((u) => `  ${u.uname || '?'}（${u.mid}）房间 ${u.roomId || '?'}${u.enabled === false ? ' [已暂停]' : ''}${u.live ? ' 直播中' : ''}`)
              .join('\n'),
        );
      }
      if (v.up) return block(`已保存：${v.up.uname || ''}（${v.up.mid}）房间 ${v.up.roomId || '?'}`);
      if (v.removed) return block(`已移除 UP 主 ${v.removed}`);
      return block(text(v));
    }),
    async execute(args) {
      const argv = ['ups', args.action];
      if (args.mid) argv.push('--mid', String(args.mid));
      if (args.action === 'toggle') argv.push('--enabled', args.enabled === false ? '0' : '1');
      const r = await backend(argv);
      return r.json;
    },
  });

  define({
    name: 'bili_live_record',
    description:
      '手动开始/停止某个 UP 主的录制（守护进程必须在运行）。默认是自动录制：开播就录、下播就停。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['start', 'stop'] },
        mid: { type: 'string', description: 'B 站 UID' },
      },
      required: ['action', 'mid'],
      additionalProperties: false,
    },
    output: genericOutput((_a, v) => {
      if (!v || v.ok === false) return block(`操作失败：${(v && v.error) || '未知错误'}`);
      if (v.started) return block(`已请求开始录制 ${v.started}（下个轮询周期生效）`);
      if (v.stopped) return block(`已请求停止录制 ${v.stopped}，随后转 mp4 并进待发布队列`);
      return block(text(v));
    }),
    async execute(args) {
      const r = await backend(['record', args.action, '--mid', String(args.mid)]);
      return r.json;
    },
  });

  define({
    name: 'bili_live_daemon',
    description:
      '录播守护进程控制：start 后台拉起（脱离 DSH 生命周期，DSH 关掉也继续录）/ stop 收尾退出 / status 看状态。',
    parameters: {
      type: 'object',
      properties: { action: { type: 'string', enum: ['start', 'stop', 'status'] } },
      required: ['action'],
      additionalProperties: false,
    },
    output: genericOutput((_a, v) => {
      if (!v || v.ok === false) return block(`操作失败：${(v && v.error) || '未知错误'}`);
      if (v.alive !== undefined) {
        if (!v.alive) return block('守护进程未运行');
        const rec = Object.keys(v.recording || {});
        return block(`守护进程运行中（PID ${v.pid}）${rec.length ? `，正在录制 ${rec.length} 个 UP 主` : '，当前没有在录'}`);
      }
      if (v.already) return block(`守护进程已在运行（PID ${v.pid}）`);
      if (v.started) return block(`守护进程已启动（PID ${v.pid}），DSH 关掉也会继续录`);
      if (v.note) return block(v.note);
      return block(text(v));
    }),
    async execute(args) {
      if (args.action === 'start') return startDaemon();
      if (args.action === 'stop') {
        const r = await backend(['daemon', 'stop']);
        return r.json;
      }
      const cur = daemonAlive();
      return {
        ok: true,
        alive: cur.alive,
        pid: cur.pid,
        heartbeat: cur.state.heartbeatAt || '',
        recording: cur.state.recording || {},
      };
    },
  });

  define({
    name: 'bili_live_queue',
    description:
      '待发布队列：list 看全部 / publish 把勾选的条目投到 B 站（同一场直播的多个分P 合成一个投稿）/ discard 丢弃 / retry 重试失败 / update 改单条发布参数。投稿在后台跑，进度看 status。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['list', 'publish', 'discard', 'retry', 'update', 'finalize', 'deleteSession'],
        },
        ids: { type: 'array', items: { type: 'string' }, description: '队列条目 id（p00001…）' },
        id: { type: 'string', description: 'update/finalize/deleteSession 用' },
        patch: {
          type: 'object',
          description: 'update 时要改的字段：title/desc/tag/tid/visibility/dtime',
          additionalProperties: true,
        },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: genericOutput((_a, v) => {
      if (!v || v.ok === false) return block(`操作失败：${(v && v.error) || '未知错误'}`);
      if (v.started) {
        return block(`投稿已开始（${(v.ids || []).length} 条，同一场直播的多个分P 会合成一个稿件）。\n上传进度看面板，或用 bili_live_status 查队列。`);
      }
      if (v.discarded) return block(`已丢弃 ${v.discarded.length} 条`);
      if (v.retried) return block(`已重试 ${v.retried.length} 条`);
      if (v.item) return block(`已更新 ${v.item.id}：${v.item.title || ''}`);
      if (v.items) {
        const items = v.items;
        if (!items.length) return block('待发布队列是空的');
        return block(
          `队列共 ${items.length} 条：\n` +
            items
              .map(
                (it) =>
                  `  ${it.id} P${it.partIndex || 1} [${it.status}] ${fmtDur(it.durationSec)} ${fmtBytes(it.bytes)} ` +
                  `${it.title || ''}${it.error ? ` ⚠ ${it.error}` : ''}${it.bvid ? ` → ${it.bvid}` : ''}`,
              )
              .join('\n'),
        );
      }
      return block(text(v));
    }),
    async execute(args) {
      if (args.action === 'list') {
        const r = await backend(['queue', 'list']);
        return r.json;
      }
      if (args.action === 'publish') {
        const ids = (args.ids || []).map(String);
        if (!ids.length) return { ok: false, error: '没指定 ids' };
        return startPublish(ids);
      }
      if (args.action === 'discard' || args.action === 'retry') {
        const ids = (args.ids || []).map(String);
        if (!ids.length) return { ok: false, error: '没指定 ids' };
        const r = await backend(['queue', args.action, '--ids', ids.join(',')]);
        return r.json;
      }
      if (args.action === 'update') {
        const r = await backend([
          'queue', 'update', '--id', String(args.id || ''), '--json', JSON.stringify(args.patch || {}),
        ]);
        return r.json;
      }
      if (args.action === 'finalize') {
        const r = await backend(['sessions', 'finalize', '--id', String(args.id || '')]);
        return r.json;
      }
      if (args.action === 'deleteSession') {
        const r = await backend(['sessions', 'delete', '--id', String(args.id || '')]);
        return r.json;
      }
      return { ok: false, error: '未知 action' };
    },
  });

  define({
    name: 'bili_live_settings',
    description:
      '读写录播姬设置（不传 patch 就是读）。可改：partSeconds 分P时长秒、maxQn 清晰度、preferCodec、pollSeconds、titleTemplate、descTemplate、tid 分区、tags、visibility（private 仅自己可见/public/scheduled）、autoPublish、deleteLocalAfterPublish 等。',
    parameters: {
      type: 'object',
      properties: {
        patch: {
          type: 'object',
          additionalProperties: true,
          description: '要修改的设置项，键名见 DEFAULT_SETTINGS',
        },
      },
      additionalProperties: false,
    },
    output: genericOutput((_a, v) => {
      if (!v || v.ok === false) return block(`操作失败：${(v && v.error) || '未知错误'}`);
      const s = v.settings || {};
      return block(
        [
          '当前设置：',
          `  分P 时长 ${Math.round((Number(s.partSeconds) || 0) / 60)} 分钟　清晰度 ${s.maxQn}　编码 ${s.preferCodec}　轮询 ${s.pollSeconds}s`,
          `  录制：弹幕 ${s.danmaku ? '开' : '关'}　自动封面 ${s.makeCover ? '开' : '关'}　保留原始 FLV ${s.keepRawFlv ? '开' : '关'}`,
          `  发布：分区 ${s.tid}　可见范围 ${s.visibility}　原创/转载 ${s.copyright}　禁止转载 ${s.noReprint}　充电 ${s.openElec}`,
          `  录完自动投稿：${s.autoPublish ? '开' : '关'}　投稿后删本地：${s.deleteLocalAfterPublish ? '开' : '关'}`,
          `  标题模板：${s.titleTemplate}`,
          `  标签：${Array.isArray(s.tags) ? s.tags.join(',') : s.tags}`,
        ].join('\n'),
      );
    }),
    async execute(args) {
      if (!args || !args.patch) {
        const r = await backend(['settings', 'get']);
        return r.json;
      }
      const r = await backend(['settings', 'set', '--json', JSON.stringify(args.patch)]);
      return r.json;
    },
  });

  define({
    name: 'bili_live_auth',
    description: '检查 B 站凭据是否有效（录制高画质与投稿都要 cookie：SESSDATA + bili_jct）。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: genericOutput((_a, v) => {
      if (!v || v.ok === false) return block(`检查失败：${(v && v.error) || '未知错误'}`);
      if (!v.hasCookie) return block('还没有 B 站 cookie：录制高画质与投稿都需要它（SESSDATA + bili_jct）');
      const acc = v.account || {};
      return block(
        v.loggedIn
          ? `凭据有效：${acc.uname || ''}（mid ${acc.mid || '?'}）\n  文件：${v.cookieFile || ''}`
          : `凭据无效（${acc.message || acc.code || '需要重新登录'}）：请在面板里重新粘贴 cookie`,
      );
    }),
    async execute() {
      const r = await backend(['auth']);
      return r.json;
    },
  });

  // ------------------------------------------------------------ 自动拉起守护进程

  if (cfg.autoStartDaemon) {
    ctx.effect(() => {
      let cancelled = false;
      const timer = setTimeout(() => {
        if (cancelled) return;
        startDaemon().catch(() => {
          /* 后台失败不影响插件加载，面板上能看到状态 */
        });
      }, 4000);
      return () => {
        cancelled = true;
        clearTimeout(timer);
      };
    }, 'bili-live: auto start daemon');
  }
}

// ---------------------------------------------------------------- Config

function fallbackSchema(spec) {
  const applyDefaults = (v) => {
    const out = { ...v };
    for (const [k, d] of Object.entries(spec)) {
      if (out[k] === undefined || out[k] === null) out[k] = d.default;
    }
    return out;
  };
  return {
    '~standard': {
      version: 1,
      vendor: 'bili-live-recorder',
      validate(value) {
        const v = value && typeof value === 'object' ? value : {};
        return { value: applyDefaults(v) };
      },
    },
  };
}

function buildConfigSchema(z) {
  const spec = {
    scriptDir: {
      type: 'string',
      default: '',
      description: 'Python 后端目录（含 bili_rec 包）。留空时会在插件目录旁、工作目录旁手动查找。',
    },
    workRoot: {
      type: 'string',
      default: '',
      description: '工作目录：settings/ups/queue/sessions 都在这里。留空 = <后端目录>/_bili_live_work。',
    },
    pythonPath: { type: 'string', default: '', description: 'Python 解释器路径。留空自动查找。' },
    ffmpegPath: { type: 'string', default: '', description: 'ffmpeg 路径。留空自动查找（会试 D:\\Shotcut）。' },
    ffprobePath: { type: 'string', default: '', description: 'ffprobe 路径。留空自动查找。' },
    cookieFile: {
      type: 'string',
      default: '',
      description: 'B 站 cookie 文件。留空 = <后端目录>/bili_cookie.txt（面板里可直接粘贴保存）。',
    },
    autoStartDaemon: {
      type: 'boolean',
      default: true,
      description: '插件加载后自动拉起录播守护进程（脱离 DSH 生命周期，DSH 退出也继续录）。',
    },
    startGraceMs: { type: 'number', default: 2500, description: '拉起守护进程后等多久确认没崩溃。' },
    backendTimeoutMs: { type: 'number', default: 180000, description: '同步类后端调用超时（毫秒）。' },
    maxLogTail: { type: 'number', default: 12000, description: '回传给面板/模型的日志尾部字符上限。' },
  };
  if (!z) return fallbackSchema(spec);
  return z.object({
    scriptDir: z.string().default('').description(spec.scriptDir.description),
    workRoot: z.string().default('').description(spec.workRoot.description),
    pythonPath: z.string().default('').description(spec.pythonPath.description),
    ffmpegPath: z.string().default('').description(spec.ffmpegPath.description),
    ffprobePath: z.string().default('').description(spec.ffprobePath.description),
    cookieFile: z.string().default('').description(spec.cookieFile.description),
    autoStartDaemon: z.boolean().default(true).description(spec.autoStartDaemon.description),
    startGraceMs: z.number().default(2500).description(spec.startGraceMs.description),
    backendTimeoutMs: z.number().default(180000).description(spec.backendTimeoutMs.description),
    maxLogTail: z.number().default(12000).description(spec.maxLogTail.description),
  });
}

const ConfigSchema = buildConfigSchema(loadSchemastery());

export { ConfigSchema as Config, DEFAULT_SETTINGS };
export const inject = ['tools'];
