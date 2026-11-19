// Core: DOM helper, API client, live store fed by the /api/ws WebSocket, toasts, modals, formatting.

type Child = Node | string | number | null | undefined | false | Child[];
type Props = Record<string, any> | null;

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, props?: Props, ...children: Child[]): HTMLElementTagNameMap[K];
export function h(tag: string, props?: Props, ...children: Child[]): HTMLElement;
export function h(tag: string, props?: Props, ...children: Child[]): HTMLElement {
  const [name, ...classes] = tag.split('.');
  const el = document.createElement(name || 'div');
  if (classes.length) el.className = classes.join(' ');
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = [el.className, v].filter(Boolean).join(' ');
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k in el && k !== 'list' && typeof v !== 'string') (el as any)[k] = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}
function append(el: Node, children: Child[]) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}
export function clear(el: Element, ...children: Child[]) { el.replaceChildren(); append(el, children); return el; }

// ---------------------------------------------------------------------------------------------- API
export class ApiError extends Error { constructor(public status: number, msg: string) { super(msg); } }

export async function api<T = any>(method: string, path: string, body?: any): Promise<T> {
  const opts: RequestInit = { method, headers: {} as Record<string, string>, credentials: 'same-origin' };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { (opts.headers as any)['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (r.status === 401) { store.set('auth', { admin: false }); }
  if (!r.ok) throw new ApiError(r.status, (data && data.detail) || r.statusText);
  return data as T;
}

/** Run an action, toast the error (never silently swallow). Returns undefined on failure. */
export async function act<T>(fn: () => Promise<T>, ok?: string): Promise<T | undefined> {
  try { const r = await fn(); if (ok) toast(ok, 'ok'); return r; }
  catch (e: any) { toast(e.message || String(e), 'error'); return undefined; }
}

// --------------------------------------------------------------------------------------------- store
type Listener = (v: any) => void;
class Store {
  data: Record<string, any> = {};
  subs: Record<string, Set<Listener>> = {};
  get(k: string) { return this.data[k]; }
  set(k: string, v: any) { this.data[k] = v; this.subs[k]?.forEach(f => { try { f(v); } catch (e) { console.error(e); } }); this.subs['*']?.forEach(f => f(k)); }
  on(k: string, f: Listener) { (this.subs[k] ||= new Set()).add(f); return () => this.subs[k].delete(f); }
}
export const store = new Store();

let ws: WebSocket | null = null;
let wsRetry = 1000;
export async function loadState() {
  const s = await api('GET', '/api/state');
  for (const [k, v] of Object.entries(s)) store.set(k, v);
  store.set('cameras', s.cameras);
}
export function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onopen = () => { wsRetry = 1000; store.set('link', 'up'); loadState().catch(() => {}); };
  ws.onclose = (e) => {
    store.set('link', 'down');
    if (e.code === 4401) { store.set('auth', { admin: false }); return; }
    setTimeout(connectWs, wsRetry); wsRetry = Math.min(wsRetry * 1.6, 8000);
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    switch (m.topic) {
      case 'ping': return;
      case 'obs.status': store.set('obs', m.data); return;
      case 'notification': {
        const n = store.get('notifications') || [];
        store.set('notifications', [m.data, ...n].slice(0, 100));
        if (m.data.level !== 'info' || /saved|confirmed|Connected/.test(m.data.message)) toast(m.data.message, m.data.level);
        return;
      }
      default: store.set(m.topic, m.data);
    }
  };
}

/** Re-render a container whenever any of the topics change; pauses while the user is typing inside it. */
export function live(el: HTMLElement, topics: string[], render: () => Child) {
  let pending = false;
  const draw = () => {
    if (!el.isConnected && pending) return;
    if (el.contains(document.activeElement) && document.activeElement !== document.body &&
        /INPUT|SELECT|TEXTAREA/.test(document.activeElement!.tagName)) { pending = true; return; }
    pending = false;
    clear(el, render());
  };
  const offs = topics.map(t => store.on(t, draw));
  el.addEventListener('focusout', () => { if (pending) setTimeout(draw, 0); });
  draw();
  disposers.push(() => offs.forEach(o => o()));
  return el;
}
export const disposers: (() => void)[] = [];
export function disposeAll() { while (disposers.length) disposers.pop()!(); }

// previews arrive as data URIs; <img data-preview="key"> elements are updated in place (no re-render)
store.on('previews', (p: Record<string, string>) => {
  document.querySelectorAll<HTMLImageElement>('img[data-preview]').forEach(img => {
    const src = p[img.dataset.preview!];
    if (src) { img.src = src; img.classList.remove('hidden'); img.nextElementSibling?.classList.add('hidden'); }
  });
});
export function previewImg(key: string, emptyText = 'No picture') {
  const p = (store.get('previews') || {})[key];
  return [h('img', { 'data-preview': key, src: p || '', class: p ? '' : 'hidden', alt: '' }),
          h('div.none', { class: p ? 'hidden' : '' }, emptyText),
          // these are small OBS screenshots every ~1.5 s (cheap for OBS), so they look choppy and soft by design
          h('span.snap', { title: 'Small snapshot from OBS every ~1.5 s, so it looks choppy and soft. The real feed '
            + '(OBS, TV output, recording) is full quality: see the resolution and FPS figures.' }, 'Snapshot · not live')];
}

// ------------------------------------------------------------------------------------------- UI bits
export function toast(msg: string, level: string = 'info', ms = 5000) {
  let box = document.getElementById('toasts');
  if (!box) { box = h('div', { id: 'toasts' }); document.body.appendChild(box); }
  const t = h('div.toast', { class: level === 'warning' ? 'warning' : level }, msg);
  box.appendChild(t);
  setTimeout(() => t.remove(), level === 'error' ? ms * 2 : ms);
}

export function modal(title: string, body: Child, buttons: { label: string; cls?: string; run?: () => any | Promise<any> }[] = [], wide = false) {
  const bg = h('div.modal-bg');
  const close = () => bg.remove();
  const box = h('div.modal', { class: wide ? 'wide' : '' },
    h('div.mh', null, title, h('button.btn.ghost.sm.right', { onclick: close, 'aria-label': 'Close' }, '✕')),
    h('div.mb', null, body),
    buttons.length ? h('div.mf', null, buttons.map(b => h('button.btn', {
      class: b.cls || '', onclick: async () => { const r = b.run ? await b.run() : undefined; if (r !== false) close(); },
    }, b.label))) : null);
  bg.appendChild(box);
  bg.addEventListener('mousedown', e => { if (e.target === bg) close(); });
  document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); } });
  document.body.appendChild(bg);
  (box.querySelector('input,select,textarea') as HTMLElement | null)?.focus();
  return close;
}

