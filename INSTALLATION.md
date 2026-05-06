# SM121 System — AI Agent Stack Installation Guide

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        SM121 System                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │  Hermes-     │    │  CLI-        │    │  LiteLLM     │  │
│  │  Agent       │◄──►│  Anything    │◄──►│  Proxy       │  │
│  │  :3000/:8000 │    │  :4000       │    │  Redis:6379  │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│       ▲                      ▲                  ▲           │
│       │                      │                  │           │
│  ┌────┴────┐           ┌────┴────┐        ┌────┴────┐      │
│  │ Agency- │           │ 51+    │        │ OpenAI  │      │
│  │ Agents  │           │ CLIs   │        │ Anthropic│      │
│  │         │           │        │        │ Gemini   │      │
│  └─────────┘           └────────┘        └─────────┘      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Services

### 1. Hermes-Agent (AI Agent Hub)
- **Port**: 3000 (Web Dashboard), 8000 (API Gateway)
- **Status**: Dockerfile ready, needs to be built
- **Features**:
  - Self-improving AI agent
  - Creates and improves skills from experience
  - Runs anywhere (cloud, local, mobile)
  - Integrated with Agency-Agents (AI specialists)

### 2. CLI-Anything (CLI Package Manager)
- **Port**: N/A (exec-only container)
- **Status**: ✅ Built and running
- **Features**:
  - 51+ agent-native CLI interfaces
  - Browse, install, manage CLIs
  - Categories: 3D, AI, Image, Video, Web, etc.

### 3. LiteLLM (LLM Proxy Server)
- **Port**: 4000
- **Status**: ✅ Built and running
- **Features**:
  - Unified API for all LLM providers
  - OpenAI, Anthropic, Google Gemini support
  - Request routing and load balancing

### 4. Redis (Cache)
- **Port**: 6379
- **Status**: ✅ Running (healthy)
- **Features**:
  - Cache for LiteLLM
  - Session storage for Hermes-Agent

## Quick Start

### Prerequisites
- Docker Engine 29.2.1+
- Docker Compose v2+

### Step 1: Configure API Keys

```bash
cd /home/murray/hermes-agent
nano .env
```

Fill in your API keys:
```bash
OPENAI_API_KEY=replace-with-openai-api-key
ANTHROPIC_API_KEY=replace-with-anthropic-api-key
GOOGLE_API_KEY=your-google-key-here
```

### Step 2: Build Hermes-Agent (largest image)

```bash
cd /home/murray/hermes-agent
docker compose build hermes-agent
```

**Note**: This will take 10-15 minutes as it downloads Playwright browsers and installs all dependencies.

### Step 3: Start All Services

```bash
cd /home/murray/hermes-agent
docker compose up -d
```

### Step 4: Verify Installation

```bash
# Check all services
docker compose ps

# View logs
docker compose logs -f

# Test LiteLLM
curl http://localhost:4000/health

# Access Hermes-Agent Web Dashboard
open http://localhost:3000
```

## Available Commands

### CLI-Anything

```bash
# Enter CLI-Anything container
docker exec -it cli-anything-sm121 bash

# List all available CLIs
docker exec cli-anything-sm121 cli-hub list

# Search for a specific CLI
docker exec cli-anything-sm121 cli-hub search blender

# Install a CLI
docker exec cli-anything-sm121 cli-hub install blender

# Update all CLIs
docker exec cli-anything-sm121 cli-hub update
```

### Hermes-Agent

```bash
# Enter Hermes-Agent container
docker exec -it hermes-agent-sm121 bash

# Run Hermes CLI
docker exec hermes-agent-sm121 hermes --help

# View Hermes logs
docker compose logs -f hermes-agent
```

## Service Management

```bash
# Start all services
docker compose up -d

# Stop all services
docker compose down

# Restart all services
docker compose restart

# View logs
docker compose logs -f

# View specific service logs
docker compose logs -f hermes-agent
docker compose logs -f litellm
docker compose logs -f redis

# Scale services (if needed)
docker compose up -d --scale hermes-agent=2
```

## Agency-Agents Integration

Agency-Agents is mounted as a read-only volume at `/opt/data/agency-agents` inside the Hermes-Agent container.

### Available Agent Categories

- **Engineering**: Frontend Developer, Backend Architect, Mobile App Builder, AI Engineer, DevOps Automator, etc.
- **Design**: UI/UX Designer, Brand Identity Specialist
- **Marketing**: Content Strategist, SEO Specialist, Social Media Manager
- **Finance**: Financial Analyst, Accountant
- **Product**: Product Manager, UX Researcher
- **Sales**: Sales Development Rep, Account Executive
- **Strategy**: Business Analyst, Competitive Analyst
- **Game Development**: Game Designer, 3D Artist, Level Designer, Narrative Designer
- **And more...**

### Using Agency-Agents with Hermes

In Hermes-Agent, you can reference any agent from the Agency roster:

```bash
# Activate an agent in Hermes
docker exec hermes-agent-sm121 hermes chat -q "Activate Frontend Developer from agency-agents and help me build a React component"
```

## Troubleshooting

### Service won't start

```bash
# Check logs for errors
docker compose logs litellm
docker compose logs hermes-agent

# Restart specific service
docker compose restart litellm
```

### Port conflicts

If ports are already in use, modify `docker-compose.yml`:

```yaml
ports:
  - "4001:4000"  # Change host port
```

### Hermes-Agent build fails

```bash
# Clear Docker cache and rebuild
docker compose build --no-cache hermes-agent

# Check system resources
docker system df
```

### CLI-Anything commands fail

```bash
# Rebuild CLI-Anything
docker compose build cli-anything
docker compose up -d cli-anything
```

## Directory Structure

```
/home/murray/
├── hermes-agent/
│   ├── Dockerfile              # Hermes-Agent Dockerfile
│   ├── docker-compose.yml      # All services configuration
│   ├── nginx.conf              # Nginx reverse proxy config
│   ├── .env.template           # Environment variables template
│   ├── start.sh                # Quick start script
│   └── docker/
│       └── entrypoint.sh       # Container entrypoint script
│
├── CLI-Anything/
│   ├── Dockerfile              # CLI-Anything Dockerfile
│   ├── docker-compose.yml      # CLI-Anything compose config
│   └── cli-hub/                # CLI package manager source
│
└── agency-agents/
    ├── README.md
    ├── scripts/                # Installation scripts
    ├── engineering/            # Engineering agents
    ├── design/                 # Design agents
    ├── marketing/              # Marketing agents
    └── ...                     # Other agent categories
```

## Next Steps

1. **Configure API Keys**: Edit `.env` file with your API keys
2. **Build Hermes-Agent**: Run `docker compose build hermes-agent`
3. **Start All Services**: Run `docker compose up -d`
4. **Access Dashboard**: Open http://localhost:3000
5. **Install CLIs**: Use `cli-hub install <name>` inside the container
6. **Start Using Agents**: Begin interacting with Hermes-Agent

## Resources

- **Hermes-Agent GitHub**: https://github.com/NousResearch/hermes-agent
- **CLI-Anything GitHub**: https://github.com/HKUDS/CLI-Anything
- **CLI-Hub Web**: https://hkuds.github.io/CLI-Anything/
- **LiteLLM Documentation**: https://docs.litellm.ai/
- **Agency-Agents GitHub**: https://github.com/msitarzewski/agency-agents
