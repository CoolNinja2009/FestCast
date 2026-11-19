import { act, api, clear, confirmBox, disposers, h, live, previewImg, store, toast } from '../lib';
import { layoutEditor } from '../components/editor';

let scenes: { name: string; owned: boolean }[] = [];
let directMode = false;
let trSel: string | null = null;
let trDur = '250';
// last scene pressed: kept outside the buttons, which are rebuilt on every OBS event
let lastPress = { name: '', t: 0 };

async function loadScenes() {
  try { scenes = await api('GET', '/api/scenes'); store.set('_scenes', scenes); } catch { /* OBS down */ }
}

export function productionPage(el: HTMLElement) {
  loadScenes();
  let t: any;
  const refresh = () => { clearTimeout(t); t = setTimeout(loadScenes, 300); };
  disposers.push(store.on('layouts', refresh), store.on('cameras', () => {}), store.on('obs', refresh));

  el.append(
    h('div.page-h', null, h('h1', null, 'Production')),
    live(h('div'), ['obs'], () => (store.get('obs') || {}).connected ? null : h('div.banner.bad', null, 'OBS is not connected: switching and layout sync are unavailable until it reconnects.')),
    h('div.grid', { style: { gridTemplateColumns: 'minmax(0,1fr) 240px minmax(0,1fr)' } },
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Preview'), live(h('span.right.small.dim'), ['obs'], () => (store.get('obs') || {}).preview_scene || 'Studio mode off')),
        h('div.pb', null, h('div.mon.pvw', null, h('span.lab', null, 'PVW'), previewImg('preview_scene', 'Select a scene to preview it')))),
      live(h('div.panel'), ['obs', 'production'], transitionPanel),
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Programme'), live(h('span.right.small'), ['obs'], () => (store.get('obs') || {}).program_scene || '')),
        h('div.pb', null, h('div.mon.pgm', null, h('span.lab', null, 'PGM'), previewImg('program_scene', 'No programme'))))),
    h('div.panel', { style: { marginTop: '14px' } },
      h('div.ph', null, h('h2', null, 'Scenes'),
        h('label.c.right.small', null, h('input', { type: 'checkbox', checked: directMode, onchange: (e: any) => { directMode = e.target.checked; } }), 'Direct cut (click goes live)')),
      live(h('div.pb'), ['_scenes', 'obs', 'production'], sceneButtons)),
    live(h('div.panel', { style: { marginTop: '14px' } }), ['production', '_scenes'], standbyPanel),
    h('div.panel', { style: { marginTop: '14px' } }, h('div.ph', null, h('h2', null, 'Layout editor')),
      h('div.pb', null, layoutEditor())),
    audioPanel(),
  );
}

function transitionPanel() {
  const obs = store.get('obs') || {};
  const trs: string[] = obs.transitions || [];
  // the chosen transition is sent with TAKE (and becomes OBS's current transition)
  const cur = trSel && trs.includes(trSel) ? trSel : obs.transition;
  const sel = h('select', { style: { width: '100%' }, onchange: (e: any) => { trSel = e.target.value; } },
    trs.map(n => h('option', { selected: n === cur }, n)));
  const durIn = h('input', { type: 'number', value: trDur, min: '50', max: '5000', step: '50', style: { width: '100%' },
    onchange: (e: any) => { trDur = e.target.value; } });
  const take = (tr?: string) => act(() => api('POST', '/api/program/take', { transition: tr || sel.value, duration_ms: Number(durIn.value) }));
  return [h('div.ph', null, h('h2', null, 'Transition')),
    h('div.pb.col', null,
      h('label.f', null, 'Transition', sel), h('label.f', null, 'Duration (ms)', durIn),
      h('button.btn.lg.red', { disabled: !obs.preview_scene, onpointerdown: (e: PointerEvent) => { if (e.button === 0) take('Cut'); }, title: 'Instant cut preview → programme' }, 'CUT'),
      h('button.btn.lg', { disabled: !obs.preview_scene, onpointerdown: (e: PointerEvent) => { if (e.button === 0) take(); }, title: 'Transition preview → programme' }, 'TAKE'),
      h('label.c.small', null, h('input', { type: 'checkbox', checked: !!obs.studio_mode, onchange: (e: any) => act(() => api('POST', `/api/program/studio?enabled=${e.target.checked}`)) }), 'Studio mode (preview/programme)'))];
}

