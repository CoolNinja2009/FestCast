import { act, ago, api, confirmBox, copy, dash, h, kbps, live, modal, n, pill, previewImg, promptBox, store, toast } from '../lib';

export function createProductionDialog() {
  const name = h('input', { type: 'text', value: 'School Festival', style: { width: '100%' } });
  const count = h('input', { type: 'number', value: '3', min: '0', max: '16', style: { width: '100%' } });
  modal('Create production', h('div.col', null,
    h('label.f', null, 'Event name', name), h('label.f', null, 'Camera slots', count)), [
    { label: 'Cancel' },
    { label: 'Create', cls: 'primary', run: async () => {
      const ok = !!(await act(() => api('POST', '/api/production', { name: name.value, cameras: Number(count.value) }), 'Production created: camera links are ready'));
      if (ok) location.hash = '#/cameras';   // the invitation links live here
      return ok;
    } },
  ]);
}

// Two ways in: the PC link (/join page with instructions; phone must reach this PC on the festival Wi-Fi) and the
// direct VDO.Ninja link (works on any network, even mobile data, but skips the instruction page).
export async function showQr(cid: string, name: string, direct = false) {
  const r = await act(() => api('GET', `/api/cameras/${cid}/qr`));
  if (!r) return;
  const link = direct ? r.direct : r.link, qr = direct ? r.direct_qr : r.qr;
  if (!link) return toast('No direct link for this camera', 'warning');
  modal(`${direct ? 'Direct link' : 'Invitation'} · ${name}`, h('div.col', { style: { alignItems: 'center' } },
    h('img.qr', { src: qr, alt: 'QR code' }),
    h('p.dim.small', { style: { textAlign: 'center', maxWidth: '360px' } }, direct
      ? "Opens the camera on VDO.Ninja directly: works on any Wi-Fi or mobile data. No instruction page, so tell the operator: landscape, rear camera, screen on. Re-share it if you change this camera's resolution."
      : "Phone must be on the festival Wi-Fi. If it can't open, use the Direct QR."),
    h('div.link', null, link),
    !direct && r.ip_link && r.ip_link !== link ? h('p.dim.small', { style: { textAlign: 'center' } }, 'Same link by IP (if the phone can’t open the name): ', h('span.mono', null, r.ip_link)) : null),
    [{ label: 'Copy link', run: () => { copy(link); return false; } }, { label: 'Done', cls: 'primary' }]);
}

async function printSheet() {
  const s = store.get('cameras') || {};
  const cams = s.cameras || [];
  const items: { name: string; link: string; qr: string; direct_qr: string | null }[] = [];
  for (const c of cams) { const r = await act(() => api('GET', `/api/cameras/${c.id}/qr`)); if (r) items.push({ name: c.name, link: r.link, qr: r.qr, direct_qr: r.direct_qr }); }
  const w = window.open('', '_blank');
  if (!w) return toast('Allow pop-ups to print the invitation sheet', 'warning');
  const esc = (x: string) => x.replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' } as any)[ch]);
  w.document.write(`<!doctype html><title>FestCast camera invitations</title><style>body{font-family:system-ui,sans-serif;margin:24px}
  .g{display:grid;grid-template-columns:1fr 1fr;gap:24px}.c{border:1px solid #ccc;border-radius:10px;padding:16px;break-inside:avoid;text-align:center}
  img{width:220px}h2{margin:4px 0}p{font-size:12px;word-break:break-all;color:#444}.q{display:flex;gap:12px;justify-content:center}.q img{width:180px}.q b{display:block;font-size:13px}</style>
  <h1>${esc(s.production?.name || 'FestCast')} · camera invitations</h1><p>Join the festival Wi-Fi, scan your code, allow the camera, choose the rear camera, press Start. Keep the screen on.</p>
  <p>If code 1 doesn't open, scan code 2 (works on any network). Hold the phone sideways, use the rear camera.</p>
  <div class="g">${items.map(i => `<div class="c"><h2>${esc(i.name)}</h2><div class="q"><div><b>1 · Festival Wi-Fi</b><img src="${i.qr}"></div>${i.direct_qr ? `<div><b>2 · Any network</b><img src="${i.direct_qr}"></div>` : ''}</div><p>${esc(i.link)}</p></div>`).join('')}</div>
  <script>setTimeout(()=>print(),300)<\/script>`);
  w.document.close();
}

