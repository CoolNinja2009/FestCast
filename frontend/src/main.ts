import './styles/main.css';
import { act, api, clear, connectWs, disposeAll, disposers, dur, h, live, loadState, store, toast } from './lib';
import { overviewPage } from './pages/overview';
import { camerasPage } from './pages/cameras';
import { productionPage } from './pages/production';
import { presentationsPage } from './pages/presentations';
import { outputsPage } from './pages/outputs';
import { recordingsPage } from './pages/recordings';
import { networkPage } from './pages/network';
import { settingsPage } from './pages/settings';

const PAGES: [string, string, (el: HTMLElement) => void][] = [
  ['overview', 'Overview', overviewPage],
  ['cameras', 'Cameras', camerasPage],
  ['production', 'Production', productionPage],
  ['presentations', 'Presentations', presentationsPage],
  ['outputs', 'Outputs', outputsPage],
  ['recordings', 'Recordings', recordingsPage],
  ['network', 'Network', networkPage],
  ['settings', 'Settings', settingsPage],
];

const app = document.getElementById('app')!;
let main: HTMLElement;

function shell() {
  const nav = h('nav.side', null,
    PAGES.map(([id, label], i) => h('a', { href: `#/${id}`, 'data-id': id }, label, h('span.k', null, `${i + 1}`))),
    h('div.foot', null, live(h('div'), ['server', 'link'], () => {
      const s = store.get('server') || {};
      return [h('div', null, store.get('link') === 'up' ? 'Dashboard connected' : 'Dashboard reconnecting…'),
              h('div.mono', null, s.public_base || '')];
    })));
  const top = h('header.top', null,
    h('div.brand', null, h('i'), 'FESTCAST'),
    live(h('div.row.grow'), ['cameras', 'obs', 'recording', 'production', 'server', 'link'], topBar));
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
    h('span.prod', null, prod ? prod.name : 'No production'),
    prod ? h('span.tally', { class: p.started ? 'ok' : '' }, p.started ? 'STARTED' : 'NOT STARTED') : null,
    server.simulation ? h('span.tally.sim', { title: 'OBS is simulated: nothing here is a real camera, programme or recording' }, 'SIMULATION') : null,
    store.get('link') === 'down' ? h('span.tally.bad', null, 'DASHBOARD OFFLINE') : null,
    h('span.right'),
    h('span.tally', { class: obs.connected ? 'ok' : 'bad', title: obs.error || '' }, obs.connected ? `OBS ${obs.version?.obs || ''}` : 'OBS DOWN'),
    obs.program_scene ? h('span.tally.pgm', { title: 'Programme' }, 'PGM ', obs.program_scene) : null,
    rec.active ? h('span.tally.rec', null, rec.paused ? 'REC PAUSED ' : 'REC ', dur(rec.duration_ms)) : h('span.tally', null, 'NOT RECORDING'),
    h('button.btn.standby', {
      class: standby ? 'active' : '', title: 'Emergency standby: puts the standby scene on programme now',
      onclick: () => standby ? (location.hash = '#/production') : act(() => api('POST', '/api/standby/on'), 'Standby on air'),
    }, standby ? 'STANDBY ON AIR' : 'STANDBY'),
  ];
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
  clear(app, h('div.login.panel', { style: { gridColumn: '1/-1' } },
    h('div.ph', null, h('h2', null, 'FestCast operator login')),
    h('div.pb.col', null,
      remoteEnabled ? [pw, h('button.btn.primary', { onclick: go }, 'Sign in')]
        : h('p.dim', null, 'Remote control is disabled. Open FestCast on the production PC (http://localhost:8420), or set FESTCAST_ADMIN_PASSWORD there to allow control from this device.'))));
  app.style.display = 'block';
}

async function boot() {
  try {
    const me = await api('GET', '/api/auth/me');
    if (!me.admin) return loginView(me.remote_enabled);
    await loadState();
  } catch (e: any) {
    clear(app, h('div.empty', { style: { gridColumn: '1/-1' } }, h('h2', null, 'Cannot reach the FestCast backend'), h('p', null, String(e.message || e)),
      h('button.btn', { onclick: () => location.reload() }, 'Retry')));
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
