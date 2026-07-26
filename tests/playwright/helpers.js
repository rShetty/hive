// @ts-check
const { test: base, expect } = require('@playwright/test');
const { v4: uuidv4 } = require('uuid');
const path = require('path');
const fs = require('fs');

// Load .env from project root so OPENROUTER_API_KEY is available
const envPath = path.resolve(__dirname, '../../.env');
if (fs.existsSync(envPath)) {
  const lines = fs.readFileSync(envPath, 'utf-8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 0) continue;
    const key = trimmed.slice(0, eq).trim();
    const val = trimmed.slice(eq + 1).trim();
    if (!process.env[key]) process.env[key] = val;
  }
}

const HIVE_BASE = process.env.HIVE_BASE || 'http://localhost:8000';

/**
 * Register a fresh user and return { email, password, token }.
 */
async function registerAndLogin(request) {
  const uname = `pw_${uuidv4().slice(0, 8)}`;
  const email = `${uname}@example.com`;
  const password = 'PwTest123!';

  // Register
  const regRes = await request.post(`${HIVE_BASE}/api/auth/register`, {
    data: { name: uname, email, password },
  });
  if (regRes.status() !== 200) {
    throw new Error(`Register failed: ${regRes.status()} ${await regRes.text()}`);
  }

  // Login
  const loginRes = await request.post(`${HIVE_BASE}/api/auth/login`, {
    data: { email, password },
  });
  const loginBody = await loginRes.json();
  const token = loginBody.access_token;
  if (!token) throw new Error('Login failed: no token');

  return { email, password, token, uname };
}

/**
 * Deploy a hosted agent and return agent details.
 */
async function deployAgent(request, token, name = 'PW Test Agent') {
  const apiKey = process.env.OPENROUTER_API_KEY || 'sk-or-v1-fake';
  const res = await request.post(`${HIVE_BASE}/api/agents/deploy-hosted`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: 30000,
    data: {
      name,
      description: 'Playwright test agent',
      framework: 'openclaw',
      model_key: { openrouter: apiKey },
      skill_names: ['terminal', 'web_extract'],
      tags: ['e2e', 'playwright'],
    },
  });
  const body = await res.json();
  return body;
}

/**
 * Fund a user's wallet directly via SQLite using a helper Python script.
 */
async function grantTokens(email, amount = 10000) {
  const { execSync } = require('child_process');
  const path = require('path');
  const script = path.join(__dirname, 'grant_tokens.py');
  try {
    execSync(`python3 "${script}" "${email}" ${amount}`, { stdio: 'pipe', timeout: 10000 });
  } catch (err) {
    console.warn('  [warn] grantTokens failed:', err.message);
  }
}

/**
 * Wait for agent status to become active/idle.
 */
async function waitForAgent(request, token, agentId, timeout = 60000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const res = await request.get(`${HIVE_BASE}/api/agents/${agentId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status() === 200) {
      const body = await res.json();
      if (body.status === 'active' || body.status === 'idle') return body;
    }
    await new Promise(r => setTimeout(r, 2000));
  }
  throw new Error(`Agent ${agentId} did not become active within ${timeout}ms`);
}

/**
 * Get the current user's agents via API.
 */
async function getUserAgents(request, token) {
  const res = await request.get(`${HIVE_BASE}/api/agents`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (res.status() !== 200) throw new Error(`Failed to list agents: ${res.status()}`);
  const body = await res.json();
  return body.items || body;
}

module.exports = {
  HIVE_BASE,
  registerAndLogin,
  deployAgent,
  grantTokens,
  waitForAgent,
  getUserAgents,
};
