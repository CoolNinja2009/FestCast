import { act, api, clear, download, h, live, pickFile, store } from '../lib';

export function settingsPage(el: HTMLElement) {
  const logs = h('div.notes', { style: { maxHeight: '480px', fontFamily: 'var(--mono)', fontSize: '12px' } });
  const health = h('dl.kv');
  const load = async () => {
    const l = await api('GET', '/api/logs?limit=300').catch(() => []);
    clear(logs, l.slice().reverse().map((x: any) => h('div.note', { class: x.level === 'warning' ? 'warning' : x.level === 'error' ? 'error' : '' },
      h('span.t', null, new Date(x.ts * 1000).toLocaleTimeString()), h('span.t', null, x.level), h('span.m', null, `${x.logger.replace('festcast.', '')}: ${x.msg}`))));
    const hh = await api('GET', '/api/health').catch(() => ({}));
    clear(health, Object.entries(hh).map(([k, v]) => [h('dt', null, k.replace(/_/g, ' ')), h('dd', { style: { color: v === 'ok' || v === true ? 'var(--pvw)' : v === 'down' || v === 'error' ? 'var(--pgm)' : '' } }, String(v))]));
  };
  load();
  el.append(
    h('div.page-h', null, h('h1', null, 'Settings')),
    h('div.grid.g2', null,
      live(h('div.panel'), ['obs', 'server'], () => {
        const o = store.get('obs') || {};
        const s = store.get('server') || {};
        return [h('div.ph', null, h('h2', null, 'OBS connection'), h('span.pill', { class: o.connected ? 'ok' : 'bad' }, o.connected ? 'connected' : 'not connected')),
          h('div.pb.col', null,
            h('dl.kv', null, h('dt', null, 'OBS'), h('dd', null, o.version?.obs || '—'), h('dt', null, 'WebSocket'), h('dd', null, o.version?.websocket || '—'),
              h('dt', null, 'Canvas'), h('dd', null, o.video ? `${o.video.base_w}×${o.video.base_h} → ${o.video.out_w}×${o.video.out_h} @ ${o.video.fps}` : '—'),
              h('dt', null, 'Studio mode'), h('dd', null, o.studio_mode ? 'on' : 'off'), h('dt', null, 'Mode'), h('dd', null, s.simulation ? 'SIMULATION' : 'live OBS')),
            o.error && !o.connected ? h('div.banner.bad', null, o.error) : null,
            h('ol.small.dim', { style: { margin: 0, paddingLeft: '18px' } },
              h('li', null, 'In OBS: Tools → WebSocket Server Settings → Enable WebSocket server, port 4455, enable authentication, copy the password.'),
              h('li', null, 'Put it in FestCast\'s .env file as FESTCAST_OBS_PASSWORD=… (it never leaves this PC).'),
              h('li', null, 'Restart FestCast. It reconnects to OBS automatically whenever OBS restarts.')))];
      }),
      live(h('div.panel'), ['server'], () => {
        const s = store.get('server') || {};
        return [h('div.ph', null, h('h2', null, 'Access')), h('div.pb.col', null,
          h('dl.kv', null, h('dt', null, 'Phones/TVs use'), h('dd', null, s.public_base), h('dt', null, 'This device'), h('dd', null, s.local ? 'production PC (full control)' : 'remote operator'),
            h('dt', null, 'Remote control'), h('dd', null, s.remote_access ? 'enabled (password)' : 'disabled')),
          !s.local ? h('button.btn.sm', { onclick: () => act(() => api('POST', '/api/auth/logout')).then(() => location.reload()) }, 'Sign out') : null)];
      })),
    h('div.grid.g2', { style: { marginTop: '14px' } },
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Backup & diagnostics')), h('div.pb.col', null,
        h('div.row.wrap', null,
          h('button.btn', { onclick: () => download('/api/config/export') }, 'Export configuration'),
          h('button.btn', { onclick: async () => { const [f] = await pickFile('.json'); if (!f) return; const fd = new FormData(); fd.append('file', f); act(() => api('POST', '/api/config/import', fd), 'Configuration imported'); } }, 'Import configuration'),
          h('button.btn', { onclick: () => download('/api/diagnostics') }, 'Download support bundle')),
        h('b.small', null, 'Health checks'), health)),
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Log'), h('button.btn.sm.right', { onclick: load }, 'Refresh')), logs)),
  );
}
