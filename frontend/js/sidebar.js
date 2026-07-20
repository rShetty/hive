/**
 * Hive 🐝 — shared left sidebar tree navigation.
 *
 * Renders a polished, collapsible tree sidebar into any element with
 * id="sidebar-root". The host page should wrap content in an element with
 * class "app-shell" so it lays out beside the sidebar.
 *
 * Usage:
 *   <div class="app-shell">
 *     <div id="sidebar-root"></div>
 *     <main class="app-main"> ... page content ... </main>
 *   </div>
 *   <script src="/js/sidebar.js"></script>
 *   <script>renderSidebar({ active: 'skills' });</script>
 *
 * `active` highlights one leaf of:
 *   home | agents | deploy | skills | mcp | tasks | settings
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
    icon: '◎',
    items: [
      { href: '/', label: 'Dashboard', key: 'home' },
    ],
  },
  {
    group: 'Agents',
    icon: '🤖',
    items: [
      { href: '/agents', label: 'Browse Agents', key: 'agents' },
      { href: '/deploy', label: 'Deploy Agent', key: 'deploy' },
    ],
  },
  {
    group: 'Registry',
    icon: '📦',
    items: [
      { href: '/skills', label: 'Skills', key: 'skills' },
      { href: '/mcp', label: 'MCP Servers', key: 'mcp' },
    ],
  },
  {
    group: 'Workflow',
    icon: '⚡',
    items: [
      { href: '/tasks', label: 'Tasks', key: 'tasks' },
    ],
  },
  {
    group: 'Account',
    icon: '⚙️',
    items: [
      { href: '/settings', label: 'Settings', key: 'settings' },
    ],
  },
];

function renderSidebar({ active = '' } = {}) {
  const root = document.getElementById('sidebar-root');
  if (!root) return;

  const auth = isHiveAuthed();

  const leaf = (item) => {
    const isActive = active === item.key;
    const cls = isActive ? 'sb-leaf sb-leaf-active' : 'sb-leaf';
    return `<a href="${item.href}" class="${cls}">
        <span class="sb-leaf-dot"></span>${item.label}</a>`;
  };

  const groupsHtml = SIDEBAR_TREE.map((g) => {
    const hasActive = g.items.some((i) => i.key === active);
    const open = hasActive ? ' open' : '';
    return `
      <div class="sb-group${open}" data-group="${g.group}">
        <button class="sb-group-head" onclick="sbToggleGroup(this)">
          <span class="sb-group-icon">${g.icon}</span>
          <span class="sb-group-label">${g.group}</span>
          <span class="sb-chevron">›</span>
        </button>
        <div class="sb-group-body">
          ${g.items.map(leaf).join('')}
        </div>
      </div>`;
  }).join('');

  const footer = auth
    ? `<div class="sb-footer">
         <button onclick="hiveLogout()" class="sb-leaf sb-leaf-logout">
           <span class="sb-leaf-dot"></span>Logout</button>
       </div>`
    : `<div class="sb-footer">
         <a href="/login" class="sb-leaf"><span class="sb-leaf-dot"></span>Login</a>
         <a href="/signup" class="sb-leaf"><span class="sb-leaf-dot"></span>Sign Up</a>
       </div>`;

  root.className = 'sb';
  root.innerHTML = `
    <div class="sb-brand">
      <div class="brand-logo">H</div>
      <span class="brand-name">Hive 🐝</span>
    </div>
    <nav class="sb-nav">
      ${groupsHtml}
    </nav>
    ${footer}
    <button class="sb-mobile-toggle" onclick="sbToggleMobile()">☰ Menu</button>
  `;
}

function sbToggleGroup(btn) {
  const group = btn.closest('.sb-group');
  if (group) group.classList.toggle('open');
}

function sbToggleMobile() {
  const sb = document.getElementById('sidebar-root');
  if (sb) sb.classList.toggle('sb-mobile-open');
}
