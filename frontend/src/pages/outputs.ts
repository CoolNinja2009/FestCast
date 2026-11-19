import { act, ago, api, confirmBox, copy, h, kbps, live, modal, n, pill, promptBox, store } from '../lib';

async function qr(r: any) {
  const q = await act(() => api('GET', `/api/outputs/receivers/${r.id}/qr`));
  if (!q) return;
  modal(`TV link · ${r.name}`, h('div.col', { style: { alignItems: 'center' } }, h('img.qr', { src: q.qr, alt: 'QR code' }),
    h('p.small.dim', null, 'Open on the TV\'s browser (or a stick/box on the TV). It connects, reconnects and goes fullscreen on double-click. Tap once to enable sound.'),
    h('div.link', null, q.link)), [{ label: 'Copy link', run: () => { copy(q.link); return false; } }, { label: 'Done', cls: 'primary' }]);
}

async function addHdmi() {
  const mons = await act(() => api('GET', '/api/outputs/monitors'));
  if (!mons) return;
  const sel = h('select', { style: { width: '100%' } }, mons.map((m: any) => h('option', { value: m.monitorIndex }, `${m.monitorIndex}: ${m.monitorName} (${m.monitorWidth}×${m.monitorHeight})`)));
  const nm = h('input', { type: 'text', value: 'HDMI projector', style: { width: '100%' } });
  modal('Add HDMI output', h('div.col', null, h('label.f', null, 'Name', nm), h('label.f', null, 'Monitor', sel),
    h('p.small.dim', null, 'Opens an OBS fullscreen programme projector on that screen. It is independent of the wireless output. An HDMI splitter on the PC output is a simple alternative.')), [
    { label: 'Cancel' },
    { label: 'Add', cls: 'primary', run: async () => !!(await act(() => api('POST', '/api/outputs/receivers', { name: nm.value, kind: 'hdmi', monitor_index: Number(sel.value) }))) }]);
}

