let currentPage = 1;
let searchLoaded = false;   // Search tab grid populated at least once
let gemmaActive = false;
let smartMode = true;
let selectMode = false;
let smartExpand = '';
const selectedUuids = new Set();
let _map = null;
let _coloc = {};
let _mapClusters = null;
let _mapStat = null;
const BAD_DESC = ['parse error','file not found','image encode failed'];

// Escape DB/model text before innerHTML interpolation — Gemma output
// occasionally contains angle brackets and quotes.
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function init() {
  const years = await fetch('/api/years').then(r => r.json());
  const yearSel = document.getElementById('yearFilter');
  years.forEach(y => {
    const opt = document.createElement('option');
    opt.value = y; opt.textContent = y;
    yearSel.appendChild(opt);
  });
  const people = await fetch('/api/people').then(r => r.json());
  const personSel = document.getElementById('personFilter');
  people.slice(0, 50).forEach(p => {
    const opt = document.createElement('option');
    opt.value = p; opt.textContent = p;
    personSel.appendChild(opt);
  });
  loadVoiceResults({});
}

function resetFilters() {
  document.getElementById('searchInput').value = '';
  document.getElementById('smartStatus').textContent = '';
  document.getElementById('yearFilter').value = '';
  document.getElementById('personFilter').value = '';
  document.getElementById('sceneFilter').value = '';
  gemmaActive = false;
  smartExpand = '';
  document.getElementById('gemmaBtn').classList.remove('active');
  if (!smartMode) toggleSmart();
  doSearch();
}

function toggleSmart() {
  smartMode = !smartMode;
  document.getElementById('smartToggle').classList.toggle('active', smartMode);
  document.getElementById('searchIcon').innerHTML = smartMode ? '&#10024;' : '&#9906;';
  document.getElementById('searchInput').placeholder = smartMode
    ? 'Smart Search - describe what you are looking for...'
    : 'Search descriptions, locations, people, tags...';
  document.getElementById('smartStatus').textContent = '';
}

function runSearch() {
  if (smartMode) smartSearch();
  else { smartExpand = ''; doSearch(); }
}

async function smartSearch() {
  const text = document.getElementById('searchInput').value.trim();
  if (!text) { doSearch(); return; }
  const btn    = document.getElementById('searchBtn');
  const status = document.getElementById('smartStatus');
  btn.disabled = true;
  status.textContent = 'Thinking...';
  let parsed = {};
  try {
    parsed = await fetch('/api/smart_parse?q=' + encodeURIComponent(text))
                   .then(r => r.json());
  } catch (e) {
    parsed = {};
  }
  btn.disabled = false;

  if (parsed && parsed.error) {
    status.textContent = 'Smart Search unavailable - ran a plain search';
    document.getElementById('searchInput').value = text;
    doSearch();
    return;
  }

  const has = parsed && (parsed.q || parsed.year || parsed.person || parsed.scene);
  if (!has) {
    status.textContent = 'No filters parsed - ran a plain search';
    document.getElementById('searchInput').value = text;
    doSearch();
    return;
  }

  document.getElementById('searchInput').value  = parsed.q || '';
  document.getElementById('yearFilter').value   = parsed.year || '';
  document.getElementById('personFilter').value = parsed.person || '';
  document.getElementById('sceneFilter').value  = parsed.scene || '';
  gemmaActive = false;
  document.getElementById('gemmaBtn').classList.remove('active');

  smartExpand = (parsed.terms || []).filter(t => t).join(',');

  const bits = [];
  if (parsed.q)      bits.push('text: ' + parsed.q);
  if (parsed.year)   bits.push('year: ' + parsed.year);
  if (parsed.person) bits.push('person: ' + parsed.person);
  if (parsed.scene)  bits.push('scene: ' + parsed.scene);
  if (smartExpand) {
    const preview = (parsed.terms || []).slice(0, 3).join(', ');
    bits.push('also: ' + preview + (parsed.terms && parsed.terms.length > 3 ? '…' : ''));
  }
  status.textContent = 'Searching  ' + bits.join('  |  ');
  doSearch();
}

function toggleGemma() {
  gemmaActive = !gemmaActive;
  document.getElementById('gemmaBtn').classList.toggle('active', gemmaActive);
  doSearch();
}

