/**
 * Hive 🐝 — shared left sidebar tree navigation.
 *
 * A modern, chic, collapsible tree sidebar. Renders into #sidebar-root.
 * The host page should wrap content in <div class="app-shell"> with
 * <main class="app-main"> so content sits beside the sidebar.
 *
 * Usage:
 *   <div class="app-shell">
 *     <div id="sidebar-root"></div>
 *     <main class="app-main"> ... </main>
 *   </div>
 *   <script src="/js/sidebar.js"></script>
 *   <script>renderSidebar({ active: 'skills' });</script>
 *
 * `active` highlights one leaf: home | agents | deploy | skills | mcp | tasks | settings
 * State (collapsed / open groups) is persisted in localStorage.
 */

function isHiveAuthed() {
  return !!localStorage.getItem('token');
}

function hiveLogout() {
  localStorage.removeItem('token');
  fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
  window.location.href = '/login';
}

const SIDEBAR_TREE = [
  {
    group: 'Overview',
    icon: 'M3 12l9-9 9 9M5 10v10h14V10',
    items: [
      { href: '/', label: 'Dashboard', key: 'home', icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10h14V10' },
    ],
  },
  {
    group: 'Agents',
    icon: 'M4 7h16M4 12h16M4 17h10',
    items: [
      { href: '/agents', label: 'Browse Agents', key: 'agents', icon: 'M3 6h18v12H3zM3 10h18' },
      { href: '/deploy', label: 'Deploy Agent', key: 'deploy', icon: 'M12 5v14M5 12h14' },
    ],
  },
  {
    group: 'Registry',
    icon: 'M4 5h16v14H4zM4 9h16M9 5v14',
    items: [
      { href: '/skills', label: 'Skills', key: 'skills', icon: 'M12 3l2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z' },
      { href: '/mcp', label: 'MCP Servers', key: 'mcp', icon: 'M5 8h14M5 8a2 2 0 110-4 2 2 0 010 4zM5 16h14M5 16a2 2 0 110 4 2 2 0 010-4z' },
    ],
  },
  {
    group: 'Workflow',
    icon: 'M13 3L4 14h7l-1 7 9-11h-7z',
    items: [
      { href: '/tasks', label: 'Tasks', key: 'tasks', icon: 'M4 6h16M4 12h16M4 18h10' },
    ],
  },
  {
    group: 'Account',
    icon: 'M12 15a4 4 0 100-8 4 4 0 000 8zM5 20a7 7 0 0114 0',
    items: [
      { href: '/settings', label: 'Settings', key: 'settings', icon: 'M12 9a3 3 0 100 6 3 3 0 000-6zM3 12h2m14 0h2M12 3v2m0 14v2' },
    ],
  },
];

function sbIcon(path) {
  return `<svg class="sb-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${path}"/></svg>`;
}

function renderSidebar({ active = '' } = {}) {
  const root = document.getElementById('sidebar-root');
  if (!root) return;

  const auth = isHiveAuthed();
  const collapsed = localStorage.getItem('sb-collapsed') === '1';

  const leaf = (item) => {
    const isActive = active === item.key;
    const cls = isActive ? 'sb-leaf sb-leaf-active' : 'sb-leaf';
    return `<a href="${item.href}" class="${cls}" title="${item.label}">
        <span class="sb-leaf-ic">${sbIcon(item.icon)}</span>
        <span class="sb-leaf-label">${item.label}</span>
      </a>`;
  };

  const groupsHtml = SIDEBAR_TREE.map((g) => {
    const hasActive = g.items.some((i) => i.key === active);
    const open = hasActive || collapsed ? ' open' : '';
    return `
      <div class="sb-group${open}${collapsed ? ' sb-collapsed' : ''}" data-group="${g.group}">
        <button class="sb-group-head" onclick="sbToggleGroup(this)" title="${g.group}">
          <span class="sb-group-ic">${sbIcon(g.icon)}</span>
          <span class="sb-group-label">${g.group}</span>
          <span class="sb-chevron">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
          </span>
        </button>
        <div class="sb-group-body">
          <div class="sb-group-inner">
            ${g.items.map(leaf).join('')}
          </div>
        </div>
      </div>`;
  }).join('');

  const footer = auth
    ? `<div class="sb-footer">
         <button onclick="hiveLogout()" class="sb-leaf sb-leaf-logout" title="Logout">
           <span class="sb-leaf-ic">${sbIcon('M16 17l5-5-5-5M21 12H9M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4')}</span>
           <span class="sb-leaf-label">Logout</span>
         </button>
       </div>`
    : `<div class="sb-footer">
         <a href="/login" class="sb-leaf"><span class="sb-leaf-ic">${sbIcon('M11 16l-4-4 4-4M7 12h9')}</span><span class="sb-leaf-label">Login</span></a>
         <a href="/signup" class="sb-leaf"><span class="sb-leaf-ic">${sbIcon('M12 5v14M5 12h14')}</span><span class="sb-leaf-label">Sign Up</span></a>
       </div>`;

  root.className = 'sb' + (collapsed ? ' sb-collapsed' : '');
  root.innerHTML = `
    <div class="sb-top">
      <a href="/" class="sb-brand" title="Hive">
        <span class="brand-logo">H</span>
        <span class="brand-name">Hive</span>
        <span class="sb-emoji">🐝</span>
      </a>
      <button class="sb-collapse" onclick="sbToggleCollapse()" title="Collapse">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg>
      </button>
    </div>
    <nav class="sb-nav">
      ${groupsHtml}
    </nav>
    ${footer}
    <div class="sb-scrim" onclick="sbToggleMobile()"></div>
    <button class="sb-mobile-toggle" onclick="sbToggleMobile()" title="Menu">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
    </button>
  `;
}

function sbToggleGroup(btn) {
  const group = btn.closest('.sb-group');
  if (group) group.classList.toggle('open');
}

function sbToggleCollapse() {
  const sb = document.getElementById('sidebar-root');
  if (!sb) return;
  sb.classList.toggle('sb-collapsed');
  const collapsed = sb.classList.contains('sb-collapsed');
  localStorage.setItem('sb-collapsed', collapsed ? '1' : '0');
}

function sbToggleMobile() {
  const sb = document.getElementById('sidebar-root');
  if (sb) sb.classList.toggle('sb-mobile-open');
}

document.addEventListener('click', (e) => {
  const sb = document.getElementById('sidebar-root');
  if (!sb) return;
  if (sb.classList.contains('sb-mobile-open') &&
      !e.target.closest('.sb') && !e.target.closest('.sb-mobile-toggle')) {
    sb.classList.remove('sb-mobile-open');
  }
});
