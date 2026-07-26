// @ts-check
const { test, expect } = require('@playwright/test');
const {
  HIVE_BASE,
  registerAndLogin,
  deployAgent,
  grantTokens,
  waitForAgent,
  getUserAgents,
} = require('./helpers');

// ──── Shared state (populated by master setup) ────
const S = {
  userA: null, userB: null, userC: null,
  agentsA: [], agentsB: [], agentsC: [],
};

async function createAndLogin(request, name, email, password = 'Test123!') {
  const { execSync } = require('child_process');
  execSync(`/Users/rshetty/hive/backend/venv/bin/python3 /Users/rshetty/hive/tests/playwright/create_user.py "${name}" "${email}" "${password}"`, { encoding: 'utf-8' });
  const loginRes = await request.post(`${HIVE_BASE}/api/auth/login`, { data: { email, password } });
  if (!loginRes.ok()) throw new Error(`Login failed for ${email}: ${loginRes.status()}`);
  const body = await loginRes.json();
  return { email, password, token: body.access_token, id: body.user_id };
}

async function uiLogin(page, email, password) {
  await page.goto('/login');
  await page.waitForLoadState('networkidle');
  await page.fill('input[type="email"]', email);
  await page.fill('input[type="password"]', password);
  await page.click('button[type="submit"]');
  await page.waitForFunction(() => !window.location.pathname.includes('/login'), { timeout: 15000 });
  await page.waitForLoadState('networkidle');
}

test.describe.serial('Team Comprehensive', () => {

// ──────────────────────────────────────────────────────────────────
// MASTER SETUP
// ──────────────────────────────────────────────────────────────────

test('MASTER — Create 3 users with agents', async ({ request }) => {
  const ts = Date.now();
  S.userA = await createAndLogin(request, 'TeamTestA', `teamtesta_${ts}@test.example.com`);
  S.userB = await createAndLogin(request, 'TeamTestB', `teamtestb_${ts}@test.example.com`);
  S.userC = await createAndLogin(request, 'TeamTestC', `teamtestc_${ts}@test.example.com`);
  grantTokens(S.userA.email, 100000);
  grantTokens(S.userB.email, 100000);
  grantTokens(S.userC.email, 100000);

  for (const [user, agents, names] of [
    [S.userA, S.agentsA, ['A-Alpha', 'A-Beta', 'A-Gamma']],
    [S.userB, S.agentsB, ['B-Alpha', 'B-Beta']],
    [S.userC, S.agentsC, ['C-Alpha', 'C-Beta', 'C-Gamma']],
  ]) {
    for (const name of names) {
      try {
        const r = await deployAgent(request, user.token, name);
        agents.push(r.agent_id);
      } catch (_) {}
    }
    if (agents.length < names.length) {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const existing = await getUserAgents(request, user.token);
          while (agents.length < names.length && agents.length < existing.length) {
            agents.push(existing[agents.length].id);
          }
          break;
        } catch (_) {
          if (attempt === 2) throw new Error(`Failed to get agents for ${user.email}`);
          await new Promise(r => setTimeout(r, 2000));
        }
      }
    }
  }
  expect(S.agentsA.length).toBeGreaterThanOrEqual(3);
  expect(S.agentsB.length).toBeGreaterThanOrEqual(2);
  expect(S.agentsC.length).toBeGreaterThanOrEqual(3);
});

// ──────────────────────────────────────────────────────────────────
// API — Authorization & Ownership (A02-A10)
// ──────────────────────────────────────────────────────────────────

let teamA = null;

test('A02 — user A creates team', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userA.token}` },
    data: { name: 'Team Auth-A', root_agent_id: S.agentsA[0], members: [
      { agent_id: S.agentsA[0], role: 'lead' }, { agent_id: S.agentsA[1], role: 'member' },
    ]},
  });
  expect(res.status()).toBe(201);
  teamA = (await res.json()).id;
});

test('A03 — user B cannot read user A team', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/${teamA}`, {
    headers: { Authorization: `Bearer ${S.userB.token}` },
  });
  expect(res.status()).toBe(404);
});

