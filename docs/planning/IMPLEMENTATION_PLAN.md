# Agent Marketplace - Implementation Plan

**Goal:** Build a marketplace where AI agents self-register with endpoint verification, humans browse/filter/deploy agents with their own model API keys.

**Architecture:** FastAPI backend + SQLite + Docker containers for agents + HTML/Tailwind frontend

---

## Phase 1: Project Setup (Tasks 1-3)

### Task 1: Create Project Structure
```bash
mkdir -p /home/hermes/projects/agent-marketplace
mkdir -p /home/hermes/projects/agent-marketplace/backend/{models,routers,services,tests}
mkdir -p /home/hermes/projects/agent-marketplace/frontend/{css,js}
mkdir -p /home/hermes/projects/agent-marketplace/docker
```

### Task 2: Backend Dependencies
Create `backend/requirements.txt`:
```
fastapi==0.109.0
uvicorn[standard]==0.27.0
sqlalchemy==2.0.25
alembic==1.13.1
pydantic==2.5.3
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
python-multipart==0.0.6
docker==7.0.0
cryptography==42.0.2
aiohttp==3.9.1
pytest==7.4.4
pytest-asyncio==0.23.3
httpx==0.26.0
```

### Task 3: Initialize Git Repository
```bash
cd /home/hermes/projects/agent-marketplace
git init
git config user.email "aira@agent.market"
git config user.name "Aira Agent"
```

---

## Phase 2: Database Models (Tasks 4-8)

### Task 4: Create Database Configuration
Create `backend/database.py` with SQLAlchemy async setup.

### Task 5: User Model
Create `backend/models/user.py`:
- id, email, hashed_password, name
- model_api_keys (encrypted JSON)
- created_at, is_active

### Task 6: Agent Model  
Create `backend/models/agent.py`:
- id, name, description, owner_id
- api_key_hash, status (pending/verifying/active/idle/offline/error)
- endpoint_url, internal_port, container_id
- last_seen, last_health_check, health_check_token
- version, created_at

### Task 7: Skill Model
Create `backend/models/skill.py`:
- id, name, display_name, description
- tier (core/connected/premium), category
- required_env_vars (JSON)

### Task 8: AgentSkill Junction
Create `backend/models/agent_skill.py`:
- agent_id, skill_id, config (JSON for connected skill env vars)

---

## Phase 3: Authentication (Tasks 9-12)

### Task 9: Password Hashing & JWT
Create `backend/auth.py` with:
- pwd_context for bcrypt
- create_access_token, verify_token
- get_current_user dependency

### Task 10: User Registration API
Create `backend/routers/auth.py`:
- POST /api/auth/register
- POST /api/auth/login
- Returns JWT token

### Task 11: Pydantic Schemas
Create `backend/schemas.py`:
- UserCreate, UserResponse
- Token, TokenData

### Task 12: Test Auth Endpoints
Verify registration and login work.

---

## Phase 4: Skill Catalog (Tasks 13-16)

### Task 13: Seed Core Skills
Create `backend/services/skill_catalog.py`:
Seed database with core skills:
- terminal, web_extract, file_ops, planning
- code_review, github_pr, arxiv, obsidian

### Task 14: Skill API Endpoints
Create `backend/routers/skills.py`:
- GET /api/skills (list all)
- GET /api/skills/{id} (details)
- POST /api/skills (admin add)

### Task 15: Skill Selection Logic
Service to filter skills by tier and validate selections.

### Task 16: Test Skill Catalog
Verify skills are seeded and API works.

---

## Phase 5: Agent Lifecycle (Tasks 17-23)

### Task 17: Container Manager Service
Create `backend/services/container_manager.py`:
- create_container(agent_id, skills, env_vars)
- start_container, stop_container
- get_logs, delete_container
- Docker client setup

### Task 18: Agent Registration Endpoint
Create `backend/routers/agents.py`:
- POST /api/agent/register
- Generates api_key, health_check_token
- Creates pending agent record
- Returns credentials to agent

### Task 19: Endpoint Challenge Logic
Create `backend/services/health_checker.py`:
- ping_agent_endpoint(agent_id, token)
- Verify agent responds with correct token
- Update status to active/error

### Task 20: Agent Heartbeat Endpoint
Create `backend/routers/agent_api.py`:
- POST /api/agent/heartbeat
- Update last_seen, status
- Requires X-API-Key header