export function outputsPage(el: HTMLElement) {
  el.append(
    h('div.page-h', null, h('h1', null, 'Outputs'), h('span.sub', null, 'OBS → WHIP → MediaMTX → WebRTC (WHEP) → TV browsers · plus HDMI projectors')),
    live(h('div.grid.g2'), ['outputs', 'obs'], () => {
      const o = store.get('outputs') || {};
      const m = o.mediamtx || {};
      const obs = store.get('obs') || {};
      return [
        h('div.panel', null, h('div.ph', null, h('h2', null, 'Programme output'), o.programme_streaming ? h('span.pill.live', null, 'publishing') : h('span.pill.stopped', null, 'off')),
          h('div.pb.col', null,
            !m.found ? h('div.banner.warn', null, 'MediaMTX is not installed. Run scripts\\get-mediamtx.ps1 (see docs/SETUP.md), then restart FestCast.') : null,
            h('dl.kv', null, h('dt', null, 'MediaMTX'), h('dd', null, pill(m.state)), h('dt', null, 'Stream ready'), h('dd', null, m.ready === undefined ? '—' : m.ready ? 'yes' : 'no'),
              h('dt', null, 'Viewers'), h('dd', null, (m.readers || []).length), h('dt', null, 'Outgoing'), h('dd', null, kbps(m.out_kbps)),
              h('dt', null, 'Ports (auto)'), h('dd', null, m.ports ? `WebRTC ${m.ports.webrtc} · ICE ${m.ports.ice}` : '—')),
            h('div.row.wrap', null,
              o.programme_streaming ? h('button.btn.red', { onclick: async () => { if (await confirmBox('Stop TV output', 'TVs will lose the programme. Recording is not affected.', 'Stop')) act(() => api('POST', '/api/outputs/programme/stop'), 'Programme output stopped'); } }, 'Stop TV output')
                : h('button.btn.green', { disabled: !obs.connected || !m.found, onclick: () => act(() => api('POST', '/api/outputs/programme/start'), 'Programme output started') }, 'Start TV output'),
              h('button.btn.sm', { onclick: () => act(() => api('POST', `/api/outputs/mediamtx/${m.state === 'running' ? 'restart' : 'start'}`)) }, m.state === 'running' ? 'Restart MediaMTX' : 'Start MediaMTX')),
            h('p.small.dim', { style: { margin: 0 } }, 'Uses OBS\'s streaming output (WHIP, H.264 + Opus). OBS can only stream to one place, so external streaming (e.g. YouTube) cannot run at the same time from OBS. For browser playback set OBS\'s stream encoder to H.264 (NVENC), not HEVC/AV1.'))),
        h('div.panel', null, h('div.ph', null, h('h2', null, 'MediaMTX log')),
          h('pre.link', { style: { margin: '10px', maxHeight: '210px', overflow: 'auto', whiteSpace: 'pre-wrap' } }, (m.log || []).slice(-25).join('\n') || 'No output yet.')),
      ];
    }),
    h('div.panel', { style: { marginTop: '14px' } },
      h('div.ph', null, h('h2', null, 'Receivers'),
        h('div.row.right', null,
          h('button.btn.sm', { onclick: async () => { const nm = await promptBox('Add TV receiver', 'Name', 'Hall TV'); if (nm) act(() => api('POST', '/api/outputs/receivers', { name: nm, kind: 'webrtc' }), 'Receiver added'); } }, '+ Wireless TV'),
          h('button.btn.sm', { onclick: addHdmi }, '+ HDMI projector'))),
      live(h('div'), ['outputs'], () => {
        const rs = (store.get('outputs') || {}).receivers || [];
        if (!rs.length) return h('div.pb.dim', null, 'No receivers. Add one wireless TV to get its link.');
        return h('table.t', null, h('tr', null, h('th', null, 'Name'), h('th', null, 'Type'), h('th', null, 'State'), h('th', null, 'Picture'), h('th', null, 'Bitrate'),
          h('th', null, 'Jitter / RTT'), h('th', null, 'Lost'), h('th', null, 'Last seen'), h('th', null, '')),
          rs.map((r: any) => h('tr', null, h('td', null, h('b', null, r.name)), h('td', null, r.kind === 'hdmi' ? `HDMI · monitor ${r.monitor_index}` : 'Wireless (WebRTC)'),
            h('td', null, r.kind === 'hdmi' ? h('span.pill.plain', null, 'projector') : pill(r.state)),
            h('td.num', null, r.width ? `${r.width}×${r.height} @ ${n(r.fps, 0)}` : '—'), h('td.num', null, kbps(r.bitrate_kbps)),
            h('td.num', null, `${n(r.jitter_ms, 0, ' ms')} / ${n(r.rtt_ms, 0, ' ms')}`), h('td.num', null, r.packets_lost ?? '—'), h('td.small.dim', null, r.kind === 'hdmi' ? '' : ago(r.last_seen)),
            h('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } },
              r.kind === 'webrtc' ? [h('button.btn.sm', { onclick: () => copy(r.link) }, 'Copy link'), h('button.btn.sm', { onclick: () => qr(r) }, 'QR')]
                : h('button.btn.sm', { onclick: () => act(() => api('POST', `/api/outputs/projector?monitor_index=${r.monitor_index}`), 'Projector opened') }, 'Open projector'),
              h('button.btn.sm.ghost', { onclick: async () => { const nm = await promptBox('Rename', 'Name', r.name); if (nm) act(() => api('PATCH', `/api/outputs/receivers/${r.id}`, { name: nm })); } }, 'Rename'),
              h('button.btn.sm.ghost.danger', { onclick: async () => { if (await confirmBox('Revoke receiver', `${r.name}'s link stops working immediately.`, 'Revoke')) act(() => api('DELETE', `/api/outputs/receivers/${r.id}`)); } }, 'Revoke')))));
      })),
  );
}