function doSearch(page = 1) {
  searchLoaded = true;
  currentPage = page;
  const q = {
    q:         document.getElementById('searchInput').value.trim(),
    year:      document.getElementById('yearFilter').value,
    person:    document.getElementById('personFilter').value,
    scene:     document.getElementById('sceneFilter').value,
    has_gemma: gemmaActive ? '1' : '',
    expand:    smartExpand,
    page:      page,
  };
  loadResults(q);
  if (_map) loadMapPoints();
}

async function loadResults(q) {
  const grid = document.getElementById('photoGrid');
  grid.innerHTML = '<div class="loading">Searching</div>';
  const params = new URLSearchParams(Object.fromEntries(
    Object.entries(q).filter(([,v]) => v !== '')
  ));
  const data = await fetch('/api/search?' + params).then(r => r.json());

  document.getElementById('resultsInfo').style.display = 'flex';
  document.getElementById('resultCount').textContent = data.total.toLocaleString();
  document.getElementById('pageInfo').textContent =
    data.pages > 1 ? `Page ${data.page} of ${data.pages}` : '';

  if (data.results.length === 0) {
    grid.innerHTML = '<div class="empty">No photos found</div>';
    document.getElementById('pagination').innerHTML = '';
    return;
  }

  grid.innerHTML = data.results.map(p => {
    const sel = selectedUuids.has(p.uuid);
    return `
    <div class="card${selectMode ? ' selectable' : ''}${sel ? ' selected' : ''}"
         onclick="handleCardClick('${p.uuid}')" data-uuid="${p.uuid}" style="position:relative">
      <div class="select-check"></div>
      ${p.has_file
        ? `<img class="card-thumb" src="/thumb/${p.uuid}" loading="lazy" alt="">`
        : `<div class="card-thumb-placeholder">&#9888;</div>`}
      ${p.media_type === 'video' ? '<div class="card-play">&#9654;</div>' : ''}
      <div class="card-body">
        <div class="card-date">${esc(p.date)}</div>
        <div class="card-desc">${esc(p.description || p.scene)}</div>
        <div class="card-badges">
          ${p.scene ? `<span class="badge scene">${esc(p.scene)}</span>` : ''}
          ${p.location ? `<span class="badge loc">&#128205; ${esc(p.location.split('(')[0].trim())}</span>` : ''}
          ${(p.people||[]).slice(0,2).map(n => `<span class="badge person">${esc(n.split(' ')[0])}</span>`).join('')}
        </div>
      </div>
    </div>`;
  }).join('');

  const pag = document.getElementById('pagination');
  if (data.pages <= 1) { pag.innerHTML = ''; return; }
  let h = '';
  if (data.page > 1) h += `<button onclick="doSearch(${data.page-1})">&larr; Prev</button>`;
  const start = Math.max(1, data.page - 3);
  const end   = Math.min(data.pages, data.page + 3);
  if (start > 1) h += `<button onclick="doSearch(1)">1</button><span style="color:var(--text3);padding:8px">...</span>`;
  for (let i = start; i <= end; i++)
    h += `<button class="${i===data.page?'active':''}" onclick="doSearch(${i})">${i}</button>`;
  if (end < data.pages) h += `<span style="color:var(--text3);padding:8px">...</span><button onclick="doSearch(${data.pages})">${data.pages}</button>`;
  if (data.page < data.pages) h += `<button onclick="doSearch(${data.page+1})">Next &rarr;</button>`;
  pag.innerHTML = h;
}

// Result list backing the open modal, for arrow-key navigation.
let modalList = [];
let modalIdx  = -1;

function visibleGridCards() {
  // The grid the user is actually looking at: map side panel beats the
  // tab views when it is open.
  if (document.getElementById('mapPanel').classList.contains('open'))
    return document.querySelectorAll('#mapPanelGrid [data-uuid]');
  if (document.getElementById('voiceView').style.display !== 'none')
    return document.querySelectorAll('#voiceGrid [data-uuid]');
  return document.querySelectorAll('#photoGrid [data-uuid]');
}

async function openModal(uuid) {
  modalList = Array.from(visibleGridCards()).map(el => el.dataset.uuid);
  modalIdx  = modalList.indexOf(uuid);
  await renderModal(uuid);
}

function modalStep(delta) {
  if (modalIdx < 0 || !modalList.length) return;
  const next = modalIdx + delta;
  if (next < 0 || next >= modalList.length) return;
  modalIdx = next;
  renderModal(modalList[modalIdx]);
}

function stopModalVideo() {
  const vid = document.getElementById('modalVid');
  vid.pause();
  vid.removeAttribute('src');
  vid.load();
  vid.style.display = 'none';
  document.getElementById('modalImg').style.display = '';
}

async function renderModal(uuid) {
  const modal = document.getElementById('modal');
  const img   = document.getElementById('modalImg');
  const info  = document.getElementById('modalInfo');
  stopModalVideo();
  img.src = `/thumb/${uuid}?size=800`;
  info.innerHTML = '<div class="loading">Loading</div>';
  modal.classList.add('open');

  const p = await fetch(`/api/photo/${uuid}`).then(r => r.json());
  if (p.media_type === 'video' && p.has_file) {
    const vid = document.getElementById('modalVid');
    img.style.display = 'none';
    vid.src = `/original/${uuid}`;
    vid.style.display = '';
  }
  const people = Array.isArray(p.persons) ? p.persons : [];
  const tags   = Array.isArray(p.gemma_tags) ? p.gemma_tags : [];
  const scenes = Array.isArray(p.scene_labels) ? p.scene_labels : [];
  const descOk = p.gemma_description &&
                 !BAD_DESC.includes(String(p.gemma_description).toLowerCase());

  info.innerHTML = `
    <div class="modal-filename">${esc(p.uuid)}.${esc((p.file_ext||'').toLowerCase())}</div>
    <div class="modal-date">${esc(p.date)}${p.time ? ' ' + esc(p.time.slice(0,5)) : ''}</div>

    ${descOk ? `
    <div class="meta-section">
      <div class="meta-label">AI Description</div>
      <div class="modal-desc">${esc(p.gemma_description)}</div>
    </div>` : ''}

    ${p.place_display ? `
    <div class="meta-section">
      <div class="meta-label">Location</div>
      <div class="meta-value">&#128205; ${esc(p.place_display)}</div>
    </div>` : (p.gemma_location_guess ? `
    <div class="meta-section">
      <div class="meta-label">Location Guess</div>
      <div class="meta-value">&#128205; ${esc(p.gemma_location_guess)}</div>
    </div>` : '')}

    ${people.length ? `
    <div class="meta-section">
      <div class="meta-label">People</div>
      <div class="tag-list">${people.map(n=>`<span class="person-tag">${esc(n)}</span>`).join('')}</div>
    </div>` : ''}

    ${tags.length ? `
    <div class="meta-section">
      <div class="meta-label">Tags</div>
      <div class="tag-list">${tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>
    </div>` : ''}

    ${scenes.length ? `
    <div class="meta-section">
      <div class="meta-label">Apple Scene Labels</div>
      <div class="tag-list">${scenes.map(t=>`<span class="tag">${esc(typeof t==='string'?t:(t.label||''))}</span>`).join('')}</div>
    </div>` : ''}

    ${(p.gps_lat!=null) ? `
    <div class="meta-section">
      <div class="meta-label">GPS</div>
      <div class="meta-value">${Number(p.gps_lat).toFixed(4)}, ${Number(p.gps_lon).toFixed(4)}</div>
    </div>` : ''}

    ${p.has_file ? `
    <div class="meta-section">
      <a class="map-popup-open" href="/original/${p.uuid}" target="_blank">Open original &rarr;</a>
    </div>` : `
    <div class="meta-section">
      <div class="meta-value" style="color:var(--red)">&#9888; No local file found for this photo</div>
    </div>`}
  `;
}

function closeModal(e) {
  if (e.target === document.getElementById('modal')) {
    document.getElementById('modal').classList.remove('open');
    stopModalVideo();
  }
}
function closeModalBtn() {
  document.getElementById('modal').classList.remove('open');
  stopModalVideo();
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.getElementById('modal').classList.remove('open');
    stopModalVideo();
  }
  if (document.getElementById('modal').classList.contains('open')) {
    if (e.key === 'ArrowLeft')  modalStep(-1);
    if (e.key === 'ArrowRight') modalStep(1);
  }
});

function showView(view) {
  document.getElementById('voiceView').style.display   = view==='voice'  ? 'block' : 'none';
  document.getElementById('searchView').style.display  = view==='search' ? 'block' : 'none';
  document.getElementById('statsView').style.display   = view==='stats'  ? 'block' : 'none';
  document.getElementById('mapView').style.display     = view==='map'    ? 'block' : 'none';
  document.getElementById('voiceStatusBar').style.display =
    (view==='voice' && document.getElementById('micStatus').textContent) ? 'block' : 'none';
  document.getElementById('searchBar').style.display   = view==='search' ? 'block' : 'none';
  document.getElementById('smartStatusBar').style.display = view==='search' ? 'block' : 'none';
  document.querySelectorAll('nav button').forEach((b,i) =>
    b.classList.toggle('active',
      (i===0&&view==='voice')||(i===1&&view==='search')||(i===2&&view==='stats')||(i===3&&view==='map')));
  if (view === 'stats') loadStats();
  if (view === 'map')   initMap();
  // First visit to the Search tab: show the full library (newest first)
  // instead of an empty grid.
  if (view === 'search' && !searchLoaded) doSearch();
}

function currentFilters() {
  return {
    q:         document.getElementById('searchInput').value.trim(),
    year:      document.getElementById('yearFilter').value,
    person:    document.getElementById('personFilter').value,
    scene:     document.getElementById('sceneFilter').value,
    has_gemma: gemmaActive ? '1' : '',
    expand:    smartExpand,
  };
}

async function initMap() {
  if (_map) { await loadMapPoints(); return; }
  _map = L.map('map', { center: [20, 0], zoom: 2, preferCanvas: true });
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '(c) OpenStreetMap (c) CARTO',
    subdomains: 'abcd',
    maxZoom: 19
  }).addTo(_map);

  _mapStat = document.createElement('div');
  _mapStat.className = 'map-stat';
  _mapStat.innerHTML = 'Loading...';
  _mapStat.style.cssText = 'position:absolute;bottom:32px;left:12px;z-index:1000';
  document.getElementById('mapView').appendChild(_mapStat);

  await loadMapPoints();
}

async function loadMapPoints() {
  if (!_map) return;
  if (_mapStat) _mapStat.innerHTML = 'Loading...';
  closeMapPanel();
  if (_mapClusters) { _map.removeLayer(_mapClusters); _mapClusters = null; }

  const params = new URLSearchParams(Object.fromEntries(
    Object.entries(currentFilters()).filter(([,v]) => v !== '')
  ));
  const data = await fetch('/api/map_points?' + params).then(r => r.json());

  const icon = L.divIcon({
    className: '',
    html: `<div style="width:10px;height:10px;background:#c8a96e;border-radius:50%;
      border:2px solid rgba(200,169,110,0.4);box-shadow:0 0 6px rgba(200,169,110,0.6)"></div>`,
    iconSize: [10, 10],
    iconAnchor: [5, 5],
  });

  const clusters = L.markerClusterGroup({
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    zoomToBoundsOnClick: false,
    iconCreateFunction: function(cluster) {
      const count = cluster.getChildCount();
      const sz = count>100?40:count>20?34:28;
      return L.divIcon({
        html: `<div style="background:rgba(200,169,110,0.85);color:#0f0f0f;
          border-radius:50%;width:${sz}px;height:${sz}px;display:flex;
          align-items:center;justify-content:center;font-family:'DM Mono',monospace;
          font-size:11px;font-weight:500;border:2px solid rgba(200,169,110,0.3);">${count}</div>`,
        className: '',
        iconSize: [40, 40],
        iconAnchor: [20, 20],
      });
    }
  });

  _coloc = {};
  data.points.forEach(p => {
    const key = p.lat.toFixed(5) + ',' + p.lon.toFixed(5);
    (_coloc[key] = _coloc[key] || []).push(p);
  });

  data.points.forEach(p => {
    const marker = L.marker([p.lat, p.lon], {icon, photo: p});
    marker.on('click', () => {
      const key = p.lat.toFixed(5) + ',' + p.lon.toFixed(5);
      const here = _coloc[key] || [p];
      renderMapPanel(here, here.length > 1 ? 'Photos at this location' : 'Photo');
    });
    clusters.addLayer(marker);
  });

  clusters.on('clusterclick', e => {
    const photos = e.layer.getAllChildMarkers().map(m => m.options.photo);
    renderMapPanel(photos, 'Photos in this group');
  });

  _map.addLayer(clusters);
  _mapClusters = clusters;
  if (data.points.length === 0)
    _mapStat.innerHTML = `<span>0</span> photos match the current filters`;
  else
    _mapStat.innerHTML = `<span>${data.points.length.toLocaleString()}</span> photos with GPS`;
}

