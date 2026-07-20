/**
 * Hive 🐝 — shared navigation component.
 *
 * Renders a polished, responsive top nav into any element with id="nav-root".
 * Usage:
 *   <div id="nav-root"></div>
 *   <script src="/js/nav.js"></script>
 *   <script>renderNav({ active: 'agents' });</script>
 *
 * `active` highlights one of: home | agents | skills | mcp | tasks | deploy | settings.
 */

function isHiveAuthed() {
  return !!localStorage.getItem('token');
}

function hiveLogout() {
  localStorage.removeItem('token');
  fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
  window.location.href = '/login';
}

function renderNav({ active = '', dark = false } = {}) {
  const root = document.getElementById('nav-root') || document.getElementById('nav');
  if (!root) return;

  const auth = isHiveAuthed();
  const link = (href, label, key) => {
    const isActive = key && active === key;
    const cls = isActive ? 'nav-link nav-link-active' : 'nav-link';
    return `<a href="${href}" class="${cls}">${label}</a>`;
  };

  const right = auth
    ? `<div class="nav-links">
         ${link('/skills', 'Skills', 'skills')}
         ${link('/mcp', 'MCP', 'mcp')}
         ${link('/tasks', 'Tasks', 'tasks')}
         <a href="/deploy" class="btn btn-green btn-sm">Deploy Agent</a>
         ${link('/settings', 'Settings', 'settings')}
         <button onclick="hiveLogout()" class="nav-link">Logout</button>
       </div>`
    : `<div class="nav-links">
         ${link('/login', 'Login', 'login')}
         <a href="/signup" class="btn btn-primary btn-sm">Sign Up</a>
       </div>`;

  root.className = 'nav-shell';
  root.innerHTML = `
    <div class="nav-inner">
      <a href="/" class="flex items-center space-x-2.5">
        <div class="brand-logo">H</div>
        <span class="brand-name">Hive 🐝</span>
      </a>
      <div class="flex items-center gap-2">
        <div class="nav-hide-sm">${link('/agents', 'Browse', 'agents')}</div>
        ${right}
      </div>
    </div>`;
}
