import { act, api, bytes, clear, confirmBox, dur, h, live, store } from '../lib';

function configPanel() {
  const box = h('div.pb');
  let lastApply: { applied?: any; apply_error?: string | null } | null = null;
  const draw = async () => {
    const r = store.get('recording') || {};
    const c = r.config || {};
    const dest = await api('GET', '/api/recording/destination').catch(() => ({ drives: [], check: {} }));
    const root = h('input', { type: 'text', value: c.root, style: { flex: '1', minWidth: '260px' } }) as HTMLInputElement;
    const f = (k: string, el: HTMLElement) => { (el as any).dataset.k = k; return el; };
    const fields = [
      f('format', h('select', null, ['mkv', 'mp4', 'hybrid_mp4', 'fragmented_mp4', 'mov'].map(x => h('option', { selected: x === c.format }, x)))),
      f('encoder', h('select', null, [['nvenc_h264', 'NVIDIA NVENC H.264'], ['nvenc_hevc', 'NVIDIA NVENC HEVC'], ['x264', 'x264 (CPU)']].map(([v, l]) => h('option', { value: v, selected: v === c.encoder }, l)))),
      f('quality', h('select', null, [['high', 'High quality'], ['small', 'Smaller files']].map(([v, l]) => h('option', { value: v, selected: v === c.quality }, l)))),
      f('filename', h('input', { type: 'text', value: c.filename })),
      f('warn_free_gb', h('input', { type: 'number', value: String(c.warn_free_gb), min: '1' })),
      f('segment_minutes', h('input', { type: 'number', value: String(c.segment_minutes || 0), min: '0', title: 'Advanced output mode only; 0 = one file' })),
      f('show_hours', h('input', { type: 'number', value: String(c.show_hours ?? 9), min: '1', max: '48', step: '0.5', title: 'FestCast warns if the drive would fill before this' })),
    ];
    const labels = ['Format', 'Encoder', 'Quality', 'Filename (OBS format codes)', 'Low-disk warning (GB)', 'Split every N minutes', 'Show length (hours)'];
    const check = (k: string, l: string) => h('label.c', null, f(k, h('input', { type: 'checkbox', checked: !!c[k] })), l);
    const save = async () => {
      const body: any = { root: root.value.trim() };
      box.querySelectorAll<HTMLInputElement>('[data-k]').forEach(i => { body[i.dataset.k!] = i.type === 'checkbox' ? i.checked : i.type === 'number' ? Number(i.value) : i.value; });
      const res = await act(() => api('PUT', '/api/recording/config', body), 'Recording settings saved');
      if (res) {
        lastApply = res;
        if (res.applied) store.set('recording', { ...(store.get('recording') || {}), applied: res.applied });
        draw();
      }
    };
    const browse = async () => {
      const res = await act(() => api('POST', '/api/recording/browse'));
      if (res && res.path) { root.value = res.path; save(); }
    };
    const applied = (store.get('recording') || {}).applied;
    const ck = dest.check || {};
    clear(box,
      h('label.f', null, 'Save recordings in this folder'),
      h('div.row.wrap', null, root,
        h('button.btn', { onclick: browse, title: 'Opens a folder picker on the FestCast PC' }, 'Browse…'),
        h('button.btn.primary', { onclick: save }, 'Save')),
      h('div.row.wrap', { style: { margin: '6px 0 4px' } },
        (dest.drives || []).map((d: any) => h('button.btn.sm', { title: `${bytes(d.free_bytes)} free of ${bytes(d.total_bytes)}`, onclick: () => { root.value = d.mount.replace(/\\?$/, '\\') + 'FestCast\\Recordings'; save(); } },
          `${d.mount} · ${bytes(d.free_bytes)} free`))),
      h('div.small', { style: { margin: '6px 0 12px', color: ck.error ? 'var(--warn)' : 'var(--pvw)' } },
        ck.writable ? `✓ Recording to ${ck.path} · ${bytes(ck.free_bytes)} free` :
          ck.error ? `Folder ${ck.path} is not usable: ${ck.error}` : `Recording to ${ck.path || c.root} (folder is created when recording starts)`),
      lastApply?.apply_error ? h('div.small', { style: { color: 'var(--warn)', marginBottom: '10px' } }, lastApply.apply_error)
        : applied?.directory ? h('div.small.dim', { style: { marginBottom: '10px' } }, 'OBS confirms it will write into: ', h('span.mono', null, applied.directory)) : null,
      h('div.col', null,
        check('auto_start', 'Record with the production (starts when you start the production, stops when you end it)'),
        check('record_during_standby', 'Keep recording during standby')),
      h('details', { style: { marginTop: '14px' } },
        h('summary', { style: { cursor: 'pointer' } }, h('span.small', null, 'Advanced (format, encoder, file names…)')),
        h('div.form', { style: { marginTop: '10px' } }, fields.map((el, i) => h('label.f', null, labels[i], el))),
        h('div.col', { style: { marginTop: '10px' } }, check('auto_remux', 'Remux MKV → MP4 after recording (lossless copy)')),
        applied ? h('table.t', { style: { marginTop: '8px' } }, h('tr', null, h('th', null, 'OBS setting'), h('th', null, 'Requested'), h('th', null, 'OBS reports')),
          h('tr', null, h('td', null, 'Output mode'), h('td', null, ''), h('td.mono', null, applied.output_mode)),
          Object.entries(applied).filter(([k]) => k.includes('.')).map(([k, v]: any) => h('tr', null, h('td.small', null, k), h('td.mono', null, v.requested),
            h('td.mono', { style: { color: String(v.actual) === String(v.requested) ? 'var(--pvw)' : 'var(--warn)' } }, String(v.actual ?? '—'))))) : null));
  };
  draw();
  return h('div.panel', null, h('div.ph', null, h('h2', null, 'Where and how to record')), box);
}

