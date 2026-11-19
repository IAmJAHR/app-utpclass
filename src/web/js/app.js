/**
 * UTP+ Cloud Frontend Architecture
 * Patterns applied:
 * - Centralized Store (State Management)
 * - Observer / Event-Driven updates via SSE
 * - Modular View Renderers (Single Responsibility per Sidebar View)
 */

const nowDt = new Date();

const Store = {
  token: localStorage.getItem('utp_session_token') || '',
  currentUser: null,
  profiles: [],
  presence: { online_count: 1, users: [], utp_profiles: [] },
  courses: [],
  tasks: [],
  evaluations: [],
  announcements: [],
  selectedCourse: null,
  courseData: null,
  subTab: 'weeks',
  currentView: 'dashboard',
  driveStatus: null,
  calendar: { reminders: [], deadlines: [] },
  calYear: nowDt.getFullYear(),
  calMonth: nowDt.getMonth(),
  calFilterDate: '',
};

const VIEW_META = {
  dashboard: { title: '📊 Resumen General', sub: 'Vista ejecutiva de tus cursos, entregas próximas, agenda y anuncios' },
  courses: { title: '📚 Cursos y Semanas', sub: 'Explora el temario semanal, sílabos y materiales por curso' },
  tasks: { title: '📝 Tareas y Evaluaciones', sub: 'Entregas pendientes, actividades calificadas y notas por curso' },
  announcements: { title: '📢 Anuncios Docentes', sub: 'Comunicados, avisos y enlaces publicados por tus profesores' },
  calendar: { title: '📅 Calendario y Clases', sub: 'Agenda mensual de clases, entregas UTP+ y recordatorios personales' },
  ai: { title: '🤖 Asistente IA Local UTP+', sub: 'Pregúntale qué pasó en cualquier semana, curso, tarea o agenda un recordatorio' },
  drive: { title: '☁️ Biblioteca Google Drive', sub: '779 archivos sincronizados en tu carpeta utp_7mociclo de Google Drive' },
  admin: { title: '🛡️ Perfiles y Control Admin', sub: 'Conecta o desconecta usuarios en tiempo real y administra cuentas UTP+' },
};

let sseSource = null;

// ---------------------------------------------------------------------------
// UI Utilities
// ---------------------------------------------------------------------------
function showToast(msg) {
  const box = document.getElementById('toastBox');
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

function toggleTheme() {
  document.body.classList.toggle('light');
  localStorage.setItem('utp_theme', document.body.classList.contains('light') ? 'light' : 'dark');
}
if (localStorage.getItem('utp_theme') === 'light') {
  document.body.classList.add('light');
}

function toggleMobileSidebar() {
  document.getElementById('appSidebar').classList.toggle('open');
}

function apiHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (Store.token) h['X-Session-Token'] = Store.token;
  return h;
}

function urgencyBadge(hoursLeft) {
  if (hoursLeft === null || hoursLeft === undefined) return `<span class="badge badge-muted">Sin fecha</span>`;
  if (hoursLeft <= 0) return `<span class="badge badge-red">Vencida</span>`;
  if (hoursLeft <= 24) return `<span class="badge badge-red">🔴 ${hoursLeft.toFixed(1)}h</span>`;
  if (hoursLeft <= 72) return `<span class="badge badge-amber">🟡 ${(hoursLeft / 24).toFixed(1)}d</span>`;
  return `<span class="badge badge-green">🟢 ${(hoursLeft / 24).toFixed(1)}d</span>`;
}

// ---------------------------------------------------------------------------
// 1. Navigation Router (Sidebar View Controller)
// ---------------------------------------------------------------------------
function switchView(view) {
  Store.currentView = view;
  document.getElementById('appSidebar').classList.remove('open');

  const meta = VIEW_META[view] || VIEW_META.dashboard;
  document.getElementById('topbarTitle').textContent = meta.title;
  document.getElementById('topbarSubtitle').textContent = meta.sub;

  Object.keys(VIEW_META).forEach(v => {
    const cap = v.charAt(0).toUpperCase() + v.slice(1);
    const navEl = document.getElementById(`nav${cap}`);
    if (navEl) navEl.classList.toggle('active', v === view);
    const viewEl = document.getElementById(`view${cap}`);
    if (viewEl) {
      viewEl.style.display = v === view ? (v === 'courses' ? 'grid' : 'block') : 'none';
    }
  });

  if (view === 'dashboard') renderDashboardView();
  if (view === 'tasks') renderEvaluationsView();
  if (view === 'announcements') renderAnnouncementsView();
  if (view === 'calendar') loadCalendarData();
  if (view === 'drive') loadDriveStatus();
  if (view === 'admin') renderAdminPanel();
  if (view === 'ai') document.getElementById('aiInput').focus();
}

// ---------------------------------------------------------------------------
// 2. Authentication & Session Lifecycle
// ---------------------------------------------------------------------------
async function initSession() {
  document.getElementById('remDate').value = new Date().toISOString().slice(0, 10);
  const res = await fetch(`/api/auth/session?token=${encodeURIComponent(Store.token)}`);
  const data = await res.json();
  Store.profiles = data.profiles || [];
  renderGateProfiles();

  if (data.authenticated && data.user) {
    Store.currentUser = data.user;
    document.getElementById('loginGate').style.display = 'none';
    updateCurrentUserBadge();
    setLockedState(!data.is_allowed);
    connectRealtimeSSE();
    await loadOverview(false);
    loadDriveStatus();
    loadCalendarData();
  } else {
    document.getElementById('loginGate').style.display = 'flex';
  }
}