function sceneButtons() {
  const obs = store.get('obs') || {};
  if (!scenes.length) return h('p.dim', null, obs.connected ? 'Loading scenes…' : 'OBS not connected.');
  const go = (name: string) => act(() => api('POST', '/api/program/live', { scene: name, transition: trSel || obs.transition || 'Fade', duration_ms: Number(trDur) }));
  const pv = (name: string) => directMode ? go(name) : act(() => api('POST', '/api/program/preview', { scene: name }));
  const group = (title: string, list: typeof scenes) => list.length ? [h('div.small.faint', { style: { margin: '8px 0 6px' } }, title),
    h('div.scenes', null, list.map(s => h('button.scene', {
      class: s.name === obs.program_scene ? 'pgm' : s.name === obs.preview_scene ? 'pvw' : '',
      // act on press, not release: a click is lost if OBS events rebuild the button mid-click
      onpointerdown: (e: PointerEvent) => {
        if (e.button !== 0) return;
        const now = performance.now(), again = lastPress.name === s.name && now - lastPress.t < 400;
        lastPress = { name: s.name, t: again ? 0 : now };
        (e.currentTarget as HTMLElement).classList.add(again || directMode ? 'pgm' : 'pvw');
        again && !directMode ? go(s.name) : pv(s.name);
      },
    }, s.name.replace(/^FC (Cam|Layout) · /, ''), h('small', null, s.name.startsWith('FC Cam') ? 'camera' : s.name.startsWith('FC Layout') ? 'layout' : s.name === 'FC Standby' ? 'standby' : 'OBS scene'))))] : [];
  return [
    ...group('Layouts', scenes.filter(s => s.name.startsWith('FC Layout')).sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }))),
    ...group('Cameras (full screen)', scenes.filter(s => s.name.startsWith('FC Cam')).sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }))),
    ...group('Other OBS scenes (not managed by FestCast)', scenes.filter(s => !s.owned)),
    ...group('Standby', scenes.filter(s => s.name === 'FC Standby')),
  ];
}

function standbyPanel() {
  const p = store.get('production') || {};
  const r = p.rules || {};
  const ret = h('select', null, h('option', { value: '' }, p.return_scene ? `Back to: ${p.return_scene}` : 'Choose scene…'),
    scenes.filter(s => s.name !== 'FC Standby').map(s => h('option', { value: s.name }, s.name)));
  const save = (patch: any) => act(() => api('PUT', '/api/standby/rules', { ...r, ...patch }), 'Standby rules saved');
  const sched = h('div.col');
  const rows: any[] = [...(r.schedule || [])];
  const drawSched = () => clear(sched, rows.map((s, i) => h('div.row', null,
    h('input', { type: 'time', value: s.start, onchange: (e: any) => { rows[i].start = e.target.value; } }), 'to',
    h('input', { type: 'time', value: s.end, onchange: (e: any) => { rows[i].end = e.target.value; } }),
    h('input', { type: 'text', value: s.label || '', placeholder: 'Label (e.g. Lunch)', onchange: (e: any) => { rows[i].label = e.target.value; } }),
    h('button.btn.sm.ghost', { onclick: () => { rows.splice(i, 1); drawSched(); } }, '✕'))),
    h('div.row', null, h('button.btn.sm', { onclick: () => { rows.push({ start: '12:00', end: '13:00', label: 'Intermission' }); drawSched(); } }, '+ Intermission'),
      h('button.btn.sm.primary', { onclick: () => save({ schedule: rows }) }, 'Save schedule')));
  drawSched();
  return [h('div.ph', null, h('h2', null, 'Standby'), p.standby_active ? h('span.pill.warn', null, `ON AIR · ${p.standby_reason || 'manual'}`) : h('span.pill.stopped', null, 'off'),
      h('span.small.dim', null, `rules: ${p.override === 'auto' ? 'armed' : p.override === 'forced_off' ? 'suppressed until a camera is back' : 'manual standby'}`)),
    h('div.pb.grid.g2', null,
      h('div.col', null,
        h('div.row.wrap', null,
          h('button.btn.standby.lg', { class: p.standby_active ? 'active' : '', onclick: () => act(() => api('POST', '/api/standby/on'), 'Standby on air') }, 'ACTIVATE STANDBY'),
          ret, h('button.btn.lg.green', { disabled: !p.standby_active, onclick: () => act(() => api('POST', '/api/standby/off', { scene: ret.value || null }), 'Back to production') }, 'Return to production')),
        h('a.small', { href: '#/presentations' }, 'Choose the standby presentation →')),
      h('div.col', null,
        h('label.c', null, h('input', { type: 'checkbox', checked: !!r.when_no_cameras, onchange: (e: any) => save({ when_no_cameras: e.target.checked }) }), 'Auto-standby when the production is started but NO camera is live'),
        h('label.c', null, h('input', { type: 'checkbox', checked: !!r.when_not_started, onchange: (e: any) => save({ when_not_started: e.target.checked }) }), 'Show standby while the production is not started'),
        h('label.c', null, h('input', { type: 'checkbox', checked: !!r.auto_return, onchange: (e: any) => save({ auto_return: e.target.checked }) }), 'Return automatically when cameras come back (off = operator returns manually)'),
        h('label.c', null, 'Grace period', h('input', { type: 'number', value: String(r.grace_seconds ?? 15), min: '0', max: '600', style: { width: '80px' }, onchange: (e: any) => save({ grace_seconds: Number(e.target.value) }) }), 's'),
        h('b.small', null, 'Scheduled intermissions'), sched))];
}