export function recordingsPage(el: HTMLElement) {
  const hist = h('div');
  const loadHist = async () => {
    const rows = await api('GET', '/api/recording/history').catch(() => []);
    clear(hist, rows.length ? h('table.t', null, h('tr', null, h('th', null, 'Started'), h('th', null, 'Duration'), h('th', null, 'Size'), h('th', null, 'Verified (ffprobe)'), h('th', null, 'Status'), h('th', null, 'File'), h('th', null, '')),
      rows.map((r: any) => {
        const a = r.meta?.actual;
        return h('tr', null, h('td.small', null, new Date(r.started_at).toLocaleString()), h('td.num', null, r.meta?.duration_s ? dur(r.meta.duration_s * 1000) : '—'),
          h('td.num', null, bytes(r.size_bytes)), h('td.small.mono', null, r.meta?.simulated ? 'simulated (not a video)' : a && a.video_codec ? `${a.video_codec} ${a.width || ''}×${a.height || ''} ${a.audio_codec || ''} · ${a.container || ''}` : (a ? 'unreadable file' : '—')),
          h('td', null, h('span.pill', { class: r.status === 'finished' ? 'ok' : r.status === 'recording' ? 'live' : 'warn' }, r.status.replace('_', ' '))),
          h('td.small.mono', { title: r.path || '' }, (r.path || '').split(/[\\/]/).pop()),
          h('td', null, r.path && /\.mkv$/i.test(r.path) && r.status !== 'recording' ? (r.remux_path ? h('span.small.dim', null, 'MP4 ✓') :
            h('button.btn.sm', { disabled: r.remux?.state === 'running', onclick: () => act(() => api('POST', `/api/recording/${r.id}/remux`), 'Remux started (lossless)') }, r.remux?.state === 'running' ? 'Remuxing…' : 'Remux → MP4')) : ''));
      })) : h('p.dim', null, 'No recordings yet.'));
  };
  loadHist();
  let lastActive: any = null;
  el.append(
    h('div.page-h', null, h('h1', null, 'Recordings')),
    live(h('div.panel'), ['recording', 'obs'], () => {
      const r = store.get('recording') || {};
      const obs = store.get('obs') || {};
      if (lastActive !== null && lastActive !== r.active) setTimeout(loadHist, 1500);
      lastActive = r.active;
      return [h('div.ph', null, h('h2', null, 'Current recording'), r.active ? h('span.tally.rec', null, r.paused ? 'PAUSED' : 'RECORDING') : h('span.pill.stopped', null, 'not recording'),
          r.active ? h('span.small.dim', null, 'state confirmed by OBS') : null),
        h('div.pb.grid.g4', null,
          h('div.stat', null, h('span.l', null, 'Duration'), h('span.v', null, r.active ? dur(r.duration_ms) : '—')),
          h('div.stat', null, h('span.l', null, 'File size'), h('span.v', null, r.active ? bytes(r.bytes) : '—')),
          h('div.stat', null, h('span.l', null, 'Free on drive'), h('span.v', null, bytes(r.dest_free_bytes))),
          h('div.stat', null, h('span.l', null, 'Remaining at this rate'), h('span.v', null, r.estimated_remaining_s ? dur(r.estimated_remaining_s * 1000) : '—'))),
        h('div.pb.row.wrap', { style: { paddingTop: 0 } },
          r.active ? [h('button.btn.red.lg', { onclick: async () => { if (await confirmBox('Stop recording', 'Stop the OBS recording now?', 'Stop recording')) act(() => api('POST', '/api/recording/stop')); } }, '■ Stop recording'),
            r.paused ? h('button.btn.lg', { onclick: () => act(() => api('POST', '/api/recording/resume')) }, 'Resume') : h('button.btn.lg', { onclick: () => act(() => api('POST', '/api/recording/pause')) }, 'Pause')]
            : h('button.btn.red.lg', { disabled: !obs.connected, onclick: () => act(() => api('POST', '/api/recording/start')) }, '● Start recording'),
          h('span.small.mono.dim', null, r.path || '')),
      ];
    }),
    h('div', { style: { marginTop: '14px' } }, configPanel()),
    h('div.panel', { style: { marginTop: '14px' } }, h('div.ph', null, h('h2', null, 'Past recordings'), h('button.btn.sm.right', { onclick: loadHist }, 'Refresh')), h('div.pb', null, hist)),
  );
}