function renderGateProfiles() {
  const el = document.getElementById('gateProfilesList');
  el.innerHTML = (Store.profiles || []).map(p => `
    <div class="login-profile-item" onclick="selectGateProfile('${p.username}')">
      <div style="display:flex; align-items:center; gap:8px;">
        <div class="avatar-circle" style="background:${p.avatar_color || '#e11d48'}">${(p.name || p.username)[0].toUpperCase()}</div>
        <div>
          <div style="font-size:12.5px; font-weight:700;">${p.name}</div>
          <div style="font-size:11px; color:var(--muted);">@${p.username} · ${p.role === 'admin' ? '🛡️ Admin' : '👤 Usuario'}</div>
        </div>
      </div>
      <span class="badge ${p.is_allowed ? 'badge-green' : 'badge-red'}">${p.is_allowed ? 'Activo' : 'Bloqueado'}</span>
    </div>
  `).join('');
}

function selectGateProfile(username) {
  document.getElementById('gateUsername').value = username;
  const pw = document.getElementById('gatePassword');
  pw.value = username === 'invitado' ? 'utp2026' : '';
  pw.focus();
}

function openLoginGate() {
  renderGateProfiles();
  document.getElementById('gateError').style.display = 'none';
  document.getElementById('loginGate').style.display = 'flex';
}

async function submitGateLogin() {
  const username = document.getElementById('gateUsername').value.trim();
  const access_code = document.getElementById('gatePassword').value.trim();
  const errEl = document.getElementById('gateError');
  errEl.style.display = 'none';

  if (!username) {
    errEl.textContent = 'Selecciona o escribe tu usuario.';
    errEl.style.display = 'block';
    return;
  }

  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ username, access_code })
  });
  const data = await res.json();
  if (!data.ok) {
    errEl.textContent = data.error || 'Credenciales incorrectas';
    errEl.style.display = 'block';
    return;
  }

  Store.token = data.token;
  localStorage.setItem('utp_session_token', data.token);
  Store.currentUser = data.user;
  document.getElementById('loginGate').style.display = 'none';
  setLockedState(false);
  updateCurrentUserBadge();
  showToast(`👋 Bienvenido, ${data.user.name}`);
  connectRealtimeSSE();
  await loadOverview(false);
  loadDriveStatus();
  loadCalendarData();
}

async function logoutSession() {
  await fetch('/api/auth/logout', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ token: Store.token })
  });
  Store.token = '';
  Store.currentUser = null;
  localStorage.removeItem('utp_session_token');
  if (sseSource) sseSource.close();
  openLoginGate();
}

function updateCurrentUserBadge() {
  const u = Store.currentUser;
  if (!u) return;
  document.getElementById('currentUserName').textContent = u.name;
  document.getElementById('currentUserRole').textContent = u.role === 'admin' ? '🛡️ Administrador' : '👤 Estudiante';
  const av = document.getElementById('currentUserAvatar');
  av.textContent = (u.name || u.username || 'U')[0].toUpperCase();
  av.style.background = u.avatar_color || '#e11d48';
}

function setLockedState(isLocked, reason) {
  const overlay = document.getElementById('lockOverlay');
  if (isLocked) {
    if (reason) document.getElementById('lockReason').textContent = reason;
    overlay.style.display = 'flex';
  } else {
    overlay.style.display = 'none';
  }
}

// ---------------------------------------------------------------------------
// 3. Real-Time SSE Observer
// ---------------------------------------------------------------------------
function connectRealtimeSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource(`/api/events?token=${encodeURIComponent(Store.token)}`);

  sseSource.addEventListener('presence_update', (e) => {
    const snap = JSON.parse(e.data);
    Store.presence = snap;
    Store.profiles = snap.users || Store.profiles;
    updatePresenceUI(snap);
  });

  sseSource.addEventListener('user_access_changed', (e) => {
    const payload = JSON.parse(e.data);
    if (Store.currentUser && (Store.currentUser.id === payload.user_id || Store.currentUser.username === payload.username)) {
      Store.currentUser.is_allowed = payload.is_allowed;
      if (!payload.is_allowed) {
        setLockedState(true, `El Administrador ha desconectado a @${payload.username} en tiempo real.`);
        showToast('⚠️ Tu sesión fue desconectada por el Administrador');
      } else {
        setLockedState(false);
        showToast('🟢 El Administrador ha reconectado tu acceso');
        loadOverview(false);
      }
    }
  });

  sseSource.addEventListener('calendar_updated', () => loadCalendarData());
  sseSource.addEventListener('data_updated', () => {
    loadOverview(false);
    loadDriveStatus();
  });
}

function updatePresenceUI(snap) {
  const cnt = snap.online_count || 1;
  document.getElementById('liveUsersCount').textContent = `${cnt} en línea`;
  document.getElementById('chipOnline').textContent = cnt;
  document.getElementById('adminOnlineBadge').textContent = `${cnt} en línea ahora`;
  if (Store.currentView === 'admin') renderAdminPanel();
}

setInterval(() => {
  if (Store.token) {
    fetch('/api/presence/ping', {
      method: 'POST',
      headers: apiHeaders(),
      body: JSON.stringify({ token: Store.token, view: Store.currentView })
    }).then(r => {
      if (r.status === 403) setLockedState(true);
    }).catch(() => {});
  }
}, 25000);