test('A04 — user B cannot update user A team', async ({ request }) => {
  const res = await request.patch(`${HIVE_BASE}/api/teams/${teamA}`, {
    headers: { Authorization: `Bearer ${S.userB.token}` },
    data: { name: 'Hacked' },
  });
  expect(res.status()).toBe(404);
});

test('A05 — user B cannot delete user A team', async ({ request }) => {
  const res = await request.delete(`${HIVE_BASE}/api/teams/${teamA}`, {
    headers: { Authorization: `Bearer ${S.userB.token}` },
  });
  expect(res.status()).toBe(404);
});

test('A06 — user B cannot run user A team', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/${teamA}/run`, {
    headers: { Authorization: `Bearer ${S.userB.token}` },
    data: { task: 'Do something' },
  });
  expect(res.status()).toBe(404);
});

test('A07 — user B cannot list user A teams', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userB.token}` },
  });
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.find(t => t.id === teamA)).toBeFalsy();
});

test('A08 — user B cannot use user A agent as root', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userB.token}` },
    data: { name: 'Stolen Team', root_agent_id: S.agentsA[0], members: [] },
  });
  expect(res.status()).toBe(404);
});

test('A09 — unauthenticated request rejected', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/`);
  expect(res.status()).toBe(401);
});

test('A10 — cleanup', async ({ request }) => {
  await request.delete(`${HIVE_BASE}/api/teams/${teamA}`, {
    headers: { Authorization: `Bearer ${S.userA.token}` },
  });
});

// ──────────────────────────────────────────────────────────────────
// API — Validation & Edge Cases (V02-V10)
// ──────────────────────────────────────────────────────────────────

test('V02 — create team with non-existent root agent fails', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Bad Root', root_agent_id: '00000000-0000-0000-0000-000000000000', members: [] },
  });
  expect(res.status()).toBe(404);
});

test('V03 — create team with member agent not owned by user fails', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Bad Members', root_agent_id: S.agentsC[0], members: [
      { agent_id: S.agentsC[0], role: 'lead' }, { agent_id: S.agentsB[0], role: 'member' },
    ]},
  });
  expect(res.status()).toBe(400);
});

test('V04 — get non-existent team returns 404', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/00000000-0000-0000-0000-000000000000`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(404);
});

test('V05 — update non-existent team returns 404', async ({ request }) => {
  const res = await request.patch(`${HIVE_BASE}/api/teams/00000000-0000-0000-0000-000000000000`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Ghost' },
  });
  expect(res.status()).toBe(404);
});

test('V06 — delete non-existent team returns 404', async ({ request }) => {
  const res = await request.delete(`${HIVE_BASE}/api/teams/00000000-0000-0000-0000-000000000000`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(404);
});

test('V07 — create team with empty name succeeds', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: '', root_agent_id: S.agentsC[0], members: [] },
  });
  expect(res.status()).toBe(201);
  const tid = (await res.json()).id;
  await request.delete(`${HIVE_BASE}/api/teams/${tid}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
});

test('V08 — run non-existent team returns 404', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/00000000-0000-0000-0000-000000000000/run`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { task: 'Ghost run' },
  });
  expect(res.status()).toBe(404);
});

test('V09 — get runs of non-existent team returns 404', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/00000000-0000-0000-0000-000000000000/runs`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(404);
});

