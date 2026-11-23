import { act, ago, api, confirmBox, copy, dur, h, kbps, live, modal, n, pill, promptBox, store } from '../lib';

async function qr(r: any) {
  const q = await act(() => api('GET', `/api/outputs/receivers/${r.id}/qr`));
  if (!q) return;
  modal(`TV link · ${r.name}`, h('div.col', { style: { alignItems: 'center' } }, h('img.qr', { src: q.qr, alt: 'QR code' }),
    h('div.link', null, q.link),
    q.ip_link && q.ip_link !== q.link ? h('p.dim.small', { style: { textAlign: 'center' } }, "If the TV can't open the name, type the IP link: ", h('b.mono', null, q.ip_link)) : null),
    [{ label: 'Copy link', run: () => { copy(q.link); return false; } }, { label: 'Done', cls: 'primary' }]);
}

async function addHdmi() {
  const mons = await act(() => api('GET', '/api/outputs/monitors'));
  if (!mons) return;
  const sel = h('select', { style: { width: '100%' } }, mons.map((m: any) => h('option', { value: m.monitorIndex }, `${m.monitorIndex}: ${m.monitorName} (${m.monitorWidth}×${m.monitorHeight})`)));
  const nm = h('input', { type: 'text', value: 'HDMI projector', style: { width: '100%' } });
  modal('Add HDMI output', h('div.col', null, h('label.f', null, 'Name', nm), h('label.f', null, 'Monitor', sel)), [
    { label: 'Cancel' },
    { label: 'Add', cls: 'primary', run: async () => !!(await act(() => api('POST', '/api/outputs/receivers', { name: nm.value, kind: 'hdmi', monitor_index: Number(sel.value) }))) }]);
}

const HELP: Record<string, string> = {
  youtube: 'YouTube Studio → Create → Go live → Stream: copy the Stream key.',
  twitch: 'Twitch dashboard → Settings → Stream: copy the Primary Stream key.',
  facebook: 'Facebook Live Producer → Streaming software: copy the Stream key.',
  kick: 'Kick dashboard → Settings → Stream URL & Key: paste both.',
  custom: 'Any RTMP, RTMPS or SRT server: paste its URL and stream key (SRT: put everything in the URL, key empty).',
};

function destForm(d?: any) {
  const platforms: Record<string, any> = (store.get('live') || {}).platforms || {};
  const plat = h('select', { style: { width: '100%' } }, Object.entries(platforms).map(([k, p]) => h('option', { value: k, selected: d ? d.platform === k : k === 'youtube' }, p.label)));
  const name = h('input', { type: 'text', value: d?.name || '', style: { width: '100%' } });
  const server = h('input', { type: 'text', value: d?.server || '', style: { width: '100%' }, spellcheck: false });
  const key = h('input', { type: 'password', value: '', autocomplete: 'off', placeholder: d?.has_key ? `saved (${d.key_hint}): leave empty to keep` : 'stream key', style: { width: '100%' } });
  const help = h('p.dim.small');
  const sync = (first: boolean) => {
    const p = platforms[plat.value] || {};
    if (!(first && d)) server.value = p.server || '';
    name.placeholder = p.label || '';
    server.placeholder = p.server || 'rtmp://…, rtmps://… or srt://…';
    help.textContent = HELP[plat.value] || '';
  };
  plat.onchange = () => sync(false);
  sync(true);
  const body = h('div.col', null, h('label.f', null, 'Platform', plat), h('label.f', null, 'Name', name),
    h('label.f', null, 'Server URL', server), h('label.f', null, 'Stream key (kept secret, never shown again)', key), help);
  const values = () => {
    const b: any = { platform: plat.value, name: name.value || (platforms[plat.value] || {}).label, server: server.value };
    if (key.value || !d) b.key = key.value;
    return b;
  };
  return { body, values };
}

function addDest() {
  const f = destForm();
  modal('Add live destination', f.body, [{ label: 'Cancel' },
    { label: 'Add', cls: 'primary', run: async () => !!(await act(() => api('POST', '/api/live/destinations', f.values()), 'Destination added')) }]);
}

function editDest(d: any) {
  const f = destForm(d);
  modal(`Edit · ${d.name}`, f.body, [{ label: 'Cancel' },
    { label: 'Save', cls: 'primary', run: async () => !!(await act(() => api('PATCH', `/api/live/destinations/${d.id}`, f.values()), 'Saved')) }]);
}