// ---------------------------------------------------------------------------
// 4. Data Fetching & Store Hydration
// ---------------------------------------------------------------------------
async function loadOverview(forceLive = false) {
  const allCycles = document.getElementById('chkAllCycles').checked ? '1' : '0';
  const liveParam = forceLive ? '1' : '0';
  const res = await fetch(`/api/overview?all_cycles=${allCycles}&live=${liveParam}&token=${encodeURIComponent(Store.token)}`);
  const data = await res.json();
  if (data.disconnected_by_admin) {
    setLockedState(true, data.error);
    return;
  }
  if (data.error) return;

  Store.courses = data.courses || [];
  Store.tasks = data.tasks || [];
  Store.evaluations = data.evaluations || [];
  Store.announcements = data.announcements || [];

  if (data.presence) {
    Store.presence = data.presence;
    updatePresenceUI(data.presence);
  }

  // Update sidebar & KPI badges
  document.getElementById('chipCourses').textContent = Store.courses.length;
  document.getElementById('chipTasks').textContent = Store.tasks.length;
  document.getElementById('chipAnnouncements').textContent = Store.announcements.length;

  // Populate course selectors in Tasks, Announcements & Calendar views
  const courseOptions = `<option value="">Todos los cursos (${Store.courses.length})</option>` +
    Store.courses.map(c => `<option value="${c.name}">${c.name}</option>`).join('');

  document.getElementById('taskCourseFilter').innerHTML = courseOptions;
  document.getElementById('annCourseFilter').innerHTML = courseOptions;
  document.getElementById('remCourse').innerHTML =
    `<option value="General UTP+">🎓 Curso: General UTP+</option>` +
    Store.courses.map(c => `<option value="${c.name}">${c.name}</option>`).join('');

  renderDashboardView();
  renderCoursesList();
  renderEvaluationsView();
  renderAnnouncementsView();

  if (!Store.selectedCourse && Store.courses.length > 0) {
    selectCourse(0);
  }
}

// ---------------------------------------------------------------------------
// 5. View Renderer: DASHBOARD (Resumen General)
// ---------------------------------------------------------------------------
function renderDashboardView() {
  document.getElementById('statCourses').textContent = Store.courses.length;
  document.getElementById('statTasks').textContent = Store.tasks.length;
  document.getElementById('statAnnouncements').textContent = Store.announcements.length;
  const totalFiles = Store.courses.reduce((acc, c) => acc + (c.downloadedCount || 0), 0);
  document.getElementById('statFiles').textContent = totalFiles;

  // Pending tasks preview (top 4)
  const tasksEl = document.getElementById('dashPendingList');
  if (!Store.tasks.length) {
    tasksEl.innerHTML = `<div style="color:var(--muted); font-size:12.5px; padding:10px;">🎉 No tienes entregas pendientes urgentes.</div>`;
  } else {
    tasksEl.innerHTML = Store.tasks.slice(0, 4).map(t => `
      <div class="resource-row" style="padding:10px 12px;">
        <div>
          <div style="display:flex; align-items:center; gap:6px; margin-bottom:2px;">
            ${urgencyBadge(t.hours_left)}
            <span class="badge badge-blue">${t.type} · Sem ${t.weekNumber || '-'}</span>
          </div>
          <div style="font-weight:700; font-size:13px;">${t.activityTitle}</div>
          <div style="font-size:11.5px; color:var(--muted);">🎓 ${t.courseName} · Vence: <strong>${(t.finishAt || '-').slice(0, 16)}</strong></div>
        </div>
        <button class="btn-ai btn-sm" onclick="sendAiPrompt('Recuérdame mañana a las 17:00 entregar ${t.activityTitle.replace(/'/g, '')}')">📅 Agendar</button>
      </div>
    `).join('');
  }

  // Courses progress preview
  const coursesEl = document.getElementById('dashCoursesList');
  coursesEl.innerHTML = Store.courses.map((c, idx) => `
    <div class="resource-row" style="padding:9px 12px; cursor:pointer;" onclick="switchView('courses'); selectCourse(${idx});">
      <div style="flex:1;">
        <div style="font-weight:700; font-size:12.5px;">${c.name}</div>
        <div style="font-size:11px; color:var(--muted);">👨‍🏫 ${c.teacher} · Clase #${c.classNumber}</div>
      </div>
      <div style="text-align:right;">
        <span class="badge badge-green">☁️ ${c.downloadedCount} arch.</span>
      </div>
    </div>
  `).join('');

  // Recent announcements preview (top 4)
  const annsEl = document.getElementById('dashAnnouncementsList');
  annsEl.innerHTML = Store.announcements.slice(0, 4).map(a => `
    <div class="resource-row" style="padding:10px 12px; flex-direction:column; align-items:flex-start;">
      <div style="display:flex; justify-content:space-between; width:100%; font-size:11px; color:var(--muted);">
        <span>🎓 <strong>${a.courseName}</strong></span>
        <span>${(a.publishAt || '').slice(0, 16)}</span>
      </div>
      <div style="font-weight:700; font-size:12.5px;">📣 ${a.title}</div>
      <div style="font-size:11.5px; color:var(--muted);">${(a.descriptionText || '').slice(0, 140)}...</div>
    </div>
  `).join('');
}

// ---------------------------------------------------------------------------
// 6. View Renderer: COURSES & WEEKLY SYLLABUS (Separated from Tasks/Anns)
// ---------------------------------------------------------------------------
function renderCoursesList() {
  const q = (document.getElementById('courseSearchInput').value || '').toLowerCase();
  const el = document.getElementById('courseList');
  const filtered = Store.courses.map((c, idx) => ({ c, idx })).filter(({ c }) =>
    !q || (c.name || '').toLowerCase().includes(q) || (c.classNumber || '').toLowerCase().includes(q)
  );

  el.innerHTML = filtered.map(({ c, idx }) => {
    const isSel = Store.selectedCourse && Store.selectedCourse.name === c.name && Store.selectedCourse.classNumber === c.classNumber;
    return `
      <div class="course-card ${isSel ? 'selected' : ''}" onclick="selectCourse(${idx})">
        <div class="course-name">${c.name}</div>
        <div class="course-sub">
          <span>${c.teacher}</span>
          <span>#${c.classNumber}</span>
        </div>
        <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--muted); margin-bottom:4px;">
          <span style="color:var(--green);">☁️ ${c.downloadedCount} archivos</span>
          <span>${c.progress || 0}%</span>
        </div>
        <div class="mini-bar"><div class="mini-fill" style="width:${Math.min(100, c.progress || 0)}%"></div></div>
      </div>
    `;
  }).join('');
}