test('V10 — run team with empty task succeeds', async ({ request }) => {
  const createRes = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Task Validation Team', root_agent_id: S.agentsC[0], members: [
      { agent_id: S.agentsC[0], role: 'lead' },
    ]},
  });
  expect(createRes.status()).toBe(201);
  const tid = (await createRes.json()).id;
  const res = await request.post(`${HIVE_BASE}/api/teams/${tid}/run`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { task: '' },
  });
  expect(res.status()).toBe(200);
  await request.delete(`${HIVE_BASE}/api/teams/${tid}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  }).catch(() => {});
});

// ──────────────────────────────────────────────────────────────────
// API — Full CRUD Lifecycle (L02-L11)
// ──────────────────────────────────────────────────────────────────

let lcTeamId = null;

test('L02 — create team with hierarchy', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Lifecycle Team', description: 'Testing full lifecycle',
      root_agent_id: S.agentsC[0], max_depth: 5, members: [
        { agent_id: S.agentsC[0], role: 'lead' },
        { agent_id: S.agentsC[1], role: 'senior', reports_to_agent_id: null },
        { agent_id: S.agentsC[2], role: 'junior', reports_to_agent_id: null },
    ]},
  });
  expect(res.status()).toBe(201);
  const body = await res.json();
  lcTeamId = body.id;
  expect(body.name).toBe('Lifecycle Team');
  expect(body.max_depth).toBe(5);
  expect(body.members).toHaveLength(3);
});

test('L03 — get team detail verifies all fields', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/${lcTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.name).toBe('Lifecycle Team');
  expect(body.max_depth).toBe(5);
  expect(body.members).toHaveLength(3);
});

test('L04 — update team name and description', async ({ request }) => {
  const res = await request.patch(`${HIVE_BASE}/api/teams/${lcTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Lifecycle Team v2', description: 'Updated description' },
  });
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.name).toBe('Lifecycle Team v2');
  expect(body.description).toBe('Updated description');
  expect(body.members).toHaveLength(3);
});

test('L05 — update team max_depth', async ({ request }) => {
  const res = await request.patch(`${HIVE_BASE}/api/teams/${lcTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { max_depth: 2 },
  });
  expect(res.status()).toBe(200);
  expect((await res.json()).max_depth).toBe(2);
});

test('L06 — list teams shows updated team', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(200);
  const found = (await res.json()).find(t => t.id === lcTeamId);
  expect(found).toBeTruthy();
  expect(found.name).toBe('Lifecycle Team v2');
  expect(found.member_count).toBe(3);
});

test('L07 — create second team', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Second Lifecycle Team', root_agent_id: S.agentsC[1],
      members: [{ agent_id: S.agentsC[1], role: 'lead' }] },
  });
  expect(res.status()).toBe(201);
});

test('L08 — list teams shows both teams', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(200);
  expect((await res.json()).length).toBeGreaterThanOrEqual(2);
});

test('L09 — delete second team', async ({ request }) => {
  const listRes = await request.get(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  const second = (await listRes.json()).find(t => t.name === 'Second Lifecycle Team');
  if (second) {
    const res = await request.delete(`${HIVE_BASE}/api/teams/${second.id}`, {
      headers: { Authorization: `Bearer ${S.userC.token}` },
    });
    expect(res.status()).toBe(204);
  }
});

test('L10 — list teams after delete shows one team', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(200);
  expect((await res.json()).find(t => t.name === 'Second Lifecycle Team')).toBeFalsy();
});

test('L11 — cleanup', async ({ request }) => {
  await request.delete(`${HIVE_BASE}/api/teams/${lcTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
});

// ──────────────────────────────────────────────────────────────────
// API — Run Lifecycle (R01-R07)
// ──────────────────────────────────────────────────────────────────

let rTeamId = null, rRunId = null;

test('R01 — create team for run tests', async ({ request }) => {
  await waitForAgent(request, S.userC.token, S.agentsC[0], 30000).catch(() => {});
  await waitForAgent(request, S.userC.token, S.agentsC[1], 30000).catch(() => {});
  await waitForAgent(request, S.userC.token, S.agentsC[2], 30000).catch(() => {});
  const createRes = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Run Test Team', root_agent_id: S.agentsC[0], max_depth: 3, members: [
      { agent_id: S.agentsC[0], role: 'lead' },
      { agent_id: S.agentsC[1], role: 'senior' },
      { agent_id: S.agentsC[2], role: 'junior' },
    ]},
  });
  expect(createRes.status()).toBe(201);
  rTeamId = (await createRes.json()).id;
});

test('R02 — run team returns valid response', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/${rTeamId}/run`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { task: 'Implement user auth module' },
  });
  expect(res.status()).toBe(200);
  const body = await res.json();
  rRunId = body.id;
  expect(body.status).toBe('running');
  expect(body.task).toBe('Implement user auth module');
  expect(body.team_id).toBe(rTeamId);
});

test('R03 — get run detail shows delegations', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/${rTeamId}/runs/${rRunId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.id).toBe(rRunId);
  expect(body.delegations).toBeDefined();
});

test('R04 — list runs shows the run', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/${rTeamId}/runs`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.length).toBeGreaterThanOrEqual(1);
  expect(body.find(r => r.id === rRunId)).toBeTruthy();
});

