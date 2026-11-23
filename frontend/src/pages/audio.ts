import { act, api, clear, confirmBox, disposers, h, live, modal, store, toast } from '../lib';
import { audioPanel } from './production';

// Hall audio inputs: mic in / line in captured by OBS directly (WASAPI, device timing off, zero-latency processing),
// nested into every FestCast scene so they are on air whatever is live. See backend/services/audio.py.

let devices: any[] = [];
const pct = (db: number | null | undefined) => db == null ? 0 : Math.max(0, Math.min(100, (db + 60) / 60 * 100));

async function loadDevices(force = false) {
  if (devices.length && !force) return devices;
  devices = await api('GET', '/api/audio-inputs/devices').catch(e => { toast(e.message, 'error'); return []; });
  return devices;
}

function deviceSelect(current: string, onchange: (v: string) => void) {
  const known = devices.some(d => d.id === current);
  return h('select', { onchange: (e: any) => onchange(e.target.value) },
    known ? null : h('option', { value: current, selected: true }, current === 'default' ? 'Default' : 'Saved device (not found now)'),
    devices.map(d => h('option', { value: d.id, selected: d.id === current, disabled: d.enabled === false }, d.name)));
}

function trackSelect(cur: number | null, tracks: number[], used: Set<number>, onchange: (v: number | null) => void) {
  return h('select', { title: 'Isolated recording track: this input alone, for the edit', onchange: (e: any) => onchange(e.target.value ? Number(e.target.value) : null) },
    h('option', { value: '', selected: cur == null }, 'Programme mix only'),
    tracks.map(t => h('option', { value: String(t), selected: cur === t, disabled: used.has(t) && cur !== t }, `+ own track ${t}`)));
}

function addDialog(kind: 'mic' | 'line', view: any, reload: () => void) {
  const used = new Set<number>(view.inputs.map((a: any) => a.iso_track).filter(Boolean));
  const st = { name: kind === 'mic' ? 'Stage mic' : 'Line in', device: '', iso: null as number | null };
  const pick = devices.find(d => d.guess === kind && d.id !== 'default') || devices.find(d => d.id === 'default');
  st.device = pick ? pick.id : 'default';
  const free = view.iso_tracks.find((t: number) => !used.has(t));
  modal(kind === 'mic' ? 'Add mic in' : 'Add line in', h('div.col', null,
    h('label.f', null, 'Name', h('input', { type: 'text', value: st.name, oninput: (e: any) => { st.name = e.target.value; } })),
    h('label.f', null, 'Device', deviceSelect(st.device, v => { st.device = v; })),
    h('label.f', null, 'Recording', trackSelect(null, view.iso_tracks, used, v => { st.iso = v; })),
    free ? h('p.small.dim', null, `Tip: give each hall input its own track (e.g. track ${free}) so it can be fixed in the edit.`) : null,
    h('p.small.dim', null, kind === 'mic'
      ? 'Mic: noise gate + limiter on. Use the interface\'s gain knob first; aim for peaks around -12 dB.'
      : 'Line in (mixing desk, playback): limiter on, no gate. Set the desk\'s output so peaks sit around -12 dB.')),
  [{ label: 'Add', cls: 'primary', run: () => act(() => api('POST', '/api/audio-inputs', { name: st.name, kind, device_id: st.device, iso_track: st.iso }), 'Audio input added').then(reload) },
   { label: 'Cancel' }]);
}

