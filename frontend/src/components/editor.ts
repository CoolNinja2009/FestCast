// Visual composition editor. Elements use normalised coordinates (0..1 of the canvas); every committed change is
// saved to SQLite and materialised in the layout's OBS scene via SetSceneItemTransform (backend/services/layouts.py).
import { act, api, clear, confirmBox, disposers, download, h, modal, pickFile, promptBox, store, toast } from '../lib';

type El = {
  id: string; type: 'camera' | 'text' | 'image' | 'color'; name: string; x: number; y: number; w: number; h: number; z: number;
  locked: boolean; hidden: boolean; fit: 'fit' | 'stretch'; crop: { l: number; t: number; r: number; b: number };
  group: string | null; opacity: number; camera_id?: string; text?: string; font?: string; size?: number; color?: string;
  bold?: boolean; src?: string;
};

const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
const r4 = (v: number) => Math.round(v * 10000) / 10000;
const W = 1920, H = 1080;

/** Even grid for any number of cameras (16:9 canvas: square-ish grids keep 16:9 tiles). */
function autoGrid(n: number): number[][] {
  if (n <= 0) return [];
  const cols = Math.ceil(Math.sqrt(n)), rows = Math.ceil(n / cols);
  const w = 1 / cols, hh = 1 / rows, out: number[][] = [];
  for (let i = 0; i < n; i++) {
    const r = Math.floor(i / cols), inRow = r === rows - 1 ? n - r * cols : cols;
    const off = (1 - inRow * w) / 2; // centre a short last row
    out.push([off + (i % cols) * w, r * hh, w, hh]);
  }
  return out;
}

function mk(type: El['type'], extra: Partial<El> = {}): El {
  return { id: uid(), type, name: type === 'camera' ? 'Camera' : type === 'text' ? 'Text' : type === 'image' ? 'Image' : 'Shape',
    x: .3, y: .3, w: .4, h: .4, z: 0, locked: false, hidden: false, fit: 'fit', crop: { l: 0, t: 0, r: 0, b: 0 }, group: null, opacity: 1,
    ...(type === 'text' ? { text: 'Text', font: 'Segoe UI', size: 64, color: '#ffffff', bold: true, w: .3, h: .08 } : {}),
    ...(type === 'color' ? { color: '#101418', w: .3, h: .12 } : {}), ...extra };
}

const TEMPLATES: { id: string; label: string; n: number; boxes: number[][] }[] = [
  { id: 'single', label: 'Full screen', n: 1, boxes: [[0, 0, 1, 1]] },
  { id: 'vsplit', label: 'Two side by side', n: 2, boxes: [[0, 0, .5, 1], [.5, 0, .5, 1]] },
  { id: 'hsplit', label: 'Two stacked', n: 2, boxes: [[0, 0, 1, .5], [0, .5, 1, .5]] },
  { id: 'pip', label: 'Picture in picture', n: 2, boxes: [[0, 0, 1, 1], [.69, .655, .28, .28]] },
  { id: 'main2', label: 'Main + two side', n: 3, boxes: [[0, .1667, .6667, .6667], [.6667, .1667, .3333, .3333], [.6667, .5, .3333, .3333]] },
  { id: 'grid3', label: 'Three grid', n: 3, boxes: [[0, 0, .5, .5], [.5, 0, .5, .5], [.25, .5, .5, .5]] },
  { id: 'grid4', label: 'Four grid', n: 4, boxes: [[0, 0, .5, .5], [.5, 0, .5, .5], [0, .5, .5, .5], [.5, .5, .5, .5]] },
  { id: 'main4', label: 'Main + four below', n: 5, boxes: [[.125, 0, .75, .75], [0, .75, .25, .25], [.25, .75, .25, .25], [.5, .75, .25, .25], [.75, .75, .25, .25]] },
  { id: 'grid6', label: 'Six grid (3×2)', n: 6, boxes: [[0, 0, 1 / 3, .5], [1 / 3, 0, 1 / 3, .5], [2 / 3, 0, 1 / 3, .5], [0, .5, 1 / 3, .5], [1 / 3, .5, 1 / 3, .5], [2 / 3, .5, 1 / 3, .5]] },
  { id: 'auto', label: 'All cameras (auto grid)', n: -1, boxes: [] },
  { id: 'free', label: 'Empty (freeform)', n: 0, boxes: [] },
];