async function selectCourse(idx) {
  const c = Store.courses[idx];
  if (!c) return;
  Store.selectedCourse = c;
  renderCoursesList();

  const panel = document.getElementById('courseDetail');
  panel.innerHTML = `<div style="padding:40px; text-align:center; color:var(--muted); font-size:13px;">Cargando semanas y materiales de <strong>${c.name}</strong>...</div>`;

  const qs = new URLSearchParams({
    courseId: c.courseId || '',
    sectionId: c.sectionId || '',
    name: c.name || '',
    classNumber: c.classNumber || '',
    token: Store.token
  });
  const res = await fetch(`/api/course?${qs.toString()}`);
  Store.courseData = await res.json();
  renderCourseDetail();
}

function setSubTab(tab) {
  Store.subTab = tab;
  renderCourseDetail();
}

function renderCourseDetail() {
  const c = Store.selectedCourse;
  const d = Store.courseData;
  if (!c || !d) return;

  const panel = document.getElementById('courseDetail');
  const sylBtn = d.syllabusUrl
    ? `<a class="btn btn-sm" href="${d.syllabusUrl}" target="_blank">📄 Sílabo Oficial</a>`
    : '';

  let bodyHtml = '';
  if (Store.subTab === 'weeks') {
    bodyHtml = (d.weeks || []).map((w, i) => `
      <div class="week-item ${w.weekNumber === d.currentWeek || i === d.weeks.length - 1 ? 'open' : ''}">
        <div class="week-head" onclick="this.parentElement.classList.toggle('open')">
          <span>📅 ${w.label} ${w.weekNumber === d.currentWeek ? '<span class="badge badge-green" style="margin-left:6px;">Semana Actual</span>' : ''}</span>
          <div style="display:flex; align-items:center; gap:8px;">
            <button class="btn-ai btn-sm" onclick="event.stopPropagation(); sendAiPrompt('¿Qué pasó en la semana ${w.weekNumber} de ${c.name.replace(/'/g, '')}?')">🤖 Qué se hizo</button>
            <span style="color:var(--muted); font-size:11.5px;">${w.themes.length} tema(s) ▾</span>
          </div>
        </div>
        <div class="week-body">
          ${w.themes.map(t => `
            <div class="theme-group">
              <div class="theme-label">📌 ${t.name}</div>
              ${t.contents.map(item => renderContentRow(item)).join('')}
            </div>
          `).join('')}
        </div>
      </div>
    `).join('');
  } else if (Store.subTab === 'local') {
    const files = d.localFiles || [];
    bodyHtml = `
      <input type="text" class="input-field" style="margin-bottom:10px;" placeholder="🔍 Filtrar archivos de este curso..." oninput="filterCourseFiles(this.value)" />
      <table class="clean-table" id="courseFilesTable">
        <thead>
          <tr><th>Semana / Carpeta</th><th>Archivo</th><th>Tamaño</th><th>Acción</th></tr>
        </thead>
        <tbody>
          ${files.map(f => `
            <tr>
              <td style="color:var(--muted);">${f.folder}</td>
              <td style="font-weight:600;">${f.name}</td>
              <td style="color:var(--muted);">${f.size_kb} KB</td>
              <td style="display:flex; gap:6px;">
                ${f.rel_path ? `<a class="btn btn-sm" href="/files/${encodeURI(f.rel_path)}" target="_blank">📄 Abrir</a>` : ''}
                ${f.drive_url ? `<a class="btn btn-drive btn-sm" href="${f.drive_url}" target="_blank">☁️ Drive</a>` : ''}
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  panel.innerHTML = `
    <div class="detail-top">
      <div>
        <h2 style="font-size:16.5px; font-weight:700;">${c.name}</h2>
        <div style="font-size:11.5px; color:var(--muted); margin-top:2px;">
          Clase #${c.classNumber} · 👨‍🏫 ${c.teacher} · Semana ${d.currentWeek || '-'}/${d.numWeeks || 18}
        </div>
      </div>
      <div style="display:flex; gap:6px; flex-wrap:wrap;">
        ${sylBtn}
        <button class="btn-ai btn-sm" onclick="askAiAboutSelectedCourse()">🤖 Resumen IA</button>
        <button class="btn-sm" onclick="openCourseTasks('${c.name.replace(/'/g, "\\'")}')">📝 Ver Tareas (${(d.tasks || []).length})</button>
        <button class="btn-sm" onclick="openCourseAnnouncements('${c.name.replace(/'/g, "\\'")}')">📢 Ver Anuncios (${(d.announcements || []).length})</button>
      </div>
    </div>

    <div class="sub-nav">
      <button class="sub-btn ${Store.subTab === 'weeks' ? 'active' : ''}" onclick="setSubTab('weeks')">📑 Temario por Semanas (${(d.weeks || []).length})</button>
      <button class="sub-btn ${Store.subTab === 'local' ? 'active' : ''}" onclick="setSubTab('local')">☁️ Archivos del Curso (${(d.localFiles || []).length})</button>
    </div>

    <div>${bodyHtml}</div>
  `;
}

function renderContentRow(item) {
  let action = '';
  if (item.html) {
    const encoded = encodeURIComponent(item.html);
    const titleEnc = encodeURIComponent(item.title || 'Contenido');
    action = `<button class="btn-sm" onclick="openRawModal('${titleEnc}', '${encoded}')">👁️ Leer Guía</button>`;
  } else if (item.url) {
    const isDrive = String(item.url).includes('drive.google.com');
    action = `<a class="btn ${isDrive ? 'btn-drive' : ''} btn-sm" href="${item.url}" target="_blank">${isDrive ? '☁️ Drive' : '↗ Abrir'}</a>`;
  }
  return `
    <div class="resource-row">
      <div style="display:flex; align-items:center; gap:7px; overflow:hidden;">
        <span class="badge badge-blue">${item.type}</span>
        <span style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${item.title}</span>
      </div>
      <div>${action}</div>
    </div>
  `;
}

function filterCourseFiles(q) {
  const term = q.toLowerCase();
  document.querySelectorAll('#courseFilesTable tbody tr').forEach(tr => {
    tr.style.display = tr.textContent.toLowerCase().includes(term) ? '' : 'none';
  });
}

function openCourseTasks(courseName) {
  document.getElementById('taskCourseFilter').value = courseName;
  document.getElementById('taskStatusFilter').value = 'all';
  switchView('tasks');
}

function openCourseAnnouncements(courseName) {
  document.getElementById('annCourseFilter').value = courseName;
  switchView('announcements');
}

// ---------------------------------------------------------------------------
// 7. View Renderer: DEDICATED TASKS & EVALUATIONS VIEW
// ---------------------------------------------------------------------------
function renderEvaluationsView() {
  const courseFilter = document.getElementById('taskCourseFilter').value;
  const statusFilter = document.getElementById('taskStatusFilter').value;
  const q = (document.getElementById('taskSearchInput').value || '').toLowerCase();

  // Section A: Urgent Pending Tasks from Agenda
  const pendingFiltered = Store.tasks.filter(t =>
    (!courseFilter || t.courseName === courseFilter) &&
    (!q || (t.activityTitle || '').toLowerCase().includes(q) || (t.courseName || '').toLowerCase().includes(q))
  );

  const pendingGrid = document.getElementById('pendingTasksSection');
  if (statusFilter === 'graded') {
    pendingGrid.style.display = 'none';
  } else {
    pendingGrid.style.display = 'block';
    const pCards = document.getElementById('tasksGrid');
    if (!pendingFiltered.length) {
      pCards.innerHTML = `<div style="color:var(--muted); font-size:12.5px; padding:10px;">🎉 No hay entregas pendientes con este filtro.</div>`;
    } else {
      pCards.innerHTML = pendingFiltered.map(t => `
        <div class="min-card">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            ${urgencyBadge(t.hours_left)}
            <span class="badge badge-blue">${t.type} · Sem ${t.weekNumber || '-'}</span>
          </div>
          <div style="font-size:11.5px; color:var(--muted); font-weight:600;">🎓 ${t.courseName}</div>
          <div style="font-size:14px; font-weight:700;">${t.activityTitle}</div>
          <div style="font-size:11.5px; color:var(--muted); margin-top:auto; padding-top:8px; border-top:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
            <span>Vence: <strong style="color:var(--text);">${(t.finishAt || '-').slice(0, 16)}</strong></span>
            <button class="btn-ai btn-sm" onclick="sendAiPrompt('Recuérdame mañana a las 17:00 entregar ${t.activityTitle.replace(/'/g, '')}')">📅 Recordarme</button>
          </div>
        </div>
      `).join('');
    }
  }

  // Section B: All Course Homework & Evaluations History
  const evalsFiltered = Store.evaluations.filter((ev, idx) => {
    ev._idx = idx;
    if (courseFilter && ev.courseName !== courseFilter) return false;
    if (statusFilter === 'pending' && (ev.status === 'DELIVERED' || ev.status === 'QUALIFIED')) return false;
    if (statusFilter === 'graded' && ev.status !== 'DELIVERED' && ev.status !== 'QUALIFIED' && ev.score === null) return false;
    if (q && !(ev.title || '').toLowerCase().includes(q) && !(ev.courseName || '').toLowerCase().includes(q)) return false;
    return true;
  });

  const evalsGrid = document.getElementById('evaluationsGrid');
  if (!evalsFiltered.length) {
    evalsGrid.innerHTML = `<div style="color:var(--muted); font-size:12.5px; padding:12px;">No se encontraron evaluaciones con este filtro.</div>`;
  } else {
    evalsGrid.innerHTML = evalsFiltered.map(t => `
      <div class="min-card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span class="badge ${t.status === 'DELIVERED' || t.status === 'QUALIFIED' ? 'badge-green' : 'badge-amber'}">${t.kind} · ${t.status}</span>
          <span style="font-size:12px; font-weight:700;">Nota: ${t.score !== null && t.score !== undefined ? t.score : '-'}/${t.topScore || 20}</span>
        </div>
        <div style="font-size:11.5px; color:var(--muted); font-weight:600;">🎓 ${t.courseName}</div>
        <div style="font-size:13.5px; font-weight:700;">${t.title}</div>
        <div style="font-size:11.5px; color:var(--muted);">Cierre: <strong>${t.visibleTo || '-'}</strong></div>
        ${t.descriptionHtml ? `<div style="margin-top:auto; padding-top:6px;"><button class="btn-sm" onclick="openGlobalTaskModal(${t._idx})">📖 Ver Indicaciones</button></div>` : ''}
      </div>
    `).join('');
  }
}

function openGlobalTaskModal(idx) {
  const t = Store.evaluations[idx];
  if (!t) return;
  document.getElementById('contentModalTitle').textContent = `${t.courseName} — ${t.title}`;
  document.getElementById('contentModalBody').innerHTML = t.descriptionHtml || '<p>Sin descripción.</p>';
  document.getElementById('contentModal').style.display = 'flex';
}

// ---------------------------------------------------------------------------
// 8. View Renderer: DEDICATED TEACHER ANNOUNCEMENTS VIEW
// ---------------------------------------------------------------------------
function renderAnnouncementsView() {
  const courseFilter = document.getElementById('annCourseFilter').value;
  const q = (document.getElementById('annSearchInput').value || '').toLowerCase();

  const filtered = Store.announcements.filter((a, idx) => {
    a._idx = idx;
    if (courseFilter && a.courseName !== courseFilter) return false;
    if (q && !(a.title || '').toLowerCase().includes(q) && !(a.descriptionText || '').toLowerCase().includes(q) && !(a.courseName || '').toLowerCase().includes(q)) return false;
    return true;
  });

  const listEl = document.getElementById('globalAnnouncementsList');
  if (!filtered.length) {
    listEl.innerHTML = `<div style="color:var(--muted); font-size:13px; padding:16px;">No hay anuncios para este filtro.</div>`;
    return;
  }

  listEl.innerHTML = filtered.slice(0, 60).map(a => `
    <div class="min-card" style="margin-bottom:10px;">
      <div style="display:flex; justify-content:space-between; align-items:center; font-size:11.5px; color:var(--muted); flex-wrap:wrap; gap:6px;">
        <div>
          <span class="badge badge-blue">🎓 ${a.courseName}</span>
          <span style="margin-left:6px;">👨‍🏫 ${a.author || a.teacher || 'Docente'}</span>
        </div>
        <span>📅 ${a.publishAt || ''}</span>
      </div>
      <div style="font-size:14px; font-weight:700;">📣 ${a.title}</div>
      <div style="font-size:12.5px; color:var(--muted); white-space:pre-line;">${(a.descriptionText || '').slice(0, 280)}${(a.descriptionText || '').length > 280 ? '...' : ''}</div>
      <div>
        <button class="btn-sm" onclick="openGlobalAnnouncementModal(${a._idx})">🔍 Leer Anuncio Completo</button>
      </div>
    </div>
  `).join('');
}

function openGlobalAnnouncementModal(idx) {
  const a = Store.announcements[idx];
  if (!a) return;
  document.getElementById('contentModalTitle').textContent = `${a.courseName} — ${a.title}`;
  document.getElementById('contentModalBody').innerHTML = a.descriptionHtml || '<p>Sin contenido.</p>';
  document.getElementById('contentModal').style.display = 'flex';
}

// ---------------------------------------------------------------------------
// 9. View Renderer: LOCAL UNIVERSITY AI ASSISTANT
// ---------------------------------------------------------------------------
async function sendAiPrompt(presetText) {
  const input = document.getElementById('aiInput');
  const query = (presetText !== undefined ? presetText : input.value).trim();
  if (!query) return;
  if (presetText === undefined) input.value = '';

  switchView('ai');
  const msgBox = document.getElementById('aiMessages');

  const uDiv = document.createElement('div');
  uDiv.className = 'ai-msg user';
  uDiv.textContent = query;
  msgBox.appendChild(uDiv);

  const bDiv = document.createElement('div');
  bDiv.className = 'ai-msg bot';
  bDiv.innerHTML = `<span style="color:var(--muted);">🤖 Consultando tus cursos, semanas y archivos en Drive...</span>`;
  msgBox.appendChild(bDiv);
  msgBox.scrollTop = msgBox.scrollHeight;

  const res = await fetch('/api/ai/ask', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ query })
  });
  const data = await res.json();

  let suggHtml = '';
  if (data.suggestions && data.suggestions.length) {
    suggHtml = `<div style="margin-top:10px; padding-top:8px; border-top:1px solid var(--border); display:flex; gap:6px; flex-wrap:wrap;">` +
      data.suggestions.map(s => `<button class="chip-btn" onclick="sendAiPrompt('${s.replace(/'/g, "\\'")}')">💡 ${s}</button>`).join('') +
      `</div>`;
  }

  bDiv.innerHTML = `
    <div style="font-weight:700; margin-bottom:6px; color:#c4b5fd;">🤖 IA Local UTP+</div>
    <div>${data.answer_html || 'Sin respuesta.'}</div>
    ${suggHtml}
  `;
  msgBox.scrollTop = msgBox.scrollHeight;

  if (data.reminder_created) {
    showToast('📅 Recordatorio agregado al Calendario');
    loadCalendarData();
  }
}