function settingsDialog(c: any) {
  const nm = h('input', { type: 'text', value: c.name, style: { width: '100%' } });
  const br = h('input', { type: 'number', value: String(c.bitrate_kbps), min: '500', max: '40000', step: '500', style: { width: '100%' } });
  const res = h('select', { style: { width: '100%' } }, ['auto', '720p', '1080p', '1440p'].map(r => h('option', { value: r, selected: r === c.resolution_target }, r)));
  const raw = h('pre.link', { style: { maxHeight: '220px', overflow: 'auto', whiteSpace: 'pre-wrap' } }, 'Loading raw stats…');
  api('GET', `/api/cameras/${c.id}/raw`).then(r => { raw.textContent = r ? JSON.stringify(r, null, 1) : 'No stats received yet (camera not live).'; }).catch(e => { raw.textContent = e.message; });
  modal(`${c.name} · settings`, h('div.col', null,
    h('div.form', null, h('label.f', null, 'Name', nm), h('label.f', null, 'Resolution target', res), h('label.f', null, 'Requested bitrate (kbps)', br)),
    h('dl.kv', null, h('dt', null, 'Camera ID'), h('dd', null, c.id), h('dt', null, 'Transport'), h('dd', null, c.transport),
      h('dt', null, 'Input type'), h('dd', null, c.input_type), h('dt', null, 'OBS input'), h('dd', null, c.obs_input), h('dt', null, 'OBS scene'), h('dd', null, c.obs_scene),
      h('dt', null, 'Invitation expires'), h('dd', null, c.invite ? new Date(c.invite.expires_at).toLocaleString() : dash)),
    h('b.small', null, c.transport === 'local' ? 'Raw stats (measured in OBS)' : 'Raw VDO.Ninja stats'), raw), [
    { label: 'Cancel' },
    { label: 'Save', cls: 'primary', run: async () => !!(await act(() => api('PATCH', `/api/cameras/${c.id}`, { name: nm.value, resolution_target: res.value, bitrate_kbps: Number(br.value) }), 'Saved')) },
  ], true);
}

function card(c: any) {
  const p = store.get('production') || {};
  const obs = store.get('obs') || {};
  const onPgm = obs.program_scene === c.obs_scene;
  const res = c.width ? `${c.width}×${c.height}` + ((c.rotation || 0) % 180 ? ' ⟳' : '') : dash;
  const resSel = h('select', { class: 'btn sm', title: 'Resolution target', onchange: (e: any) => act(() => api('PATCH', `/api/cameras/${c.id}`, { resolution_target: e.target.value })) },
    ['auto', '720p', '1080p', '1440p'].map(r => h('option', { value: r, selected: r === c.resolution_target }, r === 'auto' ? 'Auto' : r)));
  return h('div.panel.cam', null,
    h('div.mon', { class: onPgm ? 'pgm' : '' }, onPgm ? h('span.lab', null, 'ON AIR') : null, previewImg(c.id, c.status === 'live' ? 'Waiting for OBS frame…' : 'No feed')),
    h('div.head', null, h('b', null, c.name), h('span.faint.small', null, `#${c.slot}`), h('span.right'), pill(c.status), c.audio_active ? h('span.pill.ok.plain', { title: 'This camera\'s audio is live in OBS' }, '🎙 audio') : null),
    c.below_target ? h('div.warnline', null, `Receiving ${res}, below the ${c.resolution_target} target. Check the phone's camera choice and Wi-Fi.`) : null,
    c.portrait ? h('div.warnline', null, h('b', null, 'Portrait feed. '),
      'Sideways? Rotate the tile in Production, or use Fill.') : null,
    c.target_pending ? h('div.warnline', null, `New target ${c.resolution_target}: ask the operator to re-open the link.`) : null,
    c.status === 'no_probe' && obs.connected ? h('div.warnline', null, 'OBS is not running this camera\'s source yet - it will start monitoring within seconds.') : null,
    h('div.stats', null,
      h('div', null, h('span', null, 'Resolution'), h('b', null, res)), h('div', null, h('span', null, 'FPS'), h('b', null, n(c.fps, 0))),
      h('div', null, h('span', null, 'Bitrate'), h('b', null, kbps(c.bitrate_in_kbps))), h('div', null, h('span', null, 'Jitter'), h('b', null, n(c.jitter_ms, 0, ' ms'))),
      h('div', null, h('span', null, 'Loss'), h('b', { title: 'VDO.Ninja does not always report packet loss; — means unknown' }, n(c.packet_loss_pct, 1, '%'))),
      h('div', null, h('span', null, 'RTT'), h('b', null, n(c.rtt_ms, 0, ' ms'))),
      h('div', null, h('span', null, 'Audio'), h('b', null, c.audio_kbps ? kbps(c.audio_kbps) : dash)),
      h('div', null, h('span', null, 'Last seen'), h('b', null, ago(c.last_seen)))),
    h('div.actions', null,
      c.invite ? h('button.btn.sm', { onclick: () => copy(c.invite.link, `${c.name} link`) }, 'Copy link') : null,
      h('button.btn.sm', { onclick: () => showQr(c.id, c.name), title: 'QR for the PC link (festival Wi-Fi)' }, 'QR'),
      h('button.btn.sm', { onclick: () => showQr(c.id, c.name, true), title: c.transport === 'local'
        ? 'QR straight to the camera page on this PC, by IP address (festival Wi-Fi)' : 'QR straight to VDO.Ninja: works on any network' }, 'Direct QR'),
      h('select', { class: 'btn sm', title: 'Connection. Local: phone sends straight to this PC (no internet, sharpest, least delay). VDO.Ninja: through the internet.',
        onchange: (e: any) => act(() => api('PATCH', `/api/cameras/${c.id}`, { transport: e.target.value })) },
        [['local', 'Local'], ['vdoninja', 'VDO.Ninja']].map(([v, l]) => h('option', { value: v, selected: v === c.transport }, l))),
      resSel,
      h('button.btn.sm', { class: c.frame === 'fill' ? 'on' : '', title: 'Fit: whole picture, black bars if needed. Fill: crop to fill 16:9.',
        onclick: () => act(() => api('PATCH', `/api/cameras/${c.id}`, { frame: c.frame === 'fill' ? 'fit' : 'fill' })) }, c.frame === 'fill' ? 'Fill' : 'Fit'),
      h('button.btn.sm', { onclick: () => act(() => api('POST', `/api/cameras/${c.id}/reconnect`), 'Reconnecting viewer') }, 'Reconnect'),
      h('button.btn.sm', { class: c.audio_active ? 'on' : '', onclick: () => act(() => api('POST', `/api/cameras/${c.id}/audio`, { active: !c.audio_active })) }, c.audio_active ? 'Audio on' : 'Use audio'),
      h('button.btn.sm', { class: onPgm ? 'red' : '', disabled: !obs.connected, title: 'Cut this camera full-screen to programme',
        onclick: async () => { if (p.started || await confirmBox('Take camera live', `Put ${c.name} on programme?`, 'Go live')) act(() => api('POST', `/api/cameras/${c.id}/golive?transition=Fade`)); } }, 'Live'),
      h('span.right'),
      h('button.btn.sm.ghost', { title: 'Settings', onclick: () => settingsDialog(c) }, 'Settings'),
      h('button.btn.sm.ghost', { onclick: async () => {
        const nm = await promptBox('Rename camera', 'Name', c.name); if (nm) act(() => api('PATCH', `/api/cameras/${c.id}`, { name: nm }));
      } }, 'Rename'),
      h('button.btn.sm.ghost', { onclick: () => act(() => api('PATCH', `/api/cameras/${c.id}`, { enabled: !c.enabled })) }, c.enabled ? 'Disable' : 'Enable'),
      h('button.btn.sm.ghost', { onclick: async () => {
        if (await confirmBox('Revoke invitation', `The current link for ${c.name} stops working and its stream ID is retired, so the phone disconnects. A new link is created.`, 'Revoke'))
          act(() => api('POST', `/api/cameras/${c.id}/revoke`));
      } }, 'Revoke'),
      h('button.btn.sm.ghost.danger', { onclick: async () => {
        if (await confirmBox('Remove camera', `Remove ${c.name}? Its OBS source and scene are deleted; other cameras are not affected.`, 'Remove'))
          act(() => api('DELETE', `/api/cameras/${c.id}`), 'Camera removed');
      } }, 'Remove')));
}

