#!/usr/bin/env bash
# =============================================================================
# Trade Company Agent — Setup Script
# =============================================================================
# Installs all dependencies for the find-company-website and
# extract-company-info Claude Code skills.
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh
#
# What this script does:
#   1. Checks Python 3.11+ is available
#   2. Installs Typesense server (Docker or binary)
#   3. Installs Python dependencies
#   4. Installs Playwright + Chromium browser
#   5. Creates .env from template if missing
#   6. Starts Typesense and verifies health
#   7. Initializes the Typesense collection schema
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${SCRIPT_DIR}/.claude/skills"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# =============================================================================
# 1. Check Python
# =============================================================================
info "Checking Python version..."

PYTHON=""
for cmd in python3.12 python3.11 python3; do
	if command -v "$cmd" &>/dev/null; then
		version=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+')
		major=$(echo "$version" | cut -d. -f1)
		minor=$(echo "$version" | cut -d. -f2)
		if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
			PYTHON="$cmd"
			break
		fi
	fi
done

if [[ -z "$PYTHON" ]]; then
	err "Python 3.11+ is required but not found."
	echo "  Install options:"
	echo "    macOS:  brew install python@3.12"
	echo "    Ubuntu: sudo apt install python3.12 python3.12-venv"
	echo "    pyenv:  pyenv install 3.12 && pyenv global 3.12"
	exit 1
fi

ok "Found $($PYTHON --version)"

# =============================================================================
# 2. Install Typesense
# =============================================================================
info "Setting up Typesense..."

TYPESENSE_VERSION="27.1"
TYPESENSE_PORT="${TYPESENSE_PORT:-8108}"
TYPESENSE_API_KEY="${TYPESENSE_API_KEY:-xyz}"

typesense_running() {
	curl -sf "http://localhost:${TYPESENSE_PORT}/health" &>/dev/null
}

if typesense_running; then
	ok "Typesense is already running on port ${TYPESENSE_PORT}"
elif command -v docker &>/dev/null; then
	info "Starting Typesense via Docker..."

	# Stop old container if exists
	docker rm -f typesense-trade 2>/dev/null || true

	docker run -d \
		--name typesense-trade \
		-p "${TYPESENSE_PORT}:8108" \
		-v "typesense-trade-data:/data" \
		"typesense/typesense:${TYPESENSE_VERSION}" \
		--data-dir /data \
		--api-key="${TYPESENSE_API_KEY}" \
		--enable-cors

	# Wait for startup
	info "Waiting for Typesense to start..."
	for i in $(seq 1 30); do
		if typesense_running; then
			ok "Typesense started on port ${TYPESENSE_PORT}"
			break
		fi
		sleep 1
	done

	if ! typesense_running; then
		err "Typesense failed to start. Check: docker logs typesense-trade"
		exit 1
	fi
else
	# No Docker — install binary
	info "Docker not found, installing Typesense binary..."

	ARCH=$(uname -m)
	OS=$(uname -s | tr '[:upper:]' '[:lower:]')

	case "${ARCH}" in
		x86_64)  ARCH_TAG="amd64" ;;
		aarch64|arm64) ARCH_TAG="arm64" ;;
		*) err "Unsupported architecture: ${ARCH}"; exit 1 ;;
	esac

	TYPESENSE_BIN="/usr/local/bin/typesense-server"
	TYPESENSE_DATA_DIR="${HOME}/.typesense/data"

	if [[ ! -f "${TYPESENSE_BIN}" ]]; then
		DOWNLOAD_URL="https://dl.typesense.org/releases/${TYPESENSE_VERSION}/typesense-server-${TYPESENSE_VERSION}-${OS}-${ARCH_TAG}.tar.gz"
		info "Downloading Typesense ${TYPESENSE_VERSION}..."
		curl -sL "${DOWNLOAD_URL}" | sudo tar -xz -C /usr/local/bin/
	fi

	mkdir -p "${TYPESENSE_DATA_DIR}"

	info "Starting Typesense binary..."
	nohup "${TYPESENSE_BIN}" \
		--data-dir="${TYPESENSE_DATA_DIR}" \
		--api-key="${TYPESENSE_API_KEY}" \
		--port="${TYPESENSE_PORT}" \
		--enable-cors \
		> "${HOME}/.typesense/typesense.log" 2>&1 &

	for i in $(seq 1 30); do
		if typesense_running; then
			ok "Typesense started on port ${TYPESENSE_PORT}"
			break
		fi
		sleep 1
	done

	if ! typesense_running; then
		err "Typesense failed to start. Check: ${HOME}/.typesense/typesense.log"
		exit 1
	fi