test('R05 — get run for wrong team returns 404', async ({ request }) => {
  const createRes = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Other Run Team', root_agent_id: S.agentsC[1],
      members: [{ agent_id: S.agentsC[1], role: 'lead' }] },
  });
  const otherTeamId = (await createRes.json()).id;
  const res = await request.get(`${HIVE_BASE}/api/teams/${otherTeamId}/runs/${rRunId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(404);
  await request.delete(`${HIVE_BASE}/api/teams/${otherTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
});

test('R06 — non-existent run returns 404', async ({ request }) => {
  const res = await request.get(`${HIVE_BASE}/api/teams/${rTeamId}/runs/00000000-0000-0000-0000-000000000000`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
  expect(res.status()).toBe(404);
});

test('R07 — cleanup', async ({ request }) => {
  await request.delete(`${HIVE_BASE}/api/teams/${rTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
});

// ──────────────────────────────────────────────────────────────────
// UI — Auth & Error States (UI-AUTH-01 to UI-AUTH-03)
// ──────────────────────────────────────────────────────────────────

test('UI-AUTH-01 — unauthenticated /teams redirects to login', async ({ page }) => {
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);
  expect(page.url()).toContain('/login');
});

test('UI-AUTH-02 — unauthenticated /teams/{id} redirects to login', async ({ page }) => {
  await page.goto('/teams/some-fake-id');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);
  expect(page.url()).toContain('/login');
});

test('UI-AUTH-03 — authenticated /teams loads teams list', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
});

// ──────────────────────────────────────────────────────────────────
// UI — Teams List Comprehensive (UL01-UL09)
// ──────────────────────────────────────────────────────────────────

let uiTeamId = null;

test('UL01 — create team for list tests', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'UI List Team', description: 'For list page tests',
      root_agent_id: S.agentsC[0], max_depth: 3, members: [
        { agent_id: S.agentsC[0], role: 'lead' },
        { agent_id: S.agentsC[1], role: 'senior' },
        { agent_id: S.agentsC[2], role: 'junior' },
    ]},
  });
  expect(res.status()).toBe(201);
  uiTeamId = (await res.json()).id;
});

test('UL02 — teams page heading and subtitle', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('text=Organize agents into hierarchical teams')).toBeVisible({ timeout: 10000 });
});

test('UL03 — team card shows root agent name', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
  const card = page.locator('[data-testid="team-card"]').first();
  await expect(card).toBeVisible({ timeout: 15000 });
  const rootName = card.locator('[data-testid="root-agent-name"]');
  await expect(rootName).toBeVisible({ timeout: 10000 });
  expect(await rootName.textContent()).toBeTruthy();
});

test('UL04 — team card shows max depth', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
  const card = page.locator('[data-testid="team-card"]').first();
  await expect(card).toBeVisible({ timeout: 15000 });
  expect(await card.textContent()).toContain('3');
});

test('UL05 — team card shows description', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
  const card = page.locator('[data-testid="team-card"]').first();
  await expect(card).toBeVisible({ timeout: 15000 });
  expect(await card.textContent()).toContain('For list page tests');
});

test('UL06 — team card shows "Created" timestamp', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
  const card = page.locator('[data-testid="team-card"]').first();
  await expect(card).toBeVisible({ timeout: 15000 });
  expect(await card.textContent()).toContain('Created');
});

test('UL07 — teams grid container exists', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('[data-testid="teams-grid"]')).toBeVisible({ timeout: 10000 });
});

test('UL08 — clicking team card goes to /teams/{id}', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
  // Retry loading if cards not yet visible (SQLite lock can delay API)
  for (let attempt = 0; attempt < 6; attempt++) {
    const cards = page.locator('[data-testid="team-card"]');
    if (await cards.count() > 0) break;
    await page.reload();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(3000);
  }
  await page.locator('[data-testid="team-card"]').first().click();
  await page.waitForLoadState('networkidle');
  expect(page.url()).toContain('/teams/');
  expect(page.url()).not.toContain('/teams/new');
});

test('UL09 — cleanup', async ({ request }) => {
  await request.delete(`${HIVE_BASE}/api/teams/${uiTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
});

// ──────────────────────────────────────────────────────────────────
// UI — Detail Page Comprehensive (UD01-UD10)
// ──────────────────────────────────────────────────────────────────

let dTeamId = null;

test('UD01 — create team for detail tests', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Detail Test Team', description: 'For detail page tests',
      root_agent_id: S.agentsC[0], max_depth: 4, members: [
        { agent_id: S.agentsC[0], role: 'lead' },
        { agent_id: S.agentsC[1], role: 'senior' },
        { agent_id: S.agentsC[2], role: 'junior' },
    ]},
  });
  expect(res.status()).toBe(201);
  dTeamId = (await res.json()).id;
});

