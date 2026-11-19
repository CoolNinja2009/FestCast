import { act, api, bytes, clock, confirmBox, copy, dash, disposers, dur, h, kbps, live, n, pill, previewImg, spark, store } from '../lib';
import { createProductionDialog, showQr } from './cameras';

function stat(label: string, value: any, sub?: any, cls = '') {
  return h('div.panel.stat', { class: cls }, h('span.l', null, label), h('span.v', null, value), h('span.s', null, sub ?? ''));
}

export function overviewPage(el: HTMLElement) {
  let hist: any[] = [];
  const loadHist = () => api('GET', '/api/metrics/history?seconds=600').then(x => { hist = x; store.set('_hist', 1); }).catch(() => {});
  loadHist();
  disposers.push(store.on('metrics', (m) => { if (m) { hist.push({ ts: m.ts, cpu: m.system.cpu_pct, ram: m.system.ram_pct, gpu: m.system.gpu?.util_pct, enc: m.system.gpu?.encoder_pct, cam_in: m.cameras.in_kbps, rx: m.system.net_rx_kbps, tx: m.system.net_tx_kbps }); hist = hist.slice(-300); } }));

  el.append(
    h('div.page-h', null, h('h1', null, 'Overview')),
    live(h('div'), ['cameras', 'obs', 'production', 'server'], banners),
    live(h('div.grid.g4'), ['metrics', 'cameras', 'obs', 'recording', 'outputs', 'production'], tiles),
    live(h('div'), ['cameras'], cameraLinks),
    h('div.grid.g2', { style: { marginTop: '14px' } },
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Programme')),
        h('div.pb', null, h('div.mon.pgm', null, h('span.lab', null, 'PGM'), previewImg('program_scene', 'Programme preview appears when OBS is connected')),
          live(h('div.row.wrap', { style: { marginTop: '10px' } }), ['production', 'cameras'], quickActions))),
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Notifications'),
        h('button.btn.ghost.sm.right', { onclick: () => act(() => api('POST', '/api/notifications/clear')).then(() => store.set('notifications', [])) }, 'Clear')),
        live(h('div.notes'), ['notifications'], () => {
          const ns = store.get('notifications') || [];
          if (!ns.length) return h('div.note', null, h('span.m.faint', null, 'No notifications'));
          return ns.slice(0, 60).map((x: any) => h('div.note', { class: x.level }, h('span.t', null, clock(x.ts)), h('span.m', null, x.message)));
        }))),
    h('div.panel', { style: { marginTop: '14px' } }, h('div.ph', null, h('h2', null, 'Last 10 minutes')),
      live(h('div.pb.grid.g4'), ['_hist', 'metrics'], () => {
        const ser = (k: string) => hist.map(x => x[k] ?? null);
        const box = (label: string, k: string, fmt: (v: any) => string, max?: number) => {
          const last = [...hist].reverse().find(x => x[k] !== null && x[k] !== undefined);
          return h('div', null, h('div.row', null, h('span.small.dim', null, label), h('span.right.mono.small', null, last ? fmt(last[k]) : dash)), spark(ser(k), max));
        };
        return [box('CPU', 'cpu', v => n(v, 0, '%'), 100), box('GPU / NVENC', 'enc', v => n(v, 0, '%'), 100),
          box('Camera bitrate in', 'cam_in', kbps), box('Network out', 'tx', kbps)];
      })),
  );
}

function banners() {
  const obs = store.get('obs') || {};
  const server = store.get('server') || {};
  const out: any[] = [];
  if (server.simulation) out.push(h('div.banner.warn', null, h('b', null, 'Simulation'), 'Nothing shown here is real.'));
  if (!obs.connected) out.push(h('div.banner.bad', null, h('b', null, 'OBS not connected.'), obs.error || '', h('a.right', { href: '#/settings' }, 'How to connect')));
  if (!(store.get('cameras') || {}).production) out.push(h('div.banner.info', null, 'No production yet.',
    h('button.btn.primary.sm.right', { onclick: createProductionDialog }, 'Create production')));
  return out;
}