function inputsPanel() {
  const body = h('div.pb');
  const bars = new Map<string, { bar: HTMLElement; peak: HTMLElement; txt: HTMLElement; card: HTMLElement }>();
  let view: any = { inputs: [], iso_tracks: [], iso_shared: {} };
  const patch = (id: string, b: any) => act(() => api('PATCH', `/api/audio-inputs/${id}`, b));

  const load = async () => {
    if (!(store.get('obs') || {}).connected) { clear(body, h('p.dim', null, 'OBS not connected.')); return; }
    [view] = await Promise.all([api('GET', '/api/audio-inputs').catch(e => { toast(e.message, 'error'); return view; }), loadDevices()]);
    bars.clear();
    const used = new Set<number>(view.inputs.map((a: any) => a.iso_track).filter(Boolean));
    const warn: any[] = [];
    if (used.size && view.output_mode === 'Simple')
      warn.push(h('div.banner.warn', null, 'Isolated tracks are only recorded when OBS is in Advanced output mode (OBS → Settings → Output → Output Mode: Advanced). Until then recordings carry the programme mix only.'));
    for (const [t, names] of Object.entries(view.iso_shared || {}))
      warn.push(h('div.banner.warn', null, `Track ${t} also carries ${(names as string[]).join(', ')} (not a FestCast source). Untick track ${t} for it in OBS → Edit → Advanced Audio Properties to keep the isolated track clean.`));
    clear(body, warn, view.inputs.length ? h('div.strips', null, view.inputs.map((a: any) => {
      const bar = h('i', { style: { width: '0%' } }), peak = h('b', { style: { left: '0%' } }), txt = h('span.mono.tiny.right', null, '—');
      const gainVal = h('span.mono.tiny', null, `${a.gain_db > 0 ? '+' : ''}${a.gain_db} dB`);
      const card = h('div.strip', null,
        h('div.row', null,
          h('input', { type: 'text', value: a.name, style: { flex: '1', minWidth: '0', height: '28px' }, onchange: (e: any) => patch(a.id, { name: e.target.value }) }),
          h('span.pill.plain', null, a.kind === 'mic' ? 'mic in' : 'line in'), a.in_obs ? null : h('span.pill.bad', { title: 'Not in OBS yet: it is created when OBS connects' }, 'missing')),
        h('div.row', null, h('div.vu', { style: { flex: '1' } }, bar, peak), txt),
        h('label.f.small', null, 'Device', deviceSelect(a.device_id, v => patch(a.id, { device_id: v }))),
        h('div.row', null, h('span.tiny.faint', null, 'Gain'),
          h('input', { type: 'range', min: '-20', max: '30', step: '0.5', value: String(a.gain_db), style: { flex: '1' },
            oninput: (e: any) => { gainVal.textContent = `${Number(e.target.value) > 0 ? '+' : ''}${e.target.value} dB`; },
            onchange: (e: any) => patch(a.id, { gain_db: Number(e.target.value) }) }), gainVal),
        h('div.row.wrap', null,
          h('label.c.small', { title: 'Closes the mic between speeches (no latency)' }, h('input', { type: 'checkbox', checked: !!a.gate, onchange: (e: any) => patch(a.id, { gate: e.target.checked }) }), 'Noise gate'),
          h('label.c.small', { title: 'Stops peaks at -1 dBFS so the recording never clips (no look-ahead, no latency)' }, h('input', { type: 'checkbox', checked: !!a.limiter, onchange: (e: any) => patch(a.id, { limiter: e.target.checked }) }), 'Limiter')),
        h('div.row', null, trackSelect(a.iso_track ?? null, view.iso_tracks, used, v => patch(a.id, { iso_track: v }).then(load)), h('span.right'),
          h('button.btn.sm.red', { onclick: async () => { if (await confirmBox('Remove audio input', `Remove "${a.name}" from OBS? It goes off air immediately.`, 'Remove')) act(() => api('DELETE', `/api/audio-inputs/${a.id}`), 'Removed').then(load); } }, 'Remove')));
      bars.set(a.obs_input, { bar, peak, txt, card });
      return card;
    })) : h('p.dim', null, 'No hall audio inputs yet. Add the mic or the mixing desk\'s line out here: they go on air in every FestCast scene.'));
  };
  disposers.push(store.on('meters', (m: any) => {
    for (const [name, b] of bars) {
      const l = m[name];
      if (!l) continue;
      b.bar.style.width = pct(l.rms_db) + '%';
      b.bar.style.setProperty('--w', `${10000 / Math.max(1, pct(l.rms_db))}%`);
      b.peak.style.left = pct(l.peak_db) + '%';
      b.txt.textContent = l.peak_db == null ? '-∞' : `${l.peak_db} dB`;
      b.card.classList.toggle('clip', !!l.clip && Date.now() / 1000 - l.clip < 2);
    }
  }), store.on('audio', load), store.on('obs', () => { if (!bars.size && !view.inputs.length) load(); }));
  load();
  return h('div.panel', null,
    h('div.ph', null, h('h2', null, 'Hall inputs'),
      h('div.row.right', null,
        h('button.btn.sm.primary', { onclick: async () => { await loadDevices(true); addDialog('mic', view, load); } }, '+ Mic in'),
        h('button.btn.sm.primary', { onclick: async () => { await loadDevices(true); addDialog('line', view, load); } }, '+ Line in'),
        h('button.btn.sm', { title: 'Look for newly plugged-in devices', onclick: async () => { await loadDevices(true); load(); } }, 'Rescan devices'))),
    body);
}

function latencyPanel() {
  return live(h('div.panel'), ['metrics', 'server'], () => {
    const o = (store.get('metrics') || {}).obs || {};
    const sim = (store.get('server') || {}).simulation;
    const ms = o.audio_buffering_ms;
    const state = ms == null ? '' : ms <= 64 ? 'ok' : ms < 200 ? 'warn' : 'bad';
    return [h('div.ph', null, h('h2', null, 'Latency'), h('span.pill.right', { class: state }, ms == null ? (sim ? 'simulated' : 'unknown') : `OBS audio buffer ${ms} ms`)),
      h('div.pb', null, h('ul.small.dim', { style: { margin: 0, paddingLeft: '18px' } },
        h('li', null, 'Hall inputs are captured by OBS itself (WASAPI, device timing off) with gate, gain and limiter only: none of them adds delay. Noise suppression is left out on purpose (10-20 ms+).'),
        h('li', null, 'OBS raises its audio buffer when any source runs late and only lowers it on restart. Disable Desktop Audio and unused Mic/Aux devices in OBS → Settings → Audio; if the buffer above is high, restart OBS.'),
        h('li', null, 'For the lowest buffer turn on OBS → Settings → Advanced → Audio → Low latency audio buffering mode (restart OBS).'),
        h('li', null, 'Phone cameras arrive later than the hall sound. If lips and voice drift apart, delay the hall input with Sync in the mixer below (start around 200 ms) instead of slowing the video.'),
        h('li', null, 'A mic on input 1 of a 2-channel interface plays on the left only: in OBS → Edit → Advanced Audio Properties tick Mono for it.')))];
  });
}

export function audioPage(el: HTMLElement) {
  el.append(
    h('div.page-h', null, h('h1', null, 'Audio')),
    live(h('div'), ['obs'], () => (store.get('obs') || {}).connected ? null : h('div.banner.bad', null, 'OBS is not connected: audio inputs are set up when it reconnects.')),
    h('div.grid', { style: { gridTemplateColumns: 'minmax(0,2fr) minmax(0,1fr)' } }, inputsPanel(), latencyPanel()),
    audioPanel(),
  );
}