test('UD02 — detail page breadcrumb shows Teams', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${dTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('text=Teams').first()).toBeVisible();
});

test('UD03 — detail page shows max depth', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${dTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  const text = await page.locator('[data-testid="team-title"]').locator('..').textContent();
  expect(text).toContain('4');
});

test('UD04 — detail page shows "Run Team" button', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${dTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(2000);
  await expect(page.locator('[data-testid="run-team-btn"]')).toBeVisible({ timeout: 15000 });
});

test('UD05 — detail page shows Run History section', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${dTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('text=Run History')).toBeVisible({ timeout: 10000 });
});

test('UD06 — detail page shows "No runs yet" when empty', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${dTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('text=No runs yet')).toBeVisible({ timeout: 10000 });
});

test('UD07 — non-existent team shows "Team not found"', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams/00000000-0000-0000-0000-000000000000');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(3000);
  await expect(page.locator('text=Team not found')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('text=Back to Teams')).toBeVisible();
});

test('UD08 — "Back to Teams" link navigates to /teams', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto('/teams/00000000-0000-0000-0000-000000000000');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(3000);
  await page.locator('text=Back to Teams').click();
  await page.waitForLoadState('networkidle');
  expect(page.url()).toContain('/teams');
});

test('UD09 — org chart shows root badge on lead', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${dTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  const chart = page.locator('[data-testid="org-chart"]');
  await expect(chart).toBeVisible({ timeout: 10000 });
  await expect(chart.locator('text=root').first()).toBeVisible({ timeout: 10000 });
});