const MAP_PANEL_CAP = 200;

function renderMapPanel(photos, title) {
  const panel = document.getElementById('mapPanel');
  const grid  = document.getElementById('mapPanelGrid');
  document.getElementById('mapPanelTitle').textContent =
    title + ' (' + photos.length.toLocaleString() + ')';
  const shown = photos.slice(0, MAP_PANEL_CAP);
  let h = shown.map(p => `
    <div class="map-panel-tile" data-uuid="${p.uuid}" onclick="openModal('${p.uuid}')">
      <img src="/thumb/${p.uuid}?size=200" loading="lazy" alt="">
      <div class="map-panel-tile-date">${esc(p.date)}</div>
    </div>`).join('');
  if (photos.length > MAP_PANEL_CAP)
    h += `<div class="map-panel-more">+${(photos.length - MAP_PANEL_CAP).toLocaleString()} more not shown</div>`;
  grid.innerHTML = h;
  grid.scrollTop = 0;
  panel.classList.add('open');
}

function closeMapPanel() {
  document.getElementById('mapPanel').classList.remove('open');
}

async function loadStats() {
  const s = await fetch('/api/stats').then(r => r.json());
  const fmt = n => (n!=null ? Number(n).toLocaleString() : '0');

  const maxYear = Math.max(...s.by_year.map(y=>y.count), 1);
  const yearBars = s.by_year.map(y => {
    const h = Math.round((y.count/maxYear)*100);
    return `<div class="year-bar" style="height:${h}%;cursor:pointer" title="${y.year}: ${fmt(y.count)} photos" onclick="statsJump('year','${y.year}')"></div>`;
  }).join('');
  const firstYear = s.by_year[0]?.year || '';
  const lastYear  = s.by_year[s.by_year.length-1]?.year || '';

  const maxScene = Math.max(...s.top_scenes.map(x=>x.count), 1);
  const sceneBars = s.top_scenes.map(sc =>
    `<div class="bar-row" style="cursor:pointer" data-val="${esc(sc.label)}" onclick="statsJump('scene',this.dataset.val)">
      <div class="bar-label">${esc(sc.label)}</div>
      <div class="bar-track"><div class="bar-fill green" style="width:${Math.round(sc.count/maxScene*100)}%"></div></div>
      <div class="bar-count">${fmt(sc.count)}</div>
    </div>`).join('');

  const maxPerson = s.top_people[0]?.count || 1;
  const peopleBars = s.top_people.map(p =>
    `<div class="bar-row" style="cursor:pointer" data-val="${esc(p.name)}" onclick="statsJump('person',this.dataset.val)">
      <div class="bar-label">${esc(p.name)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round(p.count/maxPerson*100)}%"></div></div>
      <div class="bar-count">${fmt(p.count)}</div>
    </div>`).join('');

  document.getElementById('statsContent').innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-number">${fmt(s.total)}</div><div class="stat-label">Total Photos</div></div>
      <div class="stat-card"><div class="stat-number">${fmt(s.with_gemma)}</div><div class="stat-label">AI Described</div></div>
      <div class="stat-card"><div class="stat-number">${fmt(s.with_gps)}</div><div class="stat-label">With GPS</div></div>
      <div class="stat-card"><div class="stat-number">${fmt(s.with_people)}</div><div class="stat-label">With Named People</div></div>
      <div class="stat-card"><div class="stat-number">${s.year_min||''}-${s.year_max||''}</div><div class="stat-label">Year Range</div></div>
    </div>
    <div class="bar-chart">
      <div class="section-title">Photos by Year</div>
      <div class="year-timeline">${yearBars}</div>
      <div class="year-labels"><span>${firstYear}</span><span>${lastYear}</span></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:32px;flex-wrap:wrap">
      <div class="bar-chart"><div class="section-title">Top Scenes</div>${sceneBars}</div>
      <div class="bar-chart"><div class="section-title">Most Photographed People</div>${peopleBars}</div>
    </div>
  `;
}

function statsJump(kind, value) {
  // Stats bar -> Search tab with that filter applied.
  const sel = { year: 'yearFilter', person: 'personFilter', scene: 'sceneFilter' }[kind];
  document.getElementById('yearFilter').value   = '';
  document.getElementById('personFilter').value = '';
  document.getElementById('sceneFilter').value  = '';
  const el = document.getElementById(sel);
  el.value = value;
  // personFilter only lists the top 50 — a missing option leaves value ''.
  if (el.value !== value) {
    const opt = document.createElement('option');
    opt.value = value; opt.textContent = value;
    el.appendChild(opt);
    el.value = value;
  }
  showView('search');
  doSearch();
}

function toggleSelectMode() {
  selectMode = !selectMode;
  const btn = document.getElementById('selectBtn');
  btn.classList.toggle('active', selectMode);
  if (!selectMode) {
    selectedUuids.clear();
    updateDeleteBar();
  }
  // re-render current grid cards with/without selectable class
  document.querySelectorAll('#photoGrid .card').forEach(card => {
    const uuid = card.dataset.uuid;
    card.classList.toggle('selectable', selectMode);
    if (!selectMode) card.classList.remove('selected');
    const check = card.querySelector('.select-check');
    if (check) check.style.display = selectMode ? 'flex' : 'none';
  });
}

function handleCardClick(uuid) {
  if (selectMode) {
    toggleCardSelect(uuid);
  } else {
    openModal(uuid);
  }
}

function toggleCardSelect(uuid) {
  if (selectedUuids.has(uuid)) {
    selectedUuids.delete(uuid);
  } else {
    selectedUuids.add(uuid);
  }
  const card = document.querySelector(`#photoGrid .card[data-uuid="${uuid}"]`);
  if (card) card.classList.toggle('selected', selectedUuids.has(uuid));
  updateDeleteBar();
}

