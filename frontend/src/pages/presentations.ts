import { act, api, clear, confirmBox, h, live, modal, store, toast } from '../lib';

let openId: string | null = null;

async function remove(p: any) {
  const text = `Delete “${p.name}” and its ${p.slides.length} slide(s)? This can’t be undone.` +
    (p.active ? ' It is the standby presentation: standby will show the branded card instead.' : '');
  if (!await confirmBox('Delete presentation', text, 'Delete')) return;
  if (await act(() => api('DELETE', `/api/presentations/${p.id}`), `Deleted ${p.name}`) && openId === p.id) {
    openId = null;
    store.set('_pres', Date.now());
  }
}

async function upload(files: File[], name = '') {
  if (!files.length) return;
  const fd = new FormData();
  fd.append('name', name);
  files.forEach(f => fd.append('files', f));
  toast(`Importing ${files.length} file(s)… PDF/PPTX pages are rendered to images`, 'info');
  const p = await act(() => api('POST', '/api/presentations', fd), 'Presentation imported');
  if (p) { openId = p.id; store.set('_pres', Date.now()); }
}

function player(p: any) {
  const slides = p.slides.filter((s: any) => s.enabled !== false);
  if (!slides.length) return toast('No enabled slides', 'warning');
  let i = 0, t: any;
  const img = h('img', { style: { width: '100%', aspectRatio: '16/9', objectFit: 'contain', background: p.background, display: 'block', transition: 'opacity .5s' } });
  const cap = h('div.small.dim');
  const show = () => {
    const s = slides[i % slides.length];
    img.style.opacity = '0';
    setTimeout(() => { img.src = s.url; img.style.opacity = '1'; }, p.transition === 'cut' ? 0 : 250);
    cap.textContent = `Slide ${(i % slides.length) + 1} / ${slides.length} · ${s.duration || p.default_duration}s`;
    t = setTimeout(() => { i++; if (document.body.contains(img)) show(); }, (s.duration || p.default_duration) * 1000);
  };
  modal(`Preview · ${p.name}`, h('div.col', null, img, cap), [{ label: 'Close', run: () => clearTimeout(t) }], true);
  show();
}

function editor(p: any) {
  let slides = p.slides.map((s: any) => ({ file: s.file, enabled: s.enabled !== false, duration: s.duration, url: s.url }));
  const grid = h('div.slides');
  let dragIdx = -1;
  const save = (patch: any) => act(() => api('PATCH', `/api/presentations/${p.id}`, patch));
  const saveSlides = () => save({ slides: slides.map((s: any) => ({ file: s.file, enabled: s.enabled, duration: s.duration })) });
  const draw = () => clear(grid, slides.map((s: any, i: number) => {
    const d = h('div.slide', { class: s.enabled ? '' : 'off', draggable: 'true',
      ondragstart: () => { dragIdx = i; d.classList.add('drag'); },
      ondragend: () => d.classList.remove('drag'),
      ondragover: (e: DragEvent) => { e.preventDefault(); d.classList.add('over'); },
      ondragleave: () => d.classList.remove('over'),
      ondrop: (e: DragEvent) => { e.preventDefault(); if (dragIdx < 0 || dragIdx === i) return; const [m] = slides.splice(dragIdx, 1); slides.splice(i, 0, m); dragIdx = -1; draw(); saveSlides(); } },
      h('img', { src: s.url, loading: 'lazy', alt: '' }),
      h('div.sf', null, h('span.tiny.faint', null, `${i + 1}`),
        h('label.c.tiny', { title: 'Include in the loop' }, h('input', { type: 'checkbox', checked: s.enabled, onchange: (e: any) => { s.enabled = e.target.checked; draw(); saveSlides(); } })),
        h('input', { type: 'number', placeholder: `${p.default_duration}s`, value: s.duration ?? '', min: '1', title: 'Seconds (empty = default)', style: { width: '64px', height: '26px' },
          onchange: (e: any) => { s.duration = e.target.value ? Number(e.target.value) : null; saveSlides(); } }),
        h('button.btn.sm.ghost.right', { title: 'Remove slide', onclick: () => { slides.splice(i, 1); draw(); saveSlides(); } }, '✕')));
    return d;
  }));
  draw();
  return h('div.panel', { style: { marginTop: '14px' } },
    h('div.ph', null, h('h2', null, p.name), p.active ? h('span.pill.ok', null, 'standby source') : null,
      h('span.small.dim', null, `${p.slides.length} slides · from ${p.original_file}`),
      h('div.row.right', null,
        h('button.btn.sm', { onclick: () => player({ ...p, slides }) }, 'Preview'),
        p.active ? h('button.btn.sm', { onclick: () => act(() => api('POST', '/api/presentations/none/activate'), 'Standby uses the branded card') }, 'Deactivate')
          : h('button.btn.sm.green', { onclick: () => act(() => api('POST', `/api/presentations/${p.id}/activate`), 'Set as standby presentation') }, 'Use for standby'),
        h('button.btn.sm.ghost.danger', { onclick: () => remove(p) }, 'Delete'))),
    h('div.pb.col', null,
      h('div.form', null,
        h('label.f', null, 'Name', h('input', { type: 'text', value: p.name, onchange: (e: any) => save({ name: e.target.value }) })),
        h('label.f', null, 'Default duration (s)', h('input', { type: 'number', value: String(p.default_duration), min: '1', onchange: (e: any) => save({ default_duration: Number(e.target.value) }) })),
        h('label.f', null, 'Transition', h('select', { onchange: (e: any) => save({ transition: e.target.value }) }, ['fade', 'slide', 'cut'].map(t => h('option', { selected: t === p.transition }, t)))),
        h('label.f', null, 'Background', h('input', { type: 'color', value: p.background, onchange: (e: any) => save({ background: e.target.value }) })),
        h('label.f', null, 'Branding text (bottom left)', h('input', { type: 'text', value: p.branding, placeholder: 'e.g. #SchoolFest2026', onchange: (e: any) => save({ branding: e.target.value }) }))),
      grid));
}