function destRow(lv: any, d: any) {
  return h('tr', null,
    h('td', null, h('input', { type: 'checkbox', checked: d.enabled, title: 'Send to this destination', onchange: (e: Event) => act(() => api('PATCH', `/api/live/destinations/${d.id}`, { enabled: (e.target as HTMLInputElement).checked })) })),
    h('td', null, h('b', null, d.name), h('div.dim.small', null, (lv.platforms?.[d.platform] || {}).label || d.platform)),
    h('td.small.mono', null, d.server), h('td.small.mono', null, d.has_key ? d.key_hint : '—'),
    h('td', null, pill(d.state), d.error && d.state !== 'sending' ? h('div.small', { style: { color: 'var(--warn)', maxWidth: '280px' } }, d.error) : null),
    h('td.num', null, kbps(d.kbps)), h('td.num', null, d.uptime_s != null ? dur(d.uptime_s * 1000) : '—'), h('td.num', null, n(d.restarts, 0)),
    h('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } },
      d.log?.length ? h('button.btn.sm.ghost', { onclick: () => modal(`ffmpeg log · ${d.name}`, h('pre.link', { style: { maxHeight: '320px', overflow: 'auto', whiteSpace: 'pre-wrap' } }, d.log.join('\n')), [{ label: 'Close' }], true) }, 'Log') : null,
      h('button.btn.sm.ghost', { onclick: () => editDest(d) }, 'Edit'),
      h('button.btn.sm.ghost.danger', { onclick: async () => { if (await confirmBox('Remove destination', `Remove ${d.name} and its saved stream key?`, 'Remove')) act(() => api('DELETE', `/api/live/destinations/${d.id}`)); } }, 'Remove')));
}

function livePanel() {
  return live(h('div.panel', { style: { marginTop: '14px' } }), ['live', 'outputs', 'obs'], () => {
    const lv = store.get('live') || {};
    const ds: any[] = lv.destinations || [];
    const enabled = ds.filter(d => d.enabled);
    const obs = store.get('obs') || {};
    const m = (store.get('outputs') || {}).mediamtx || {};
    const allOk = !!lv.sending && lv.sending === enabled.length;
    return [
      h('div.ph', null, h('h2', null, 'Live stream (YouTube, Twitch, …)'),
        lv.on ? h('span.pill', { class: allOk ? 'sending' : 'retrying' }, `on · ${lv.sending || 0}/${enabled.length} sending`) : h('span.pill.off', null, 'off'),
        h('div.row.right', null, h('button.btn.sm', { onclick: addDest }, '+ Destination'))),
      h('div.pb.col', null,
        !lv.ffmpeg ? h('div.banner.warn', null, 'ffmpeg is not installed: run "winget install Gyan.FFmpeg" (or set FESTCAST_FFMPEG_PATH), then restart FestCast.') : null,
        !m.found ? h('div.banner.warn', null, 'Live streaming goes through MediaMTX, which is not installed (see docs/SETUP.md).') : null,
        h('p.dim.small', null, 'Sends the programme to every switched-on destination at once, alongside the TVs. The picture goes out exactly as OBS encodes it (bitrate: OBS → Settings → Output → Streaming). "Sending" means it is leaving this PC; check the platform’s own page to see it arrive.'),
        ds.length ? h('table.t', null, h('tr', null, h('th', null, 'On'), h('th', null, 'Destination'), h('th', null, 'Server'), h('th', null, 'Key'), h('th', null, 'State'),
          h('th', null, 'Bitrate'), h('th', null, 'Up for'), h('th', null, 'Reconnects'), h('th', null, '')),
          ds.map(d => destRow(lv, d)))
          : h('div.dim', null, 'No destinations yet. Add YouTube, Twitch or any RTMP server with its stream key.'),
        h('div.row.wrap', null,
          lv.on ? h('button.btn.red', { onclick: async () => { if (await confirmBox('End live stream', 'Every platform stops receiving the show. TVs and recording are not affected.', 'End stream')) act(() => api('POST', '/api/live/stop'), 'Live stream ended'); } }, 'End live stream')
            : h('button.btn.green', {
              disabled: !obs.connected || !lv.ffmpeg || !m.found || !enabled.length, onclick: async () => {
                if (await confirmBox('Go live', `The programme goes out publicly to: ${enabled.map(d => d.name).join(', ')}.`, 'Go live', 'green')) act(() => api('POST', '/api/live/start'), 'Going live');
              },
            }, 'Go live'))),
    ];
  });
}

export function outputsPage(el: HTMLElement) {
  el.append(
    h('div.page-h', null, h('h1', null, 'Outputs')),
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
            )),
        h('div.panel', null, h('div.ph', null, h('h2', null, 'MediaMTX log')),
          h('pre.link', { style: { margin: '10px', maxHeight: '210px', overflow: 'auto', whiteSpace: 'pre-wrap' } }, (m.log || []).slice(-25).join('\n') || 'No output yet.')),
      ];
    }),
    livePanel(),
    h('div.panel', { style: { marginTop: '14px' } },
      h('div.ph', null, h('h2', null, 'Receivers'),
        h('div.row.right', null,
          h('button.btn.sm', { onclick: async () => { const nm = await promptBox('Add TV receiver', 'Name', 'Hall TV'); if (nm) act(() => api('POST', '/api/outputs/receivers', { name: nm, kind: 'webrtc' }), 'Receiver added'); } }, '+ Wireless TV'),
          h('button.btn.sm', { onclick: addHdmi }, '+ HDMI projector'))),
      live(h('div'), ['outputs'], () => {
        const rs = (store.get('outputs') || {}).receivers || [];
        if (!rs.length) return h('div.pb.dim', null, 'No receivers');
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