function updateDeleteBar() {
  const bar = document.getElementById('deleteBar');
  const n   = selectedUuids.size;
  document.getElementById('deleteCount').textContent = n;
  bar.classList.toggle('visible', selectMode && n > 0);
}

function getDeleteToken(forceAsk = false) {
  // Shared secret checked by /api/delete. Asked for once, kept in
  // localStorage — embedding it in the page would expose it to anyone
  // who can load the app.
  let token = forceAsk ? '' : (localStorage.getItem('deleteToken') || '');
  if (!token) {
    token = (window.prompt('Enter the delete token (photo-intel.conf [web] delete_token):') || '').trim();
    if (token) localStorage.setItem('deleteToken', token);
  }
  return token;
}

async function deleteSelected() {
  const uuids = Array.from(selectedUuids);
  if (!uuids.length) return;
  const confirmed = window.confirm(
    `Permanently delete ${uuids.length} photo${uuids.length === 1 ? '' : 's'} from the server?\n\nThis removes the file and the database record. Your Apple Photos library is not affected.`
  );
  if (!confirmed) return;

  const token = getDeleteToken();
  if (!token) return;

  const btn = document.getElementById('deleteConfirmBtn');
  btn.disabled = true;
  btn.textContent = 'Deleting...';

  try {
    let resp = await fetch('/api/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Delete-Token': token},
      body: JSON.stringify({uuids}),
    });
    if (resp.status === 403) {
      // stored token stale or wrong — re-ask once
      localStorage.removeItem('deleteToken');
      const retryToken = getDeleteToken(true);
      if (retryToken) {
        resp = await fetch('/api/delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-Delete-Token': retryToken},
          body: JSON.stringify({uuids}),
        });
      }
    }
    if (resp.status === 403) {
      const err = await resp.json();
      alert('Delete refused: ' + (err.error || 'invalid token'));
      btn.disabled = false;
      btn.textContent = 'Delete';
      return;
    }
    const result = await resp.json();
    if (result.errors && result.errors.length) {
      console.warn('Delete errors:', result.errors);
    }
    // remove deleted cards from grid
    uuids.forEach(uuid => {
      const card = document.querySelector(`#photoGrid .card[data-uuid="${uuid}"]`);
      if (card) card.remove();
      selectedUuids.delete(uuid);
    });
    // update count in results-info
    const countEl = document.getElementById('resultCount');
    if (countEl) {
      const cur = parseInt(countEl.textContent.replace(/,/g, ''), 10) || 0;
      countEl.textContent = Math.max(0, cur - result.deleted).toLocaleString();
    }
  } catch (e) {
    alert('Delete failed: ' + e);
  }

  btn.disabled = false;
  btn.textContent = 'Delete';
  updateDeleteBar();
  if (selectedUuids.size === 0) {
    selectMode = false;
    document.getElementById('selectBtn').classList.remove('active');
    document.getElementById('deleteBar').classList.remove('visible');
  }
}

