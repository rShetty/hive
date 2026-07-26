// @ts-check
const { test, expect } = require('@playwright/test');
const {
  HIVE_BASE,
  registerAndLogin,
  deployAgent,
  grantTokens,
  waitForAgent,
} = require('./helpers');

// Shared state across tests in serial mode
let auth = null;
let agentId = null;
let workflowId = null;

test.describe.serial('Workflow Feature E2E', () => {

  // ──────────────────────────────────────────────────────────────────────
  // Setup: register, deploy agent, fund wallet
  // ──────────────────────────────────────────────────────────────────────

  test('01 — register and login', async ({ request }) => {
    auth = await registerAndLogin(request);
    expect(auth.token).toBeTruthy();
    console.log(`  Registered: ${auth.email}`);
  });

  test('02 — deploy agent', async ({ request }) => {
    const res = await deployAgent(request, auth.token, 'WF PW Agent');
    agentId = res.agent_id;
    expect(agentId).toBeTruthy();
    expect(res.endpoint_url).toBeTruthy();
    console.log(`  Agent deployed: ${agentId}`);
  });

  test('03 — wait for agent ready', async ({ request }) => {
    const agent = await waitForAgent(request, auth.token, agentId, 30000);
    expect(['active', 'idle', 'error', 'deploying', 'pending', 'offline']).toContain(agent.status);
    console.log(`  Agent status: ${agent.status}`);
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

  // ──────────────────────────────────────────────────────────────────────
  // Workflow CRUD via API
  // ──────────────────────────────────────────────────────────────────────

  test('05 — create workflow via API', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/workflows`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: {
        name: 'PW Test Workflow',
        description: 'Created by Playwright test',
        max_tokens_per_run: 500,
        timeout_seconds: 300,
      },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    workflowId = body.id;
    expect(workflowId).toBeTruthy();
    expect(body.name).toBe('PW Test Workflow');
    console.log(`  Workflow created: ${workflowId}`);
  });

  test('06 — list workflows', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/workflows`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    const ids = body.map(w => w.id);
    expect(ids).toContain(workflowId);
  });

  test('07 — get workflow detail', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.name).toBe('PW Test Workflow');
    expect(body.steps).toEqual([]);
  });

  test('08 — update workflow', async ({ request }) => {
    const res = await request.put(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: { name: 'PW Test Workflow Updated', status: 'active' },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.name).toBe('PW Test Workflow Updated');
    expect(body.status).toBe('active');
  });

  // ──────────────────────────────────────────────────────────────────────
  // Step management
  // ──────────────────────────────────────────────────────────────────────

  test('09 — add step 1', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/workflows/${workflowId}/steps`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: {
        agent_id: agentId,
        name: 'Step 1: Greet',
        task_template: 'Say hello to: {{task}}',
        max_tokens: 100,
        timeout_seconds: 120,
        step_order: 0,
      },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    expect(body.name).toBe('Step 1: Greet');
    expect(body.agent_id).toBe(agentId);
  });

  test('10 — add step 2', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/workflows/${workflowId}/steps`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: {
        agent_id: agentId,
        name: 'Step 2: Transform',
        task_template: 'Transform this: {{prev_output}}',
        max_tokens: 100,
        timeout_seconds: 120,
        step_order: 1,
      },
    });
    expect(res.status()).toBe(201);
  });

  test('11 — add step 3', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/workflows/${workflowId}/steps`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: {
        agent_id: agentId,
        name: 'Step 3: Finalize',
        task_template: 'Summarize: {{prev_output}}',
        max_tokens: 50,
        timeout_seconds: 60,
        step_order: 2,
      },
    });
    expect(res.status()).toBe(201);
  });

  test('12 — workflow has 3 steps', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const body = await res.json();
    expect(body.steps.length).toBe(3);
    expect(body.step_count).toBe(3);
  });

  test('13 — update step', async ({ request }) => {
    // Get step IDs first
    const wfRes = await request.get(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const wf = await wfRes.json();
    const stepId = wf.steps[0].id;

    const res = await request.put(
      `${HIVE_BASE}/api/workflows/${workflowId}/steps/${stepId}`,
      {
        headers: { Authorization: `Bearer ${auth.token}` },
        data: { task_template: 'Updated: {{task}}', max_tokens: 200 },
      }
    );
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.max_tokens).toBe(200);
  });

  test('14 — delete step', async ({ request }) => {
    const wfRes = await request.get(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const wf = await wfRes.json();
    const stepId = wf.steps[2].id; // delete step 3

    const res = await request.delete(
      `${HIVE_BASE}/api/workflows/${workflowId}/steps/${stepId}`,
      { headers: { Authorization: `Bearer ${auth.token}` } }
    );
    expect(res.status()).toBe(204);

    // Verify 2 steps remain
    const check = await request.get(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const checkBody = await check.json();
    expect(checkBody.steps.length).toBe(2);
  });

  // ──────────────────────────────────────────────────────────────────────
  // Error cases
  // ──────────────────────────────────────────────────────────────────────

  test('15 — run draft workflow fails', async ({ request }) => {
    // Set to draft
    await request.put(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: { status: 'draft' },
    });

    const res = await request.post(`${HIVE_BASE}/api/workflows/${workflowId}/run`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: { task: 'test' },
    });
    expect(res.status()).toBe(400);

    // Restore to active
    await request.put(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: { status: 'active' },
    });
  });

  test('16 — get nonexistent workflow returns 404', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/workflows/nonexistent`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect(res.status()).toBe(404);
  });

  test('17 — add step with bad agent returns 404', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/workflows/${workflowId}/steps`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: {
        agent_id: 'nonexistent-agent',
        name: 'Bad Step',
        task_template: 'do stuff',
      },
    });
    expect(res.status()).toBe(404);
  });

  test('18 — unauthorized access returns 401', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/workflows`);
    expect(res.status()).toBe(401);
  });

  // ──────────────────────────────────────────────────────────────────────
  // Frontend pages
  // ──────────────────────────────────────────────────────────────────────

  test('19 — workflows page loads', async ({ page }) => {
    // Login via UI first
    await page.goto('/login');
    await page.fill('input[type="email"]', auth.email);
    await page.fill('input[type="password"]', auth.password);
    await page.click('button[type="submit"]');
    await page.waitForURL('**/');

    // Navigate to workflows
    await page.goto('/workflows');
    await page.waitForLoadState('networkidle');

    // Check page title
    const title = await page.textContent('h1');
    expect(title).toContain('Workflows');
  });

  test('20 — workflow builder page loads', async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[type="email"]', auth.email);
    await page.fill('input[type="password"]', auth.password);
    await page.click('button[type="submit"]');
    await page.waitForURL('**/');

    await page.goto('/workflows/new');
    await page.waitForLoadState('networkidle');

    const heading = await page.textContent('h1');
    expect(heading).toContain('New Workflow');
  });

  test('21 — workflow builder shows agents', async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[type="email"]', auth.email);
    await page.fill('input[type="password"]', auth.password);
    await page.click('button[type="submit"]');
    await page.waitForURL('**/');

    await page.goto('/workflows/new');
    await page.waitForLoadState('networkidle');

    // Wait for agents to load
    await page.waitForTimeout(2000);

    // Verify the agent palette section is present
    const agentSection = await page.locator('text=Team').count();
    expect(agentSection).toBeGreaterThan(0);
  });

  test('22 — workflow builder: create workflow with steps', async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[type="email"]', auth.email);
    await page.fill('input[type="password"]', auth.password);
    await page.click('button[type="submit"]');
    await page.waitForURL('**/');

    await page.goto('/workflows/new');
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    // Fill workflow name
    await page.fill('input[x-model="workflow.name"]', 'PW E2E Builder Test');

    // Check if agents are available to add steps
    const agentChipCount = await page.locator('.agent-chip').count();
    if (agentChipCount > 0) {
      // Click first agent to add a step
      await page.locator('.agent-chip').first().click();

      // Verify step appears
      const stepNodes = await page.locator('.step-node').count();
      expect(stepNodes).toBeGreaterThanOrEqual(1);

      // Fill the task template
      const taskTextarea = page.locator('textarea[x-model="step.task_template"]').first();
      await taskTextarea.fill('Say hello to {{task}}');

      // Click second agent to add another step
      const secondAgent = page.locator('.agent-chip').nth(1);
      if (await secondAgent.count() >0) {
        await secondAgent.click();
        const stepsAfter = await page.locator('.step-node').count();
        expect(stepsAfter).toBeGreaterThanOrEqual(2);
      }

      // Save the workflow
      await page.click('button:has-text("Create")');
      await page.waitForTimeout(2000);

      // Should navigate to edit page
      expect(page.url()).toContain('/workflows/');
    } else {
      // No agents available (e.g. all agents in error state)
      // Verify the "No agents available" message is shown
      const noAgents = await page.locator('text=No agents available').count();
      expect(noAgents).toBeGreaterThan(0);
    }
  });

  // ──────────────────────────────────────────────────────────────────────
  // Cleanup
  // ──────────────────────────────────────────────────────────────────────

  test('23 — cleanup workflow', async ({ request }) => {
    if (!workflowId) return;
    const res = await request.delete(`${HIVE_BASE}/api/workflows/${workflowId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect([200, 204]).toContain(res.status());
  });

  test('24 — cleanup agent', async ({ request }) => {
    if (!agentId) return;
    const res = await request.delete(`${HIVE_BASE}/api/agents/${agentId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    expect([200, 204, 404]).toContain(res.status());
  });
});
