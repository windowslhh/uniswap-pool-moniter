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

# --- TheGraph ---
THEGRAPH_API_KEY = os.getenv("THEGRAPH_API_KEY", "")
THEGRAPH_BASE = f"https://gateway.thegraph.com/api/{THEGRAPH_API_KEY}/subgraphs/id"

# V3: Official Uniswap schema (UniV3-Base)
SUBGRAPH_ID_V3 = "HMuAwufqZ1YCRmzL2SfHTVkzZovC9VL2UAKhjvRqKiR1"
SUBGRAPH_URL = f"{THEGRAPH_BASE}/{SUBGRAPH_ID_V3}"

# V4: Uniswap official schema
SUBGRAPH_ID_V4 = "HNCFA9TyBqpo5qpe6QreQABAA1kV8g46mhkCcicu6v2R"
SUBGRAPH_URL_V4 = f"{THEGRAPH_BASE}/{SUBGRAPH_ID_V4}"

# ============================================================
# Pool Configuration (Uniswap V3 + V4 on Base)
# ============================================================
POOLS = [
    # --- cbBTC / USDC ---
    {
        "name": "cbBTC / USDC",
        "address": "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef",
        "fee_tier": 0.05,
        "token0": "USDC",
        "token1": "cbBTC",
    },
    {
        "name": "cbBTC / USDC",
        "address": "0xec558e484cc9f2210714e345298fdc53b253c27d",
        "fee_tier": 0.3,
        "token0": "USDC",
        "token1": "cbBTC",
    },
    {
        "name": "cbBTC / USDC",
        "address": "0x3e7586d52a9d07f8611b8ecf6ccc8a689c34a659",
        "fee_tier": 1.0,
        "token0": "USDC",
        "token1": "cbBTC",
    },
    {
        "name": "cbBTC / USDC",
        "address": "0xe9e25e35aa99a2a60155010802b81a25c45ba185",
        "fee_tier": 0.01,
        "token0": "USDC",
        "token1": "cbBTC",
    },
    # --- cbBTC / WETH ---
    {
        "name": "cbBTC / WETH",
        "address": "0x8c7080564b5a792a33ef2fd473fba6364d5495e5",
        "fee_tier": 0.3,
        "token0": "WETH",
        "token1": "cbBTC",
    },
    {
        "name": "cbBTC / WETH",
        "address": "0x7aea2e8a3843516afa07293a10ac8e49906dabd1",
        "fee_tier": 0.05,
        "token0": "WETH",
        "token1": "cbBTC",
    },
    # --- WETH / USDC ---
    {
        "name": "WETH / USDC",
        "address": "0x6c561b446416e1a00e8e93e221854d6ea4171372",
        "fee_tier": 0.3,
        "token0": "WETH",
        "token1": "USDC",
    },
    {
        "name": "WETH / USDC",
        "address": "0xd0b53d9277642d899df5c87a3966a349a798f224",
        "fee_tier": 0.05,
        "token0": "WETH",
        "token1": "USDC",
    },
    {
        "name": "WETH / USDC",
        "address": "0x0b1c2dcbbfa744ebd3fc17ff1a96a1e1eb4b2d69",
        "fee_tier": 1.0,
        "token0": "WETH",
        "token1": "USDC",
    },
    {
        "name": "WETH / USDC",
        "address": "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
        "fee_tier": 0.01,
        "token0": "WETH",
        "token1": "USDC",
    },
    # --- WETH / USDT ---
    {
        "name": "WETH / USDT",
        "address": "0xce1d8c90a5f0ef28fe0f457e5ad615215899319a",
        "fee_tier": 0.3,
        "token0": "WETH",
        "token1": "USDT",
    },
    {
        "name": "WETH / USDT",
        "address": "0xd92e0767473d1e3ff11ac036f2b1db90ad0ae55f",
        "fee_tier": 0.05,
        "token0": "WETH",
        "token1": "USDT",
    },
    # --- USDC / USDT ---
    {
        "name": "USDC / USDT",
        "address": "0xd56da2b74ba826f19015e6b7dd9dae1903e85da1",
        "fee_tier": 0.01,
        "token0": "USDC",
        "token1": "USDT",
    },
]