// ─── voice tab ────────────────────────────────────────────────────────────
let micListening = false;
let voiceSR = null;
let lastVoiceQuery = {};

// The transcript/status strip under the header renders only while it
// has something to say.
function setMicStatus(text) {
  document.getElementById('micStatus').textContent = text;
  document.getElementById('voiceStatusBar').style.display = text ? 'block' : 'none';
}

// The Voice nav button IS the mic: clicking it switches to the voice
// view (if needed) and toggles listening in the same gesture.
function voiceTabClick() {
  if (document.getElementById('voiceView').style.display === 'none')
    showView('voice');
  toggleMic();
}

function toggleMic() {
  if (!('SpeechRecognition' in window) && !('webkitSpeechRecognition' in window)) {
    setMicStatus('Voice not supported — try Chrome or Safari (HTTPS may be required in Chrome)');
    return;
  }
  if (micListening) {
    if (voiceSR) voiceSR.stop();
  } else {
    startMic();
  }
}

function startMic() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  voiceSR = new SR();
  voiceSR.lang = 'en-US';
  voiceSR.interimResults = true;
  voiceSR.maxAlternatives = 1;

  voiceSR.onstart = () => {
    micListening = true;
    document.getElementById('micBtn').classList.add('listening');
    setMicStatus('Listening…');
  };

  voiceSR.onresult = (e) => {
    const transcript = Array.from(e.results).map(r => r[0].transcript).join(' ');
    setMicStatus(transcript);
  };

  voiceSR.onend = async () => {
    micListening = false;
    const btn = document.getElementById('micBtn');
    btn.classList.remove('listening');
    const text = document.getElementById('micStatus').textContent;
    if (!text || text === 'Listening…') {
      setMicStatus('');
      return;
    }
    btn.classList.add('processing');
    setMicStatus('“' + text + '” — thinking…');

    let q = {};
    try {
      const parsed = await fetch('/api/smart_parse?q=' + encodeURIComponent(text)).then(r => r.json());
      const expand  = (parsed.terms || []).filter(t => t).join(',');
      const persons = (parsed.persons || []).filter(p => p);
      q = { q: parsed.q||'', year: parsed.year||'',
            year_from: parsed.year_from||'', year_to: parsed.year_to||'',
            person: persons.join(','), scene: parsed.scene||'', expand };
      const parts = [];
      if (parsed.q)         parts.push('“' + parsed.q + '”');
      if (parsed.year)      parts.push('year: ' + parsed.year);
      if (parsed.year_from) parts.push('years: ' + parsed.year_from + '–' + parsed.year_to);
      if (persons.length)   parts.push((persons.length > 1 ? 'people: ' : 'person: ') + persons.join(', '));
      if (parsed.scene)     parts.push('scene: ' + parsed.scene);
      if (expand)           parts.push('also: ' + expand);
      setMicStatus(parts.length ? parts.join('  │  ') : text);
    } catch(_) {
      q = { q: text };
      setMicStatus('“' + text + '”');
    }

    btn.classList.remove('processing');
    lastVoiceQuery = q;
    loadVoiceResults(q, 1, true);
  };

  voiceSR.onerror = (e) => {
    micListening = false;
    const btn = document.getElementById('micBtn');
    btn.classList.remove('listening');
    btn.classList.remove('processing');
    setMicStatus(e.error === 'no-speech' ? 'No speech detected — try again' : 'Error: ' + e.error);
  };

  voiceSR.start();
}

