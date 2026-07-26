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

// ── Shared state across serial tests ──────────────────────────────────────
let auth = null;
let rootAgent = null;
let workerAgent = null;
let teamId = null;
let teamRunId = null;

// ──────────────────────────────────────────────────────────────────────────
// Test: Agent handles task itself (no delegation needed)
// Scenario: Simple 1+1=2 calculation that agent can handle directly
// ──────────────────────────────────────────────────────────────────────────

test.describe.serial('Team Delegation — Self-Handle + A2A Delegation', () => {

  test('01 — register and login', async ({ request }) => {
    auth = await registerAndLogin(request);
    expect(auth.token).toBeTruthy();
    console.log(`  Registered: ${auth.email}`);
  });

  test('02 — deploy root agent', async ({ request }) => {
    const res = await deployAgent(request, auth.token, 'Root Orchestrator');
    rootAgent = res;
    expect(res.agent_id).toBeTruthy();
    console.log(`  Root agent: ${res.agent_id}`);
  });

  test('03 — deploy worker agent', async ({ request }) => {
    const res = await deployAgent(request, auth.token, 'Worker Agent');
    workerAgent = res;
    expect(res.agent_id).toBeTruthy();
    console.log(`  Worker agent: ${res.agent_id}`);
  });

  test('04 — wait for agents to be active', async ({ request }) => {
    const a = await waitForAgent(request, auth.token, rootAgent.agent_id, 60000);
    const b = await waitForAgent(request, auth.token, workerAgent.agent_id, 60000);
    expect(a.status).toBeTruthy();
    expect(b.status).toBeTruthy();
    console.log(`  Root: ${a.status}, Worker: ${b.status}`);
  });

  test('05 — fund wallet', async ({ request }) => {
    grantTokens(auth.email, 50000);
    const res = await request.get(`${HIVE_BASE}/api/wallet/balance`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const body = await res.json();
    expect(body.balance).toBeGreaterThanOrEqual(10000);
    console.log(`  Wallet balance: ${body.balance}`);
  });

  test('06 — create team with root + worker', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/teams/`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: {
        name: 'Delegation Test Team',
        description: 'Team for testing delegation flow',
        root_agent_id: rootAgent.agent_id,
        max_depth: 3,
        members: [
          { agent_id: rootAgent.agent_id, role: 'lead' },
          { agent_id: workerAgent.agent_id, role: 'worker' },
        ],
      },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    teamId = body.id;
    expect(teamId).toBeTruthy();
    console.log(`  Team created: ${teamId}`);
  });

  // ── Test 1: Agent handles task itself (no delegation) ──────────────────

  test('07 — run team: agent handles 1+1 itself', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/teams/${teamId}/run`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: { task: 'What is 1+1? Reply with just the number.' },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    teamRunId = body.id;
    expect(teamRunId).toBeTruthy();
    expect(body.status).toBe('running');
    console.log(`  Team run started: ${teamRunId}`);
  });

  test('08 — team run completes with result', async ({ request }) => {
    // Poll until run completes (max 120s)
    const deadline = Date.now() + 120_000;
    let finalStatus = 'running';
    let result = null;
    
    while (Date.now() < deadline) {
      try {
        const res = await request.get(`${HIVE_BASE}/api/teams/${teamId}/runs/${teamRunId}`, {
          headers: { Authorization: `Bearer ${auth.token}` },
          timeout: 30000,
        });
        const body = await res.json();
        finalStatus = body.status;
        result = body;
        
        if (finalStatus === 'completed' || finalStatus === 'failed') break;
      } catch (e) {
        // Transient connection error — retry
      }
      await new Promise(r => setTimeout(r, 5000));
    }
    
    console.log(`  Final status: ${finalStatus}`);
    expect(finalStatus).toBe('completed');
    
    // Check that output contains "2" (the answer to 1+1)
    const tree = result.delegation_tree || {};
    const values = Object.values(tree);
    expect(values.length).toBeGreaterThanOrEqual(1);
    const output = values[0]?.result?.output || '';
    console.log(`  Result: ${output.substring(0, 200)}`);
    expect(output).toContain('2');
  });

  test('09 — team run has delegations recorded', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/teams/${teamId}/runs/${teamRunId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const body = await res.json();
    expect(body.delegations).toBeDefined();
    console.log(`  Delegations count: ${body.delegations.length}`);
  });

  // ── Test 2: Agent delegates to another agent ──────────────────────────

  test('10 — run team: agent delegates calculation to worker', async ({ request }) => {
    const res = await request.post(`${HIVE_BASE}/api/teams/${teamId}/run`, {
      headers: { Authorization: `Bearer ${auth.token}` },
      data: { task: 'Calculate 2+2. Delegate this calculation to the worker agent and return the result.' },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    teamRunId = body.id;
    expect(teamRunId).toBeTruthy();
    console.log(`  Delegation run started: ${teamRunId}`);
  });

  test('11 — delegation run completes with delegated result', async ({ request }) => {
    const deadline = Date.now() + 180_000;
    let finalStatus = 'running';
    let result = null;
    
    while (Date.now() < deadline) {
      try {
        const res = await request.get(`${HIVE_BASE}/api/teams/${teamId}/runs/${teamRunId}`, {
          headers: { Authorization: `Bearer ${auth.token}` },
          timeout: 30000,
        });
        const body = await res.json();
        finalStatus = body.status;
        result = body;
        
        if (finalStatus === 'completed' || finalStatus === 'failed') break;
      } catch (e) {
        // Transient connection error — retry
      }
      await new Promise(r => setTimeout(r, 5000));
    }
    
    console.log(`  Final status: ${finalStatus}`);
    expect(finalStatus).toBe('completed');
    
    const tree = result.delegation_tree || {};
    console.log(`  Delegation tree: ${JSON.stringify(tree).substring(0, 500)}`);
  });

  test('12 — check delegation tree has sub-delegations', async ({ request }) => {
    const res = await request.get(`${HIVE_BASE}/api/teams/${teamId}/runs/${teamRunId}`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    const body = await res.json();
    
    // Should have at least 2 delegations (root + sub)
    console.log(`  Total delegations: ${body.delegations.length}`);
    expect(body.delegations.length).toBeGreaterThanOrEqual(2);
    
    // Check that there's a delegation to the worker agent
    const workerDelegation = body.delegations.find(d => d.agent_id === workerAgent.agent_id);
    expect(workerDelegation).toBeTruthy();
    console.log(`  Worker delegation status: ${workerDelegation.status}`);
    expect(workerDelegation.status).toBe('completed');
  });

  // ── Cleanup ────────────────────────────────────────────────────────────

  test('99 — cleanup team', async ({ request }) => {
    if (teamId) {
      const res = await request.delete(`${HIVE_BASE}/api/teams/${teamId}`, {
        headers: { Authorization: `Bearer ${auth.token}` },
      });
      expect([200, 204]).toContain(res.status());
      console.log(`  Team deleted`);
    }
  });
});
