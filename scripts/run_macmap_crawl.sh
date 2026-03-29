#!/usr/bin/env bash
# =============================================================================
# MacMap Trade Data Collection
# =============================================================================
# Sets up prerequisites and crawls macmap.org trade data into Typesense.
#
# Usage:
#   # Full crawl (default 15 reporter countries)
#   ./scripts/run_macmap_crawl.sh
#
#   # Specific reporters
#   ./scripts/run_macmap_crawl.sh --reporters 842,704,356
#
#   # Test run (10 combos only)
#   ./scripts/run_macmap_crawl.sh --limit 10
#
#   # Resume after interruption
#   ./scripts/run_macmap_crawl.sh --resume
#
#   # All arguments are forwarded to fetch_macmap_trade.py
#   ./scripts/run_macmap_crawl.sh --reporters 842 --concurrency 10 --delay 0.1
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PROJECT_DIR}/.venv"

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
# 1. Check Python venv
# =============================================================================
if [[ ! -d "${VENV_DIR}" ]]; then
    err "Python venv not found at ${VENV_DIR}"
    echo "  Run ./setup.sh first to set up the environment."
    exit 1
fi

source "${VENV_DIR}/bin/activate"
ok "Activated venv"

# =============================================================================
# 2. Load .env
# =============================================================================
if [[ -f "${PROJECT_DIR}/.env" ]]; then
    set -a
    source "${PROJECT_DIR}/.env"
    set +a
    ok "Loaded .env"
fi

# =============================================================================
# 3. Ensure Typesense is running
# =============================================================================
TYPESENSE_PORT="${TYPESENSE_PORT:-8108}"

typesense_running() {
    curl -sf "http://localhost:${TYPESENSE_PORT}/health" &>/dev/null
}

if typesense_running; then
    ok "Typesense is running on port ${TYPESENSE_PORT}"
else
    err "Typesense is not running on port ${TYPESENSE_PORT}"
    echo "  Start it with: docker start typesense-trade"
    echo "  Or run ./setup.sh to set up everything."
    exit 1
fi

# =============================================================================
# 4. Ensure HS codes are indexed
# =============================================================================
info "Checking HS codes collection..."

HS_COUNT=$(python -c "
import typesense, os
client = typesense.Client({
    'api_key': os.environ.get('TYPESENSE_API_KEY', 'xyz'),
    'nodes': [{'host': os.environ.get('TYPESENSE_HOST', 'localhost'),
               'port': os.environ.get('TYPESENSE_PORT', '8108'),
               'protocol': os.environ.get('TYPESENSE_PROTOCOL', 'http')}],
    'connection_timeout_seconds': 10,
})
try:
    info = client.collections['hscodes'].retrieve()
    print(info['num_documents'])
except:
    print('0')
" 2>/dev/null)

if [[ "$HS_COUNT" -gt 0 ]]; then
    ok "HS codes already indexed ($HS_COUNT documents)"
else
    info "Indexing HS codes..."
    python "${PROJECT_DIR}/scripts/fetch_hscodes.py"
    ok "HS codes indexed"
fi

# =============================================================================
# 5. Ensure countries are indexed
# =============================================================================
info "Checking countries collection..."

COUNTRY_COUNT=$(python -c "
import typesense, os
client = typesense.Client({
    'api_key': os.environ.get('TYPESENSE_API_KEY', 'xyz'),
    'nodes': [{'host': os.environ.get('TYPESENSE_HOST', 'localhost'),
               'port': os.environ.get('TYPESENSE_PORT', '8108'),
               'protocol': os.environ.get('TYPESENSE_PROTOCOL', 'http')}],
    'connection_timeout_seconds': 10,
})
try:
    info = client.collections['macmap_countries'].retrieve()
    print(info['num_documents'])
except:
    print('0')
" 2>/dev/null)

if [[ "$COUNTRY_COUNT" -gt 0 ]]; then
    ok "Countries already indexed ($COUNTRY_COUNT documents)"
else
    info "Indexing countries..."
    python "${PROJECT_DIR}/scripts/fetch_macmap_countries.py"
    ok "Countries indexed"
fi

# =============================================================================
# 6. Print crawl estimates
# =============================================================================
echo ""
echo "=============================================="
echo -e "${BLUE}  MacMap Trade Data Crawl${NC}"
echo "=============================================="

# Parse --reporters from args to show estimate
REPORTERS=""
prev_arg=""
for arg in "$@"; do
    if [[ "$prev_arg" == "--reporters" ]]; then
        REPORTERS="$arg"
    fi
    prev_arg="$arg"
done

python -c "
import typesense, os

client = typesense.Client({
    'api_key': os.environ.get('TYPESENSE_API_KEY', 'xyz'),
    'nodes': [{'host': os.environ.get('TYPESENSE_HOST', 'localhost'),
               'port': os.environ.get('TYPESENSE_PORT', '8108'),
               'protocol': os.environ.get('TYPESENSE_PROTOCOL', 'http')}],
    'connection_timeout_seconds': 10,
})

hs_info = client.collections['hscodes'].retrieve()
country_info = client.collections['macmap_countries'].retrieve()

products = int(hs_info['num_documents'])
countries = int(country_info['num_documents'])

# Count 6-digit subheading codes
page, six_digit = 1, 0
while True:
    r = client.collections['hscodes'].documents.search({
        'q': '*', 'filter_by': 'level:=subheading', 'per_page': 250, 'page': page
    })
    six_digit += len(r['hits'])
    if len(r['hits']) < 250:
        break
    page += 1

reporters_arg = '${REPORTERS}'
if reporters_arg:
    n_reporters = len(reporters_arg.split(','))
else:
    n_reporters = 15  # default shortlist

combos = n_reporters * countries * six_digit
api_calls = combos * 4

print(f'  Reporters:    {n_reporters}')
print(f'  Partners:     {countries}')
print(f'  HS products:  {six_digit} (6-digit subheadings)')
print(f'  Total combos: {combos:,}')
print(f'  API calls:    {api_calls:,}')
print(f'')
print(f'  Estimated time at 5 concurrent (default):')
print(f'    ~{api_calls / 20 / 3600:.0f} hours ({api_calls / 20 / 3600 / 24:.1f} days)')
print(f'')
print(f'  Tip: increase speed with --concurrency 10 --delay 0.1')
" 2>/dev/null || true

echo "=============================================="
echo ""

# =============================================================================
# 7. Run the crawl
# =============================================================================
info "Starting trade data crawl..."
echo "  Args: $*"
echo "  Press Ctrl+C to stop (use --resume to continue later)"
echo ""

python "${PROJECT_DIR}/scripts/fetch_macmap_trade.py" "$@"

echo ""
ok "Crawl finished!"
echo ""

# Show final stats
python -c "
import typesense, os
client = typesense.Client({
    'api_key': os.environ.get('TYPESENSE_API_KEY', 'xyz'),
    'nodes': [{'host': os.environ.get('TYPESENSE_HOST', 'localhost'),
               'port': os.environ.get('TYPESENSE_PORT', '8108'),
               'protocol': os.environ.get('TYPESENSE_PROTOCOL', 'http')}],
    'connection_timeout_seconds': 10,
})
try:
    info = client.collections['macmap_trade'].retrieve()
    print(f'  Total trade records in Typesense: {info[\"num_documents\"]:,}')
except:
    pass
" 2>/dev/null || true
