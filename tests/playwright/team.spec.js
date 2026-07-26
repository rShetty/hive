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

// ──── Shared state for API tests ────
let auth = null;
let agent1 = null, agent2 = null, agent3 = null;
let teamId = null;
let teamRunId = null;

async function deployWithRetry(request, token, name, retries = 3) {
  for (let i = 0; i < retries; i++) {
    try {
      const res = await deployAgent(request, token, name);
      if (res.agent_id) return res;
    } catch (e) {
      if (i === retries - 1) throw e;
      await new Promise(r => setTimeout(r, 3000));
    }
  }
  throw new Error(`Failed to deploy ${name} after ${retries} retries`);
}

// ──────────────────────────────────────────────────────────────────────────
// 1. Backend harness: Team CRUD + run lifecycle
// ──────────────────────────────────────────────────────────────────────────

test.describe.serial('Team Backend Harness — CRUD + Run', () => {

  test('01 — register and login', async ({ request }) => {
    auth = await registerAndLogin(request);
    expect(auth.token).toBeTruthy();
    console.log(`  Registered: ${auth.email}`);
  });

  test('02 — deploy 3 agents', async ({ request }) => {
    try {
      agent1 = await deployWithRetry(request, auth.token, 'Team Lead Agent');
      expect(agent1.agent_id).toBeTruthy();
      console.log(`  Agent 1 (Lead): ${agent1.agent_id}`);

      agent2 = await deployWithRetry(request, auth.token, 'Senior Agent');
      expect(agent2.agent_id).toBeTruthy();
      console.log(`  Agent 2 (Senior): ${agent2.agent_id}`);

      agent3 = await deployWithRetry(request, auth.token, 'Junior Agent');
      expect(agent3.agent_id).toBeTruthy();
      console.log(`  Agent 3 (Junior): ${agent3.agent_id}`);
    } catch (e) {
      console.log(`  Deploy failed (${e.message}), trying existing agents...`);
      const agents = await getUserAgents(request, auth.token);
      if (agents.length >= 3) {
        agent1 = { agent_id: agents[0].id };
        agent2 = { agent_id: agents[1].id };
        agent3 = { agent_id: agents[2].id };
        console.log(`  Using existing agents: ${agents[0].name}, ${agents[1].name}, ${agents[2].name}`);
      } else {
        throw new Error(`Not enough agents (need 3, have ${agents.length})`);
      }
    }
  });

  test('03 — wait for agents', async ({ request }) => {
    for (const a of [agent1, agent2, agent3]) {
      try {
        const agent = await waitForAgent(request, auth.token, a.agent_id, 30000);
        console.log(`  Agent ${a.agent_id.slice(0, 8)}: ${agent.status}`);
      } catch (e) {
        console.log(`  Agent ${a.agent_id.slice(0, 8)} wait skipped: ${e.message}`);
      }
    }
    console.log('  Agents checked');
  });

  test('04 — fund wallet', async ({ request }) => {
    grantTokens(auth.email, 10000);
    const res = await request.get(`${HIVE_BASE}/api/wallet/balance`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const body = await res.json();
    expect(body.balance).toBeGreaterThanOrEqual(5000);
    console.log(`  Wallet balance: ${body.balance}`);
  });

  test('05 — list teams (empty)', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/teams/`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(Array.isArray(body)).toBeTruthy();
    console.log(`  Teams: ${body.length}`);
  });

  test('06 — create team', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/teams/`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: {
        name: 'PW Test Team',
        description: 'Created by Playwright test',
        root_agent_id: agent1.agent_id,
        max_depth: 3,
        members: [
          { agent_id: agent1.agent_id, role: 'lead' },
          { agent_id: agent2.agent_id, role: 'senior' },
          { agent_id: agent3.agent_id, role: 'junior', reports_to_agent_id: null },
        ],
      },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    teamId = body.id;
    expect(teamId).toBeTruthy();
    expect(body.name).toBe('PW Test Team');
    expect(body.root_agent_name).toBe('Team Lead Agent');
    expect(body.members).toHaveLength(3);
    console.log(`  Team created: ${teamId}`);
  });

  test('07 — get team detail', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/teams/${teamId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.name).toBe('PW Test Team');
    expect(body.description).toBe('Created by Playwright test');
    expect(body.max_depth).toBe(3);
    expect(body.members).toHaveLength(3);
    const roles = body.members.map(m => m.role);
    expect(roles).toContain('lead');
    expect(roles).toContain('senior');
    expect(roles).toContain('junior');
    console.log(`  Members: ${body.members.map(m => `${m.agent_name}(${m.role})`).join(', ')}`);
  });

  test('08 — list teams (has one)', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/teams/`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveLength(1);
    expect(body[0].id).toBe(teamId);
    expect(body[0].name).toBe('PW Test Team');
    expect(body[0].member_count).toBe(3);
    console.log(`  Team listed: ${body[0].name}`);
  });

  test('09 — update team', async ({ request }) => {
    const res = await request.patch(`${HIVE_BASE}/api/teams/${teamId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: { name: 'PW Test Team v2', description: 'Updated' },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.name).toBe('PW Test Team v2');
    expect(body.description).toBe('Updated');
    console.log(`  Team updated: ${body.name}`);
  });

  test('10 — run team', async ({ request }) => {
    // Ensure agents are ready before running
    for (const a of [agent1, agent2, agent3]) {
      try {
        await waitForAgent(request, auth.token, a.agent_id, 15000);
      } catch (_) {}
    }
    const res = await request.post(`${HIVE_BASE}/api/teams/${teamId}/run`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: { task: 'Build a user management API' },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    teamRunId = body.id;
    expect(teamRunId).toBeTruthy();
    expect(body.status).toBe('running');
    expect(body.task).toBe('Build a user management API');
    console.log(`  Team run started: ${teamRunId}`);
  });

  test('11 — get team run detail', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/teams/${teamId}/runs/${teamRunId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.id).toBe(teamRunId);
    expect(body.status).toBeTruthy();
    expect(body.task).toBe('Build a user management API');
    expect(body.delegations).toBeDefined();
    console.log(`  Run status: ${body.status}, delegations: ${body.delegations.length}`);
  });

  test('12 — list team runs', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/teams/${teamId}/runs`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.length).toBeGreaterThanOrEqual(1);
    const match = body.find(r => r.id === teamRunId);
    expect(match).toBeTruthy();
    console.log(`  Team runs: ${body.length}`);
  });

  test('13 — create team with bad agent fails', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/teams/`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: {
        name: 'Bad Team',
        root_agent_id: '00000000-0000-0000-0000-000000000000',
        members: [],
      },
    });
    expect(res.status()).toBe(404);
    console.log(`  Bad agent rejected: ${res.status()}`);
  });

  test('14 — delete team', async ({ request }) => {
    const res = await request.delete(`${HIVE_BASE}/api/teams/${teamId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(204);

    const check = await request.get(`${HIVE_BASE}/api/teams/${teamId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(check.status()).toBe(404);
    console.log(`  Team deleted`);
  });
});