function speakSummary(total, q) {
  if (!('speechSynthesis' in window)) return;
  const bits = [];
  if (q.q)         bits.push(q.q);
  if (q.person)    bits.push('with ' + q.person.split(',').join(' and '));
  if (q.year)      bits.push('from ' + q.year);
  if (q.year_from) bits.push('from ' + q.year_from + ' to ' + q.year_to);
  const what = bits.length ? ' of ' + bits.join(', ') : '';
  const msg = total === 0
    ? 'No photos found' + what
    : 'Found ' + total.toLocaleString() + ' photo' + (total === 1 ? '' : 's') + what;
  speechSynthesis.cancel();
  speechSynthesis.speak(new SpeechSynthesisUtterance(msg));
}

function loadVoiceResults(q, page = 1, speak = false) {
  lastVoiceQuery = q;
  const params = new URLSearchParams(
    Object.fromEntries(Object.entries(Object.assign({}, q, {page})).filter(([,v]) => v !== '' && v !== undefined))
  );
  const grid = document.getElementById('voiceGrid');
  grid.innerHTML = '<div class="loading">Loading</div>';
  fetch('/api/search?' + params).then(r => r.json()).then(data => {
    if (speak) speakSummary(data.total, q);
    document.getElementById('voiceResultsInfo').style.display = 'flex';
    document.getElementById('voiceResultCount').textContent = data.total.toLocaleString();
    document.getElementById('voicePageInfo').textContent =
      data.pages > 1 ? 'Page ' + data.page + ' of ' + data.pages : '';
    if (data.results.length === 0) {
      grid.innerHTML = '<div class="empty">No photos found</div>';
      document.getElementById('voicePagination').innerHTML = '';
      return;
    }
    grid.innerHTML = data.results.map(p => `
      <div class="card" onclick="openModal('${p.uuid}')" data-uuid="${p.uuid}">
        ${p.has_file
          ? `<img class="card-thumb" src="/thumb/${p.uuid}" loading="lazy" alt="">`
          : `<div class="card-thumb-placeholder">&#9888;</div>`}
        ${p.media_type === 'video' ? '<div class="card-play">&#9654;</div>' : ''}
        <div class="card-body">
          <div class="card-date">${esc(p.date)}</div>
          <div class="card-desc">${esc(p.description || p.scene)}</div>
          <div class="card-badges">
            ${p.scene ? `<span class="badge scene">${esc(p.scene)}</span>` : ''}
            ${p.location ? `<span class="badge loc">&#128205; ${esc(p.location.split('(')[0].trim())}</span>` : ''}
            ${(p.people||[]).slice(0,2).map(n => `<span class="badge person">${esc(n.split(' ')[0])}</span>`).join('')}
          </div>
        </div>
      </div>`).join('');
    const pag = document.getElementById('voicePagination');
    if (data.pages <= 1) { pag.innerHTML = ''; return; }
    let h = '';
    if (data.page > 1) h += `<button onclick="loadVoiceResults(lastVoiceQuery,${data.page-1})">&larr; Prev</button>`;
    const s = Math.max(1, data.page - 3), e = Math.min(data.pages, data.page + 3);
    if (s > 1) h += `<button onclick="loadVoiceResults(lastVoiceQuery,1)">1</button><span style="color:var(--text3);padding:8px">…</span>`;
    for (let i = s; i <= e; i++)
      h += `<button class="${i===data.page?'active':''}" onclick="loadVoiceResults(lastVoiceQuery,${i})">${i}</button>`;
    if (e < data.pages) h += `<span style="color:var(--text3);padding:8px">…</span><button onclick="loadVoiceResults(lastVoiceQuery,${data.pages})">${data.pages}</button>`;
    if (data.page < data.pages) h += `<button onclick="loadVoiceResults(lastVoiceQuery,${data.page+1})">Next &rarr;</button>`;
    pag.innerHTML = h;
  });
}

init();
