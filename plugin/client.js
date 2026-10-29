/**
 * 直播小助手（浏览器侧）。
 *
 * 纯 JavaScript 注册，无 JSX、无构建步骤。数据来自 Host 侧接口
 * `/bili-live/api/data`（只读快照）与 `/bili-live/api/action`（写入动作），
 * 组件每 2 秒轮询一次拿守护进程 / 录制 / 队列 / 投稿的实时状态。
 *
 * 布局约定（与本宿主上已跑通的插件一致）：
 *   - `sidebar.panellist` 只提供左侧图标与标题，id 必须等于 `main` 的 key
 *   - `main`（keyed）渲染整页；宿主通过相同的 key 把两者配成一对
 *
 * 颜色与背景一律走宿主主题变量（--dsw-alias-*），保证明暗主题一致。
 */
window.__ModuleLoader__.load({
  id: 'bili-live',
  factory(require) {
    const React = require('react');
    const h = React.createElement;

    const API = '/bili-live/api';
    const POLL_MS = 2000;
    const PANEL_ID = 'bili-live';
    const NS = 'bili-live';

    // ---------------------------------------------------------------- 文案

    const T = {
      panel: '直播小助手',
      title: '直播小助手',
      intro: '监控 B站 / 虎牙 主播开播 · 自动分P录制 · 收尾后排队投稿到 B 站',
      loading: '正在读取…',
      refresh: '刷新',
      unknown: '未知',
      none: '无',
    };

    // ---------------------------------------------------------------- 工具

    function pad2(n) {
      const v = Number(n) || 0;
      return v < 10 ? '0' + v : String(v);
    }

    function fmtBytes(n) {
      const v = Number(n) || 0;
      if (v < 1024) return v + ' B';
      if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
      if (v < 1024 * 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + ' MB';
      return (v / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    }

    /** 秒 → 时长文本；不足 1 小时用 mm:ss，满足录制中的「已录时长 mm:ss」。 */
    function fmtDur(sec) {
      const s = Math.max(0, Math.round(Number(sec) || 0));
      const hh = Math.floor(s / 3600);
      const mm = Math.floor((s % 3600) / 60);
      const ss = s % 60;
      if (hh > 0) return hh + ':' + pad2(mm) + ':' + pad2(ss);
      return pad2(mm) + ':' + pad2(ss);
    }

    function fmtUptime(sec) {
      const s = Math.max(0, Math.floor(Number(sec) || 0));
      const d = Math.floor(s / 86400);
      const hh = Math.floor((s % 86400) / 3600);
      const mm = Math.floor((s % 3600) / 60);
      if (d > 0) return d + ' 天 ' + hh + ' 小时';
      if (hh > 0) return hh + ' 小时 ' + mm + ' 分';
      return mm + ' 分';
    }

    function fmtGb(v) {
      const n = Number(v);
      if (!Number.isFinite(n)) return T.unknown;
      return n.toFixed(1) + ' GB';
    }

    /** `now` 可能是 +08:00 也可能是 ISO(UTC)，统一显示成本地时间文本。 */
    function fmtNow(v) {
      const s = String(v || '');
      if (!s) return '';
      const t = Date.parse(s);
      if (!Number.isFinite(t)) return s;
      const d = new Date(t);
      return (
        d.getFullYear() +
        '-' +
        pad2(d.getMonth() + 1) +
        '-' +
        pad2(d.getDate()) +
        ' ' +
        pad2(d.getHours()) +
        ':' +
        pad2(d.getMinutes()) +
        ':' +
        pad2(d.getSeconds())
      );
    }

    /** sessionId = `<mid>_20261027_214422` → 2026-10-27 21:44:22 */
    function parseSessionId(sid) {
      const m = String(sid || '').match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
      if (!m) return { date: '', time: '', full: '' };
      const date = m[1] + '-' + m[2] + '-' + m[3];
      const time = m[4] + ':' + m[5] + ':' + m[6];
      return { date, time, full: date + ' ' + time };
    }

    function splitTags(text) {
      return String(text || '')
        .split(/[,，\s]+/)
        .map(function (s) {
          return s.trim();
        })
        .filter(function (s) {
          return s.length > 0;
        });
    }

    /** dtime（秒）→ <input type="datetime-local"> 的本地时间字符串 */
    function toLocalInput(sec) {
      const n = Number(sec) || 0;
      if (n <= 0) return '';
      const d = new Date(n * 1000);
      if (Number.isNaN(d.getTime())) return '';
      return (
        d.getFullYear() +
        '-' +
        pad2(d.getMonth() + 1) +
        '-' +
        pad2(d.getDate()) +
        'T' +
        pad2(d.getHours()) +
        ':' +
        pad2(d.getMinutes())
      );
    }

    /** <input type="datetime-local"> → dtime（秒）；0 表示不启用 */
    function fromLocalInput(text) {
      const v = String(text || '').trim();
      if (!v) return 0;
      const t = Date.parse(v);
      if (!Number.isFinite(t)) return 0;
      return Math.floor(t / 1000);
    }

    function fmtDtime(sec) {
      const n = Number(sec) || 0;
      if (n <= 0) return '';
      const v = toLocalInput(n);
      return v ? v.replace('T', ' ') : '';
    }

    const QN_OPTIONS = [
      [10000, '原画（10000）'],
      [20000, '4K 超清（20000）'],
      [400, '蓝光（400）'],
      [250, '超清（250）'],
    ];

    const CODEC_OPTIONS = [
      ['avc', 'AVC / H.264（兼容好）'],
      ['hevc', 'HEVC / H.265（仅 4K）'],
    ];

    const HUYA_QUALITY_OPTIONS = [
      ['', '原画（不转码）'],
      ['1080p', '1080p（约 4Mbps）'],
      ['720p', '720p（约 2.2Mbps）'],
      ['480p', '480p（约 1Mbps）'],
    ];

    const VIS_OPTIONS = [
      ['private', '仅自己可见'],
      ['public', '公开'],
      ['scheduled', '定时发布'],
    ];

    const TID_SUGGEST = [
      [21, '日常'],
      [4, '游戏'],
      [257, '虚拟主播'],
    ];

    const QUEUE_FILTERS = ['all', 'pending', 'running', 'failed', 'done', 'discarded'];
    const QUEUE_FILTER_LABELS = {
      all: '全部',
      pending: '待发布',
      running: '上传中',
      failed: '失败',
      done: '已投稿',
      discarded: '已丢弃',
    };

    const REC_STATE = {
      recording: '录制中',
      starting: '连接中',
      stopping: '正在停止',
      error: '录制出错',
      idle: '未在录制',
    };

    const SESSION_STATE = {
      recording: '录制中',
      finalizing: '收尾中',
      done: '已完成',
      failed: '失败',
    };

    /** 队列条目的归类：pending | running | done | failed | discarded */
    function itemKind(it) {
      const st = String((it && it.status) || '').toLowerCase();
      if (st === 'done' || st === 'published') return 'done';
      if (st === 'failed') return 'failed';
      if (st === 'discarded') return 'discarded';
      if (st === 'running' || st === 'uploading' || st === 'publishing') return 'running';
      // 后端只在非终态条目上挂 progress；有阶段或已有进度就说明在跑
      const pg = it && it.progress;
      const phase = String((pg && pg.phase) || '').toLowerCase();
      if (pg && (phase || Number(pg.percent) > 0)) return 'running';
      return 'pending';
    }

    /** 上传阶段的中文名；后端新增的阶段名直接原样显示。 */
    const PHASE_TEXT = {
      uploading: '上传中',
      upload: '上传中',
      publishing: '投稿中',
      publish: '投稿中',
      posting: '投稿中',
      submit: '投稿中',
      cover: '上传封面',
      danmaku: '上传弹幕',
      merge: '合并中',
      convert: '转码中',
      probe: '探测中',
    };

    function phaseText(phase) {
      const k = String(phase || '').toLowerCase();
      return PHASE_TEXT[k] || (k ? k : '');
    }

    function itemKindText(kind, it) {
      if (kind === 'done') return '已投稿';
      if (kind === 'failed') return '失败';
      if (kind === 'discarded') return '已丢弃';
      if (kind === 'running') {
        const pg = it && it.progress;
        const pct = Number(pg && pg.percent);
        const head = phaseText(pg && pg.phase) || '上传中';
        return head + (Number.isFinite(pct) ? ' ' + Math.max(0, Math.min(100, Math.round(pct))) + '%' : '');
      }
      return '待发布';
    }

    function badgeKindOfSort(itemKindValue) {
      if (itemKindValue === 'done') return 'Ok';
      if (itemKindValue === 'failed') return 'Live';
      if (itemKindValue === 'running') return 'Warn';
      if (itemKindValue === 'discarded') return 'Idle';
      return 'Idle';
    }

    function isVertical(it) {
      const w = Number(it && it.width) || 0;
      const h = Number(it && it.height) || 0;
      return w > 0 && h > 0 && h > w;
    }

    function visText(v) {
      const k = String(v || 'private');
      if (k === 'public') return '公开';
      if (k === 'scheduled') return '定时发布';
      return '仅自己可见';
    }

    // ---------------------------------------------------------------- 样式表
    //
    // 按钮必须走 CSS 类，不能只用内联 style：
    //  1) 内联样式拿不到 :hover / :active；
    //  2) 主按钮文字色必须用 --dsw-alias-label-primary-foreground 自动反色，
    //     否则深色主题下 brand 是近白色，会出现白底白字。
    const PANEL_STYLE_ID = 'bili-live/panel.css';
    const PANEL_CSS = `
.blbtn{display:inline-flex;align-items:center;justify-content:center;gap:4px;
  padding:5px 12px;border-radius:6px;border:1px solid var(--dsw-alias-border-l2);
  background:transparent;color:var(--dsw-alias-label-primary);
  cursor:pointer;font-size:12px;line-height:18px;font-family:inherit;
  white-space:nowrap;transition:background .12s ease,border-color .12s ease,color .12s ease}
.blbtn:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover)}
.blbtn:active:not(:disabled){background:var(--dsw-alias-interactive-bg-active)}
.blbtn:disabled{cursor:not-allowed;opacity:.4}
.blbtnPrimary{border-color:transparent;background:var(--dsw-alias-button-primary-fill);
  color:var(--dsw-alias-label-primary-foreground)}
.blbtnPrimary:hover:not(:disabled){background:var(--dsw-alias-button-primary-hover)}
.blbtnDanger{border-color:var(--dsw-alias-state-error-primary);
  background:transparent;color:var(--dsw-alias-state-error-primary)}
.blbtnDanger:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover-danger)}
.blbtnSm{padding:2px 8px;font-size:11px;line-height:16px}
.blcheck{width:14px;height:14px;flex:none;cursor:pointer;accent-color:var(--dsw-alias-brand-primary)}
.blbadge{display:inline-flex;align-items:center;gap:4px;padding:0 7px;border-radius:999px;
  border:1px solid var(--dsw-alias-border-l2);color:var(--dsw-alias-label-secondary);
  font-size:10px;line-height:17px;white-space:nowrap}
.blbadgeLive{border-color:var(--dsw-alias-state-error-primary);
  color:var(--dsw-alias-state-error-primary)}
.blbadgeOk{border-color:var(--dsw-alias-state-success-primary);
  color:var(--dsw-alias-state-success-primary)}
.blbadgeWarn{border-color:var(--dsw-alias-state-warn-primary);
  color:var(--dsw-alias-state-warn-primary)}
.blbadgeIdle{border-color:var(--dsw-alias-border-l2);color:var(--dsw-alias-label-secondary)}
.blcard{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;
  background:var(--dsw-alias-bg-layer-1);padding:14px 16px;margin-bottom:14px}
.blupcard{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;
  background:var(--dsw-alias-bg-layer-2);padding:10px 12px}
.blupcard:hover{background:var(--dsw-alias-interactive-bg-hover)}
.blrowitem{display:flex;gap:10px;align-items:flex-start;padding:8px;
  border:1px solid var(--dsw-alias-border-l1);border-radius:8px;
  background:var(--dsw-alias-bg-layer-2)}
.blrowitem:hover{background:var(--dsw-alias-interactive-bg-hover)}
.blcover{width:72px;height:72px;flex:none;object-fit:cover;border-radius:6px;
  background:var(--dsw-alias-bg-layer-1);display:block;
  border:1px solid var(--dsw-alias-border-l1)}
.blthumb{width:76px;height:44px;flex:none;object-fit:cover;border-radius:5px;
  background:var(--dsw-alias-bg-layer-1);display:block;
  border:1px solid var(--dsw-alias-border-l1)}
.blthumbTall{width:44px;height:66px;flex:none;object-fit:cover;border-radius:5px;
  background:var(--dsw-alias-bg-layer-1);display:block;
  border:1px solid var(--dsw-alias-border-l1)}
.bltitlelink{display:block;width:100%;text-align:left;background:transparent;
  border:0;padding:0;margin:0;font-family:inherit;font-size:13px;line-height:19px;
  color:var(--dsw-alias-label-primary);cursor:pointer;
  overflow:hidden;text-overflow:ellipsis;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical}
.bltitlelink:hover{color:var(--dsw-alias-brand-primary)}
.blinput{width:100%;box-sizing:border-box;padding:5px 9px;border-radius:6px;
  border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);
  color:var(--dsw-alias-label-primary);font-size:12px;line-height:18px;font-family:inherit}
.blinput:disabled{opacity:.5}
.blbar{height:5px;border-radius:3px;background:var(--dsw-alias-bg-layer-2);
  overflow:hidden;margin-top:6px;border:1px solid var(--dsw-alias-border-l1)}
.blbarFill{height:100%;background:var(--dsw-alias-brand-primary);transition:width .4s ease}
.blpre{margin:8px 0 0;padding:10px 12px;border-radius:8px;
  background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-secondary);
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;line-height:1.5;
  max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-all;
  border:1px solid var(--dsw-alias-border-l1)}
.blsec{margin-top:10px;padding-top:10px;border-top:1px solid var(--dsw-alias-border-l1)}
.blsecttl{font-size:12px;font-weight:600;color:var(--dsw-alias-label-primary);margin-bottom:6px}
`;

    /** 注入面板样式；在 apply 里用 ctx.effect 注册，插件卸载时一并移除。 */
    function ensurePanelStyle() {
      if (typeof document === 'undefined') return function () {};
      const existing = document.querySelector('style[data-plugin-css="' + PANEL_STYLE_ID + '"]');
      if (existing) return function () {};
      const tag = document.createElement('style');
      tag.dataset.plugin = 'bili-live';
      tag.dataset.pluginCss = PANEL_STYLE_ID;
      tag.textContent = PANEL_CSS;
      document.head.appendChild(tag);
      return function () {
        try {
          tag.remove();
        } catch (e) {
          /* 忽略 */
        }
      };
    }

    // ---------------------------------------------------------------- 内联样式

    const S = {
      page: {
        padding: '24px clamp(20px, 4vw, 40px) 40px',
        // main 容器是 overflow:hidden 的 flex 列，不给 width:100% 会收缩到内容宽
        width: '100%',
        maxWidth: '1120px',
        margin: '0 auto',
        color: 'var(--dsw-alias-label-primary)',
        background: 'var(--dsw-alias-bg-base)',
        fontSize: '13px',
        lineHeight: 1.6,
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        boxSizing: 'border-box',
      },
      head: { display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '4px', flexWrap: 'wrap' },
      h1: { fontSize: '20px', fontWeight: 500, margin: 0, lineHeight: '28px' },
      subtitle: { color: 'var(--dsw-alias-label-secondary)', fontSize: '12px', marginBottom: '14px' },
      spacer: { flex: '1 1 auto' },
      card: {},
      cardHead: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', marginBottom: '10px' },
      cardTitle: { fontSize: '14px', fontWeight: 600, marginRight: '2px' },
      muted: { color: 'var(--dsw-alias-label-secondary)', fontSize: '12px' },
      tiny: { color: 'var(--dsw-alias-label-secondary)', fontSize: '11px' },
      hint: { color: 'var(--dsw-alias-label-secondary)', fontSize: '11px', marginTop: '3px' },
      warnText: { color: 'var(--dsw-alias-state-warn-primary)', fontSize: '11px', marginTop: '3px' },
      errText: { color: 'var(--dsw-alias-state-error-primary)', fontSize: '11px', marginTop: '3px' },
      mono: {
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: '11px',
        color: 'var(--dsw-alias-label-secondary)',
      },
      btn: { className: 'blbtn' },
      btnSm: { className: 'blbtn blbtnSm' },
      check: { display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', cursor: 'pointer' },
      kvRow: {
        display: 'flex',
        gap: '8px',
        alignItems: 'center',
        flexWrap: 'wrap',
        padding: '5px 0',
        borderTop: '1px solid var(--dsw-alias-border-l1)',
      },
      toolbar: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', margin: '8px 0 10px' },
      upGrid: {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(330px, 1fr))',
        gap: '10px',
        marginTop: '4px',
      },
      upTop: { display: 'flex', gap: '10px', alignItems: 'flex-start' },
      grow: { flex: '1 1 auto', minWidth: 0 },
      upName: {
        fontSize: '13px',
        fontWeight: 600,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      },
      list: { display: 'flex', flexDirection: 'column', gap: '8px' },
      groupHead: {
        display: 'flex',
        gap: '8px',
        alignItems: 'baseline',
        flexWrap: 'wrap',
        margin: '14px 0 6px',
        fontSize: '12px',
        color: 'var(--dsw-alias-label-secondary)',
      },
      groupName: { color: 'var(--dsw-alias-label-primary)', fontWeight: 600, fontSize: '12px' },
      row: {
        display: 'flex',
        gap: '10px',
        alignItems: 'center',
        padding: '6px 0',
        borderTop: '1px solid var(--dsw-alias-border-l1)',
      },
      grid2: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: '10px' },
      sec: { marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--dsw-alias-border-l1)' },
      secttl: { fontSize: '12px', fontWeight: 600, marginBottom: '6px' },
      field: { display: 'flex', flexDirection: 'column', gap: '3px', marginBottom: '10px' },
      label: { fontSize: '11px', color: 'var(--dsw-alias-label-secondary)' },
      banner: {
        display: 'flex',
        gap: '10px',
        alignItems: 'flex-start',
        border: '1px solid var(--dsw-alias-state-error-primary)',
        color: 'var(--dsw-alias-state-error-primary)',
        borderRadius: '8px',
        padding: '8px 12px',
        margin: '10px 0',
        fontSize: '12px',
      },
      okBanner: {
        display: 'flex',
        gap: '10px',
        alignItems: 'flex-start',
        border: '1px solid var(--dsw-alias-state-success-primary)',
        color: 'var(--dsw-alias-state-success-primary)',
        borderRadius: '8px',
        padding: '8px 12px',
        margin: '10px 0',
        fontSize: '12px',
      },
      mask: {
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: 'var(--dsw-alias-bg-base)',
        opacity: 0.7,
        zIndex: 9998,
      },
      modal: {
        position: 'fixed',
        left: '50%',
        top: '50%',
        transform: 'translate(-50%, -50%)',
        width: 'min(680px, 94vw)',
        maxHeight: '88vh',
        overflow: 'auto',
        zIndex: 9999,
        border: '1px solid var(--dsw-alias-border-l1)',
        borderRadius: '12px',
        background: 'var(--dsw-alias-bg-layer-1)',
        padding: '16px 18px',
        boxSizing: 'border-box',
        boxShadow: '0 18px 64px rgba(0, 0, 0, .45)',
      },
      stepper: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' },
    };

    function button(label, onClick, extra) {
      const merged = Object.assign({}, S.btn, extra || {});
      const props = Object.assign(
        { type: 'button', onClick: onClick, disabled: Boolean(merged.disabled) },
        merged,
      );
      return h('button', props, label);
    }

    function badge(kind, text, titleText) {
      const cls = 'blbadge' + (kind ? ' blbadge' + kind : '');
      return h('span', { className: cls, title: titleText || undefined }, text);
    }

    function checkbox(label, checked, onChange, disabled) {
      return h(
        'label',
        { style: Object.assign({}, S.check, disabled ? { opacity: 0.5 } : null) },
        h('input', {
          type: 'checkbox',
          className: 'blcheck',
          checked: Boolean(checked),
          disabled: Boolean(disabled),
          onChange: function (e) {
            onChange(Boolean(e.target.checked));
          },
        }),
        h('span', null, label),
      );
    }

    function field(labelText, node, hintText) {
      return h(
        'div',
        { style: S.field },
        h('div', { style: S.label }, labelText),
        node,
        hintText ? h('div', { style: S.hint }, hintText) : null,
      );
    }

    function selectBox(value, options, onChange, disabled) {
      return h(
        'select',
        {
          className: 'blinput',
          value: String(value),
          disabled: Boolean(disabled),
          onChange: function (e) {
            onChange(e.target.value);
          },
        },
        options.map(function (op) {
          return h('option', { key: String(op[0]), value: String(op[0]) }, op[1]);
        }),
      );
    }

    function textInput(value, onChange, placeholder, disabled) {
      return h('input', {
        className: 'blinput',
        type: 'text',
        value: value == null ? '' : String(value),
        placeholder: placeholder || '',
        disabled: Boolean(disabled),
        onChange: function (e) {
          onChange(e.target.value);
        },
      });
    }

    function numInput(value, onChange, min, max, step) {
      return h('input', {
        className: 'blinput',
        type: 'number',
        value: value == null ? '' : String(value),
        min: min == null ? undefined : String(min),
        max: max == null ? undefined : String(max),
        step: step == null ? '1' : String(step),
        onChange: function (e) {
          const v = Number(e.target.value);
          onChange(Number.isFinite(v) ? v : 0);
        },
      });
    }

    // ---------------------------------------------------------------- 图标

    /** 录制红点 + 光盘：viewBox 24×24，单色 currentColor，跟随主题。 */
    function PanelIcon(props) {
      const size = (props && props.size) || 18;
      const active = Boolean(props && props.active);
      return h(
        'svg',
        {
          viewBox: '0 0 24 24',
          width: size,
          height: size,
          fill: 'currentColor',
          'aria-hidden': true,
          style: { opacity: active ? 1 : 0.85, display: 'block' },
        },
        // 光盘（外圈 + 中心孔）
        h('path', {
          d: 'M12 5.5a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 1.6a6.4 6.4 0 1 1 0 12.8 6.4 6.4 0 0 1 0-12.8Z',
        }),
        h('circle', { cx: 12, cy: 13.5, r: 2 }),
        // 录制红点
        h('circle', { cx: 5.2, cy: 5.2, r: 2.6 }),
      );
    }

    // ---------------------------------------------------------------- 数据

    function useLiveData() {
      const [data, setData] = React.useState(null);
      const [error, setError] = React.useState('');
      const [busy, setBusy] = React.useState('');

      const load = React.useCallback(async function () {
        try {
          const res = await fetch(API + '/data', { headers: { accept: 'application/json' } });
          if (!res.ok) throw new Error('HTTP ' + res.status);
          const json = await res.json();
          if (json && json.ok) {
            setData(json);
            setError('');
          } else {
            setError((json && json.error) || '读取失败');
          }
        } catch (e) {
          setError((e && e.message) || String(e));
        }
      }, []);

      React.useEffect(
        function () {
          let alive = true;
          load();
          const timer = setInterval(function () {
            if (alive) load();
          }, POLL_MS);
          return function () {
            alive = false;
            clearInterval(timer);
          };
        },
        [load],
      );

      const act = React.useCallback(
        async function (key, action, extra) {
          setBusy(key);
          try {
            const res = await fetch(API + '/action', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify(Object.assign({ action: action }, extra || {})),
            });
            let json = null;
            try {
              json = await res.json();
            } catch (e) {
              json = { ok: false, error: '返回内容不是 JSON（HTTP ' + res.status + '）' };
            }
            if (!json || typeof json !== 'object') {
              json = { ok: false, error: '后端返回为空' };
            } else if (json.ok !== true && !json.error) {
              json = { ok: false, error: 'HTTP ' + res.status };
            }
            await load();
            return json;
          } catch (e) {
            return { ok: false, error: (e && e.message) || String(e) };
          } finally {
            setBusy('');
          }
        },
        [load],
      );

      return { data: data, error: error, busy: busy, act: act, reload: load };
    }

    // ---------------------------------------------------------------- 本地持久化

    function readFlag(key) {
      try {
        return window.localStorage.getItem(key) || '';
      } catch (e) {
        return '';
      }
    }

    function writeFlag(key, value) {
      try {
        window.localStorage.setItem(key, String(value));
      } catch (e) {
        /* 存不进就算了 */
      }
    }

    // ---------------------------------------------------------------- 设置表单

    function normalizeSettings(s) {
      const src = s || {};
      return {
        pollSeconds: Number(src.pollSeconds) || 30,
        maxQn: Number(src.maxQn) || 10000,
        huyaQuality: String(src.huyaQuality || ''),
        preferCodec: src.preferCodec || 'avc',
        partMinutes: Math.max(1, Math.round((Number(src.partSeconds) || 7200) / 60)),
        danmaku: Boolean(src.danmaku),
        makeCover: Boolean(src.makeCover),
        keepRawFlv: Boolean(src.keepRawFlv),
        minFreeGb: Number.isFinite(Number(src.minFreeGb)) ? Number(src.minFreeGb) : 8,
        keepAwake: src.keepAwake === undefined ? true : Boolean(src.keepAwake),
        rollingPublish: src.rollingPublish === undefined ? true : Boolean(src.rollingPublish),
        titleTemplate: src.titleTemplate || '',
        descTemplate: src.descTemplate || '',
        tid: Number(src.tid) || 21,
        tagsText: Array.isArray(src.tags) ? src.tags.join(',') : String(src.tags || ''),
        copyright: Number(src.copyright) === 2 ? 2 : 1,
        source: src.source || '',
        visibility: src.visibility || 'private',
        dynamic: src.dynamic || '',
        noReprint: Number(src.noReprint) === 1 ? 1 : 0,
        openElec: Number(src.openElec) === 1 ? 1 : 0,
        closeDanmaku: Number(src.closeDanmaku) === 1 ? 1 : 0,
        closeReply: Number(src.closeReply) === 1 ? 1 : 0,
        autoPublish: Boolean(src.autoPublish),
        deleteLocalAfterPublish: Boolean(src.deleteLocalAfterPublish),
      };
    }

    function settingsPatch(form) {
      return {
        pollSeconds: Number(form.pollSeconds) || 30,
        maxQn: Number(form.maxQn) || 10000,
        huyaQuality: String(form.huyaQuality || ''),
        preferCodec: form.preferCodec,
        partSeconds: Math.max(60, Math.round((Number(form.partMinutes) || 120) * 60)),
        danmaku: Boolean(form.danmaku),
        makeCover: Boolean(form.makeCover),
        keepRawFlv: Boolean(form.keepRawFlv),
        minFreeGb: Number(form.minFreeGb) || 0,
        keepAwake: Boolean(form.keepAwake),
        rollingPublish: Boolean(form.rollingPublish),
        titleTemplate: form.titleTemplate,
        descTemplate: form.descTemplate,
        tid: Number(form.tid) || 21,
        tags: splitTags(form.tagsText),
        copyright: Number(form.copyright) === 2 ? 2 : 1,
        source: Number(form.copyright) === 2 ? form.source : '',
        visibility: form.visibility,
        dynamic: form.dynamic,
        noReprint: Number(form.noReprint) ? 1 : 0,
        openElec: Number(form.openElec) ? 1 : 0,
        closeDanmaku: Number(form.closeDanmaku) ? 1 : 0,
        closeReply: Number(form.closeReply) ? 1 : 0,
        autoPublish: Boolean(form.autoPublish),
        deleteLocalAfterPublish: Boolean(form.deleteLocalAfterPublish),
      };
    }

    // ---------------------------------------------------------------- 页面

    function PanelPage() {
      const live = useLiveData();
      const data = live.data;
      const busy = live.busy;
      const reload = live.reload;
      const act = live.act;

      const [banner, setBanner] = React.useState('');
      const [notice, setNotice] = React.useState('');

      // 二次确认：confirm 里存的是被武装的按钮 key，4 秒后自动复位
      const [confirm, setConfirm] = React.useState('');
      const timerRef = React.useRef(null);
      const arm = React.useCallback(function (key) {
        if (timerRef.current) clearTimeout(timerRef.current);
        setConfirm(key);
        timerRef.current = setTimeout(function () {
          setConfirm('');
          timerRef.current = null;
        }, 4000);
      }, []);
      React.useEffect(function () {
        return function () {
          if (timerRef.current) clearTimeout(timerRef.current);
        };
      }, []);

      const run = React.useCallback(
        async function (key, action, extra) {
          setBanner('');
          setNotice('');
          const r = await act(key, action, extra);
          if (!r || r.ok !== true) {
            setBanner(String((r && r.error) || '操作失败：后端没有返回错误文本'));
          }
          return r;
        },
        [act],
      );

      const [newMid, setNewMid] = React.useState('');
      const [addPlat, setAddPlat] = React.useState('bili');
      const [showAdd, setShowAdd] = React.useState(false);
      const [addMsg, setAddMsg] = React.useState('');

      const [cookieText, setCookieText] = React.useState('');
      const [authMsg, setAuthMsg] = React.useState('');
      // 合集：账号下的合集列表按需加载（走 checkAuth，一次拿账号+合集）
      const [seasonList, setSeasonList] = React.useState(null);
      const [seasonErr, setSeasonErr] = React.useState('');

      const [filter, setFilter] = React.useState(function () {
        const v = readFlag('bl:queueFilter');
        // 默认只看「待发布」：这个区块叫待发布队列，已投稿的属于历史记录，
        // 需要时在筛选里切到「已投稿」查看或删除。
        return QUEUE_FILTERS.indexOf(v) >= 0 ? v : 'pending';
      });
      React.useEffect(
        function () {
          writeFlag('bl:queueFilter', filter);
        },
        [filter],
      );

      const [settingsOpen, setSettingsOpen] = React.useState(function () {
        return readFlag('bl:settingsOpen') === '1';
      });
      React.useEffect(
        function () {
          writeFlag('bl:settingsOpen', settingsOpen ? '1' : '0');
        },
        [settingsOpen],
      );

      const [sel, setSel] = React.useState(function () {
        return new Set();
      });
      const [editing, setEditing] = React.useState(null);
      const [form, setForm] = React.useState(null);
      const dirtyRef = React.useRef(false);

      const settingsKey = data && data.settings ? JSON.stringify(data.settings) : '';

      React.useEffect(
        function () {
          if (!data || !data.settings) return;
          if (dirtyRef.current) return;
          setForm(normalizeSettings(data.settings));
        },
        [settingsKey],
      );

      const queueList = data && Array.isArray(data.queue) ? data.queue : [];
      const queueIdsKey = queueList
        .map(function (it) {
          return String(it.id || '');
        })
        .join(',');

      // 队列条目消失后自动把它从勾选里摘掉
      React.useEffect(
        function () {
          setSel(function (prev) {
            const alive = new Set(
              queueList.map(function (it) {
                return String(it.id || '');
              }),
            );
            let changed = false;
            const next = new Set();
            prev.forEach(function (id) {
              if (alive.has(id)) next.add(id);
              else changed = true;
            });
            return changed ? next : prev;
          });
        },
        [queueIdsKey],
      );

      // -------- 动作

      const doDaemon = async function (start) {
        const key = start ? 'daemonStart' : 'daemonStop';
        if (busy) return;
        const r = await run(key, start ? 'daemonStart' : 'daemonStop');
        if (r && r.ok) setNotice(start ? '已发出启动指令' : '已发出停止指令');
      };

      const doAdd = async function () {
        const raw = newMid.trim();
        if (!raw) {
          setAddMsg('请填写 UID / 房间号 / 直播间链接');
          return;
        }
        // 已经是 huya:xxx / bili:xxx 或贴了链接就原样交给后端自动识别；
        // 纯数字按上面选的平台补前缀，避免虎牙房间号和 B 站 UID 撞号。
        const explicit = /^(huya|bili|bilibili)\s*:/i.test(raw) || /huya\.com|bilibili\.com/i.test(raw);
        const target = explicit ? raw : String(addPlat || 'bili') + ':' + raw;
        setAddMsg('正在添加…');
        const r = await run('addUp', 'addUp', { mid: target });
        if (r && r.ok) {
          setNewMid('');
          setAddMsg('已添加 ' + String((r.mid || raw)));
        } else {
          setAddMsg('失败：' + String((r && r.error) || '未知错误').slice(0, 200));
        }
      };

      const doToggleUp = async function (up) {
        await run('toggle:' + up.mid, 'toggleUp', { mid: up.mid, enabled: !up.enabled });
      };

      const doStartRec = async function (up) {
        await run('start:' + up.mid, 'startRec', { mid: up.mid });
      };

      const doStopRec = async function (up) {
        await run('stop:' + up.mid, 'stopRec', { mid: up.mid });
      };

      // 合集：拉列表（复用 checkAuth 的返回）与保存到 settings.seasonMap
      const loadSeasons = async function () {
        setSeasonErr('');
        const r = await run('seasonList', 'checkAuth');
        const list = (r && r.seasons) || [];
        setSeasonList(list);
        if (!list.length) {
          setSeasonErr(String((r && (r.seasonsError || r.error)) || '账号下还没有合集（先去创作中心建一个）'));
        }
      };
      const saveUpSeason = async function (mid, seasonId) {
        const cur = (data && data.settings && data.settings.seasonMap) || {};
        const next = Object.assign({}, cur);
        if (!seasonId) {
          delete next[String(mid)];
        } else {
          const t = (seasonList || []).filter(function (x) {
            return String(x.id) === String(seasonId);
          })[0];
          next[String(mid)] = { id: Number(seasonId), title: (t && t.title) || '' };
        }
        const r = await run('seasonSave:' + mid, 'saveSettings', { settings: { seasonMap: next } });
        if (r && r.ok) {
          setNotice(seasonId ? '已设置该 主播的投稿合集' : '已清除该 主播的合集设置');
        }
      };

      const doRemoveUp = async function (up) {        const key = 'rmUp:' + up.mid;
        if (confirm !== key) {
          arm(key);
          return;
        }
        setConfirm('');
        await run('rmUp:' + up.mid, 'removeUp', { mid: up.mid });
      };

      const doSaveCookie = async function () {
        if (!cookieText.trim()) {
          setAuthMsg('输入框是空的，没有需要保存的内容');
          return;
        }
        setAuthMsg('正在保存…');
        const r = await run('saveCookie', 'saveCookie', { cookie: cookieText });
        if (r && r.ok) {
          setCookieText('');
          setAuthMsg('已保存凭据，之后录制与投稿会使用新的 cookie');
        } else {
          setAuthMsg('失败：' + String((r && r.error) || '未知错误').slice(0, 200));
        }
      };

      const doCheckAuth = async function () {
        setAuthMsg('正在检测…');
        const r = await run('checkAuth', 'checkAuth');
        const acc = (r && r.account) || {};
        if (r && r.loggedIn) {
          setAuthMsg('有效：' + (acc.uname || '') + (acc.mid ? '（mid ' + acc.mid + '）' : ''));
        } else {
          // 把后端/接口给的真实原因一并显示，别只给一句笼统的「无效」
          const why = (r && (r.error || r.note)) || acc.message || (acc.code ? 'code ' + acc.code : '');
          setAuthMsg('无效：' + (why || '账号未登录或 cookie 已过期') + '（可点「填写凭据」重新粘贴整行 cookie）');
        }
      };

      const doSaveSettings = async function () {
        if (!form) return;
        setBanner('');
        setNotice('');
        const r = await act('saveSettings', 'saveSettings', { settings: settingsPatch(form) });
        if (r && r.ok) {
          dirtyRef.current = false;
          setNotice('设置已保存');
        } else {
          setBanner(String((r && r.error) || '保存设置失败'));
        }
      };

      const doPublish = async function (ids) {
        if (!ids.length) return;
        const r = await run('publish', 'publish', { ids: ids });
        if (r && r.ok) {
          setSel(new Set());
          setNotice('已提交投稿：' + ids.length + ' 个分P');
        }
      };

      const doRetry = async function (ids) {
        if (!ids.length) return;
        const r = await run('retry', 'retry', { ids: ids });
        if (r && r.ok) setNotice('已提交重试：' + ids.length + ' 条');
      };

      // 删除 = 从队列移除 + 删本地成品文件（后端 delete_queue_items 干的）
      const doDiscard = async function (ids, key) {
        if (!ids.length) return;
        const k = key || 'discard';
        if (confirm !== k) {
          arm(k);
          return;
        }
        setConfirm('');
        const r = await run('discard', 'discard', { ids: ids });
        if (r && r.ok) {
          setSel(new Set());
          setNotice(
            '已删除 ' + (r.count || ids.length) + ' 条，释放本地空间 ' + (r.freedText || '0B'),
          );
        }
      };

      // 清空队列 = 删除所有「待发布/失败/已丢弃」条目及其本地成品文件。
      // 已投稿(done)的条目不动（要删请单条勾选后点「删除选中」）。
      const doClearQueue = async function () {
        const ids = queueList
          .filter(function (it) {
            const k = itemKind(it);
            return k === 'pending' || k === 'failed' || k === 'discarded';
          })
          .map(function (it) {
            return String(it.id);
          });
        if (!ids.length) {
          setNotice('队列里没有可清空的条目');
          return;
        }
        setConfirm('');
        const r = await run('clearQueue', 'discard', { ids: ids });
        if (r && r.ok) {
          setSel(new Set());
          setNotice(
            '已清空 ' + (r.count || ids.length) + ' 条，释放本地空间 ' + (r.freedText || '0B'),
          );
        }
      };

      const doFinalize = async function (sid) {
        const r = await run('finalize:' + sid, 'finalizeSession', { id: sid });
        if (r && r.ok) setNotice('已收尾并推进队列');
      };

      const doDeleteSession = async function (sid) {
        const key = 'delSes:' + sid;
        if (confirm !== key) {
          arm(key);
          return;
        }
        setConfirm('');
        await run('delSes:' + sid, 'deleteSession', { id: sid });
      };

      const doSaveEdit = async function () {
        if (!editing) return;
        const scheduled = editing.visibility === 'scheduled';
        const dtime = scheduled ? fromLocalInput(editing.dtimeText) : 0;
        if (scheduled && dtime <= 0) {
          setBanner('定时发布需要填写发布时间（本地时间，精确到分钟）');
          return;
        }
        const patch = {
          title: editing.title,
          desc: editing.desc,
          tag: editing.tag,
          tid: Number(editing.tid) || 21,
          visibility: editing.visibility,
          dtime: dtime,
        };
        const r = await run('updateItem:' + editing.id, 'updateItem', { id: editing.id, patch: patch });
        if (r && r.ok) {
          setEditing(null);
          setNotice('已更新发布参数');
        }
      };

      const openEdit = function (it) {
        setEditing({
          id: String(it.id || ''),
          title: it.title || '',
          desc: it.desc || '',
          tag: it.tag || '',
          tid: Number(it.tid) || 21,
          visibility: it.visibility || 'private',
          dtimeText: toLocalTime(it.dtime),
          partIndex: it.partIndex,
          uname: it.uname,
        });
      };

      function toLocalTime(v) {
        const n = Number(v) || 0;
        return n > 0 ? toLocalInput(n) : '';
      }

      const patchForm = function (key, value) {
        dirtyRef.current = true;
        setForm(function (prev) {
          if (!prev) return prev;
          const next = Object.assign({}, prev);
          next[key] = value;
          return next;
        });
      };

      // -------- 派生数据

      const daemon = (data && data.daemon) || {};
      const cookie = data && data.cookie ? data.cookie : null;
      const disk = (data && data.disk) || {};
      const ups = data && Array.isArray(data.ups) ? data.ups : [];
      const sessions = data && Array.isArray(data.sessions) ? data.sessions : [];
      const counts = (data && data.queueCounts) || {};
      const tids =
        data && data.publishOptions && Array.isArray(data.publishOptions.tids) && data.publishOptions.tids.length
          ? data.publishOptions.tids
          : TID_SUGGEST.map(function (p) {
              return { tid: p[0], name: p[1] };
            });

      const tidOptions = tids.map(function (t) {
        return [t.tid, t.name + '（' + t.tid + '）'];
      });

      const freeRaw = disk ? disk.freeGb : null;
      const freeGb = Number(freeRaw);
      const freeKnown = freeRaw !== null && freeRaw !== undefined && Number.isFinite(freeGb);
      const minFree = Number((data && data.settings && data.settings.minFreeGb) || 0);
      const diskLow = freeKnown && minFree > 0 && freeGb < minFree;

      // 队列分组
      const groups = [];
      const bySid = new Map();
      queueList.forEach(function (it) {
        const sid = String(it.sessionId || '');
        let g = bySid.get(sid);
        if (!g) {
          g = { sid: sid, uname: it.uname || '', items: [] };
          bySid.set(sid, g);
          groups.push(g);
        }
        g.items.push(it);
      });

      const visible = queueList.filter(function (it) {
        if (filter === 'all') return true;
        return itemKind(it) === filter;
      });

      const pendingVisible = visible.filter(function (it) {
        return itemKind(it) === 'pending';
      });
      const failedVisible = visible.filter(function (it) {
        return itemKind(it) === 'failed';
      });
      // 「清空队列」针对全部（不受筛选影响）的待发布/失败/已丢弃条目
      const clearableAll = queueList.filter(function (it) {
        const k = itemKind(it);
        return k === 'pending' || k === 'failed' || k === 'discarded';
      });
      const selIds = Array.from(sel).filter(function (id) {
        const it = queueList.filter(function (q) {
          return String(q.id) === id;
        })[0];
        if (!it) return false;
        const k = itemKind(it);
        // 已投稿(done)也允许选中删除：只删本地记录与文件，B 站上的稿件不受影响
        return k === 'pending' || k === 'failed' || k === 'running' || k === 'done';
      });
      const selCount = selIds.length;

      const busyDisabled = Boolean(busy);

      const onDaemonStart = function () {
        doDaemon(true);
      };
      const onDaemonStop = function () {
        doDaemon(false);
      };

      // -------- 渲染

      const children = [];

      // 标题
      children.push(
        h(
          'div',
          { key: 'head' },
          h(
            'div',
            { style: S.head },
            h('h1', { style: S.h1 }, T.title),
            h('span', { style: S.spacer }),
            data && data.now ? h('span', { style: S.mono }, '数据时间 ' + fmtNow(data.now)) : null,
            button(busy === 'reload' ? T.loading : T.refresh, function () {
              setBanner('');
              reload();
            }),
          ),
          h('div', { style: S.subtitle }, T.intro),
        ),
      );

      // 顶部状态条
      children.push(
        h(
          'div',
          { key: 'status', className: 'blcard', style: S.card },
          h(
            'div',
            { style: S.cardHead },
            h('span', { style: S.cardTitle }, '运行状态'),
            daemon.alive
              ? badge('Ok', '守护进程运行中')
              : badge('Idle', '守护进程已停止'),
            daemon.alive && daemon.pid ? h('span', { style: S.mono }, 'PID ' + daemon.pid) : null,
            daemon.alive ? h('span', { style: S.muted }, '已运行 ' + fmtUptime(daemon.uptimeSec)) : null,
            daemon.heartbeat ? h('span', { style: S.tiny }, '心跳 ' + daemon.heartbeat) : null,
            daemon.version ? badge('', 'v' + daemon.version) : null,
            h('span', { style: S.spacer }),
            daemon.alive
              ? button(busy === 'daemonStop' ? '正在停止…' : '停止守护进程', onDaemonStop, {
                  className: 'blbtn blbtnDanger',
                  disabled: busyDisabled,
                })
              : button(busy === 'daemonStart' ? '正在启动…' : '启动守护进程', onDaemonStart, {
                  className: 'blbtn blbtnPrimary',
                  disabled: busyDisabled,
                }),
          ),
          h(
            'div',
            { style: S.kvRow },
            h('span', { style: S.muted }, '磁盘剩余'),
            h(
              'span',
              { style: diskLow ? { color: 'var(--dsw-alias-state-warn-primary)' } : null },
              freeKnown ? fmtGb(freeGb) : T.unknown,
            ),
            minFree > 0
              ? h(
                  'span',
                  { style: diskLow ? S.warnText : S.tiny },
                  diskLow ? '低于磁盘下限 ' + minFree + ' GB，录制可能被拒绝' : '下限 ' + minFree + ' GB',
                )
              : null,
          ),
          h(
            'div',
            { style: S.kvRow },
            h('span', { style: S.muted }, '登录凭据'),
            cookie && cookie.present ? badge('Ok', '已保存') : badge('Live', '未设置'),
            cookie && cookie.present && Number.isFinite(Number(cookie.bytes))
              ? h('span', { style: S.tiny }, Number(cookie.bytes) + ' 字节')
              : null,
            cookie && cookie.account && cookie.account.uname
              ? h('span', { style: S.muted }, '账号 ' + cookie.account.uname)
              : null,
            cookie && cookie.account && cookie.account.mid
              ? h('span', { style: S.mono }, 'UID ' + cookie.account.mid)
              : null,
            cookie && cookie.file
              ? h('span', { style: Object.assign({}, S.tiny, { wordBreak: 'break-all' }) }, cookie.file)
              : null,
            button(busy === 'checkAuth' ? '正在检测…' : '检测有效性', doCheckAuth, {
              disabled: busyDisabled,
            }),
            button('填写凭据', function () {
              setSettingsOpen(true);
              try {
                const el = document.querySelector('[data-bl="cookie-box"]');
                if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
                if (el && el.focus) el.focus();
              } catch (e) {
                /* 忽略 */
              }
            }, { disabled: busyDisabled }),
            !cookie || !cookie.present
              ? h('span', { style: S.warnText }, '未设置 B 站 cookie 时无法投稿；虎牙录制不需要 cookie，直接在下方列表填房间号即可')
              : null,
            authMsg ? h('span', { style: authedStyle(authMsg) }, authMsg) : null,
          ),
        ),
      );

      function authedStyle(msg) {
        if (/^无效|失败/.test(msg)) return S.errText;
        if (/^有效|已保存/.test(msg)) return { color: 'var(--dsw-alias-state-success-primary)', fontSize: '11px' };
        return S.tiny;
      }

      // 错误 / 成功提示
      if (banner) {
        children.push(
          h(
            'div',
            { key: 'banner', style: S.banner },
            h('span', { style: { flex: '1 1 auto', wordBreak: 'break-all' } }, banner),
            button('关闭', function () {
              setBanner('');
            }, S.btnSm),
          ),
        );
      } else if (live.error) {
        children.push(
          h(
            'div',
            { key: 'banner2', style: S.banner },
            h('span', { style: { flex: '1 1 auto' } }, '读取数据失败：' + live.error),
          ),
        );
      } else if (notice) {
        children.push(h('div', { key: 'notice', style: S.okBanner }, notice));
      }

      // ---------------- 主播列表
      const upChildren = [
        h(
          'div',
          { key: 'h', style: S.cardHead },
          h('span', { style: S.cardTitle }, '主播'),
          h('span', { style: S.tiny }, ups.length + ' 位'),
          h('span', { style: S.spacer }),
          button(showAdd ? '收起' : '添加主播', function () {
            setShowAdd(!showAdd);
            setAddMsg('');
          }, S.btnSm),
        ),
      ];

      if (showAdd) {
        upChildren.push(
          h(
            'div',
            { key: 'add', style: Object.assign({}, S.stepper, { marginBottom: '10px' }) },
            h('select', {
              className: 'blinput',
              value: addPlat,
              disabled: busyDisabled,
              style: { flex: '0 0 92px', width: '92px' },
              onChange: function (e) {
                setAddPlat(e.target.value);
              },
            },
              h('option', { value: 'bili' }, 'B站'),
              h('option', { value: 'huya' }, '虎牙'),
            ),
            h(
              'div',
              { style: { flex: '1 1 260px', minWidth: '200px' } },
              textInput(
                newMid,
                setNewMid,
                addPlat === 'huya'
                  ? '虎牙房间号，比如 919191（也可直接贴 huya.com 链接）'
                  : 'B站 UID 或直播间号（也可直接贴直播间链接）',
                busyDisabled,
              ),
            ),
            button(busy === 'addUp' ? '正在添加…' : '添加', doAdd, {
              className: 'blbtn blbtnPrimary',
              disabled: busyDisabled,
            }),
          ),
        );
        if (addMsg) {
          upChildren.push(
            h('div', { key: 'addmsg', style: Object.assign({}, S.tiny, { marginBottom: '8px' }) }, addMsg),
          );
        }
      }

      if (!ups.length) {
        upChildren.push(
          h('div', { key: 'empty', style: S.tiny }, '还没有监控的主播。点「添加主播」，选平台后填 UID / 房间号，或直接贴直播间链接。'),
        );
      } else {
        upChildren.push(
          h(
            'div',
            { key: 'grid', style: S.upGrid },
            ups.map(function (up, i) {
              return renderUpCard(up, i);
            }),
          ),
        );
      }

      children.push(h('div', { key: 'ups', className: 'blcard', style: S.card }, upChildren));

      function renderUpCard(up, index) {
        const rec = up.recording || null;
        const state = String((rec && rec.state) || '').toLowerCase();
        const recording = state === 'recording';
        const stopping = Boolean(rec && rec.stopping) || state === 'stopping';
        const mid = String(up.mid || '');
        const curPart = Number(rec && rec.curPart) || 0;
        const parts = Number(rec && rec.parts) || 0;
        const partName = curPart > 0 ? 'P' + curPart + (parts > 0 ? '/' + parts : '') : '-';

        const meta = h(
          'div',
          { style: S.grow },
          h(
            'div',
            { style: { display: 'flex', gap: '6px', alignItems: 'center' } },
            h('span', { style: S.upName, title: up.uname || mid }, up.uname || mid || '未知 主播'),
            up.live ? badge('Live', '直播中') : badge('Idle', '未开播'),
            up.enabled ? null : badge('Warn', '已暂停监控'),
            recording ? badge('Warn', stopping ? '正在停止' : '录制中') : null,
          ),
          h(
            'div',
            { style: Object.assign({}, S.tiny, { marginTop: '2px' }) },
            ((up.platform || 'bili') === 'huya' ? '虎牙' : 'B站') +
              ' 直播间 ' + (up.roomId != null ? up.roomId : '-') +
              (up.online != null ? ' · 人气 ' + up.online : '') +
              (mid ? ' · UID ' + mid : ''),
          ),
          up.title ? h('div', { style: Object.assign({}, S.tiny, { marginTop: '2px' }) }, up.title) : null,
        );

        // 头像优先（face），没有头像就退回直播封面
        const coverSrc = up.face || up.cover;
        const cover = coverSrc
          ? h('img', {
              className: 'blcover',
              src: String(coverSrc),
              alt: '',
              loading: 'lazy',
              onError: function (e) {
                e.currentTarget.style.visibility = 'hidden';
              },
            })
          : h('div', {
              className: 'blcover',
              style: { display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '11px', color: 'var(--dsw-alias-label-secondary)' },
            }, '无封面');

        const rows = [];

        if (rec) {
          const bitrate = Number(rec.bitrateKbps) || 0;
          rows.push(
            h(
              'div',
              { key: 'rec', style: { marginTop: '8px' } },
              h(
                'div',
                { style: Object.assign({}, S.tiny, { display: 'flex', gap: '10px', flexWrap: 'wrap' }) },
                h('span', null, '分P ' + partName),
                h('span', null, '已录 ' + fmtDur((Number(rec.curPartMs) || 0) / 1000)),
                h('span', null, '本P ' + fmtBytes(rec.curPartBytes)),
                h('span', null, '合计 ' + fmtBytes(rec.bytes)),
                bitrate > 0 ? h('span', null, bitrate + ' kbps') : null,
                h('span', null, '弹幕 ' + (Number(rec.danmaku) || 0) + ' 条'),
                Number(rec.reconnects) > 0
                  ? h('span', { style: { color: 'var(--dsw-alias-state-warn-primary)' } }, '重连 ' + rec.reconnects + ' 次')
                  : h('span', null, '重连 0 次'),
              ),
              h(
                'div',
                { style: Object.assign({}, S.tiny, { marginTop: '2px' }) },
                h('span', null, REC_STATE[state] || (state ? state : '空闲')),
                rec.stream ? h('span', { style: S.mono }, ' · ' + rec.stream) : null,
              ),
              rec.error ? h('div', { style: S.errText }, String(rec.error)) : null,
            ),
          );
        }

        const seasonMap = (data && data.settings && data.settings.seasonMap) || {};
        const mySeason = seasonMap[String(mid)] || null;
        rows.push(
          h(
            'div',
            {
              key: 'season',
              style: Object.assign({}, S.tiny, {
                display: 'flex',
                gap: '6px',
                alignItems: 'center',
                flexWrap: 'wrap',
                marginTop: '6px',
              }),
            },
            h('span', null, '投稿合集：'),
            mySeason ? badge('Ok', mySeason.title || String(mySeason.id)) : h('span', null, '未设置'),
            h(
              'select',
              {
                className: 'blinput',
                style: { width: 'auto', minWidth: '130px' },
                value: mySeason ? String(mySeason.id) : '',
                onChange: function (e) {
                  saveUpSeason(mid, e.target.value);
                },
              },
              h('option', { key: 'none', value: '' }, seasonList ? '不设合集' : '（先点「刷新合集」）'),
              (seasonList || []).map(function (s) {
                return h(
                  'option',
                  { key: String(s.id), value: String(s.id) },
                  (s.title || '合集 ' + s.id) + (s.epCount ? '（' + s.epCount + 'P）' : ''),
                );
              }),
            ),
            button('刷新合集', function () {
              loadSeasons();
            }, { className: 'blbtn blbtnSm', disabled: busyDisabled }),
            seasonErr ? h('span', { style: S.errText }, seasonErr) : null,
          ),
        );

        rows.push(
          h(
            'div',
            { key: 'btns', style: Object.assign({}, S.stepper, { marginTop: '8px' }) },
            recording || stopping
              ? button(busy === 'stop:' + mid ? '正在停止…' : '停止录制', function () {
                  doStopRec(up);
                }, { className: 'blbtn blbtnSm blbtnDanger', disabled: busyDisabled || stopping })
              : button(busy === 'start:' + mid ? '正在开始…' : '开始录制', function () {
                  doStartRec(up);
                }, { className: 'blbtn blbtnSm blbtnPrimary', disabled: busyDisabled }),
            button(up.enabled ? '暂停监控' : '恢复监控', function () {
              doToggleUp(up);
            }, { className: 'blbtn blbtnSm', disabled: busyDisabled }),
            button(confirm === 'rmUp:' + mid ? '再点一次确认' : '移除', function () {
              doRemoveUp(up);
            }, { className: 'blbtn blbtnSm blbtnDanger', disabled: busyDisabled }),
          ),
        );

        return h('div', { key: mid || 'up' + index, className: 'blupcard' },
          h('div', { style: S.upTop }, cover, meta),
          rows,
        );
      }

      // ---------------- 待发布队列
      const queueChildren = [
        h(
          'div',
          { key: 'h', style: S.cardHead },
          h('span', { style: S.cardTitle }, '待发布队列'),
          h('span', { style: S.tiny },
            '待发布 ' + (Number(counts.pending) || 0) +
              ' · 已投稿 ' + (Number(counts.done) || 0) +
              ' · 失败 ' + (Number(counts.failed) || 0)),
          h('span', { style: S.spacer }),
          h(
            'label',
            { style: Object.assign({}, S.check, { gap: '4px' }) },
            h('span', { style: S.tiny }, '筛选'),
            h(
              'select',
              {
                className: 'blinput',
                style: { width: 'auto', minWidth: '104px' },
                value: filter,
                onChange: function (e) {
                  setFilter(e.target.value);
                },
              },
              QUEUE_FILTERS.map(function (k) {
                return h('option', { key: k, value: k }, QUEUE_FILTER_LABELS[k]);
              }),
            ),
          ),
        ),
        h(
          'div',
          { key: 'tb', style: S.toolbar },
          button('全选当前(' + visible.length + ')', function () {
            setSel(function (prev) {
              const next = new Set(prev);
              visible.forEach(function (it) {
                next.add(String(it.id));
              });
              return next;
            });
          }, { className: 'blbtn blbtnSm', disabled: !visible.length }),
          button('清空勾选', function () {
            setSel(new Set());
          }, { className: 'blbtn blbtnSm', disabled: !sel.size }),
          button(
            confirm === 'clearQueue' ? '再点一次确认清空' : '清空队列(' + clearableAll.length + ')',
            function () {
              if (confirm !== 'clearQueue') {
                arm('clearQueue');
                return;
              }
              doClearQueue();
            },
            { className: 'blbtn blbtnSm blbtnDanger', disabled: busyDisabled || !clearableAll.length },
          ),
          button(busy === 'publish' ? '正在提交…' : '发布选中(' + selCount + ')', function () {
            doPublish(selIds);
          }, { className: 'blbtn blbtnSm blbtnPrimary', disabled: busyDisabled || !selCount }),
          button(busy === 'retry' ? '正在重试…' : '重试失败(' + failedVisible.length + ')', function () {
            doRetry(
              failedVisible.map(function (it) {
                return String(it.id);
              }),
            );
          }, { className: 'blbtn blbtnSm', disabled: busyDisabled || !failedVisible.length }),
          button(confirm === 'discard' ? '再点一次确认删除' : '删除选中(' + selCount + ')', function () {
            doDiscard(selIds, 'discard');
          }, { className: 'blbtn blbtnSm blbtnDanger', disabled: busyDisabled || !selCount }),
        ),
      ];

      if (!queueList.length) {
        queueChildren.push(
          h('div', { key: 'empty', style: S.tiny }, '队列是空的。直播录完并收尾后，分P会出现在这里等你投稿。'),
        );
      } else if (!visible.length) {
        queueChildren.push(h('div', { key: 'empty2', style: S.tiny }, '当前筛选下没有条目。'));
      } else {
        if (filter === 'pending' && Number(counts.done) > 0) {
          queueChildren.push(
            h(
              'div',
              { key: 'donehint', style: S.tiny },
              '另有 ' +
                Number(counts.done) +
                ' 条已投稿记录（稿件已在 B 站）。把上面的筛选切到「已投稿」可以查看、并删掉本地记录与文件；删了不影响 B 站上的稿件。',
            ),
          );
        }
        const shown = new Set();
        const blocks = [];
        groups.forEach(function (g) {
          const items = g.items.filter(function (it) {
            if (filter === 'all') return true;
            return itemKind(it) === filter;
          });
          if (!items.length) return;
          const sid = g.sid;
          if (shown.has(sid)) return;
          shown.add(sid);
          const stamp = parseSessionId(sid);
          blocks.push(
            h(
              'div',
              { key: 'g:' + sid, style: S.groupHead },
              h('span', { style: S.groupName }, items[0].uname || g.uname || '未知 主播'),
              h('span', null, stamp.full || ''),
              h('span', null, '共 ' + g.items.length + ' P'),
              h('span', { style: S.spacer }),
              button('全选本组', function () {
                setSel(function (prev) {
                  const next = new Set(prev);
                  items.forEach(function (it) {
                    const k = itemKind(it);
                    if (k === 'pending' || k === 'failed' || k === 'running') next.add(String(it.id));
                  });
                  return next;
                });
              }, S.btnSm),
            ),
          );
          blocks.push(
            h(
              'div',
              { key: 'gi:' + sid, style: S.list },
              items.map(function (it) {
                return renderQueueItem(it);
              }),
            ),
          );
        });
        queueChildren.push(h('div', { key: 'list' }, blocks));
      }

      children.push(h('div', { key: 'queue', className: 'blcard', style: S.card }, queueChildren));

      function renderQueueItem(it) {
        const id = String(it.id || '');
        const kind = itemKind(it);
        const checked = sel.has(id);
        const vertical = isVertical(it);
        const pct = Number(it.progress && it.progress.percent);
        const note = it.progress && it.progress.note ? String(it.progress.note) : '';
        const w = Number(it.width) || 0;
        const hh = Number(it.height) || 0;

        const thumb = it.cover
          ? h('img', {
              className: vertical ? 'blthumbTall' : 'blthumb',
              src: String(it.cover),
              alt: '',
              loading: 'lazy',
              onError: function (e) {
                e.currentTarget.style.visibility = 'hidden';
              },
            })
          : h('div', {
              className: vertical ? 'blthumbTall' : 'blthumb',
              style: { display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '10px', color: 'var(--dsw-alias-label-secondary)' },
            }, '无封面');

        const lines = [];

        lines.push(
          h(
            'div',
            { key: 'meta', style: Object.assign({}, S.tiny, { display: 'flex', gap: '10px', flexWrap: 'wrap', marginTop: '2px' }) },
            h('span', null, 'P' + (Number(it.partIndex) || 1)),
            h('span', null, fmtDur(it.durationSec)),
            h('span', null, fmtBytes(it.bytes)),
            w > 0 && hh > 0
              ? h('span', null, w + '×' + hh + (vertical ? ' 竖屏' : ''))
              : null,
            it.tid ? h('span', null, '分区 ' + tidName(it.tid)) : null,
            it.tag ? h('span', null, '标签 ' + it.tag) : null,
          ),
        );

        if (kind === 'running') {
          const p = Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0;
          lines.push(
            h(
              'div',
              { key: 'prog' },
              h('div', { className: 'blbar' }, h('div', { className: 'blbarFill', style: { width: p + '%' } })),
              h(
                'div',
                { style: Object.assign({}, S.tiny, { marginTop: '2px' }) },
                (Number.isFinite(pct) ? pct.toFixed(0) + '%' : '进行中') + (note ? ' · ' + note : ''),
              ),
            ),
          );
        }

        if (kind === 'done') {
          lines.push(
            h(
              'div',
              { key: 'done', style: Object.assign({}, S.tiny, { marginTop: '2px' }) },
              it.bvid ? h('span', { style: S.mono }, 'BV ' + it.bvid) : null,
              Number(it.aid) ? h('span', { style: S.mono }, ' · aid ' + it.aid) : null,
              it.visibility ? h('span', null, ' · ' + visText(it.visibility)) : null,
            ),
          );
        }

        if (it.visibilityNote) {
          lines.push(h('div', { key: 'vis', style: S.warnText }, '可见范围提示：' + String(it.visibilityNote)));
        }

        if (kind === 'failed' && it.error) {
          lines.push(h('div', { key: 'err', style: S.errText }, String(it.error)));
        }

        if (it.visibility === 'scheduled' && Number(it.dtime) > 0) {
          lines.push(h('div', { key: 'dt', style: S.tiny }, '定时发布 ' + fmtDtime(it.dtime)));
        }

        return h(
          'div',
          { key: id, className: 'blrowitem' },
          h('input', {
            type: 'checkbox',
            className: 'blcheck',
            checked: checked,
            style: { marginTop: '3px' },
            onChange: function () {
              setSel(function (prev) {
                const next = new Set(prev);
                if (next.has(id)) next.delete(id);
                else next.add(id);
                return next;
              });
            },
          }),
          thumb,
          h(
            'div',
            { style: S.grow },
            h(
              'button',
              {
                type: 'button',
                className: 'bltitlelink',
                title: '点击编辑标题、简介、标签、分区与可见范围',
                onClick: function () {
                  openEdit(it);
                },
              },
              it.title || '（无标题）',
            ),
            lines,
            h(
              'div',
              { key: 'ops', style: Object.assign({}, S.stepper, { marginTop: '4px' }) },
              badge(badgeKindOfSort(kind), itemKindText(kind, it)),
              button('编辑', function () {
                openEdit(it);
              }, S.btnSm),
              kind === 'failed'
                ? button('重试', function () {
                    doRetry([id]);
                  }, S.btnSm)
                : null,
              button(
                confirm === 'discard:' + id ? '再点一次确认' : '删除',
                function () {
                  doDiscard([id], 'discard:' + id);
                },
                {
                  className: 'blbtn blbtnSm blbtnDanger',
                  disabled: busyDisabled || kind === 'running',
                  title:
                    kind === 'done'
                      ? '删掉这条记录与本地文件；B 站上的稿件不会被删除'
                      : kind === 'running'
                        ? '正在上传/投稿，等它结束再删'
                        : '删除这条并删除本地成品文件',
                },
              ),
            ),
          ),
        );
      }

      function tidName(tid) {
        const t = Number(tid);
        for (let i = 0; i < tidOptions.length; i += 1) {
          if (Number(tidOptions[i][0]) === t) return String(tidOptions[i][1]).replace(/（\d+）$/, '');
        }
        return String(tid);
      }

      // ---------------- 已录直播
      const sesChildren = [
        h(
          'div',
          { key: 'h', style: S.cardHead },
          h('span', { style: S.cardTitle }, '已录直播'),
          h('span', { style: S.tiny }, sessions.length + ' 场'),
        ),
      ];

      if (!sessions.length) {
        sesChildren.push(h('div', { key: 'empty', style: S.tiny }, '还没有录制会话。'));
      } else {
        sesChildren.push(
          h(
            'div',
            { key: 'list', style: { marginTop: '4px' } },
            sessions.map(function (s, i) {
              const sid = String(s.id || '');
              const st = String(s.status || '').toLowerCase();
              const stBadge = st === 'recording' ? 'Live' : st === 'finalizing' ? 'Warn' : st === 'done' ? 'Ok' : st === 'failed' ? 'Live' : 'Idle';
              // 本地文件数：后端没给就是 null（不能当 0，否则全部会话都被误判为空壳）
              const localFiles = typeof s.localFiles === 'number' ? s.localFiles : null;
              // 队列条数：后端没给就按 sessionId 自己数
              const queueCount = typeof s.queueCount === 'number'
                ? s.queueCount
                : (data.queue || []).filter(function (q) {
                    return String((q && q.sessionId) || '') === sid;
                  }).length;
              // 录制中/收尾中本来就还没有成品文件，不能算「空壳」
              const active = st === 'recording' || st === 'finalizing' || st === 'starting';
              // 只有「确实知道本地没文件」且「队列里也没有」才算空壳
              const hollow = !active && localFiles === 0 && queueCount === 0;
              return h(
                'div',
                { key: sid || 'ses' + i, style: S.row },
                h(
                  'div',
                  { style: S.grow },
                  h(
                    'div',
                    { style: { display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' } },
                    h('span', { style: S.upName, title: s.title || '' }, s.uname || s.mid || '未知 主播'),
                    badge(stBadge, SESSION_STATE[st] || (st || T.unknown)),
                    localFiles === null
                      ? null
                      : localFiles > 0
                        ? badge('Ok', '本地 ' + localFiles + ' 个文件')
                        : active
                          ? badge('Live', '正在写入')
                          : badge('Idle', '本地无文件'),
                    queueCount > 0 ? badge('Ok', '队列 ' + queueCount + ' 条') : null,
                    s.startText ? h('span', { style: S.tiny }, String(s.startText)) : null,
                  ),
                  h(
                    'div',
                    { style: Object.assign({}, S.tiny, { display: 'flex', gap: '10px', flexWrap: 'wrap', marginTop: '2px' }) },
                    h('span', null, (s.title || '（无标题）')),
                    h('span', null, (Number(s.parts) || 0) + ' P'),
                    h('span', null, fmtDur(s.durationSec)),
                    h('span', null, '录到 ' + fmtBytes(s.bytes)),
                    h('span', null, '弹幕 ' + (Number(s.danmaku) || 0) + ' 条'),
                    localFiles > 0
                      ? h('span', null, '盘上 ' + fmtBytes(s.localBytes))
                      : null,
                  ),
                  hollow
                    ? h(
                        'div',
                        { style: S.tiny },
                        '这条只剩录制记录：成品文件与队列条目都没有了（可能已被删除，或录到的分段太短没进队列）。点「删除记录」可把这条也清掉。',
                      )
                    : null,
                  s.error ? h('div', { style: S.errText }, String(s.error)) : null,
                ),
                h(
                  'div',
                  { style: Object.assign({}, S.stepper, { flex: '0 0 auto' }) },
                  st === 'done' || localFiles === 0
                    ? null
                    : button(busy === 'finalize:' + sid ? '正在收尾…' : '收尾进队列', function () {
                        doFinalize(sid);
                      }, { className: 'blbtn blbtnSm', disabled: busyDisabled }),
                  st === 'recording' || st === 'finalizing'
                    ? null
                    : button(
                        confirm === 'delSes:' + sid
                          ? '再点一次确认'
                          : localFiles > 0
                            ? '删除本地'
                            : '删除记录',
                        function () {
                          doDeleteSession(sid);
                        },
                        { className: 'blbtn blbtnSm blbtnDanger', disabled: busyDisabled },
                      ),
                ),
              );
            }),
          ),
        );
      }

      children.push(h('div', { key: 'sessions', className: 'blcard', style: S.card }, sesChildren));

      // ---------------- 设置
      const setChildren = [
        h(
          'div',
          { key: 'h', style: S.cardHead },
          h('span', { style: S.cardTitle }, '设置'),
          h('span', { style: S.tiny }, settingsOpen ? '录制 / 发布默认值 / 凭据' : '已折叠'),
          h('span', { style: S.spacer }),
          button(settingsOpen ? '收起' : '展开', function () {
            setSettingsOpen(!settingsOpen);
          }, S.btnSm),
        ),
      ];

      if (settingsOpen) {
        if (!form) {
          setChildren.push(h('div', { key: 'loading', style: S.tiny }, T.loading));
        } else {
          setChildren.push(h('div', { key: 'secRec', style: { marginTop: '2px' } },
            h('div', { style: S.secttl }, '录制'),
            h(
              'div',
              { style: S.grid2 },
              field('分P 时长（分钟）', numInput(form.partMinutes, function (v) {
                patchForm('partMinutes', v);
              }, 1, 1440, 1), '默认 120 分钟一段，录满自动切下一段'),
              field('清晰度', selectBox(form.maxQn, QN_OPTIONS, function (v) {
                patchForm('maxQn', Number(v));
              }, busyDisabled), '原画 10000 / 4K 20000 / 蓝光 400 / 超清 250'),
              field('虎牙录完画质', selectBox(form.huyaQuality, HUYA_QUALITY_OPTIONS, function (v) {
                patchForm('huyaQuality', v);
              }, busyDisabled), '虎牙只有源画质一路流，选降档会在录制结束后转码压缩（比较吃 CPU）'),
              field('编码偏好', selectBox(form.preferCodec, CODEC_OPTIONS, function (v) {
                patchForm('preferCodec', v);
              }, busyDisabled), 'AVC 兼容性好；HEVC 体积小但仅 4K 有'),
              field('轮询间隔（秒）', numInput(form.pollSeconds, function (v) {
                patchForm('pollSeconds', v);
              }, 5, 600, 5), '开播检测频率'),
              field('磁盘下限（GB）', numInput(form.minFreeGb, function (v) {
                patchForm('minFreeGb', v);
              }, 0, 10000, 1), '剩余空间低于该值不再开始新录制'),
            ),
            h(
              'div',
              { style: Object.assign({}, S.stepper, { marginBottom: '4px' }) },
              checkbox('录弹幕', form.danmaku, function (v) {
                patchForm('danmaku', v);
              }),
              checkbox('自动生成封面', form.makeCover, function (v) {
                patchForm('makeCover', v);
              }),
              checkbox('保留原始 FLV', form.keepRawFlv, function (v) {
                patchForm('keepRawFlv', v);
              }),
              checkbox('录制时阻止 Mac 休眠', form.keepAwake, function (v) {
                patchForm('keepAwake', v);
              }),
              checkbox('长直播分段即投（满一个分P 立刻上传并删本地）', form.rollingPublish, function (v) {
                patchForm('rollingPublish', v);
              }),
            ),
          ));

          setChildren.push(h('div', { key: 'secPub', style: S.sec },
            h('div', { style: S.secttl }, '发布默认值'),
            field('标题模板', textInput(form.titleTemplate, function (v) {
              patchForm('titleTemplate', v);
            }, '【录播】{date} {uname} {title}', busyDisabled),
              '可用占位符：{date} {uname} {title} {roomUrl} {duration} {part}'),
            field('简介模板',
              h('textarea', {
                className: 'blinput',
                rows: 3,
                value: form.descTemplate,
                onChange: function (e) {
                  patchForm('descTemplate', e.target.value);
                },
              }),
              '同样支持：{date} {uname} {title} {roomUrl} {duration} {part}'),
            h(
              'div',
              { style: S.grid2 },
              field('默认分区',
                selectBox(form.tid, tidOptions, function (v) {
                  patchForm('tid', Number(v));
                }, busyDisabled),
                '建议：日常 21 / 游戏 4 / 虚拟主播 257'),
              field('可见范围', selectBox(form.visibility, VIS_OPTIONS, function (v) {
                patchForm('visibility', v);
              }, busyDisabled)),
            ),
            field('标签（逗号分隔）', textInput(form.tagsText, function (v) {
              patchForm('tagsText', v);
            }, '录播,{uname}', busyDisabled), '每个标签不超过 20 字，最多 10 个'),
            h(
              'div',
              { style: S.grid2 },
              field('版权来源', selectBox(form.copyright, [[1, '原创'], [2, '转载']], function (v) {
                patchForm('copyright', Number(v));
              }, busyDisabled)),
              form.copyright === 2
                ? field('转载来源', textInput(form.source, function (v) {
                    patchForm('source', v);
                  }, 'https://live.bilibili.com/xxxx', busyDisabled))
                : null,
            ),
            field('发布同时发动态', textInput(form.dynamic, function (v) {
              patchForm('dynamic', v);
            }, '留空则不发动态', busyDisabled)),
            h(
              'div',
              { style: Object.assign({}, S.stepper, { marginTop: '4px' }) },
              checkbox('禁止转载', Number(form.noReprint) === 1, function (v) {
                patchForm('noReprint', v ? 1 : 0);
              }),
              checkbox('允许充电', Number(form.openElec) === 1, function (v) {
                patchForm('openElec', v ? 1 : 0);
              }),
              checkbox('关闭弹幕', Number(form.closeDanmaku) === 1, function (v) {
                patchForm('closeDanmaku', v ? 1 : 0);
              }),
              checkbox('关闭评论', Number(form.closeReply) === 1, function (v) {
                patchForm('closeReply', v ? 1 : 0);
              }),
            ),
            h(
              'div',
              { style: Object.assign({}, S.stepper, { marginTop: '8px' }) },
              checkbox(
                form.autoPublish ? '录完自动投稿（已开启）' : '录完自动投稿',
                form.autoPublish,
                function () {
                  if (form.autoPublish) {
                    setConfirm('');
                    patchForm('autoPublish', false);
                    return;
                  }
                  if (confirm !== 'autoPublish') {
                    arm('autoPublish');
                    return;
                  }
                  setConfirm('');
                  patchForm('autoPublish', true);
                },
              ),
              checkbox('发布成功后删本地', form.deleteLocalAfterPublish, function (v) {
                patchForm('deleteLocalAfterPublish', v);
              }),
            ),
            !form.autoPublish && confirm === 'autoPublish'
              ? h('div', { style: S.warnText },
                  '再点一次开关确认：开启后录完会自动排队并直接投稿到 B 站，不再等你确认。')
              : null,
            h(
              'div',
              { style: Object.assign({}, S.stepper, { marginTop: '10px' }) },
              button(busy === 'saveSettings' ? '正在保存…' : '保存设置', doSaveSettings, {
                className: 'blbtn blbtnPrimary',
                disabled: busyDisabled,
              }),
              dirtyRef.current ? h('span', { style: S.warnText }, '有未保存的改动') : null,
            ),
          ));

          setChildren.push(h('div', { key: 'secCred', style: S.sec },
            h('div', { style: S.secttl }, '登录凭据（B 站帐号）'),
            h('div', { style: Object.assign({}, S.tiny, { marginBottom: '6px' }) },
              '整行粘贴浏览器里复制的 Cookie（含 SESSDATA、bili_jct）。只写入本机后端目录；界面不回显内容，也不会通过网络返回。'),
            h('div', { style: Object.assign({}, S.tiny, { marginBottom: '6px', color: 'var(--dsw-alias-state-success-primary)' }) },
              '虎牙录制不需要任何 cookie：填房间号就能录，这里的凭据只用于把你的录播投稿到 B 站。'),
            h('div', { style: Object.assign({}, S.tiny, { marginBottom: '6px' }) },
              cookie && cookie.present
                ? '当前：已保存 ' + (Number(cookie.bytes) || 0) + ' 字节' +
                  (cookie.account && cookie.account.uname ? ' · 账号 ' + cookie.account.uname : '')
                : '当前：未设置'),
            h('textarea', {
              className: 'blinput',
              rows: 3,
              autoComplete: 'off',
              spellCheck: false,
              readOnly: false,
              'data-bl': 'cookie-box',
              placeholder: '在这里粘贴整行 cookie（Ctrl+V 即可），或手动输入；保存后输入框会被清空',
              style: { pointerEvents: 'auto', userSelect: 'text', WebkitUserSelect: 'text' },
              value: cookieText,
              onMouseDown: function (e) {
                e.stopPropagation();
              },
              onClick: function (e) {
                e.stopPropagation();
              },
              onChange: function (e) {
                setCookieText(e.target.value);
              },
            }),
            h(
              'div',
              { style: Object.assign({}, S.tiny, { marginTop: '4px' }) },
              cookieText.trim()
                ? '已输入 ' + cookieText.trim().length + ' 个字符' +
                  (cookieText.indexOf('SESSDATA=') >= 0 ? ' · 有 SESSDATA ✓' : ' · 缺 SESSDATA ✗') +
                  (cookieText.indexOf('bili_jct=') >= 0 ? ' · 有 bili_jct ✓' : ' · 缺 bili_jct ✗')
                : '输入框可以直接编辑：粘贴或手打都可以',
            ),
            h(
              'div',
              { style: Object.assign({}, S.stepper, { marginTop: '8px' }) },
              button(busy === 'saveCookie' ? '正在保存…' : '保存凭据', doSaveCookie, {
                className: 'blbtn blbtnPrimary',
                disabled: busyDisabled,
              }),
              button(busy === 'checkAuth' ? '正在检测…' : '检测有效性', doCheckAuth, {
                className: 'blbtn',
                disabled: busyDisabled,
              }),
              cookie && cookie.file
                ? h('span', { style: Object.assign({}, S.mono, { wordBreak: 'break-all' }) }, cookie.file)
                : null,
            ),
            authMsg ? h('div', { style: authedStyle(authMsg) }, authMsg) : null,
          ));
        }
      }

      children.push(h('div', { key: 'settings', className: 'blcard', style: S.card }, setChildren));

      // ---------------- 日志
      if (data && data.logTail) {
        children.push(
          h(
            'div',
            { key: 'log', className: 'blcard', style: S.card },
            h('div', { style: S.cardHead }, h('span', { style: S.cardTitle }, '日志')),
            h('pre', { className: 'blpre' }, String(data.logTail)),
          ),
        );
      }

      // ---------------- 编辑浮层
      if (editing) {
        children.push(
          h('div', {
            key: 'mask',
            style: S.mask,
            onClick: function () {
              setEditing(null);
            },
          }),
        );
        children.push(
          h(
            'div',
            { key: 'modal', style: S.modal },
            h(
              'div',
              { style: S.cardHead },
              h('span', { style: S.cardTitle }, '编辑发布参数'),
              h('span', { style: S.tiny }, 'P' + (Number(editing.partIndex) || 1) +
                (editing.uname ? ' · ' + editing.uname : '')),
              h('span', { style: S.spacer }),
              button('关闭', function () {
                setEditing(null);
              }, S.btnSm),
            ),
            field('标题', textInput(editing.title, function (v) {
              setEditing(function (o) {
                return Object.assign({}, o, { title: v });
              });
            }, '', busyDisabled)),
            field('简介',
              h('textarea', {
                className: 'blinput',
                rows: 4,
                value: editing.desc,
                onChange: function (e) {
                  const v = e.target.value;
                  setEditing(function (o) {
                    return Object.assign({}, o, { desc: v });
                  });
                },
              })),
            h(
              'div',
              { style: S.grid2 },
              field('标签（逗号分隔）', textInput(editing.tag, function (v) {
                setEditing(function (o) {
                  return Object.assign({}, o, { tag: v });
                });
              }, '', busyDisabled)),
              field('分区', selectBox(editing.tid, tidOptions, function (v) {
                setEditing(function (o) {
                  return Object.assign({}, o, { tid: Number(v) });
                });
              }, busyDisabled)),
            ),
            h(
              'div',
              { style: S.grid2 },
              field('可见范围', selectBox(editing.visibility, VIS_OPTIONS, function (v) {
                setEditing(function (o) {
                  return Object.assign({}, o, { visibility: v });
                });
              }, busyDisabled)),
              editing.visibility === 'scheduled'
                ? field('定时发布时间',
                    h('input', {
                      className: 'blinput',
                      type: 'datetime-local',
                      value: editing.dtimeText || '',
                      onChange: function (e) {
                        const v = e.target.value;
                        setEditing(function (o) {
                          return Object.assign({}, o, { dtimeText: v });
                        });
                      },
                    }),
                    '到达该时间后由 B 站自动公开')
                : null,
            ),
            h(
              'div',
              { style: Object.assign({}, S.stepper, { marginTop: '6px' }) },
              button(busy === 'updateItem:' + editing.id ? '正在保存…' : '保存', doSaveEdit, {
                className: 'blbtn blbtnPrimary',
                disabled: busyDisabled,
              }),
              button('取消', function () {
                setEditing(null);
              }, { className: 'blbtn', disabled: busyDisabled }),
            ),
          ),
        );
      }

      return h('div', { style: S.page }, children);
    }

    // ---------------------------------------------------------------- 注册

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        // 字典走宿主 locale 服务；取不到时退回内置中文 T，面板仍可用
        let t = null;
        try {
          ctx.effect(
            () =>
              ctx.locale.register(NS, {
                zh: { panel: T.panel, title: T.title, intro: T.intro, refresh: T.refresh },
                en: {
                  panel: 'Bilibili Live Recorder',
                  title: 'Bilibili Live Recorder',
                  intro: 'Watch UPs, auto-record live streams in parts, and queue uploads to Bilibili',
                  refresh: 'Refresh',
                },
              }),
            'bili-live: dictionaries',
          );
          if (typeof ctx.locale.bind === 'function') t = ctx.locale.bind(NS);
        } catch (e) {
          t = null;
        }
        const tr = function (key, fallback) {
          if (!t) return fallback;
          try {
            const v = t(key);
            return v && v !== key ? v : fallback;
          } catch (e) {
            return fallback;
          }
        };

        ctx.slots.inject('sidebar.panellist', () =>
          ctx.slots.register(
            {
              name: 'sidebar.panellist',
              id: PANEL_ID,
              order: 55,
              // thunk：切换语言时无需重新注册
              label: () => tr('panel', T.panel),
              locale: NS,
            },
            PanelIcon,
          ),
        );

        // 面板样式（含按钮 hover/active 与官方语义色令牌），随插件卸载移除
        ctx.effect(ensurePanelStyle, 'bili-live: panel css');

        ctx.slots.inject('main', () =>
          ctx.slots.register(
            {
              name: 'main',
              key: PANEL_ID,
              locale: NS,
            },
            PanelPage,
          ),
        );
      },
    };
  },
});
