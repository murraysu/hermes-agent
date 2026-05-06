#!/bin/bash
# ========================================
# SM121 System — AI Agent Stack Launcher
# ========================================
# This script builds and starts all services:
#   - LiteLLM (LLM Proxy on port 4000)
#   - CLI-Anything (CLI Manager)
#   - Hermes-Agent (AI Agent Hub on port 3000)
#   - Redis (Cache)
#   - Nginx (Reverse Proxy on port 80)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  SM121 System — AI Agent Stack${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is not installed.${NC}"
    exit 1
fi

# Check Docker Compose
if ! command -v docker compose &> /dev/null; then
    echo -e "${RED}Error: Docker Compose is not installed.${NC}"
    exit 1
fi

# Check .env file
if [ ! -f .env ]; then
    echo -e "${YELLOW}Warning: .env file not found. Creating from template...${NC}"
    cp .env.template .env
    echo -e "${YELLOW}Please edit .env and add your API keys.${NC}"
    read -p "Press Enter to continue..."
fi

echo ""
echo -e "${GREEN}Step 1/3: Building Docker images...${NC}"
docker compose build --no-cache

echo ""
echo -e "${GREEN}Step 2/3: Starting all services...${NC}"
docker compose up -d

echo ""
echo -e "${GREEN}Step 3/3: Waiting for services to be healthy...${NC}"
sleep 10

# Check service health
echo ""
echo -e "${GREEN}Checking service status...${NC}"
echo ""

docker compose ps

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  All services started successfully!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "  ${YELLOW}Hermes-Agent Web Dashboard:${NC}  http://localhost:3000"
echo -e "  ${YELLOW}LiteLLM Proxy API:${NC}          http://localhost:4000"
echo -e "  ${YELLOW}LiteLLM Model List:${NC}         curl http://localhost:4000/models"
echo -e "  ${YELLOW}CLI-Anything Container:${NC}     docker exec -it cli-anything-sm121 bash"
echo -e "  ${YELLOW}Hermes-Agent Container:${NC}     docker exec -it hermes-agent-sm121 bash"
echo ""
echo -e "  ${YELLOW}View logs:${NC}              docker compose logs -f"
echo -e "  ${YELLOW}Stop all services:${NC}      docker compose down"
echo -e "  ${YELLOW}Restart all services:${NC}   docker compose restart"
echo ""