export function confirmBox(title: string, text: string, label = 'Confirm', cls = 'red'): Promise<boolean> {
  return new Promise(res => {
    let done = false;
    const close = modal(title, h('p', null, text), [
      { label: 'Cancel', run: () => { done = true; res(false); } },
      { label, cls, run: () => { done = true; res(true); } }]);
    void close;
    const obs = new MutationObserver(() => { if (!document.querySelector('.modal-bg')) { obs.disconnect(); if (!done) res(false); } });
    obs.observe(document.body, { childList: true });
  });
}

export function promptBox(title: string, label: string, value = ''): Promise<string | null> {
  return new Promise(res => {
    const inp = h('input', { type: 'text', value, style: { width: '100%' } });
    let done = false;
    modal(title, h('label.f', null, label, inp), [
      { label: 'Cancel', run: () => { done = true; res(null); } },
      { label: 'Save', cls: 'primary', run: () => { done = true; res(inp.value.trim()); } }]);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') { done = true; res(inp.value.trim()); document.querySelector('.modal-bg')?.remove(); } });
    const obs = new MutationObserver(() => { if (!document.querySelector('.modal-bg')) { obs.disconnect(); if (!done) res(null); } });
    obs.observe(document.body, { childList: true });
  });
}

export async function copy(text: string, what = 'Link') {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`, 'ok', 2000); }
  catch {
    const ta = h('textarea', { value: text }); document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast(`${what} copied`, 'ok', 2000); } catch { toast('Copy failed - select the text manually', 'warning'); }
    ta.remove();
  }
}

// ---------------------------------------------------------------------------------------- formatting
export const dash = '—';
export function n(v: any, digits = 0, unit = ''): string {
  if (v === null || v === undefined || Number.isNaN(v)) return dash;
  return Number(v).toFixed(digits) + unit;
}
export function bytes(b: any): string {
  if (b === null || b === undefined) return dash;
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let x = Number(b);
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return `${x.toFixed(i >= 3 ? 2 : i ? 1 : 0)} ${u[i]}`;
}
export function kbps(v: any): string {
  if (v === null || v === undefined) return dash;
  return v >= 1000 ? `${(v / 1000).toFixed(1)} Mb/s` : `${Math.round(v)} kb/s`;
}
export function dur(ms: any): string {
  if (ms === null || ms === undefined) return dash;
  const s = Math.floor(ms / 1000), hh = Math.floor(s / 3600), mm = Math.floor(s / 60) % 60, ss = s % 60;
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
}
export function ago(ts: any): string {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return 'now'; if (s < 60) return `${Math.round(s)}s ago`; if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return new Date(ts * 1000).toLocaleTimeString();
}
export function clock(ts: number) { return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }

export const STATUS_LABEL: Record<string, string> = {
  live: 'Live', waiting: 'Waiting for phone', disconnected: 'Disconnected', no_probe: 'Not monitored',
  disabled: 'Disabled', reconnecting: 'Reconnecting',
};

export function pill(state: string | null | undefined, label?: string) {
  const s = state || 'unknown';
  return h('span.pill', { class: s }, label || STATUS_LABEL[s] || s.replace(/_/g, ' '));
}

export function spark(values: (number | null)[], max?: number) {
  const vals = values.map(v => (v === null || v === undefined ? null : v));
  const real = vals.filter((v): v is number => v !== null);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'spark'); svg.setAttribute('viewBox', '0 0 100 30'); svg.setAttribute('preserveAspectRatio', 'none');
  if (real.length < 2) return svg;
  const top = max ?? Math.max(...real, 1) * 1.1;
  const pts = vals.map((v, i) => v === null ? null : [i / (vals.length - 1) * 100, 30 - (v / top) * 28 - 1] as [number, number]);
  let d = ''; let started = false;
  for (const p of pts) { if (!p) { started = false; continue; } d += `${started ? 'L' : 'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`; started = true; }
  const first = pts.find(Boolean)!, last = [...pts].reverse().find(Boolean)!;
  const area = document.createElementNS(svg.namespaceURI, 'path');
  area.setAttribute('class', 'a'); area.setAttribute('d', `${d}L${last[0]},30L${first[0]},30Z`);
  const line = document.createElementNS(svg.namespaceURI, 'path');
  line.setAttribute('class', 'l'); line.setAttribute('d', d);
  svg.append(area, line);
  return svg;
}

export function download(url: string) { const a = h('a', { href: url, download: '' }); document.body.appendChild(a); a.click(); a.remove(); }

export function pickFile(accept: string, multiple = false): Promise<File[]> {
  return new Promise(res => {
    const i = h('input', { type: 'file', accept, multiple });
    i.onchange = () => res(Array.from(i.files || []));
    i.click();
  });
}
