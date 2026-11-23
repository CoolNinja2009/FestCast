import './styles/main.css';
import { act, api, clear, connectWs, disposeAll, disposers, dur, h, live, loadState, store, toast } from './lib';
import { overviewPage } from './pages/overview';
import { camerasPage } from './pages/cameras';
import { productionPage } from './pages/production';
import { audioPage } from './pages/audio';
import { presentationsPage } from './pages/presentations';
import { outputsPage } from './pages/outputs';
import { recordingsPage } from './pages/recordings';
import { networkPage } from './pages/network';
import { settingsPage } from './pages/settings';
import { fileMenu } from './components/filemenu';

const PAGES: [string, string, (el: HTMLElement) => void][] = [
  ['overview', 'Overview', overviewPage],
  ['cameras', 'Cameras', camerasPage],
  ['production', 'Production', productionPage],
  ['audio', 'Audio', audioPage],
  ['presentations', 'Presentations', presentationsPage],
  ['outputs', 'Outputs', outputsPage],
  ['recordings', 'Recordings', recordingsPage],
  ['network', 'Network', networkPage],
  ['settings', 'Settings', settingsPage],
];

// Line icons (Lucide-style, 24px grid, stroke = currentColor). Static strings only.
const I = (d: string) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON: Record<string, string> = {
  overview: I('<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
  cameras: I('<path d="m16 13 5.2 3.5a.5.5 0 0 0 .8-.4V7.9a.5.5 0 0 0-.8-.4L16 11"/><rect x="2" y="6" width="14" height="12" rx="2"/>'),
  production: I('<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>'),
  audio: I('<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>'),
  presentations: I('<path d="M2 3h20M21 3v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3M7 21l5-5 5 5"/>'),
  outputs: I('<rect x="2" y="7" width="20" height="15" rx="2"/><path d="m17 2-5 5-5-5"/>'),
  recordings: I('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4" fill="currentColor"/>'),
  network: I('<path d="M12 20h.01M2 8.8a15 15 0 0 1 20 0M5 12.9a10 10 0 0 1 14 0M8.5 16.4a5 5 0 0 1 7 0"/>'),
  settings: I('<path d="M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0l-.2-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.5a2 2 0 0 1-1 1.7l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.3a2 2 0 0 1 1 1.7v.2a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.5a2 2 0 0 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4-.3a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>'),
  manual: I('<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2zM22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>'),
};
const icon = (id: string) => h('span', { html: ICON[id] || '', style: { display: 'contents' } });

const app = document.getElementById('app')!;
let main: HTMLElement;

function shell() {
  const nav = h('nav.side', null,
    PAGES.map(([id, label], i) => h('a', { href: `#/${id}`, 'data-id': id }, icon(id), label, h('span.k', null, `${i + 1}`))),
    h('div.sep'),
    h('a', { href: '/manual', target: '_blank', rel: 'noopener' }, icon('manual'), 'Manual', h('span.k', null, '↗')),
    h('div.foot', null, live(h('div'), ['server', 'link'], () => {
      const s = store.get('server') || {};
      return [h('div', { class: store.get('link') === 'up' ? 'ok' : 'bad' }, store.get('link') === 'up' ? 'Connected' : 'Reconnecting…'),
              h('div.mono', null, s.public_base || '')];
    })));
  const top = h('header.top', null,
    h('div.lead', null, h('div.brand', null, h('i'), 'FestCast'), fileMenu()),
    live(h('div.row.grow'), ['cameras', 'obs', 'recording', 'production', 'server', 'link', '_file', 'live'], topBar));
  main = h('main', { id: 'main' });
  clear(app, top, nav, main);
}

function topBar() {
  const obs = store.get('obs') || {};
  const prod = (store.get('cameras') || {}).production;
  const rec = store.get('recording') || {};
  const p = store.get('production') || {};
  const server = store.get('server') || {};
  const standby = p.standby_active;
  return [
    h('span.prod', { title: store.get('_file') ? `File: ${store.get('_file')}` : '' }, prod ? prod.name : 'No production'),
    prod ? h('span.tally', { class: p.started ? 'ok' : '' }, p.started ? 'STARTED' : 'NOT STARTED') : null,
    server.simulation ? h('span.tally.sim', { title: 'OBS is simulated: nothing here is a real camera, programme or recording' }, 'SIMULATION') : null,
    store.get('link') === 'down' ? h('span.tally.bad', null, 'DASHBOARD OFFLINE') : null,
    h('span.right'),
    h('span.tally', { class: obs.connected ? 'ok' : 'bad', title: obs.error || '' }, obs.connected ? `OBS ${obs.version?.obs || ''}` : 'OBS DOWN'),
    obs.program_scene ? h('span.tally.pgm', { title: 'Programme' }, 'PGM ', obs.program_scene) : null,
    liveTally(),
    rec.active ? h('span.tally.rec', null, rec.paused ? 'REC PAUSED ' : 'REC ', dur(rec.duration_ms)) : h('span.tally', null, 'NOT RECORDING'),
    h('button.btn.standby', {
      class: standby ? 'active' : '', title: 'Emergency standby: puts the standby scene on programme now',
      onclick: () => standby ? (location.hash = '#/production') : act(() => api('POST', '/api/standby/on'), 'Standby on air'),
    }, standby ? 'STANDBY ON AIR' : 'STANDBY'),
  ];
}

function liveTally() {
  const lv = store.get('live') || {};
  if (!lv.on) return null;
  const ds = (lv.destinations || []).filter((d: any) => d.enabled);
  const ok = lv.sending || 0;
  return h('span.tally', { class: ok === ds.length && ok ? 'onair' : 'warn', title: ds.map((d: any) => `${d.name}: ${d.state}`).join('\n'),
    onclick: () => { location.hash = '#/outputs'; }, style: { cursor: 'pointer' } }, ok === ds.length ? `LIVE ${ok}` : `LIVE ${ok}/${ds.length}`);
}

function route() {
  const id = (location.hash.replace(/^#\/?/, '') || 'overview').split('/')[0];
  const page = PAGES.find(p => p[0] === id) || PAGES[0];
  document.querySelectorAll('nav.side a').forEach(a => a.classList.toggle('on', (a as HTMLElement).dataset.id === page[0]));
  disposeAll();
  clear(main);
  main.scrollTop = 0;
  page[2](main);
}

function loginView(remoteEnabled: boolean) {
  const pw = h('input', { type: 'password', placeholder: 'Operator password', style: { width: '100%' } });
  const go = async () => {
    const r = await act(() => api('POST', '/api/auth/login', { password: pw.value }));
    if (r) location.reload();
  };
  pw.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  clear(app, h('div.login-wrap', { style: { gridColumn: '1/-1' } },
    h('div.brand', null, h('i'), 'FestCast'),
    h('div.panel', null, h('div.pb.col', { style: { gap: '12px', padding: '20px' } },
      remoteEnabled ? [pw, h('button.btn.primary', { onclick: go }, 'Sign in')]
        : h('p.dim', null, 'Remote control is off.')))));
  app.style.display = 'block';
  if (remoteEnabled) pw.focus();
}

async function boot() {
  try {
    const me = await api('GET', '/api/auth/me');
    if (!me.admin) return loginView(me.remote_enabled);
    await loadState();
  } catch (e: any) {
    clear(app, h('div.empty', { style: { gridColumn: '1/-1' } }, h('h2', null, 'Can’t reach FestCast'), h('p', null, String(e.message || e)),
      h('button.btn.primary', { onclick: () => location.reload() }, 'Try again')));
    return;
  }
  shell();
  disposers.length = 0; // top bar + footer live for the whole session; only page regions are disposed on navigation
  connectWs();
  window.addEventListener('hashchange', route);
  route();
  store.on('auth', a => { if (a && a.admin === false) location.reload(); });
  document.addEventListener('keydown', e => {
    const t = e.target as HTMLElement;
    if (/INPUT|SELECT|TEXTAREA/.test(t.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (location.hash.startsWith('#/production')) return; // the editor owns the keyboard there
    const i = Number(e.key);
    if (i >= 1 && i <= PAGES.length) location.hash = `#/${PAGES[i - 1][0]}`;
  });
}
boot();
void toast;