function audioPanel() {
  const body = h('div.pb');
  const bars = new Map<string, { bar: HTMLElement; peak: HTMLElement; strip: HTMLElement; txt: HTMLElement }>();
  // Mute all remembers which sources were on, so undoing it leaves deliberately muted ones (camera mics) muted.
  const RESTORE = 'festcast.audio.unmuteOnRestore';
  let current: any[] = [], allMuted = false;
  const setMute = (names: string[], muted: boolean) =>
    Promise.all(names.map(n => api('PATCH', `/api/audio/${encodeURIComponent(n)}`, { muted })));
  const muteAll = h('button.btn.sm', { disabled: true, title: 'Mute or restore every audio source in OBS', onclick: async () => {
    if (!allMuted) {
      const on = current.filter(a => !a.muted).map(a => a.name);
      try { localStorage.setItem(RESTORE, JSON.stringify(on)); } catch {}
      await act(() => setMute(on, true), 'All audio muted');
    } else {
      let names: string[] = [];
      try { names = JSON.parse(localStorage.getItem(RESTORE) || '[]'); } catch {}
      const known = new Set(current.map(a => a.name));
      names = names.filter(n => known.has(n));
      if (!names.length) names = current.filter(a => !a.is_camera).map(a => a.name);
      await act(() => setMute(names, false), 'Audio restored');
      try { localStorage.removeItem(RESTORE); } catch {}
    }
    load();
  } }, 'Mute all') as HTMLButtonElement;
  const load = async () => {
    if (!(store.get('obs') || {}).connected) { muteAll.disabled = true; clear(body, h('p.dim', null, 'OBS not connected.')); return; }
    const list = await api('GET', '/api/audio').catch(e => { toast(e.message, 'error'); return []; });
    bars.clear();
    allMuted = list.length > 0 && list.every((a: any) => a.muted);
    muteAll.textContent = allMuted ? 'Unmute all' : 'Mute all';
    muteAll.classList.toggle('red', allMuted);
    muteAll.disabled = !list.length;
    current = list;
    clear(body, list.length ? h('div.strips', null, list.map((a: any) => {
      const bar = h('i', { style: { width: '0%' } }), peak = h('b', { style: { left: '0%' } }), txt = h('span.mono.tiny.right', null, '—');
      const vol = h('input', { type: 'range', min: '-60', max: '6', step: '0.5', value: String(a.volume_db), style: { width: '100%' },
        onchange: (e: any) => act(() => api('PATCH', `/api/audio/${encodeURIComponent(a.name)}`, { volume_db: Number(e.target.value) })) });
      const strip = h('div.strip', null,
        h('div.row', null, h('b.small', { title: a.name }, a.label.length > 26 ? a.label.slice(0, 25) + '…' : a.label), a.is_camera ? h('span.pill.plain', null, 'camera') : null, txt),
        h('div.vu', null, bar, peak),
        h('div.row', null, vol, h('span.mono.tiny', null, `${a.volume_db} dB`)),
        h('div.row.wrap', null,
          h('button.btn.sm', { class: a.muted ? 'red' : '', onclick: () => act(() => api('PATCH', `/api/audio/${encodeURIComponent(a.name)}`, { muted: !a.muted })).then(load) }, a.muted ? 'MUTED' : 'Mute'),
          h('select.btn.sm', { title: 'Monitoring', onchange: (e: any) => act(() => api('PATCH', `/api/audio/${encodeURIComponent(a.name)}`, { monitor: e.target.value })) },
            [['none', 'Monitor off'], ['monitor', 'Monitor only'], ['both', 'Monitor + output']].map(([v, l]) => h('option', { value: v, selected: a.monitor === v }, l))),
          h('label.row.tiny.faint', null, 'Sync', h('input', { type: 'number', value: String(a.sync_offset_ms ?? 0), step: '10', style: { width: '70px', height: '26px' },
            onchange: (e: any) => act(() => api('PATCH', `/api/audio/${encodeURIComponent(a.name)}`, { sync_offset_ms: Number(e.target.value) })) }), 'ms')));
      bars.set(a.name, { bar, peak, strip, txt });
      return strip;
    })) : h('p.dim', null, 'No audio sources in OBS.'));
  };
  const pct = (db: number | null) => db === null ? 0 : Math.max(0, Math.min(100, (db + 60) / 60 * 100));
  disposers.push(store.on('meters', (m: any) => {
    for (const [name, b] of bars) {
      const l = m[name];
      if (!l) continue;
      b.bar.style.width = pct(l.rms_db) + '%';
      b.bar.style.setProperty('--w', `${10000 / Math.max(1, pct(l.rms_db))}%`);
      b.peak.style.left = pct(l.peak_db) + '%';
      b.txt.textContent = l.peak_db === null ? '-∞' : `${l.peak_db} dB`;
      b.strip.classList.toggle('clip', !!l.clip && Date.now() / 1000 - l.clip < 2);
    }
  }));
  load();
  return h('div.panel', { style: { marginTop: '14px' } }, h('div.ph', null, h('h2', null, 'Audio'),
    h('div.row.right', null, muteAll, h('button.btn.sm', { onclick: load }, 'Refresh'))), body);
}

void confirmBox;