# ============================================================
# V4 Pools (Uniswap V4 on Base - different subgraph & schema)
# ============================================================
POOLS_V4 = [
    # --- cbBTC / USDC ---
    {
        "name": "cbBTC / USDC",
        "address": "0x12d76c5c8ec8edffd3c143995b0aa43fe44a6d71eb9113796272909e54b8e078",
        "fee_tier": 0.05,
        "token0": "USDC",
        "token1": "cbBTC",
    },
    {
        "name": "cbBTC / USDC",
        "address": "0x64f978ef116d3c2e1231cfd8b80a369dcd8e91b28037c9973b65b59fd2cbbb96",
        "fee_tier": 0.3,
        "token0": "USDC",
        "token1": "cbBTC",
    },
    {
        "name": "cbBTC / USDC",
        "address": "0x179492f1f9c7b2e2518a01eda215baab8adf0b02dd3a90fe68059c0cac5686f5",
        "fee_tier": 0.3,
        "token0": "USDC",
        "token1": "cbBTC",
    },
    # --- ETH / cbBTC ---
    {
        "name": "ETH / cbBTC",
        "address": "0xe6195a1f1c8f5d0bcf0a880db26738a1df4f6863017700a8f6377a72d45366f2",
        "fee_tier": 0.3,
        "token0": "ETH",
        "token1": "cbBTC",
    },
    # --- ETH / USDC ---
    {
        "name": "ETH / USDC",
        "address": "0xe070797535b13431808f8fc81fdbe7b41362960ed0b55bc2b6117c49c51b7eb9",
        "fee_tier": 0.3,
        "token0": "ETH",
        "token1": "USDC",
    },
    {
        "name": "ETH / USDC",
        "address": "0x96d4b53a38337a5733179751781178a2613306063c511b78cd02684739288c0a",
        "fee_tier": 0.05,
        "token0": "ETH",
        "token1": "USDC",
    },
    {
        "name": "ETH / USDC",
        "address": "0x23daa22d82b17ba6204d6230506ad2a9d756cfaba4d7b4758237905e67afa370",
        "fee_tier": 2.0,
        "token0": "ETH",
        "token1": "USDC",
    },
    # --- USDC / USDT ---
    {
        "name": "USDC / USDT",
        "address": "0xf13203ddbf2c9816a79b656a1a952521702715d92fea465b84ae2ed6e94a7f22",
        "fee_tier": 0.0007,
        "token0": "USDC",
        "token1": "USDT",
    },
]

# --- Base RPC (for on-chain balanceOf TVL) ---
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")

# Token addresses on Base (lowercase, for TheGraph queries)
TOKENS = {
    "cbBTC": {"address": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", "decimals": 8},
    "WETH": {"address": "0x4200000000000000000000000000000000000006", "decimals": 18},
    "ETH": {"address": "0x4200000000000000000000000000000000000006", "decimals": 18},
    "USDC": {"address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "decimals": 6},
    "USDT": {"address": "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2", "decimals": 6},
}

# Price oracle pools — high-liquidity stablecoin pairs for on-chain USD price derivation
# slot0() is called on these pools to read sqrtPriceX96 and compute token USD prices
PRICE_ORACLE_POOLS = [
    {
        "address": "0xd0b53d9277642d899df5c87a3966a349a798f224",  # WETH/USDC 0.05%
        "token0": "WETH", "token0_decimals": 18,
        "token1": "USDC", "token1_decimals": 6,
        "derive": "WETH",
    },
    {
        "address": "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef",  # cbBTC/USDC 0.05%
        "token0": "USDC", "token0_decimals": 6,
        "token1": "cbBTC", "token1_decimals": 8,
        "derive": "cbBTC",
    },
    {
        "address": "0xd56da2b74ba826f19015e6b7dd9dae1903e85da1",  # USDC/USDT 0.01%
        "token0": "USDC", "token0_decimals": 6,
        "token1": "USDT", "token1_decimals": 6,
        "derive": "USDT",
    },
]

# Cache TTL in seconds
CACHE_TTL = int(os.getenv("CACHE_TTL", "120"))

# Flask
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