function askAiAboutSelectedCourse() {
  const c = Store.selectedCourse;
  if (!c) {
    switchView('ai');
    return;
  }
  const wk = (Store.courseData && Store.courseData.currentWeek) || 15;
  sendAiPrompt(`¿Qué pasó en la semana ${wk} de ${c.name}?`);
}

// ---------------------------------------------------------------------------
// 10. View Renderer: CALENDAR, CLASSES & REMINDERS
// ---------------------------------------------------------------------------
async function loadCalendarData() {
  const res = await fetch(`/api/calendar?token=${encodeURIComponent(Store.token)}`);
  const data = await res.json();
  Store.calendar = {
    reminders: data.reminders || [],
    deadlines: data.deadlines || []
  };
  const totalEvents = Store.calendar.reminders.length + Store.calendar.deadlines.length;
  document.getElementById('chipCalendar').textContent = totalEvents;
  renderCalendar();
}

function changeCalMonth(delta) {
  Store.calMonth += delta;
  if (Store.calMonth > 11) { Store.calMonth = 0; Store.calYear++; }
  if (Store.calMonth < 0) { Store.calMonth = 11; Store.calYear--; }
  renderCalendar();
}

function resetCalToday() {
  const d = new Date();
  Store.calYear = d.getFullYear();
  Store.calMonth = d.getMonth();
  Store.calFilterDate = '';
  renderCalendar();
}