export function layoutEditor(): HTMLElement {
  let curId: string | null = null;
  let els: El[] = [];
  let sel = new Set<string>();
  let undo: string[] = [], redo: string[] = [];
  let grid = true, snap = true, holdLive = true, pendingPush = false;
  let saveT: any = null, saving = false;
  let clipboard: El[] = [];

  const root = h('div.editor');
  const listBox = h('div.col');
  const stageWrap = h('div.stage-wrap');
  const stage = h('div.stage', { class: 'grid-on', tabindex: '0' });
  const toolbar = h('div.toolbar');
  const insp = h('div.panel.insp', { style: { padding: '4px 12px' } });
  const status = h('div.row.small', { style: { marginTop: '8px' } });
  stageWrap.append(toolbar, stage, status);

  const layouts = () => (store.get('layouts') || []) as any[];
  const cur = () => layouts().find(l => l.id === curId);
  const cams = () => ((store.get('cameras') || {}).cameras || []) as any[];
  const byId = (id: string) => els.find(e => e.id === id)!;
  const selected = () => els.filter(e => sel.has(e.id));

  // ------------------------------------------------------------------ persistence / history
  function snapshot() { undo.push(JSON.stringify(els)); if (undo.length > 100) undo.shift(); redo = []; }
  function commit() { snapshot(); scheduleSave(); render(); }
  function change(fn: () => void) { snapshot(); fn(); scheduleSave(); render(); }
  function scheduleSave() { clearTimeout(saveT); saveT = setTimeout(save, 350); drawStatus('saving'); }
  async function save() {
    saveT = null;
    if (!curId) return;
    const l = cur();
    const syncNow = !(l && l.live && holdLive);
    saving = true;
    const r = await act(() => api('PUT', `/api/layouts/${curId}?sync=${syncNow}`, { elements: els }));
    saving = false;
    if (r && !syncNow) pendingPush = true;
    if (r && syncNow) pendingPush = false;
    drawStatus();
  }
  async function pushToAir() {
    if (!curId) return;
    const r = await act(() => api('POST', `/api/layouts/${curId}/sync`));
    if (r) { pendingPush = false; toast(r.state === 'synced' ? 'Pushed to OBS' : `OBS: ${r.error}`, r.state === 'synced' ? 'ok' : 'warning'); }
    drawStatus();
  }
  function doUndo() { if (!undo.length) return; redo.push(JSON.stringify(els)); els = JSON.parse(undo.pop()!); sel = new Set([...sel].filter(id => els.some(e => e.id === id))); scheduleSave(); render(); }
  function doRedo() { if (!redo.length) return; undo.push(JSON.stringify(els)); els = JSON.parse(redo.pop()!); scheduleSave(); render(); }

  function load(id: string | null) {
    clearTimeout(saveT);
    curId = id;
    const l = cur();
    els = l ? JSON.parse(JSON.stringify(l.elements)) : [];
    sel = new Set(); undo = []; redo = []; pendingPush = false;
    render();
  }

  // ------------------------------------------------------------------ list
  function drawList() {
    const ls = layouts();
    clear(listBox,
      h('div.row', null, h('b.small.faint', { style: { letterSpacing: '.08em' } }, 'LAYOUTS'), h('span.right'),
        h('button.btn.sm', { title: 'New layout', onclick: async () => {
          const nm = await promptBox('New layout', 'Name', `Layout ${ls.length + 1}`);
          if (nm) { const l = await act(() => api('POST', '/api/layouts', { name: nm, elements: [] })); if (l) { store.set('layouts', [...layouts().filter(x => x.id !== l.id), l]); load(l.id); } }
        } }, '+'),
        h('button.btn.sm', { title: 'Import layout (.json)', onclick: async () => {
          const [f] = await pickFile('.json'); if (!f) return;
          const fd = new FormData(); fd.append('file', f);
          const l = await act(() => api('POST', '/api/layouts/import', fd), 'Layout imported'); if (l) load(l.id);
        } }, '⇪')),
      h('div.lay-list', null, ls.length ? ls.map(l => h('div.lay-item', { class: l.id === curId ? 'on' : '', onclick: () => l.id !== curId && load(l.id) },
        h('span.grow', null, l.name), l.live ? h('span.live', null, 'LIVE') : null,
        h('span.pill.plain', { class: l.sync?.state, title: l.sync?.error || '' }, l.sync?.state === 'synced' ? 'OBS' : l.sync?.state || ''))) :
        h('p.small.dim', null, 'No layouts yet. Create one, then drag cameras in or pick a template.')));
  }

  // ------------------------------------------------------------------ palette
  function palette() {
    const box = h('div.col');
    const draw = () => clear(box,
      h('b.small.faint', { style: { letterSpacing: '.08em' } }, 'ADD'),
      cams().map(c => h('div.chip', { draggable: 'true', title: 'Drag onto the canvas, or click to add',
        ondragstart: (e: DragEvent) => e.dataTransfer!.setData('text/fc-cam', c.id),
        onclick: () => addEl(mk('camera', { camera_id: c.id, name: c.name, x: .25, y: .25, w: .5, h: .5 })) },
        h('span.pill', { class: c.status }, ''), c.name)),
      h('div.row.wrap', null,
        h('button.btn.sm', { onclick: () => addEl(mk('text')) }, 'Text'),
        h('button.btn.sm', { onclick: () => addEl(mk('color')) }, 'Shape'),
        h('button.btn.sm', { onclick: addImage }, 'Image / logo'),
        h('button.btn.sm', { onclick: addLowerThird }, 'Lower third')));
    disposers.push(store.on('cameras', () => draw()));
    draw();
    return box;
  }
  function addEl(e: El) {
    if (!curId) return toast('Create or select a layout first', 'warning');
    e.z = Math.max(0, ...els.map(x => x.z)) + 1;
    change(() => { els.push(e); sel = new Set([e.id]); });
  }
  async function addImage() {
    if (!curId) return toast('Create or select a layout first', 'warning');
    const [f] = await pickFile('image/png,image/jpeg,image/webp,image/gif,image/svg+xml'); if (!f) return;
    const fd = new FormData(); fd.append('file', f);
    const r = await act(() => api('POST', '/api/media', fd)); if (!r) return;
    const img = new Image();
    img.onload = () => {
      const w = .2, hh = w * (img.naturalHeight / img.naturalWidth) * (W / H);
      addEl(mk('image', { src: r.src, name: f.name.slice(0, 40), x: .03, y: .04, w, h: clamp(hh, .02, 1) }));
    };
    img.onerror = () => addEl(mk('image', { src: r.src, name: f.name.slice(0, 40), x: .03, y: .04, w: .2, h: .2 }));
    img.src = r.url;
  }
  function addLowerThird() {
    if (!curId) return toast('Create or select a layout first', 'warning');
    const g = uid();
    const z = Math.max(0, ...els.map(x => x.z)) + 1;
    const bar = mk('color', { name: 'Lower third bar', color: '#0b0d10', opacity: .82, x: .05, y: .78, w: .5, h: .13, z, group: g });
    const accent = mk('color', { name: 'Lower third accent', color: '#e5484d', x: .05, y: .78, w: .006, h: .13, z: z + 1, group: g });
    const t1 = mk('text', { name: 'Lower third title', text: 'Name Surname', size: 56, x: .068, y: .79, w: .45, h: .06, z: z + 2, group: g });
    const t2 = mk('text', { name: 'Lower third subtitle', text: 'Role / performance', size: 34, bold: false, color: '#c9ccd1', x: .068, y: .852, w: .45, h: .04, z: z + 3, group: g });
    change(() => { els.push(bar, accent, t1, t2); sel = new Set([bar.id, accent.id, t1.id, t2.id]); });
  }
  function applyTemplate(t: (typeof TEMPLATES)[number]) {
    if (!curId) return toast('Create or select a layout first', 'warning');
    const cs = [...cams()].sort((a, b) => (a.status === 'live' ? 0 : 1) - (b.status === 'live' ? 0 : 1) || a.slot - b.slot);
    if (t.id === 'auto') t = { ...t, boxes: autoGrid(cs.length) };
    change(() => {
      const keep = els.filter(e => e.type !== 'camera');
      const camEls = t.boxes.map((b, i) => mk('camera', { x: b[0], y: b[1], w: b[2], h: b[3], z: i + 1, camera_id: cs[i]?.id || '', name: cs[i]?.name || `Camera ${i + 1}` }));
      keep.forEach((e, i) => { e.z = camEls.length + 1 + i; });
      els = [...camEls, ...keep];
      sel = new Set();
    });
  }

  // ------------------------------------------------------------------ stage
  function px() { const r = stage.getBoundingClientRect(); return { r, sx: r.width, sy: r.height }; }

  function elNode(e: El): HTMLElement {
    const prev = (store.get('previews') || {})[e.camera_id || ''];
    const n = h('div.el', {
      'data-id': e.id, 'data-cam': e.type === 'camera' ? e.camera_id : null,
      class: [e.type === 'camera' ? 'cam' : e.type === 'text' ? 'txt' : '', sel.has(e.id) ? 'sel' : '', e.locked ? 'locked' : '', e.hidden ? 'hid' : ''].join(' '),
      style: { left: e.x * 100 + '%', top: e.y * 100 + '%', width: e.w * 100 + '%', height: e.h * 100 + '%', zIndex: String(e.z), opacity: String(e.hidden ? .25 : e.opacity) },
    });
    const bgCrop = () => {
      const c = e.crop, sx = 1 - c.l - c.r, sy = 1 - c.t - c.b;
      n.style.backgroundSize = e.fit === 'stretch' || e.type === 'camera' ? `${100 / sx}% ${100 / sy}%` : 'contain';
      n.style.backgroundPosition = `${c.l + c.r ? (c.l / (c.l + c.r)) * 100 : 0}% ${c.t + c.b ? (c.t / (c.t + c.b)) * 100 : 0}%`;
      n.style.backgroundRepeat = 'no-repeat';
    };
    if (e.type === 'camera') {
      const cam = cams().find(c => c.id === e.camera_id);
      if (prev) n.style.backgroundImage = `url("${prev}")`;
      bgCrop();
      n.append(h('span.cl', null, cam ? cam.name : 'No camera assigned'));
    } else if (e.type === 'image') {
      n.style.backgroundImage = `url("/api/media/${encodeURIComponent(e.src || '')}")`;
      n.style.backgroundSize = e.fit === 'stretch' ? '100% 100%' : 'contain';
      n.style.backgroundPosition = 'center'; n.style.backgroundRepeat = 'no-repeat';
    } else if (e.type === 'color') {
      n.style.background = e.color || '#000';
    } else {
      n.style.color = e.color || '#fff';
      n.style.fontFamily = `"${e.font}", sans-serif`;
      n.style.fontWeight = e.bold ? '700' : '400';
      n.style.fontSize = `${(e.size || 64) / H * (stage.clientHeight || 400)}px`;
      n.textContent = e.text || '';
    }
    if (sel.size === 1 && sel.has(e.id) && !e.locked) for (const k of ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w']) n.append(h('span.h', { class: k, 'data-h': k }));
    return n;
  }

  function drawStage() {
    stage.classList.toggle('grid-on', grid);
    clear(stage, [...els].sort((a, b) => a.z - b.z).map(elNode));
    if (!curId) stage.append(h('div.none', { style: { position: 'absolute', inset: '0', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-3)' } }, 'Select or create a layout'));
  }
  disposers.push(store.on('previews', (p: any) => {
    stage.querySelectorAll<HTMLElement>('.el.cam').forEach(n => { const s = p[n.dataset.cam || '']; if (s) n.style.backgroundImage = `url("${s}")`; });
  }));
  const ro = new ResizeObserver(() => drawStage());
  ro.observe(stage);
  disposers.push(() => ro.disconnect());

  // snapping
  const TH = 0.008;
  function snapLines(exclude: Set<string>) {
    const xs = [0, .5, 1], ys = [0, .5, 1];
    for (const e of els) if (!exclude.has(e.id) && !e.hidden) { xs.push(e.x, e.x + e.w / 2, e.x + e.w); ys.push(e.y, e.y + e.h / 2, e.y + e.h); }
    return { xs, ys };
  }
  function guides(gx: number[], gy: number[]) {
    stage.querySelectorAll('.guide').forEach(g => g.remove());
    for (const x of gx) stage.append(h('div.guide', { style: { left: x * 100 + '%', top: '0', bottom: '0', width: '1px' } }));
    for (const y of gy) stage.append(h('div.guide', { style: { top: y * 100 + '%', left: '0', right: '0', height: '1px' } }));
  }
  function snapVal(v: number, cands: number[], gridStep: number): [number, number | null] {
    if (!snap) return [v, null];
    let best: number | null = null, bd = TH;
    for (const c of cands) { const d = Math.abs(v - c); if (d < bd) { bd = d; best = c; } }
    if (best !== null) return [best, best];
    if (grid) return [Math.round(v / gridStep) * gridStep, null];
    return [v, null];
  }

  stage.addEventListener('dragover', e => { if (e.dataTransfer?.types.includes('text/fc-cam')) e.preventDefault(); });
  stage.addEventListener('drop', e => {
    const cid = e.dataTransfer?.getData('text/fc-cam'); if (!cid) return;
    e.preventDefault();
    const { r } = px();
    const cam = cams().find(c => c.id === cid);
    const w = .4, hh = .4;
    addEl(mk('camera', { camera_id: cid, name: cam?.name || 'Camera', w, h: hh, x: r4(clamp((e.clientX - r.left) / r.width - w / 2, 0, 1 - w)), y: r4(clamp((e.clientY - r.top) / r.height - hh / 2, 0, 1 - hh)) }));
  });

  stage.addEventListener('pointerdown', (ev) => {
    stage.focus();
    const t = ev.target as HTMLElement;
    const node = t.closest('.el') as HTMLElement | null;
    if (!node) { if (!ev.shiftKey) { sel = new Set(); render(); } return; }
    const e = byId(node.dataset.id!);
    const groupIds = e.group ? els.filter(x => x.group === e.group).map(x => x.id) : [e.id];
    if (ev.shiftKey || ev.ctrlKey) { groupIds.forEach(id => sel.has(id) ? sel.delete(id) : sel.add(id)); render(); return; }
    if (!sel.has(e.id)) { sel = new Set(groupIds); render(); }
    const handle = t.dataset.h;
    const movers = selected().filter(x => !x.locked);
    if (!movers.length) return;
    const { r } = px();
    const start = { x: ev.clientX, y: ev.clientY };
    const orig = new Map(movers.map(m => [m.id, { x: m.x, y: m.y, w: m.w, h: m.h }]));
    const bb = { x: Math.min(...movers.map(m => m.x)), y: Math.min(...movers.map(m => m.y)), x2: Math.max(...movers.map(m => m.x + m.w)), y2: Math.max(...movers.map(m => m.y + m.h)) };
    const lines = snapLines(new Set(movers.map(m => m.id)));
    let moved = false;
    const before = JSON.stringify(els);
    try { stage.setPointerCapture(ev.pointerId); } catch { /* synthetic or already released pointer */ }
    const onMove = (me: PointerEvent) => {
      const dx = (me.clientX - start.x) / r.width, dy = (me.clientY - start.y) / r.height;
      if (!moved && Math.abs(me.clientX - start.x) + Math.abs(me.clientY - start.y) < 3) return;
      moved = true;
      const gx: number[] = [], gy: number[] = [];
      if (!handle) {
        let nx = bb.x + dx, ny = bb.y + dy;
        const bw = bb.x2 - bb.x, bh = bb.y2 - bb.y;
        // try snapping left, centre, right edges
        let best: [number, number | null] = [nx, null];
        for (const off of [0, bw / 2, bw]) { const [v, g] = snapVal(nx + off, lines.xs, 1 / 48); if (g !== null) { best = [v - off, g]; break; } if (off === 0) best = [v, null]; }
        nx = best[0]; if (best[1] !== null) gx.push(best[1]);
        let bestY: [number, number | null] = [ny, null];
        for (const off of [0, bh / 2, bh]) { const [v, g] = snapVal(ny + off, lines.ys, 1 / 27); if (g !== null) { bestY = [v - off, g]; break; } if (off === 0) bestY = [v, null]; }
        ny = bestY[0]; if (bestY[1] !== null) gy.push(bestY[1]);
        const ddx = nx - bb.x, ddy = ny - bb.y;
        for (const m of movers) { const o = orig.get(m.id)!; m.x = r4(o.x + ddx); m.y = r4(o.y + ddy); }
      } else {
        const m = movers[0], o = orig.get(m.id)!;
        let x1 = o.x, y1 = o.y, x2 = o.x + o.w, y2 = o.y + o.h;
        if (handle.includes('w')) { const [v, g] = snapVal(x1 + dx, lines.xs, 1 / 48); x1 = Math.min(v, x2 - .01); if (g !== null) gx.push(g); }
        if (handle.includes('e')) { const [v, g] = snapVal(x2 + dx, lines.xs, 1 / 48); x2 = Math.max(v, x1 + .01); if (g !== null) gx.push(g); }
        if (handle.includes('n')) { const [v, g] = snapVal(y1 + dy, lines.ys, 1 / 27); y1 = Math.min(v, y2 - .01); if (g !== null) gy.push(g); }
        if (handle.includes('s')) { const [v, g] = snapVal(y2 + dy, lines.ys, 1 / 27); y2 = Math.max(v, y1 + .01); if (g !== null) gy.push(g); }
        if (me.shiftKey && handle.length === 2) { // keep aspect ratio
          const ar = o.w / o.h; const w = x2 - x1; const hh = w / ar;
          if (handle.includes('n')) y1 = y2 - hh; else y2 = y1 + hh;
        }
        m.x = r4(x1); m.y = r4(y1); m.w = r4(x2 - x1); m.h = r4(y2 - y1);
      }
      for (const m of movers) {
        const n = stage.querySelector<HTMLElement>(`.el[data-id="${m.id}"]`);
        if (n) Object.assign(n.style, { left: m.x * 100 + '%', top: m.y * 100 + '%', width: m.w * 100 + '%', height: m.h * 100 + '%' });
      }
      guides(gx, gy);
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp);
      guides([], []);
      if (moved) { undo.push(before); redo = []; scheduleSave(); render(); }
    };
    window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', onUp);
  });

  // ------------------------------------------------------------------ operations
  function align(mode: string) {
    const s = selected().filter(e => !e.locked); if (!s.length) return;
    const ref = s.length === 1 ? { x: 0, y: 0, x2: 1, y2: 1 } :
      { x: Math.min(...s.map(e => e.x)), y: Math.min(...s.map(e => e.y)), x2: Math.max(...s.map(e => e.x + e.w)), y2: Math.max(...s.map(e => e.y + e.h)) };
    change(() => s.forEach(e => {
      if (mode === 'l') e.x = ref.x; if (mode === 'r') e.x = r4(ref.x2 - e.w); if (mode === 'c') e.x = r4((ref.x + ref.x2) / 2 - e.w / 2);
      if (mode === 't') e.y = ref.y; if (mode === 'b') e.y = r4(ref.y2 - e.h); if (mode === 'm') e.y = r4((ref.y + ref.y2) / 2 - e.h / 2);
    }));
  }
  function distribute(axis: 'x' | 'y') {
    const s = selected().filter(e => !e.locked); if (s.length < 3) return toast('Select 3 or more elements to distribute', 'warning');
    const k = axis, sz = axis === 'x' ? 'w' : 'h';
    s.sort((a, b) => a[k] - b[k]);
    const total = s.reduce((a, e) => a + e[sz], 0);
    const span = (s[s.length - 1][k] + s[s.length - 1][sz]) - s[0][k];
    const gap = (span - total) / (s.length - 1);
    change(() => { let p = s[0][k]; for (const e of s) { e[k] = r4(p); p += e[sz] + gap; } });
  }
  function order(dir: 'front' | 'back' | 'up' | 'down') {
    const s = selected(); if (!s.length) return;
    change(() => {
      const sorted = [...els].sort((a, b) => a.z - b.z);
      const ids = new Set(s.map(e => e.id));
      let arr = sorted;
      if (dir === 'front') arr = [...sorted.filter(e => !ids.has(e.id)), ...sorted.filter(e => ids.has(e.id))];
      if (dir === 'back') arr = [...sorted.filter(e => ids.has(e.id)), ...sorted.filter(e => !ids.has(e.id))];
      if (dir === 'up') for (let i = arr.length - 2; i >= 0; i--) if (ids.has(arr[i].id) && !ids.has(arr[i + 1].id)) [arr[i], arr[i + 1]] = [arr[i + 1], arr[i]];
      if (dir === 'down') for (let i = 1; i < arr.length; i++) if (ids.has(arr[i].id) && !ids.has(arr[i - 1].id)) [arr[i], arr[i - 1]] = [arr[i - 1], arr[i]];
      arr.forEach((e, i) => { e.z = i + 1; });
    });
  }
  function duplicate() {
    const s = selected(); if (!s.length) return;
    const gmap = new Map<string, string>();
    const copies = s.map(e => ({ ...JSON.parse(JSON.stringify(e)), id: uid(), x: r4(clamp(e.x + .02, -.5, 1)), y: r4(clamp(e.y + .02, -.5, 1)),
      z: e.z + 100, locked: false, group: e.group ? (gmap.get(e.group) || (gmap.set(e.group, uid()), gmap.get(e.group)!)) : null, name: e.name + ' copy' }));
    change(() => { els.push(...copies); sel = new Set(copies.map(c => c.id)); });
  }
  function remove() {
    const s = selected().filter(e => !e.locked); if (!s.length) return;
    change(() => { const ids = new Set(s.map(e => e.id)); els = els.filter(e => !ids.has(e.id)); sel = new Set(); });
  }
  function group(on: boolean) {
    const s = selected(); if (on && s.length < 2) return toast('Select 2 or more elements to group', 'warning');
    const g = on ? uid() : null;
    change(() => s.forEach(e => { e.group = g; }));
  }
  function setAspect(ar: number) {
    const s = selected().filter(e => !e.locked);
    change(() => s.forEach(e => { e.h = r4(clamp(e.w * (W / H) / ar, .01, 2)); }));
  }

  // ------------------------------------------------------------------ toolbar / inspector
  function drawToolbar() {
    const t = (label: string, title: string, fn: () => void, on = false, dis = false) => h('button.btn.sm', { title, onclick: fn, class: on ? 'on' : '', disabled: dis }, label);
    const none = !sel.size;
    clear(toolbar,
      t('↶', 'Undo (Ctrl+Z)', doUndo, false, !undo.length), t('↷', 'Redo (Ctrl+Y)', doRedo, false, !redo.length), h('span.sep'),
      t('Grid', 'Show grid', () => { grid = !grid; render(); }, grid), t('Snap', 'Snap to grid, edges and centres', () => { snap = !snap; render(); }, snap), h('span.sep'),
      t('⇤', 'Align left', () => align('l'), false, none), t('↔', 'Align centre', () => align('c'), false, none), t('⇥', 'Align right', () => align('r'), false, none),
      t('⤒', 'Align top', () => align('t'), false, none), t('↕', 'Align middle', () => align('m'), false, none), t('⤓', 'Align bottom', () => align('b'), false, none),
      t('⋯H', 'Distribute horizontally', () => distribute('x'), false, sel.size < 3), t('⋮V', 'Distribute vertically', () => distribute('y'), false, sel.size < 3), h('span.sep'),
      t('Front', 'Bring to front (Ctrl+])', () => order('front'), false, none), t('Back', 'Send to back (Ctrl+[)', () => order('back'), false, none),
      t('Group', 'Group (Ctrl+G)', () => group(true), false, sel.size < 2), t('Ungroup', 'Ungroup (Ctrl+Shift+G)', () => group(false), false, none),
      t('Duplicate', 'Duplicate (Ctrl+D)', duplicate, false, none), t('Delete', 'Delete (Del)', remove, false, none));
  }

  function num(label: string, v: number, set: (x: number) => void, step = 1) {
    return h('label.f', null, label, h('input', { type: 'number', value: String(Math.round(v * 100) / 100), step: String(step), onchange: (e: any) => change(() => set(Number(e.target.value))) }));
  }

  function drawInsp() {
    const l = cur();
    if (!l) return clear(insp, h('div.sec', null, h('p.dim.small', null, 'No layout selected.')));
    const s = selected();
    const tplSuggest = cams().filter(c => c.status === 'live').length || cams().length;
    const autoBoxes = autoGrid(cams().length);
    const layoutSec = h('div.sec', null, h('b', null, 'Layout'),
      h('div.row', null, h('span.grow', { style: { fontWeight: '600' } }, l.name), l.live ? h('span.pill.bad.plain', null, 'LIVE') : null),
      h('div.row.wrap', null,
        h('button.btn.sm.green', { onclick: () => act(() => api('POST', `/api/layouts/${l.id}/preview`), 'In preview') }, 'Preview'),
        h('button.btn.sm.red', { onclick: async () => { if (await confirmBox('Take layout live', `Put "${l.name}" on programme now?`, 'Take live')) act(() => api('POST', `/api/layouts/${l.id}/live`, { transition: 'Fade' })); } }, 'Take live'),
        h('button.btn.sm', { onclick: async () => { const nm = await promptBox('Rename layout', 'Name', l.name); if (nm) act(() => api('PUT', `/api/layouts/${l.id}`, { name: nm })); } }, 'Rename'),
        h('button.btn.sm', { onclick: async () => { const d = await act(() => api('POST', `/api/layouts/${l.id}/duplicate`), 'Duplicated'); if (d) load(d.id); } }, 'Duplicate'),
        h('button.btn.sm', { onclick: () => download(`/api/layouts/${l.id}/export`) }, 'Export'),
        h('button.btn.sm', { onclick: checkObs }, 'Check OBS'),
        h('button.btn.sm.ghost.danger', { onclick: async () => { if (await confirmBox('Delete layout', `Delete "${l.name}" and its OBS scene?`, 'Delete')) { const r = await act(() => api('DELETE', `/api/layouts/${l.id}`), 'Layout deleted'); if (r) load(null); } } }, 'Delete')),
      h('label.c.small', null, h('input', { type: 'checkbox', checked: holdLive, onchange: (e: any) => { holdLive = e.target.checked; } }), 'When this layout is live, hold edits until "Push to air"'));
    const tplSec = h('div.sec', null, h('b', null, 'Templates'), h('div.tpl', null, TEMPLATES.map(t => h('button', { title: t.id === 'auto' ? `${t.label}: ${cams().length} camera(s)` : t.label,
      class: t.n === tplSuggest || (t.id === 'auto' && tplSuggest > 6) ? 'sug' : '', onclick: () => applyTemplate(t) },
      (t.id === 'auto' ? autoBoxes : t.boxes).map(b => h('i', { style: { left: b[0] * 100 + '%', top: b[1] * 100 + '%', width: b[2] * 100 + '%', height: b[3] * 100 + '%' } }))))),
      h('span.tiny.faint', null, 'Green = suggested for your camera count. Templates replace camera tiles and keep graphics.'));
    if (!s.length) return clear(insp, layoutSec, tplSec, h('div.sec', null, h('b', null, 'Shortcuts'), h('div.tiny.dim', null,
      'Drag to move · handles resize (Shift keeps ratio) · Shift-click multi-select · Ctrl+Z/Y undo/redo · Ctrl+D duplicate · Del delete · arrows nudge (Shift ×10) · Ctrl+G group · Ctrl+A all · Esc deselect')));
    if (s.length > 1) return clear(insp, layoutSec, h('div.sec', null, h('b', null, `${s.length} selected`),
      h('div.row.wrap', null, h('button.btn.sm', { onclick: () => change(() => s.forEach(e => { e.locked = !s.every(x => x.locked); })) }, s.every(x => x.locked) ? 'Unlock' : 'Lock'),
        h('button.btn.sm', { onclick: () => change(() => s.forEach(e => { e.hidden = !s.every(x => x.hidden); })) }, s.every(x => x.hidden) ? 'Show' : 'Hide'))), tplSec);
    const e = s[0];
    const P = (v: number, axis: 'x' | 'y') => v * (axis === 'x' ? W : H);
    const sec: any[] = [layoutSec, h('div.sec', null, h('b', null, e.type),
      h('label.f', null, 'Name', h('input', { type: 'text', value: e.name, onchange: (ev: any) => change(() => { e.name = ev.target.value; }) })),
      h('div.row.wrap', null,
        h('button.btn.sm', { class: e.locked ? 'on' : '', onclick: () => change(() => { e.locked = !e.locked; }) }, e.locked ? 'Locked' : 'Lock'),
        h('button.btn.sm', { class: e.hidden ? 'on' : '', onclick: () => change(() => { e.hidden = !e.hidden; }) }, e.hidden ? 'Hidden' : 'Hide'),
        h('button.btn.sm', { onclick: () => order('up') }, 'Forward'), h('button.btn.sm', { onclick: () => order('down') }, 'Backward')),
      h('div.two', null, num('X px', P(e.x, 'x'), v => { e.x = r4(v / W); }), num('Y px', P(e.y, 'y'), v => { e.y = r4(v / H); }),
        num('Width px', P(e.w, 'x'), v => { e.w = r4(clamp(v / W, .005, 3)); }), num('Height px', P(e.h, 'y'), v => { e.h = r4(clamp(v / H, .005, 3)); })),
      h('div.row.wrap', null, h('span.tiny.faint', null, 'Aspect'), ...[['16:9', 16 / 9], ['4:3', 4 / 3], ['1:1', 1], ['9:16', 9 / 16]].map(([lab, ar]) => h('button.btn.sm', { onclick: () => setAspect(ar as number) }, lab as string))),
      h('label.f', null, `Opacity ${Math.round(e.opacity * 100)}%`, h('input', { type: 'range', min: '0', max: '1', step: '0.05', value: String(e.opacity), onchange: (ev: any) => change(() => { e.opacity = Number(ev.target.value); }) })))];
    if (e.type === 'camera' || e.type === 'image') {
      sec.push(h('div.sec', null, h('b', null, 'Source'),
        e.type === 'camera' ? h('label.f', null, 'Camera', h('select', { onchange: (ev: any) => change(() => { e.camera_id = ev.target.value; const c = cams().find(x => x.id === ev.target.value); if (c) e.name = c.name; }) },
          h('option', { value: '' }, '— none —'), cams().map(c => h('option', { value: c.id, selected: c.id === e.camera_id }, c.name)))) : null,
        h('label.f', null, 'Fit', h('select', { onchange: (ev: any) => change(() => { e.fit = ev.target.value; }) },
          h('option', { value: 'fit', selected: e.fit === 'fit' }, 'Fit inside (keep ratio)'), h('option', { value: 'stretch', selected: e.fit === 'stretch' }, 'Stretch to box'))),
        h('b.tiny.faint', null, 'Crop (% of source)'),
        h('div.four', null, (['l', 't', 'r', 'b'] as const).map(k => h('label.f', null, { l: 'Left', t: 'Top', r: 'Right', b: 'Bottom' }[k],
          h('input', { type: 'number', min: '0', max: '45', step: '1', value: String(Math.round(e.crop[k] * 100)), onchange: (ev: any) => change(() => { e.crop[k] = clamp(Number(ev.target.value) / 100, 0, .45); }) }))))));
    }
    if (e.type === 'text') {
      sec.push(h('div.sec', null, h('b', null, 'Text'),
        h('textarea', { rows: 2, value: e.text, onchange: (ev: any) => change(() => { e.text = ev.target.value; }) }),
        h('div.two', null, h('label.f', null, 'Font', h('input', { type: 'text', value: e.font, onchange: (ev: any) => change(() => { e.font = ev.target.value; }) })),
          num('Size (px @1080)', e.size || 64, v => { e.size = clamp(Math.round(v), 8, 400); })),
        h('div.two', null, h('label.f', null, 'Colour', h('input', { type: 'color', value: e.color, onchange: (ev: any) => change(() => { e.color = ev.target.value; }) })),
          h('label.c.small', { style: { marginTop: '18px' } }, h('input', { type: 'checkbox', checked: !!e.bold, onchange: (ev: any) => change(() => { e.bold = ev.target.checked; }) }), 'Bold'))));
    }
    if (e.type === 'color') sec.push(h('div.sec', null, h('b', null, 'Fill'), h('input', { type: 'color', value: e.color, onchange: (ev: any) => change(() => { e.color = ev.target.value; }) })));
    clear(insp, ...sec);
  }

  async function checkObs() {
    if (!curId) return;
    const items = await act(() => api('GET', `/api/layouts/${curId}/obs`)); if (!items) return;
    modal('What OBS actually has', h('table.t', null, h('tr', null, h('th', null, '#'), h('th', null, 'Source'), h('th', null, 'Visible'), h('th', null, 'Position'), h('th', null, 'Bounds')),
      items.map((it: any, i: number) => h('tr', null, h('td', null, i), h('td.mono.small', null, it.sourceName), h('td', null, it.sceneItemEnabled ? 'yes' : 'no'),
        h('td.mono.small', null, `${Math.round(it.sceneItemTransform?.positionX ?? 0)}, ${Math.round(it.sceneItemTransform?.positionY ?? 0)}`),
        h('td.mono.small', null, `${Math.round(it.sceneItemTransform?.boundsWidth ?? 0)}×${Math.round(it.sceneItemTransform?.boundsHeight ?? 0)} ${it.sceneItemTransform?.boundsType || ''}`)))),
    [{ label: 'Re-apply layout to OBS', cls: 'primary', run: pushToAir }, { label: 'Close' }], true);
  }

  function drawStatus(state?: string) {
    const l = cur();
    if (!l) return clear(status);
    const sync = l.sync || {};
    clear(status,
      state === 'saving' || saving ? h('span.pill.pending', null, 'saving…') : h('span.pill', { class: sync.state }, sync.state === 'synced' ? 'Synced to OBS' : sync.state === 'pending' ? 'Waiting for OBS' : sync.state || 'not synced'),
      sync.error ? h('span.small', { style: { color: 'var(--warn)' } }, sync.error) : null,
      h('span.right'),
      l.live && pendingPush ? h('button.btn.sm.red', { onclick: pushToAir }, 'Push changes to air') : null,
      l.live ? h('span.small', { style: { color: '#ff8b8f' } }, holdLive ? 'LIVE: edits are held until you push' : 'LIVE: edits go to air immediately') : h('span.small.faint', null, l.obs_scene));
  }

  function render() { drawList(); drawToolbar(); drawStage(); drawInsp(); drawStatus(); }

  // ------------------------------------------------------------------ keyboard
  const onKey = (ev: KeyboardEvent) => {
    const t = ev.target as HTMLElement;
    if (/INPUT|SELECT|TEXTAREA/.test(t.tagName) || document.querySelector('.modal-bg') || !root.isConnected || !curId) return;
    const k = ev.key.toLowerCase(), c = ev.ctrlKey || ev.metaKey;
    if (c && k === 'z' && !ev.shiftKey) { ev.preventDefault(); doUndo(); }
    else if (c && (k === 'y' || (k === 'z' && ev.shiftKey))) { ev.preventDefault(); doRedo(); }
    else if (c && k === 'd') { ev.preventDefault(); duplicate(); }
    else if (c && k === 'a') { ev.preventDefault(); sel = new Set(els.map(e => e.id)); render(); }
    else if (c && k === 'g') { ev.preventDefault(); group(!ev.shiftKey); }
    else if (c && k === 'c') { clipboard = JSON.parse(JSON.stringify(selected())); }
    else if (c && k === 'v' && clipboard.length) { ev.preventDefault(); const cp = clipboard.map(e => ({ ...e, id: uid(), x: r4(e.x + .02), y: r4(e.y + .02), group: null })); change(() => { els.push(...cp); sel = new Set(cp.map(x => x.id)); }); }
    else if (c && k === ']') { ev.preventDefault(); order('front'); }
    else if (c && k === '[') { ev.preventDefault(); order('back'); }
    else if (k === 'delete' || k === 'backspace') { if (sel.size) { ev.preventDefault(); remove(); } }
    else if (k === 'escape') { sel = new Set(); render(); }
    else if (k.startsWith('arrow') && sel.size) {
      ev.preventDefault();
      const step = (ev.shiftKey ? 10 : 1);
      const dx = k === 'arrowleft' ? -step / W : k === 'arrowright' ? step / W : 0, dy = k === 'arrowup' ? -step / H : k === 'arrowdown' ? step / H : 0;
      change(() => selected().filter(e => !e.locked).forEach(e => { e.x = r4(e.x + dx); e.y = r4(e.y + dy); }));
    }
  };
  document.addEventListener('keydown', onKey);
  disposers.push(() => document.removeEventListener('keydown', onKey));

  // server updates: refresh list + sync badge only (never clobber local edits)
  disposers.push(store.on('layouts', () => {
    if (curId && !cur()) { load(null); return; }
    drawList(); drawStatus();
    const l = cur();
    if (l && !undo.length && !saveT && JSON.stringify(l.elements) !== JSON.stringify(els)) { els = JSON.parse(JSON.stringify(l.elements)); drawStage(); drawInsp(); }
  }));
  disposers.push(store.on('obs', () => { drawStatus(); }));

  root.append(h('div.col', null, listBox, h('div.panel', { style: { padding: '10px' } }, palette())), stageWrap, insp);
  const first = layouts()[0];
  load(first ? first.id : null);
  return root;
}