function tiles() {
  const m = store.get('metrics') || {};
  const cams = m.cameras || {};
  const sys = m.system || {};
  const obs = store.get('obs') || {};
  const o = m.obs || {};
  const rec = store.get('recording') || {};
  const outs = m.outputs || {};
  const net = m.network || {};
  const p = store.get('production') || {};
  return [
    stat('Production', p.standby_active ? 'STANDBY' : p.started ? 'ON AIR' : 'IDLE', p.program_scene || 'no programme scene', p.standby_active ? 'warn' : p.started ? 'good' : ''),
    stat('OBS', obs.connected ? 'Connected' : 'Down', obs.connected ? `${n(o.fps, 1)} fps · render ${n(o.render_ms, 1)} ms · ${o.output_skipped ?? dash} skipped` : (obs.error || ''), obs.connected ? 'good' : 'bad'),
    stat('Cameras', `${cams.connected ?? 0}/${cams.registered ?? 0}`, cams.registered ? `avg ${cams.avg_resolution || dash} · ${n(cams.avg_fps, 0)} fps · ${cams.below_1080p || 0} below 1080p` : 'no cameras',
      cams.registered && cams.connected === cams.registered ? 'good' : cams.connected ? 'warn' : cams.registered ? 'bad' : ''),
    stat('Camera quality', kbps(cams.in_kbps), `max loss ${n(cams.max_packet_loss_pct, 1, '%')} · ${cams.poor ?? 0} poor link(s)`, cams.poor ? 'warn' : ''),
    stat('Recording', rec.active ? dur(rec.duration_ms) : 'Off', rec.active ? `${bytes(rec.bytes)} · ${rec.paused ? 'paused' : 'confirmed by OBS'}` : (rec.config?.root || ''), rec.active ? (rec.paused ? 'warn' : 'bad') : ''),
    stat('Storage', bytes(rec.dest_free_bytes), rec.estimated_remaining_s ? `≈ ${Math.floor(rec.estimated_remaining_s / 3600)} h ${Math.floor(rec.estimated_remaining_s / 60) % 60} m left at current rate` : 'free on recording drive',
      rec.dest_free_bytes !== null && rec.dest_free_bytes !== undefined && rec.dest_free_bytes < (rec.config?.warn_free_gb || 30) * 1024 ** 3 ? 'warn' : ''),
    stat('TV receivers', `${outs.connected ?? 0}/${outs.registered ?? 0}`, `${outs.programme_streaming ? 'programme publishing' : 'programme output off'} · ${kbps(outs.out_kbps)}`, outs.registered && outs.connected === outs.registered ? 'good' : ''),
    stat('Network', net.health ? net.health.toUpperCase() : dash, `${net.lan_ip || ''} · in ${kbps(sys.net_rx_kbps)} · out ${kbps(sys.net_tx_kbps)}`, net.health === 'good' ? 'good' : net.health === 'bad' ? 'bad' : net.health ? 'warn' : ''),
    stat('CPU', n(sys.cpu_pct, 0, '%'), 'Ryzen load'),
    stat('GPU', sys.gpu ? n(sys.gpu.util_pct, 0, '%') : dash, sys.gpu ? `NVENC ${n(sys.gpu.encoder_pct, 0, '%')} · ${n(sys.gpu.temp_c, 0, '°C')}` : 'nvidia-smi not available'),
    stat('Memory', n(sys.ram_pct, 0, '%'), `${n(sys.ram_used_gb, 1)} / ${n(sys.ram_total_gb, 0)} GB · OBS ${n(o.mem_mb, 0)} MB`),
    stat('Uptime', dur((sys.uptime_s ?? null) === null ? null : sys.uptime_s * 1000), `OBS link ${o.uptime_s ? dur(o.uptime_s * 1000) : dash}`),
  ];
}

function quickActions() {
  const p = store.get('production') || {};
  const prod = (store.get('cameras') || {}).production;
  if (!prod) return h('button.btn.primary', { onclick: createProductionDialog }, 'Create production');
  return [
    p.started ? h('button.btn', { onclick: async () => {
      const rec = store.get('recording') || {};
      const stops = rec.active && rec.config?.auto_start;
      if (await confirmBox('End production', stops ? 'End the production? The recording stops too.' : 'End the production?', 'End production'))
        act(() => api('POST', '/api/production/end'), stops ? 'Production ended, recording stopped' : 'Production ended');
    } }, 'End production')
      : h('button.btn.green', { onclick: () => act(() => api('POST', '/api/production/start'), 'Production started') }, 'Start production'),
    h('a.btn', { href: '#/cameras' }, 'Cameras'), h('a.btn', { href: '#/production' }, 'Switcher & layouts'), h('a.btn', { href: '#/recordings' }, 'Recording'),
  ];
}

function cameraLinks() {
  const s = store.get('cameras') || {};
  const cams = s.cameras || [];
  if (!s.production || !cams.length) return null;
  return h('div.panel', { style: { marginTop: '14px' } },
    h('div.ph', null, h('h2', null, 'Camera links'),
      h('a.btn.sm.right', { href: '#/cameras' }, 'All camera controls')),
    h('table.t', null, cams.map((c: any) => h('tr', null,
      h('td', { style: { width: '150px' } }, h('b', null, c.name)),
      h('td', { style: { width: '170px' } }, pill(c.status)),
      h('td.mono.small', { style: { wordBreak: 'break-all' } }, c.invite ? c.invite.link : '—',
        c.invite && c.invite.ip_link && c.invite.ip_link !== c.invite.link ? h('div.dim', { title: 'Same link by IP, for devices that cannot open .local names' }, c.invite.ip_link) : null),
      h('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } },
        c.invite ? [h('button.btn.sm', { onclick: () => copy(c.invite.link, `${c.name} link`) }, 'Copy'),
          h('button.btn.sm', { onclick: () => showQr(c.id, c.name), title: 'QR for the PC link (festival Wi-Fi)' }, 'QR'),
          h('button.btn.sm', { onclick: () => showQr(c.id, c.name, true), title: 'QR straight to VDO.Ninja: works on any network' }, 'Direct QR')] : null)))));
}