function selectCalDate(dateStr) {
  Store.calFilterDate = Store.calFilterDate === dateStr ? '' : dateStr;
  document.getElementById('remDate').value = dateStr;
  renderCalendar();
}

function renderCalendar() {
  const monthNames = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'];
  document.getElementById('calMonthTitle').textContent = `📅 ${monthNames[Store.calMonth]} ${Store.calYear}`;

  const allEvents = [
    ...Store.calendar.reminders.map(r => ({ ...r, source: 'reminder' })),
    ...Store.calendar.deadlines.map(d => ({ ...d, source: 'utp' }))
  ];

  const eventsByDate = {};
  allEvents.forEach(ev => {
    const d = ev.event_date;
    if (!eventsByDate[d]) eventsByDate[d] = [];
    eventsByDate[d].push(ev);
  });

  const grid = document.getElementById('calGrid');
  const dayHeads = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom'].map(d => `<div class="cal-day-head">${d}</div>`).join('');

  const firstDay = new Date(Store.calYear, Store.calMonth, 1);
  const startOffset = (firstDay.getDay() + 6) % 7;
  const daysInMonth = new Date(Store.calYear, Store.calMonth + 1, 0).getDate();
  const todayStr = new Date().toISOString().slice(0, 10);

  let cellsHtml = dayHeads;
  for (let i = 0; i < startOffset; i++) {
    cellsHtml += `<div class="cal-cell other-month"></div>`;
  }

  for (let day = 1; day <= daysInMonth; day++) {
    const dStr = `${Store.calYear}-${String(Store.calMonth + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const dayEvents = eventsByDate[dStr] || [];
    const isToday = dStr === todayStr;
    const isSel = dStr === Store.calFilterDate;

    const pills = dayEvents.slice(0, 3).map(ev => {
      const bg = ev.source === 'utp' ? 'var(--amber-soft)' : (ev.category === 'clase' ? 'var(--blue-soft)' : 'var(--green-soft)');
      const col = ev.source === 'utp' ? 'var(--amber)' : (ev.category === 'clase' ? '#60a5fa' : 'var(--green)');
      return `<div class="cal-dot-item" style="background:${bg}; color:${col};">${ev.event_time} ${ev.title}</div>`;
    }).join('');

    cellsHtml += `
      <div class="cal-cell ${isToday ? 'today' : ''} ${isSel ? 'selected' : ''}" onclick="selectCalDate('${dStr}')">
        <div style="font-weight:700; display:flex; justify-content:space-between;">
          <span>${day}</span>
          ${dayEvents.length > 0 ? `<span style="font-size:10px; color:var(--muted);">${dayEvents.length}</span>` : ''}
        </div>
        ${pills}
      </div>
    `;
  }
  grid.innerHTML = cellsHtml;

  const listEl = document.getElementById('calendarEventsList');
  const labelEl = document.getElementById('calSelectedDateLabel');
  let filtered = allEvents.slice().sort((a, b) => `${a.event_date} ${a.event_time}`.localeCompare(`${b.event_date} ${b.event_time}`));
  if (Store.calFilterDate) {
    filtered = filtered.filter(e => e.event_date === Store.calFilterDate);
    labelEl.textContent = `Eventos del ${Store.calFilterDate} (${filtered.length})`;
  } else {
    labelEl.textContent = `Todos los eventos y entregas (${filtered.length})`;
  }

  if (!filtered.length) {
    listEl.innerHTML = `<div style="color:var(--muted); font-size:12.5px; padding:12px;">No hay clases ni recordatorios para esta fecha.</div>`;
    return;
  }

  listEl.innerHTML = filtered.map(ev => {
    const isUtp = ev.source === 'utp';
    const badgeClass = isUtp ? 'badge-amber' : (ev.category === 'clase' ? 'badge-blue' : (ev.category === 'examen' ? 'badge-red' : 'badge-green'));
    return `
      <div class="resource-row" style="padding:10px 12px; opacity:${ev.completed ? '0.55' : '1'};">
        <div style="flex:1;">
          <div style="display:flex; align-items:center; gap:6px; margin-bottom:3px;">
            <span class="badge ${badgeClass}">${isUtp ? '⏰ ENTREGA UTP+' : ev.category.toUpperCase()}</span>
            <span style="font-size:11.5px; font-weight:700;">📅 ${ev.event_date} · 🕒 ${ev.event_time}</span>
            ${ev.recurrence === 'weekly' ? '<span class="badge badge-purple">Semanal</span>' : ''}
          </div>
          <div style="font-weight:700; font-size:13px; ${ev.completed ? 'text-decoration:line-through;' : ''}">${ev.title}</div>
          <div style="font-size:11.5px; color:var(--muted);">🎓 ${ev.course_name || 'General UTP+'}</div>
        </div>
        <div style="display:flex; gap:5px; align-items:center;">
          ${ev.gcal_url ? `<a class="btn btn-drive btn-sm" href="${ev.gcal_url}" target="_blank">📅 GCal</a>` : ''}
          ${!isUtp ? `
            <button class="btn-sm" onclick="toggleReminder(${ev.id}, ${!ev.completed})">${ev.completed ? '↩️' : '✅'}</button>
            <button class="btn-danger btn-sm" onclick="deleteReminder(${ev.id})">🗑️</button>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

async function addCalendarReminder() {
  const title = document.getElementById('remTitle').value.trim();
  const course_name = document.getElementById('remCourse').value;
  const category = document.getElementById('remCategory').value;
  const event_date = document.getElementById('remDate').value;
  const event_time = document.getElementById('remTime').value;
  const recurrence = document.getElementById('remRecurrence').value;

  if (!title) {
    showToast('⚠️ Escribe un título para la clase o recordatorio');
    return;
  }

  const res = await fetch('/api/calendar/add', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ title, course_name, category, event_date, event_time, recurrence })
  });
  const data = await res.json();
  if (data.ok) {
    document.getElementById('remTitle').value = '';
    showToast('✅ Agregado al Calendario UTP+');
    loadCalendarData();
  }
}

async function toggleReminder(id, completed) {
  await fetch('/api/calendar/toggle', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ id, completed })
  });
  loadCalendarData();
}

async function deleteReminder(id) {
  await fetch('/api/calendar/delete', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ id })
  });
  showToast('🗑️ Recordatorio eliminado');
  loadCalendarData();
}

// ---------------------------------------------------------------------------
// 11. View Renderer: GOOGLE DRIVE & ADMIN PANEL
// ---------------------------------------------------------------------------
async function loadDriveStatus() {
  const res = await fetch(`/api/gdrive/status?token=${encodeURIComponent(Store.token)}`);
  const data = await res.json();
  Store.driveStatus = data;

  document.getElementById('driveFolderIdLabel').textContent = data.folder_id;
  document.getElementById('driveDirectLink').href = data.folder_url;
  document.getElementById('headerDriveBtn').href = data.folder_url;
  document.getElementById('chipDrive').textContent = data.indexed_drive_files || data.local_files_count || '779';

  const tbody = document.getElementById('driveFilesTbody');
  const files = data.files || [];
  tbody.innerHTML = files.map(f => `
    <tr>
      <td style="font-weight:600;">${f.course_folder}</td>
      <td style="color:var(--muted);">${f.folder_path}</td>
      <td>${f.name}</td>
      <td style="color:var(--muted);">${f.size_kb} KB</td>
      <td>
        <a class="btn btn-drive btn-sm" href="${f.web_view_link}" target="_blank">☁️ Abrir en Drive</a>
      </td>
    </tr>
  `).join('');
}

function filterDriveTable(q) {
  const term = q.toLowerCase();
  document.querySelectorAll('#driveFilesTbody tr').forEach(tr => {
    tr.style.display = tr.textContent.toLowerCase().includes(term) ? '' : 'none';
  });
}

async function triggerDriveSync() {
  showToast('☁️ Sincronizando con Google Drive...');
  await fetch('/api/gdrive/sync', { method: 'POST', headers: apiHeaders() });
}

function renderAdminPanel() {
  const users = (Store.presence && Store.presence.users) || [];
  const utpProfiles = (Store.presence && Store.presence.utp_profiles) || [];

  const tbody = document.getElementById('adminUsersTbody');
  tbody.innerHTML = users.map(u => {
    const onlineBadge = u.online
      ? `<span class="badge badge-green">● En línea (${u.active_sessions})</span>`
      : `<span class="badge badge-muted">○ Offline</span>`;
    const accessBadge = u.is_allowed
      ? `<span class="badge badge-blue">Habilitado</span>`
      : `<span class="badge badge-red">Desconectado</span>`;
    const toggleBtn = u.is_allowed
      ? `<button class="btn-danger btn-sm" onclick="toggleWebUser(${u.id}, false)">🔌 Desconectar</button>`
      : `<button class="btn-success btn-sm" onclick="toggleWebUser(${u.id}, true)">⚡ Conectar</button>`;

    return `
      <tr>
        <td>
          <div style="display:flex; align-items:center; gap:8px;">
            <div class="avatar-circle" style="background:${u.avatar_color || '#3b82f6'}">${(u.name || u.username)[0].toUpperCase()}</div>
            <div>
              <div style="font-weight:700;">${u.name}</div>
              <div style="font-size:11px; color:var(--muted);">@${u.username}</div>
            </div>
          </div>
        </td>
        <td><span class="badge ${u.role === 'admin' ? 'badge-amber' : 'badge-muted'}">${u.role.toUpperCase()}</span></td>
        <td><code style="font-size:11px; color:var(--muted);">${u.access_code || '-'}</code></td>
        <td>${onlineBadge}</td>
        <td>${accessBadge}</td>
        <td style="display:flex; gap:6px;">
          ${toggleBtn}
          <button class="btn-sm" onclick="deleteWebUser(${u.id})">🗑️</button>
        </td>
      </tr>
    `;
  }).join('');

  const utpEl = document.getElementById('utpProfilesList');
  utpEl.innerHTML = utpProfiles.map(p => `
    <div class="resource-row" style="padding:10px 12px;">
      <div>
        <div style="font-weight:700; font-size:12.5px;">
          ${p.label || p.username}
          ${p.is_active && p.is_connected ? '<span class="badge badge-green" style="margin-left:6px;">Activa</span>' : ''}
        </div>
        <div style="font-size:11px; color:var(--muted);">Código: <strong>${p.username}</strong></div>
      </div>
      <div style="display:flex; gap:6px;">
        ${p.is_connected
          ? `<button class="btn-danger btn-sm" onclick="toggleUtpProfile(${p.id}, false, false)">Desconectar</button>`
          : `<button class="btn-success btn-sm" onclick="toggleUtpProfile(${p.id}, true, true)">Conectar</button>`
        }
      </div>
    </div>
  `).join('');
}

async function toggleWebUser(userId, allow) {
  const res = await fetch('/api/admin/users/toggle', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ user_id: userId, is_allowed: allow })
  });
  const data = await res.json();
  if (!data.ok) {
    showToast('⚠️ ' + (data.error || 'Requiere cuenta Admin'));
    return;
  }
  showToast(allow ? '🟢 Usuario conectado en tiempo real' : '🔌 Usuario desconectado en tiempo real');
}

async function saveWebUser() {
  const username = document.getElementById('newUsername').value.trim();
  const name = document.getElementById('newName').value.trim();
  const access_code = document.getElementById('newCode').value.trim();
  const role = document.getElementById('newRole').value;
  if (!username) return;
  const res = await fetch('/api/admin/users/save', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ username, name: name || username, access_code: access_code || '1234', role, is_allowed: true })
  });
  const data = await res.json();
  if (data.ok) {
    document.getElementById('newUsername').value = '';
    document.getElementById('newName').value = '';
    document.getElementById('newCode').value = '';
    showToast('✅ Perfil guardado');
  }
}

async function deleteWebUser(userId) {
  await fetch('/api/admin/users/delete', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ user_id: userId })
  });
}

async function toggleUtpProfile(profileId, isConnected, makeActive) {
  await fetch('/api/admin/utp-profiles/toggle', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ profile_id: profileId, is_connected: isConnected, make_active: makeActive })
  });
}

async function saveUtpProfile() {
  const username = document.getElementById('utpUser').value.trim();
  const password = document.getElementById('utpPass').value.trim();
  const label = document.getElementById('utpLabel').value.trim();
  if (!username) return;
  await fetch('/api/admin/utp-profiles/save', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({ username, password, label, make_active: true })
  });
  showToast('✅ Cuenta UTP+ guardada');
}

function openRawModal(titleEnc, htmlEnc) {
  document.getElementById('contentModalTitle').textContent = decodeURIComponent(titleEnc);
  document.getElementById('contentModalBody').innerHTML = decodeURIComponent(htmlEnc);
  document.getElementById('contentModal').style.display = 'flex';
}
function closeContentModal() {
  document.getElementById('contentModal').style.display = 'none';
}

initSession();
