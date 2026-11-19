import { api, clear, clock, disposers, h, kbps, live, n, pill, spark, store } from '../lib';

export function networkPage(el: HTMLElement) {
  const ifBox = h('div');
  const histBox = h('div.notes', { style: { maxHeight: '420px' } });
  let hist: any[] = [];
  const load = async () => {
    const d = await api('GET', '/api/network').catch(() => null);
    if (!d) return;
    clear(ifBox, h('table.t', null, h('tr', null, h('th', null, 'Interface'), h('th', null, 'IPv4'), h('th', null, 'Link'), h('th', null, 'Speed')),
      d.interfaces.map((i: any) => h('tr', null, h('td', null, i.name, i.primary ? h('span.pill.ok.plain', { style: { marginLeft: '6px' } }, 'used for links') : null),
        h('td.mono', null, i.ipv4.join(', ')), h('td', null, i.up ? 'up' : 'down'), h('td.num', null, i.speed_mbps ? `${i.speed_mbps} Mb/s` : '—')))));
    clear(histBox, d.history.length ? d.history.map((x: any) => h('div.note', { class: x.status === 'disconnected' || x.status === 'failed' ? 'warning' : '' },
      h('span.t', null, clock(x.ts)), h('span.m', null, `${x.kind === 'camera' ? 'Camera' : 'Receiver'} ${x.name}: ${x.status}`))) : h('div.note', null, h('span.m.dim', null, 'No connection events yet.')));
  };
  load();
  const t = setInterval(load, 5000);
  disposers.push(() => clearInterval(t));
  api('GET', '/api/metrics/history?seconds=600').then(x => { hist = x; store.set('_nh', 1); }).catch(() => {});
  disposers.push(store.on('metrics', m => { if (m) { hist.push({ cam_in: m.cameras.in_kbps, out: m.outputs.out_kbps, rx: m.system.net_rx_kbps, tx: m.system.net_tx_kbps }); hist = hist.slice(-300); } }));

  el.append(
    h('div.page-h', null, h('h1', null, 'Network'), h('span.sub', null, 'Local network health for cameras and receivers')),
    live(h('div.grid.g4'), ['metrics', '_nh'], () => {
      const m = store.get('metrics') || {};
      const s = m.system || {};
      const box = (l: string, v: string, k: string) => h('div.panel.stat', null, h('span.l', null, l), h('span.v', null, v), spark(hist.map(x => x[k] ?? null)));
      return [box('Cameras in (WebRTC)', kbps(m.cameras?.in_kbps), 'cam_in'), box('TV out (WebRTC)', kbps(m.outputs?.out_kbps), 'out'),
        box('NIC receive', kbps(s.net_rx_kbps), 'rx'), box('NIC send', kbps(s.net_tx_kbps), 'tx')];
    }),
    h('div.grid.g2', { style: { marginTop: '14px' } },
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Camera links')),
        live(h('div'), ['cameras'], () => {
          const cs = (store.get('cameras') || {}).cameras || [];
          return cs.length ? h('table.t', null, h('tr', null, h('th', null, 'Camera'), h('th', null, 'State'), h('th', null, 'Bitrate'), h('th', null, 'Jitter'), h('th', null, 'Loss'), h('th', null, 'RTT')),
            cs.map((c: any) => h('tr', null, h('td', null, c.name), h('td', null, pill(c.status)), h('td.num', null, kbps(c.bitrate_in_kbps)), h('td.num', null, n(c.jitter_ms, 0, ' ms')),
              h('td.num', null, n(c.packet_loss_pct, 1, '%')), h('td.num', null, n(c.rtt_ms, 0, ' ms'))))) : h('div.pb.dim', null, 'No cameras.');
        })),
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Receiver links')),
        live(h('div'), ['outputs'], () => {
          const rs = ((store.get('outputs') || {}).receivers || []).filter((r: any) => r.kind === 'webrtc');
          return rs.length ? h('table.t', null, h('tr', null, h('th', null, 'Receiver'), h('th', null, 'State'), h('th', null, 'Bitrate'), h('th', null, 'Jitter'), h('th', null, 'Lost'), h('th', null, 'RTT')),
            rs.map((r: any) => h('tr', null, h('td', null, r.name), h('td', null, pill(r.state)), h('td.num', null, kbps(r.bitrate_kbps)), h('td.num', null, n(r.jitter_ms, 0, ' ms')),
              h('td.num', null, r.packets_lost ?? '—'), h('td.num', null, n(r.rtt_ms, 0, ' ms'))))) : h('div.pb.dim', null, 'No wireless receivers.');
        }))),
    h('div.grid.g2', { style: { marginTop: '14px' } },
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Interfaces')), h('div.pb', null, ifBox,
        live(h('p.small.dim'), ['server'], () => `Phones and TVs use ${(store.get('server') || {}).public_base || ''}. If that is the wrong adapter (e.g. a VirtualBox/VPN address), set FESTCAST_PUBLIC_HOST in .env.`),
        h('p.small.dim', null, 'Camera transport: VDO.Ninja signalling needs internet access; media flows peer-to-peer on the LAN when possible. The TV output (MediaMTX) is fully local.'))),
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Connection history')), histBox)),
  );
}