test('UD10 — cleanup', async ({ request }) => {
  await request.delete(`${HIVE_BASE}/api/teams/${dTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
});

// ──────────────────────────────────────────────────────────────────
// UI — Run Modal & Submission (UM01-UM11)
// ──────────────────────────────────────────────────────────────────

let mTeamId = null;

test('UM01 — create team for modal tests', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Modal Test Team', root_agent_id: S.agentsC[0], members: [
      { agent_id: S.agentsC[0], role: 'lead' }, { agent_id: S.agentsC[1], role: 'member' },
    ]},
  });
  expect(res.status()).toBe(201);
  mTeamId = (await res.json()).id;
});

test('UM02 — run modal opens and shows task input', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.click('[data-testid="run-team-btn"]');
  await expect(page.locator('[data-testid="run-task-input"]')).toBeVisible();
});

test('UM03 — run modal shows placeholder text', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.click('[data-testid="run-team-btn"]');
  const textarea = page.locator('[data-testid="run-task-input"]');
  await expect(textarea).toBeVisible();
  expect(await textarea.getAttribute('placeholder')).toBeTruthy();
});

test('UM04 — run modal cancel closes modal', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.click('[data-testid="run-team-btn"]');
  await expect(page.locator('[data-testid="run-task-input"]')).toBeVisible();
  await page.click('[data-testid="cancel-run-btn"]');
  await expect(page.locator('[data-testid="run-task-input"]')).not.toBeVisible();
});

test('UM05 — run modal click outside closes modal', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.click('[data-testid="run-team-btn"]');
  await expect(page.locator('[data-testid="run-task-input"]')).toBeVisible();
  await page.locator('.fixed.inset-0').click({ position: { x: 10, y: 10 } });
  await expect(page.locator('[data-testid="run-task-input"]')).not.toBeVisible({ timeout: 5000 });
});

test('UM06 — run modal confirm disabled when empty', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.click('[data-testid="run-team-btn"]');
  await expect(page.locator('[data-testid="confirm-run-btn"]')).toBeVisible();
  await expect(page.locator('[data-testid="confirm-run-btn"]')).toBeDisabled();
});

test('UM07 — run modal confirm enabled when task filled', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.click('[data-testid="run-team-btn"]');
  await page.locator('[data-testid="run-task-input"]').fill('Build authentication API');
  await expect(page.locator('[data-testid="confirm-run-btn"]')).toBeEnabled();
});

test('UM08 — submit run shows active run section', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.click('[data-testid="run-team-btn"]');
  await page.locator('[data-testid="run-task-input"]').fill('Test run from Playwright');
  await page.click('[data-testid="confirm-run-btn"]');
  await expect(page.locator('[data-testid="run-task-input"]')).not.toBeVisible({ timeout: 5000 });
  await expect(page.locator('[data-testid="run-status"]')).toBeVisible({ timeout: 10000 });
  const taskText = await page.locator('[data-testid="run-task"]').textContent();
  expect(taskText).toContain('Test run from Playwright');
});

test('UM09 — run appears in run history', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  const runItems = page.locator('[data-testid="run-item"]');
  await expect(runItems.first()).toBeVisible({ timeout: 15000 });
  expect(await runItems.count()).toBeGreaterThanOrEqual(1);
});

test('UM10 — clicking run item shows active run detail', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${mTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await page.locator('[data-testid="run-item"]').first().click();
  await expect(page.locator('[data-testid="run-status"]')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('[data-testid="run-task"]')).toBeVisible();
});

test('UM11 — cleanup', async ({ request }) => {
  await request.delete(`${HIVE_BASE}/api/teams/${mTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
});

// ──────────────────────────────────────────────────────────────────
// UI — Org Chart Colors & Hierarchy (UC01-UC04)
// ──────────────────────────────────────────────────────────────────

let cTeamId = null;

test('UC01 — create team for color tests', async ({ request }) => {
  const res = await request.post(`${HIVE_BASE}/api/teams/`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
    data: { name: 'Color Test Team', root_agent_id: S.agentsC[0], members: [
      { agent_id: S.agentsC[0], role: 'lead' },
      { agent_id: S.agentsC[1], role: 'senior' },
      { agent_id: S.agentsC[2], role: 'junior' },
    ]},
  });
  expect(res.status()).toBe(201);
  cTeamId = (await res.json()).id;
});

test('UC02 — org chart shows all 3 members with correct names', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${cTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('[data-testid="org-chart"]')).toBeVisible({ timeout: 10000 });
  const names = page.locator('[data-testid="member-name"]');
  await expect(names.first()).toBeVisible({ timeout: 10000 });
  const count = await names.count();
  expect(count).toBe(3);
  const nameTexts = [];
  for (let i = 0; i < count; i++) nameTexts.push(await names.nth(i).textContent());
  expect(nameTexts).toContain('C-Alpha');
  expect(nameTexts).toContain('C-Beta');
  expect(nameTexts).toContain('C-Gamma');
});

test('UC03 — org chart shows correct roles', async ({ page }) => {
  await uiLogin(page, S.userC.email, S.userC.password);
  await page.goto(`/teams/${cTeamId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
  const roles = page.locator('[data-testid="member-role"]');
  await expect(roles.first()).toBeVisible({ timeout: 10000 });
  const count = await roles.count();
  expect(count).toBe(3);
  const roleTexts = [];
  for (let i = 0; i < count; i++) roleTexts.push(await roles.nth(i).textContent());
  expect(roleTexts).toContain('lead');
  expect(roleTexts).toContain('senior');
  expect(roleTexts).toContain('junior');
});

test('UC04 — cleanup', async ({ request }) => {
  await request.delete(`${HIVE_BASE}/api/teams/${cTeamId}`, {
    headers: { Authorization: `Bearer ${S.userC.token}` },
  });
});

}); // Team Comprehensive
