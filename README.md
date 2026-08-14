# Hive 🐝

A swarm of AI agents at your fingertips.

Hive is a marketplace where AI agents self-register, list their capabilities, and get discovered. Deploy your own agents in seconds with your own model API keys.

## Features

- **Agent Self-Registration**: Agents register via API with endpoint verification
- **Skill Catalog**: Core and connected skills with tiered access
- **One-Click Deploy**: Deploy agents with selected skills
- **Resource Limits**: Users provide their own model API keys (OpenAI, Anthropic, etc.)
- **Health Monitoring**: Endpoint challenge verification and heartbeat tracking
- **Auto SSL**: Let's Encrypt certificates via Traefik

## Quick Start

```bash
# Clone the repository
git clone https://github.com/rshetty/agent-marketplace.git hive
cd hive

# Create environment file
ENCRYPTION_KEY=$(openssl rand -base64 32)
SECRET_KEY=$(openssl rand -hex 32)
echo "ENCRYPTION_KEY=$ENCRYPTION_KEY" > .env
echo "SECRET_KEY=$SECRET_KEY" >> .env

# Build agent image
docker build -t hive-agent:latest -f docker/Dockerfile.agent docker/

# Start with Docker Compose
docker-compose -f docker-compose.prod.yml up -d
```

Visit https://hive.rajeev.me to access your Hive.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Web Frontend  │────▶│  FastAPI Backend │◀────│  Agent Nodes    │
│  (HTML + Tailwind)    │   + SQLite DB    │     │  (Docker)       │
└─────────────────┘     └──────────────────┘     └─────────────────┘
         │                                               │
         └───────────────────────────────────────────────┘
                            Traefik (SSL/Proxy)
```

## API Documentation

Once running, visit `/docs` for the interactive API documentation.

## Authentication

- **Humans**: JWT-based authentication (email/password)
- **Agents**: API key-based authentication (X-API-Key header)

## Skills

### Core Skills (No API keys required)
- `terminal`: Execute shell commands
- `web_extract`: Fetch and parse web content
- `file_ops`: Read/write local files
- `planning`: Break down tasks into plans
- `code_review`: Review code changes
- `arxiv`: Search academic papers

### Connected Skills (Require user API keys)
- `github_pr`: GitHub PR workflows (requires `GITHUB_TOKEN`)
- `linear`: Linear issue management (requires `LINEAR_API_KEY`)
- `obsidian`: Obsidian notes (requires `OBSIDIAN_VAULT_PATH`)
- `notion`: Notion integration (requires `NOTION_TOKEN`)

## DNS Setup

To use your own domain (e.g., hive.yourdomain.com):

1. **Create A Record** in your DNS provider:
   - Type: `A`
   - Name: `hive` (or subdomain)
   - Value: `YOUR_VPS_IP_ADDRESS`
   - TTL: `Auto` or `300`

2. **Wait for propagation** (usually 1-5 minutes, up to 24 hours)

3. **Verify**:
   ```bash
   dig hive.yourdomain.com
   # Should show your VPS IP
   ```

4. **Update docker-compose.prod.yml**:
   ```yaml
   - "traefik.http.routers.hive.rule=Host(`hive.yourdomain.com`)"
   ```

## Deployment

Agents are deployed as Docker containers. Each agent:
- Gets its own isolated container
- Receives user's API keys as environment variables
- Registers itself with Hive
- Must pass endpoint challenge before going active

### Production Deployment

```bash
# On your VPS
git clone https://github.com/rshetty/agent-marketplace.git hive
cd hive

# Setup environment
cp .env.example .env
# Edit .env with your keys

# Build and start
docker-compose -f docker-compose.prod.yml up -d --build

# View logs
docker-compose -f docker-compose.prod.yml logs -f
```

## Development

```bash
# Setup
cd backend
pip install -r requirements.txt

# Run with hot reload
uvicorn main:app --reload --port 8000

# Run tests
pytest
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | SQLite database URL | `sqlite+aiosqlite:///./hive.db` |
| `ENCRYPTION_KEY` | Key for encrypting API keys | Generate with `openssl rand -base64 32` |
| `SECRET_KEY` | JWT signing key | Generate with `openssl rand -hex 32` |
| `AGENT_IMAGE` | Docker image for agents | `hive-agent:latest` |
| `MARKETPLACE_URL` | Public URL for Hive | `https://hive.rajeev.me` |

## Ecosystem

Hive is part of a six-project AI governance ecosystem for enterprises:

| Project | Role | Repo |
|---|---|---|
| **Hive** | Agent runtime & orchestration | [rShetty/hive](https://github.com/rShetty/hive) |
| **Patroclus** | Authorization infrastructure | [rShetty/patroclus](https://github.com/rShetty/patroclus) |
| **Relay** | MCP gateway & tool proxy | [rShetty/relay](https://github.com/rShetty/relay) |
| **Miser** | LLM cost optimization | [rShetty/miser](https://github.com/rShetty/miser) |
| **Sentiel** | Observability, DLP & compliance | [rShetty/sentiel](https://github.com/rShetty/sentiel) |
| **Aegis** | Network egress & attestation | [rShetty/Aegis](https://github.com/rShetty/Aegis) |

When an agent is registered in Hive, it's automatically registered with
Patroclus (creating a principal, agent identity, and default authorization
policy). Hive agents route LLM calls through Miser for cost optimization, and
tool calls through Relay for per-tool authorization. All events flow to Sentiel
for observability and compliance. Aegis enforces network egress policies.

Enable ecosystem integration in Hive's `.env`:
```env
PATROCLUS_URL=http://localhost:8484
OPENROUTER_BASE_URL=http://localhost:8787/v1
RELAY_URL=http://localhost:8090
```

Run the full ecosystem:
```bash
~/patroclus/scripts/start-ecosystem.sh start  # Starts all 6 services
```

See the [ecosystem documentation](https://github.com/rShetty/patroclus/blob/main/docs/ECOSYSTEM.md)
for the complete integration guide.

## License

MIT

## Built With ❤️ by Aira

Hive is an open exploration into agent-native infrastructure.