### Task 21: Status Calculation
Background job to calculate agent status from last_seen.

### Task 22: Public Agent Endpoints
- GET /api/agents (list with filters)
- GET /api/agents/{id} (details)
- GET /api/agents/{id}/skills

### Task 23: Test Agent Lifecycle
Full test: register → challenge → heartbeat → status update.

---

## Phase 6: Deployment API (Tasks 24-28)

### Task 24: User API Key Storage
- PATCH /api/me/keys (update model_api_keys)
- Encrypt keys with Fernet

### Task 25: Deploy Agent Endpoint
Create `backend/routers/deploy.py`:
- POST /api/agents/deploy
- Body: name, description, skill_ids
- Creates container with user API keys as env vars
- Triggers endpoint challenge
- Returns agent details

### Task 26: Container Proxy Middleware
Create middleware to route:
- /agents/{id}/invoke → container internal_port
- /agents/{id}/health → container health endpoint

### Task 27: Agent Dockerfile
Create `docker/Dockerfile.agent`:
- Based on python:3.11-slim
- Installs Hermes with skill support
- Exposes port 8000
- Reads skills from env

### Task 28: Test Deployment Flow
Full deployment test with actual container.

---

## Phase 7: Frontend (Tasks 29-36)

### Task 29: Base HTML Template
Create `frontend/index.html`:
- Tailwind CDN
- Alpine.js for reactivity
- Navigation structure

### Task 30: Home Page
Hero, featured agents, stats.

### Task 31: Browse Agents Page
Create `frontend/agents.html`:
- Grid of agent cards
- Status badges
- Skill filters
- Search

### Task 32: Agent Detail Page
Create `frontend/agent-detail.html`:
- Agent info
- Skills list
- Status indicator
- Owner actions (if owner)

### Task 33: Deploy Page
Create `frontend/deploy.html`:
- Step 1: Name/description
- Step 2: Skill selection (core vs connected)
- Step 3: Review & deploy
- Progress indicator

### Task 34: Login/Signup Pages
Create `frontend/login.html`, `frontend/signup.html`:
- Forms with validation
- JWT storage in localStorage

### Task 35: User Settings Page
Create `frontend/settings.html`:
- Update profile
- Add model API keys (OpenAI, Anthropic, OpenRouter)

### Task 36: Frontend JavaScript
Create `frontend/js/app.js`:
- API client functions
- Auth state management
- UI helpers

---

## Phase 8: Integration & Testing (Tasks 37-40)

### Task 37: Main FastAPI App
Create `backend/main.py`:
- Include all routers
- CORS middleware
- Container proxy middleware
- Startup/shutdown events

### Task 38: Docker Compose
Create `docker-compose.yml`:
- Backend service
- Traefik reverse proxy
- Network configuration

### Task 39: End-to-End Testing
Test full flow:
1. User signup
2. Add API keys
3. Deploy agent
4. Browse agents
5. View agent details

### Task 40: Documentation
Create `README.md` with:
- Setup instructions
- API documentation
- Deployment guide

---

## Phase 9: Aira Registration (Tasks 41-43)

### Task 41: Agent SDK
Create `agent-sdk/marketplace_client.py`:
- MarketplaceClient class
- register(), heartbeat(), update_skills()

### Task 42: Aira Self-Registration Script
Create `scripts/register_aira.py`:
- Registers me as first agent
- Starts heartbeat loop
- Lists all my skills

### Task 43: Test Aira in Marketplace
Verify I appear in browse list with correct skills.

---

## Phase 10: GitHub Push (Task 44)

### Task 44: Push to GitHub
```bash
git add .
git commit -m "feat: initial agent marketplace implementation"
git remote add origin https://github.com/rshetty/agent-marketplace.git
git push -u origin main
```

---

## Success Criteria
- [ ] User can signup/login
- [ ] User can add model API keys
- [ ] User can deploy agent with selected skills
- [ ] Agent passes endpoint challenge
- [ ] Agent shows online/idle/offline status
- [ ] User can browse and filter agents
- [ ] Aira appears as first registered agent
- [ ] All code pushed to GitHub

---

**Total Tasks:** 44
**Estimated Time:** 4-6 hours
**Output:** Working agent marketplace + GitHub repo link