// ──────────────────────────────────────────────────────────────────────────
// 2. UI tests: Teams list page, team detail page, org chart, run modal
// ──────────────────────────────────────────────────────────────────────────

async function uiLogin(page, email, password, authObj) {
  await page.goto('/login');
  await page.fill('input[type="email"]', email);
  await page.fill('input[type="password"]', password);
  await page.click('button[type="submit"]');
  const navigated = await page.waitForFunction(
    () => !window.location.pathname.includes('/login'),
    { timeout: 10000 }
  ).then(() => true).catch(() => false);
  if (!navigated) {
    try {
      const loginRes = await page.request.post(`${HIVE_BASE}/api/auth/login`, {
        data: { email, password },
      });
      if (loginRes.ok()) {
        const loginBody = await loginRes.json();
        if (loginBody.access_token && authObj) {
          authObj.token = loginBody.access_token;
        }
      }
    } catch (_) {}
    await page.goto('/login');
    await page.fill('input[type="email"]', email);
    await page.fill('input[type="password"]', password);
    await page.click('button[type="submit"]');
    await page.waitForFunction(
      () => !window.location.pathname.includes('/login'),
      { timeout: 10000 }
    );
  }
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(500);
}

test.describe.serial('Team UI — List, Detail, Org Chart, Run Modal', () => {

  let uiAuth = null;
  let uiAgent1 = null, uiAgent2 = null, uiAgent3 = null;
  let uiTeamId = null;

  // ── Setup ───────────────────────────────────────────────────────────────

  test('U01 — register + deploy 3 agents + fund', async ({ request }) => {
    uiAuth = await registerAndLogin(request);
    grantTokens(uiAuth.email, 50000);

    try {
      const a1 = await deployAgent(request, uiAuth.token, 'UI Team Lead');
      uiAgent1 = a1.agent_id;
      const a2 = await deployAgent(request, uiAuth.token, 'UI Senior Dev');
      uiAgent2 = a2.agent_id;
      const a3 = await deployAgent(request, uiAuth.token, 'UI Junior Dev');
      uiAgent3 = a3.agent_id;
      await waitForAgent(request, uiAuth.token, uiAgent1, 30000);
      await waitForAgent(request, uiAuth.token, uiAgent2, 30000);
      await waitForAgent(request, uiAuth.token, uiAgent3, 30000);
    } catch (e) {
      const agents = await getUserAgents(request, uiAuth.token);
      if (agents.length >= 3) {
        uiAgent1 = agents[0].id;
        uiAgent2 = agents[1].id;
        uiAgent3 = agents[2].id;
      } else {
        throw new Error(`Not enough agents (${agents.length})`);
      }
    }
  });

  test('U02 — create team via API for UI testing', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/teams/`, {
      headers: { Authorization: `Bearer ${uiAuth.token}` },
      data: {
        name: 'UI Test Team',
        description: 'For Playwright UI tests',
        root_agent_id: uiAgent1,
        max_depth: 3,
        members: [
          { agent_id: uiAgent1, role: 'lead' },
          { agent_id: uiAgent2, role: 'senior' },
          { agent_id: uiAgent3, role: 'junior', reports_to_agent_id: null },
        ],
      },
    });
    expect(res.status()).toBe(201);
    uiTeamId = (await res.json()).id;
  });

  // ── Teams list page ─────────────────────────────────────────────────────

  test('U10 — /teams page loads', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto('/teams');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
  });

  test('U11 — teams page shows team card', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto('/teams');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
    const cards = page.locator('[data-testid="team-card"]');
    await expect(cards.first()).toBeVisible({ timeout: 15000 });
    const count = await cards.count();
    expect(count).toBeGreaterThanOrEqual(1);
    console.log(`  Team cards: ${count}`);
  });

  test('U12 — team card shows name and member count', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto('/teams');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
    const card = page.locator('[data-testid="team-card"]').first();
    await expect(card.locator('[data-testid="team-name"]')).toBeVisible({ timeout: 10000 });
    await expect(card.locator('[data-testid="member-count"]')).toBeVisible();
    const name = await card.locator('[data-testid="team-name"]').textContent();
    expect(name).toBeTruthy();
    console.log(`  First card name: ${name}`);
  });

  test('U13 — teams page has "New Team" button', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto('/teams');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
    const btn = page.locator('[data-testid="create-team-btn"]');
    await expect(btn).toBeVisible({ timeout: 10000 });
  });

  test('U14 — clicking team card navigates to detail', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto('/teams');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
    await page.locator('[data-testid="team-card"]').first().click();
    await page.waitForLoadState('networkidle');

    expect(page.url()).toContain('/teams/');
    expect(page.url()).not.toBe('/teams/new');
  });

  // ── Team detail page ────────────────────────────────────────────────────

  test('U20 — team detail page loads', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(3000);

    const title = page.locator('[data-testid="team-title"]');
    await expect(title).toBeVisible({ timeout: 10000 });
    const text = await title.textContent();
    expect(text).toContain('UI Test Team');
    console.log(`  Team title: ${text}`);
  });

  test('U21 — detail page shows description', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    const desc = page.locator('[data-testid="team-description"]');
    await expect(desc).toBeVisible({ timeout: 10000 });
    const text = await desc.textContent();
    expect(text).toContain('For Playwright UI tests');
  });

  test('U22 — detail page shows root agent', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    const root = page.locator('[data-testid="root-agent"]');
    await expect(root).toBeVisible({ timeout: 10000 });
    const text = await root.textContent();
    expect(text).toBeTruthy();
    console.log(`  Root agent: ${text}`);
  });

  test('U23 — detail page shows member count', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    const count = page.locator('[data-testid="member-count"]');
    await expect(count).toBeVisible({ timeout: 10000 });
    const text = await count.textContent();
    expect(text).toBe('3');
  });

  // ── Org chart ───────────────────────────────────────────────────────────

  test('U30 — org chart shows 3 members', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    const chart = page.locator('[data-testid="org-chart"]');
    await expect(chart).toBeVisible({ timeout: 10000 });
    const members = chart.locator('[data-testid="member-name"]');
    await expect(members.first()).toBeVisible({ timeout: 10000 });
    const count = await members.count();
    expect(count).toBe(3);
    console.log(`  Org chart members: ${count}`);
  });

  test('U31 — org chart member shows role', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    const roles = page.locator('[data-testid="member-role"]');
    await expect(roles.first()).toBeVisible({ timeout: 10000 });
    const count = await roles.count();
    expect(count).toBe(3);
    const roleTexts = [];
    for (let i = 0; i < count; i++) {
      roleTexts.push(await roles.nth(i).textContent());
    }
    expect(roleTexts).toContain('lead');
    expect(roleTexts).toContain('senior');
    expect(roleTexts).toContain('junior');
    console.log(`  Roles: ${roleTexts.join(', ')}`);
  });

  test('U32 — org chart shows hierarchy indentation', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    const chart = page.locator('[data-testid="org-chart"]');
    await expect(chart).toBeVisible({ timeout: 10000 });
    const members = chart.locator('.member-node');
    await expect(members.first()).toBeVisible({ timeout: 10000 });
    const count = await members.count();
    expect(count).toBe(3);
    const hasIndented = await members.nth(2).evaluate(el => {
      const ml = el.style.marginLeft;
      return ml && ml !== '0px' && ml !== '0';
    });
    console.log(`  Has indented member: ${hasIndented}`);
  });

  // ── Run modal ───────────────────────────────────────────────────────────

  test('U40 — "Run Team" button opens modal', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    await page.click('[data-testid="run-team-btn"]');
    await expect(page.locator('[data-testid="run-task-input"]')).toBeVisible();
    console.log(`  Run modal opened`);
  });

  test('U41 — run modal has task textarea', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    await page.click('[data-testid="run-team-btn"]');
    const textarea = page.locator('[data-testid="run-task-input"]');
    await expect(textarea).toBeVisible();
    await textarea.fill('Test task from Playwright');
    expect(await textarea.inputValue()).toBe('Test task from Playwright');
  });

  test('U42 — run modal has cancel button', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    await page.click('[data-testid="run-team-btn"]');
    const cancelBtn = page.locator('[data-testid="cancel-run-btn"]');
    await expect(cancelBtn).toBeVisible();
    await cancelBtn.click();
    await expect(page.locator('[data-testid="run-task-input"]')).not.toBeVisible();
  });

  test('U43 — run modal confirm button disabled when empty', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    await page.click('[data-testid="run-team-btn"]');
    const confirmBtn = page.locator('[data-testid="confirm-run-btn"]');
    await expect(confirmBtn).toBeVisible();
    await expect(confirmBtn).toBeDisabled();
  });

  test('U44 — run modal confirm button enabled when task filled', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto(`/teams/${uiTeamId}`);
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[data-testid="team-title"]')).toBeVisible({ timeout: 15000 });
    await page.click('[data-testid="run-team-btn"]');
    await page.locator('[data-testid="run-task-input"]').fill('Build API');
    const confirmBtn = page.locator('[data-testid="confirm-run-btn"]');
    await expect(confirmBtn).toBeEnabled();
  });

  // ── Sidebar ─────────────────────────────────────────────────────────────

  test('U50 — sidebar shows Teams link', async ({ page }) => {
    await uiLogin(page, uiAuth.email, uiAuth.password, uiAuth);
    await page.goto('/teams');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
    const teamsLink = page.locator('a[href="/teams"]').first();
    await expect(teamsLink).toBeVisible();
  });

  // ── Empty state ─────────────────────────────────────────────────────────

  test('U60 — empty teams page shows empty state', async ({ request, page }) => {
    // Create a fresh user with no teams
    const fresh = await registerAndLogin(request);
    await uiLogin(page, fresh.email, fresh.password, fresh);
    await page.goto('/teams');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('h1:has-text("Teams")')).toBeVisible({ timeout: 15000 });
    const empty = page.locator('[data-testid="empty-state"]');
    await expect(empty).toBeVisible({ timeout: 15000 });
    console.log(`  Empty state shown`);
  });

  // ── Cleanup ─────────────────────────────────────────────────────────────

  test('U99 — cleanup: delete team', async ({ request }) => {
    const res = await request.delete(`${HIVE_BASE}/api/teams/${uiTeamId}`, {
      headers: { Authorization: `Bearer ${uiAuth.token}` },
    });
    expect(res.status()).toBe(204);
    console.log(`  Cleaned up team`);
  });
});