export function camerasPage(el: HTMLElement) {
  el.append(
    h('div.page-h', null, h('h1', null, 'Cameras'),
      live(h('div.row.right'), ['cameras'], () => {
        const s = store.get('cameras') || {};
        if (!s.production) return null;
        return [
          h('button.btn', { onclick: async () => { const nm = await promptBox('Add camera', 'Name (optional)'); if (nm !== null) act(() => api('POST', '/api/cameras', { name: nm || null }), 'Camera added'); } }, '+ Add camera'),
          h('button.btn', { onclick: () => copy((s.cameras || []).filter((c: any) => c.invite).map((c: any) => `${c.name}: ${c.invite.link}`).join('\n'), 'All links') }, 'Copy all links'),
          h('button.btn', { onclick: printSheet }, 'Print QR sheet'),
          h('button.btn', { onclick: () => Promise.all((s.cameras || []).map((c: any) => api('POST', `/api/cameras/${c.id}/reconnect`))).then(() => toast('Reconnecting all viewers', 'ok')) }, 'Reconnect all'),
        ];
      })),
    live(h('div'), ['cameras', 'obs', 'production'], () => {
      const s = store.get('cameras') || {};
      if (!s.production) return h('div.panel.empty', null, h('h2', null, 'No production yet'),
        h('p', null, 'Create a production to get camera links.'),
        h('button.btn.primary.lg', { onclick: createProductionDialog }, 'Create production'));
      const cams = s.cameras || [];
      return [
        h('div.row.small.dim', { style: { marginBottom: '10px' } }, `Room ${s.production.room_id}`, h('span', null, '·'), `${cams.length} camera slot(s)`,
          h('span', null, '·'), `${cams.filter((c: any) => c.status === 'live').length} live`,
          h('span.right'), h('button.btn.sm.ghost.danger', { onclick: async () => {
            if (await confirmBox('Close production', 'Removes all cameras, their invitations and their OBS sources. Layouts, presentations and recordings are kept.', 'Close production'))
              act(() => api('DELETE', '/api/production'), 'Production closed');
          } }, 'Close production')),
        cams.length ? h('div.grid.gauto', null, cams.map(card)) : h('div.panel.empty', null, h('p', null, 'No cameras. Add one.')),
      ];
    }),
  );
}
