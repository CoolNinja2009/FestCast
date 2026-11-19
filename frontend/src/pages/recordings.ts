import { act, api, bytes, clear, confirmBox, dur, h, live, store } from '../lib';

function configPanel() {
  const box = h('div.pb');
  const draw = async () => {
    const r = store.get('recording') || {};
    const c = r.config || {};
    const dest = await api('GET', '/api/recording/destination').catch(() => ({ drives: [], check: {} }));
    const root = h('input', { type: 'text', value: c.root, style: { width: '100%' } });
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
    const checks = [['auto_start', 'Start recording automatically when the production starts'], ['record_during_standby', 'Keep recording during standby'], ['auto_remux', 'Remux MKV → MP4 after recording (lossless copy)']]
      .map(([k, l]) => h('label.c', null, f(k, h('input', { type: 'checkbox', checked: !!c[k] })), l));
    const labels = ['Format', 'Encoder', 'Quality', 'Filename (OBS format codes)', 'Low-disk warning (GB)', 'Split every N minutes', 'Show length (hours)'];
    const save = async () => {
      const body: any = { root: root.value };
      box.querySelectorAll<HTMLInputElement>('[data-k]').forEach(i => { body[i.dataset.k!] = i.type === 'checkbox' ? i.checked : i.type === 'number' ? Number(i.value) : i.value; });
      if (await act(() => api('PUT', '/api/recording/config', body), 'Recording settings saved')) draw();
    };
    const applied = (store.get('recording') || {}).applied;
    clear(box,
      h('label.f', null, 'Destination folder (any local drive)', root),
      h('div.row.wrap', { style: { margin: '6px 0 12px' } }, h('span.tiny.faint', null, 'Drives:'),
        (dest.drives || []).map((d: any) => h('button.btn.sm', { title: `${bytes(d.free_bytes)} free of ${bytes(d.total_bytes)}`, onclick: () => { root.value = d.mount.replace(/\\?$/, '\\') + 'FestCast\\Recordings'; } },
          `${d.mount} · ${bytes(d.free_bytes)} free`))),
      dest.check ? h('div.small', { style: { marginBottom: '10px', color: dest.check.writable ? 'var(--pvw)' : 'var(--warn)' } },
        dest.check.writable ? `✓ writable · ${bytes(dest.check.free_bytes)} free` : `Folder does not exist yet or is not writable${dest.check.error ? ': ' + dest.check.error : ' (it is created when recording starts)'}`) : null,
      h('div.form', null, fields.map((el, i) => h('label.f', null, labels[i], el))),
      h('div.col', { style: { marginTop: '12px' } }, checks),
      h('div.row', { style: { marginTop: '12px' } }, h('button.btn.primary', { onclick: save }, 'Save'),
        h('button.btn', { onclick: async () => { const a = await act(() => api('POST', '/api/recording/apply'), 'Applied to OBS'); if (a) { store.set('recording', { ...(store.get('recording') || {}), applied: a }); draw(); } } }, 'Apply to OBS & verify')),
      h('p.small.dim', null, 'Files go to <folder>\\YYYY-MM-DD\\ with a metadata.json. FestCast never deletes recordings. After each recording, ffprobe reads the real container, codec and resolution.'),
      applied ? h('table.t', { style: { marginTop: '8px' } }, h('tr', null, h('th', null, 'OBS setting'), h('th', null, 'Requested'), h('th', null, 'OBS reports')),
        h('tr', null, h('td', null, 'Output mode'), h('td', null, ''), h('td.mono', null, applied.output_mode)),
        h('tr', null, h('td', null, 'Record directory'), h('td', null, ''), h('td.mono.small', null, applied.directory)),
        Object.entries(applied).filter(([k]) => k.includes('.')).map(([k, v]: any) => h('tr', null, h('td.small', null, k), h('td.mono', null, v.requested),
          h('td.mono', { style: { color: String(v.actual) === String(v.requested) ? 'var(--pvw)' : 'var(--warn)' } }, String(v.actual ?? '—'))))) : null);
  };
  draw();
  return h('div.panel', null, h('div.ph', null, h('h2', null, 'Recording settings')), box);
}

export function recordingsPage(el: HTMLElement) {
  const hist = h('div');
  const files = h('div');
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
    const fl = await api('GET', '/api/recording/files').catch(() => []);
    clear(files, fl.length ? h('table.t', null, fl.slice(0, 100).map((f: any) => h('tr', null, h('td.mono.small', null, f.rel), h('td.num', null, bytes(f.size_bytes)), h('td.small.dim', null, new Date(f.modified * 1000).toLocaleString())))) :
      h('p.dim', null, 'Nothing in the destination folder yet.'));
  };
  loadHist();
  let lastActive: any = null;
  el.append(
    h('div.page-h', null, h('h1', null, 'Recordings'), h('span.sub', null, 'OBS records; FestCast configures, confirms and watches it')),
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
    h('div.grid.g2', { style: { marginTop: '14px' } }, configPanel(),
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Files'), h('button.btn.sm.right', { onclick: loadHist }, 'Refresh')), h('div.pb', { style: { maxHeight: '520px', overflow: 'auto' } }, files))),
    h('div.panel', { style: { marginTop: '14px' } }, h('div.ph', null, h('h2', null, 'History')), h('div.pb', null, hist)),
  );
}