export function presentationsPage(el: HTMLElement) {
  const drop = h('div.drop', { onclick: () => { const i = h('input', { type: 'file', multiple: true, accept: '.pdf,.pptx,.ppt,.odp,.png,.jpg,.jpeg,.webp' }); i.onchange = () => upload(Array.from(i.files || [])); i.click(); } },
    h('b', null, 'Drop a PDF, PPTX or images here'), h('div.small', null, 'or click to choose'));
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); upload(Array.from(e.dataTransfer?.files || [])); });

  el.append(
    h('div.page-h', null, h('h1', null, 'Presentations')),
    h('div.grid.g2', null,
      h('div.panel', null, h('div.ph', null, h('h2', null, 'Import')), h('div.pb.col', null, drop,
        live(h('div.small.dim'), ['presentations'], () => (store.get('presentations') || {}).pptx_supported ? 'PPTX import: LibreOffice found.' :
          'PPTX import needs LibreOffice. Without it, export the deck as PDF from PowerPoint and import the PDF.'))),
      live(h('div.panel'), ['presentations'], () => {
        const s = store.get('presentations') || {};
        const msg = h('input', { type: 'text', value: s.message || '', style: { width: '100%' } });
        return [h('div.ph', null, h('h2', null, 'Standby card')), h('div.pb.col', null,
          h('p.small.dim', { style: { margin: 0 } }, 'Shown when no presentation is selected: event name, this message and a clock.'),
          h('label.f', null, 'Message', msg),
          h('div.row', null, h('button.btn.sm.primary', { onclick: () => act(() => api('PUT', '/api/standby/message', { message: msg.value }), 'Saved') }, 'Save message'),
            h('a.btn.sm', { href: '#/production' }, 'Standby controls →')))];
      })),
    live(h('div.panel', { style: { marginTop: '14px' } }), ['presentations'], () => {
      const items = (store.get('presentations') || {}).items || [];
      return [h('div.ph', null, h('h2', null, 'Library')),
        items.length ? h('table.t', null, h('tr', null, h('th', null, 'Name'), h('th', null, 'Slides'), h('th', null, 'Transition'), h('th', null, 'Status'), h('th', null, '')),
          items.map((p: any) => h('tr', null, h('td', null, h('b', null, p.name)), h('td.num', null, `${p.slides.filter((s: any) => s.enabled !== false).length}/${p.slides.length}`),
            h('td', null, p.transition), h('td', null, p.active ? h('span.pill.ok', null, 'standby source') : ''),
            h('td', { style: { textAlign: 'right' } }, h('div.row', { style: { justifyContent: 'flex-end' } },
              h('button.btn.sm', { onclick: () => { openId = p.id; store.set('_pres', Date.now()); } }, 'Edit'),
              h('button.btn.sm.ghost.danger', { title: 'Delete this presentation', onclick: () => remove(p) }, 'Delete')))))) :
          h('div.pb.dim', null, 'No presentations yet.')];
    }),
    live(h('div'), ['_pres', 'presentations'], () => {
      const p = ((store.get('presentations') || {}).items || []).find((x: any) => x.id === openId);
      return p ? editor(p) : null;
    }),
  );
}
