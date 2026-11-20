// File menu (top left): New / Open / Save / Save as / Close for .festcast production files.
// Where the browser allows it (Chrome/Edge on this PC) Save writes back to the file that was opened or saved last,
// like a desktop app; elsewhere Save downloads the file and Open uses the file chooser.
import { act, api, ApiError, h, modal, pickFile, store, toast } from '../lib';

const TYPES = [{ description: 'FestCast production', accept: { 'application/x-festcast': ['.festcast'] } }];
const fsa = () => typeof (window as any).showSaveFilePicker === 'function';
let handle: any = null;                 // FileSystemFileHandle of the current production file, if any

const prod = () => (store.get('cameras') || {}).production;
const fileName = (r: Response) => {
  const m = /filename\*=UTF-8''([^;]+)/.exec(r.headers.get('content-disposition') || '');
  return m ? decodeURIComponent(m[1]) : 'FestCast.festcast';
};

async function fetchFile(): Promise<{ name: string; blob: Blob }> {
  const r = await fetch('/api/production/file', { credentials: 'same-origin' });
  if (!r.ok) throw new ApiError(r.status, r.statusText);
  return { name: fileName(r), blob: await r.blob() };
}

function setCurrent(h: any, name: string | null) {
  handle = h;
  store.set('_file', name);
}

export async function save(as = false): Promise<boolean> {
  try {
    const f = await fetchFile();
    if (fsa()) {
      let target = as ? null : handle;
      if (!target) {
        try { target = await (window as any).showSaveFilePicker({ suggestedName: f.name, types: TYPES }); }
        catch (e: any) { if (e?.name === 'AbortError') return false; throw e; }
      }
      const w = await target.createWritable();
      await w.write(f.blob);
      await w.close();
      setCurrent(target, target.name);
      toast(`Saved ${target.name}`, 'ok');
    } else {
      const a = h('a', { href: URL.createObjectURL(f.blob), download: f.name });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      store.set('_file', f.name);
      toast(`Saved ${f.name} to your Downloads folder`, 'ok');
    }
    return true;
  } catch (e: any) {
    toast(`Could not save: ${e.message || e}`, 'error');
    return false;
  }
}

/** Ask to save the current workspace before it is replaced. Resolves false when the user cancels. */
function saveFirst(title: string, text: string, go: string): Promise<boolean> {
  return new Promise(res => {
    let done = false;
    const finish = (v: boolean) => { done = true; res(v); };
    modal(title, h('p', null, text), [
      { label: 'Cancel', run: () => finish(false) },
      { label: 'Don’t save', run: () => finish(true) },
      { label: `Save, then ${go}`, cls: 'primary', run: async () => { const ok = await save(); if (ok) finish(true); return ok; } },
    ]);
    const obs = new MutationObserver(() => { if (!document.querySelector('.modal-bg')) { obs.disconnect(); if (!done) res(false); } });
    obs.observe(document.body, { childList: true });
  });
}

export async function openFile() {
  let file: File | undefined, picked: any = null;
  if (typeof (window as any).showOpenFilePicker === 'function') {
    try { [picked] = await (window as any).showOpenFilePicker({ types: TYPES }); file = await picked.getFile(); }
    catch (e: any) { if (e?.name !== 'AbortError') toast(String(e.message || e), 'error'); return; }
  } else {
    [file] = await pickFile('.festcast');
  }
  if (!file) return;
  const empty = !prod() && !(store.get('layouts') || []).length && !((store.get('presentations') || {}).items || []).length;
  if (!empty && !await saveFirst('Open production', `Opening “${file.name}” replaces the cameras, layouts and presentations here. ` +
    'Camera links change, so phones need their new links. Save the current production first?', 'open')) return;
  const fd = new FormData();
  fd.append('file', file);
  toast(`Opening ${file.name}…`, 'info', 2500);
  const r = await act(() => api('POST', '/api/production/file', fd), `Opened ${file.name}: send the camera operators their new links`);
  if (r) { setCurrent(picked, file.name); location.hash = '#/cameras'; }
}

export async function newProduction() {
  if (prod() && !await saveFirst('New production', `“${prod().name}” will be closed: its cameras, camera links and ` +
    'their OBS sources are removed. Save it first?', 'continue')) return;
  const name = h('input', { type: 'text', value: 'School Festival', style: { width: '100%' } });
  const count = h('input', { type: 'number', value: '3', min: '0', max: '16', style: { width: '100%' } });
  const keep = h('input', { type: 'checkbox', checked: true });
  modal('New production', h('div.col', null,
    h('label.f', null, 'Event name', name), h('label.f', null, 'Camera slots', count),
    h('label.c', null, keep, 'Keep the current layouts and presentations')), [
    { label: 'Cancel' },
    { label: 'Create', cls: 'primary', run: async () => {
      const ok = !!(await act(() => api('POST', '/api/production/new', { name: name.value, cameras: Number(count.value), keep: keep.checked }),
        'Production created: camera links are ready'));
      if (ok) { setCurrent(null, null); location.hash = '#/cameras'; }
      return ok;
    } },
  ]);
}

async function closeProduction() {
  if (!prod()) return toast('No production is open', 'info');
  if (!await saveFirst('Close production', 'Removes all cameras, their links and their OBS sources. Layouts, ' +
    'presentations and recordings are kept. Save the production first?', 'close')) return;
  if (await act(() => api('DELETE', '/api/production'), 'Production closed')) setCurrent(null, null);
}

type Item = [label: string, keys: string, run: () => any, enabled?: () => boolean] | null;
const ITEMS: Item[] = [
  ['New production…', '', newProduction],
  ['Open…', 'Ctrl+O', openFile],
  null,
  ['Save', 'Ctrl+S', () => save()],
  ['Save as…', 'Ctrl+Shift+S', () => save(true)],
  null,
  ['Close production', '', closeProduction, () => !!prod()],
];

export function fileMenu() {
  const pop = h('div.menu-pop', { role: 'menu' });
  const btn = h('button.menu-btn', { 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, 'File');
  const wrap = h('div.menu', null, btn, pop);
  const close = () => { wrap.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); };
  const open = () => {
    pop.replaceChildren(...ITEMS.map(it => it ? h('button.mi', {
      role: 'menuitem', disabled: it[3] && !it[3]() ? true : undefined,
      onclick: () => { close(); it[2](); },
    }, h('span', null, it[0]), h('span.k', null, it[1])) : h('div.msep')));
    const cur = store.get('_file');
    if (cur) pop.append(h('div.msep'), h('div.mnote', null, 'Current file: ', h('b', null, cur)));
    wrap.classList.add('open'); btn.setAttribute('aria-expanded', 'true');
    (pop.querySelector('button.mi:not(:disabled)') as HTMLElement | null)?.focus();
  };
  btn.onclick = () => wrap.classList.contains('open') ? close() : open();
  document.addEventListener('mousedown', e => { if (!wrap.contains(e.target as Node)) close(); });
  wrap.addEventListener('keydown', e => {
    if (e.key === 'Escape') { close(); btn.focus(); }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const items = Array.from(pop.querySelectorAll('button.mi:not(:disabled)')) as HTMLElement[];
      const i = items.indexOf(document.activeElement as HTMLElement);
      items[(i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
    }
  });
  document.addEventListener('keydown', e => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || document.querySelector('.modal-bg')) return;
    const k = e.key.toLowerCase();
    if (k === 's') { e.preventDefault(); save(e.shiftKey); }
    else if (k === 'o' && !e.shiftKey) { e.preventDefault(); openFile(); }
  });
  return wrap;
}
