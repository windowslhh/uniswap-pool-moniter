import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Data Source Configuration
# ============================================================
# Primary: GeckoTerminal API (free, no key needed)
# Fallback: TheGraph Subgraph (needs API key from thegraph.com/studio)

DATA_SOURCE = os.getenv("DATA_SOURCE", "geckoterminal")  # "geckoterminal" or "thegraph"

# --- GeckoTerminal ---
GECKO_BASE_URL = "https://api.geckoterminal.com/api/v2"
GECKO_NETWORK = "base"
GECKO_DEX = "uniswap-v3-base"

# --- TheGraph (fallback) ---
THEGRAPH_API_KEY = os.getenv("THEGRAPH_API_KEY", "")
SUBGRAPH_ID = "FUbEPQw1oMghy39fwWBFY5fE6MXPXZQtjncQy2cXdrNS"
SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{THEGRAPH_API_KEY}/subgraphs/id/{SUBGRAPH_ID}"

# ============================================================
# Pool Configuration (Uniswap V3 on Base)
# ============================================================
POOLS = [
    {
        "name": "cbBTC / WETH",
        "address": "0x8c7080564b5a792a33ef2fd473fba6364d5495e5",
        "fee_tier": 0.3,  # 0.3%
        "token0": "cbBTC",
        "token1": "WETH",
    },
    {
        "name": "USDC / USDT",
        "address": "0xd56da2b74ba826f19015e6b7dd9dae1903e85da1",
        "fee_tier": 0.01,  # 0.01%
        "token0": "USDC",
        "token1": "USDT",
    },
]

# Token addresses on Base (lowercase, for TheGraph queries)
TOKENS = {
    "cbBTC": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
    "WETH": "0x4200000000000000000000000000000000000006",
    "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "USDT": "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",
}

# Cache TTL in seconds
CACHE_TTL = int(os.getenv("CACHE_TTL", "120"))

# Flask
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