fi

# =============================================================================
# 3. Create virtual environment and install Python deps
# =============================================================================
info "Setting up Python virtual environment..."

VENV_DIR="${SCRIPT_DIR}/.venv"

if [[ ! -d "${VENV_DIR}" ]]; then
	$PYTHON -m venv "${VENV_DIR}"
	ok "Created virtual environment at ${VENV_DIR}"
else
	ok "Virtual environment already exists"
fi

source "${VENV_DIR}/bin/activate"

info "Installing Python dependencies..."
pip install --quiet --upgrade pip

pip install --quiet \
	httpx \
	beautifulsoup4 \
	lxml \
	pydantic \
	pyyaml \
	python-dotenv \
	typesense \
	playwright \
	openai \
	anthropic \
	litellm

ok "Python dependencies installed"

# =============================================================================
# 4. Install Playwright Chromium
# =============================================================================
info "Installing Playwright Chromium browser..."

if $PYTHON -m playwright install chromium 2>/dev/null; then
	ok "Playwright Chromium installed"
else
	warn "Playwright Chromium install failed (may need: sudo playwright install-deps)"
	echo "  Run manually: playwright install chromium"
fi

# =============================================================================
# 5. Create .env file
# =============================================================================
if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
	info "Creating .env file from template..."
	cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
	warn "Edit .env and add your API keys before running the skills"
	echo "  Required: GEMINI_API_KEY"
	echo "  Recommended: SERPER_API_KEY"
else
	ok ".env file already exists"
fi

# =============================================================================
# 6. Initialize Typesense collection
# =============================================================================
info "Initializing Typesense collection schema..."

$PYTHON -c "
import sys
sys.path.insert(0, '${SKILLS_DIR}/extract-company-info/scripts')
from db import get_typesense_client, init_schema
client = get_typesense_client()
init_schema(client)
print('[OK] Typesense collection initialized')
"

# =============================================================================
# 7. Verify setup
# =============================================================================
echo ""
echo "=============================================="
echo -e "${GREEN}  Setup complete!${NC}"
echo "=============================================="
echo ""
echo "  Typesense:  http://localhost:${TYPESENSE_PORT}"
echo "  Venv:       source ${VENV_DIR}/bin/activate"
echo "  Config:     ${SCRIPT_DIR}/.env"
echo ""
echo "  Skills:"
echo "    /find-company-website  — Discover company URLs from trade CSV data"
echo "    /extract-company-info  — Extract + index company website data"
echo ""
echo "  Quick test:"
echo "    source .env"
echo "    python ${SKILLS_DIR}/find-company-website/scripts/find_website.py \\"
echo "      '{\"company_name\": \"SWIFT BEEF COMPANY\", \"address\": \"GREELEY, CO\"}'"
echo ""

if [[ -f "${SCRIPT_DIR}/.env" ]]; then
	source "${SCRIPT_DIR}/.env" 2>/dev/null || true
	if [[ "${GEMINI_API_KEY:-}" == "your_gemini_api_key_here" || -z "${GEMINI_API_KEY:-}" ]]; then
		warn "Remember to set GEMINI_API_KEY in .env before using the skills"
	fi
fi
