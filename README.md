# Uniswap V3 Pool Monitor

Base 链上 Uniswap V3 池子监控工具，实时追踪 cbBTC/WETH 和 USDC/USDT 池子的交易量、手续费和年化收益率。

## Features

- **双数据源**：GeckoTerminal（免费、无需注册）+ TheGraph Subgraph（备选）
- **多维度指标**：1天 / 7天 / 30天的交易量、手续费、年化 APY
- **Web 仪表盘**：自动每 5 分钟刷新，响应式设计
- **JSON API**：`/api/pools` 接口供其他程序调用

## Quick Start

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务（默认使用 GeckoTerminal，无需配置）
python app.py

# 3. 打开浏览器
# http://localhost:5000
```

## Configuration

复制 `.env.example` 为 `.env` 进行配置：

```bash
cp .env.example .env
```

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `DATA_SOURCE` | 数据源：`geckoterminal` 或 `thegraph` | `geckoterminal` |
| `THEGRAPH_API_KEY` | TheGraph API Key（仅 thegraph 模式需要） | - |
| `CACHE_TTL` | 缓存时间（秒） | `120` |
| `FLASK_PORT` | 服务端口 | `5000` |

### 使用 TheGraph 数据源

1. 注册 [TheGraph Studio](https://thegraph.com/studio/) 获取免费 API Key
2. 在 `.env` 中设置：
   ```
   DATA_SOURCE=thegraph
   THEGRAPH_API_KEY=your_key_here
   ```

## Monitored Pools

| Pool | Address | Fee Tier |
|------|---------|----------|
| cbBTC/WETH | `0x8c7080564b5a792a33ef2fd473fba6364d5495e5` | 0.3% |
| USDC/USDT | `0xd56da2b74ba826f19015e6b7dd9dae1903e85da1` | 0.01% |

## API

### GET /api/pools

返回所有监控池的数据，JSON 格式：

```json
{
  "success": true,
  "pools": [
    {
      "name": "cbBTC / WETH",
      "fee_tier": "0.3%",
      "tvl_usd": 9300000,
      "metrics": {
        "1d": { "volume": 343000, "fees": 1029, "apy": 4.04 },
        "7d": { "volume": 2401000, "fees": 7203, "apy": 4.04 },
        "30d": { "volume": 10290000, "fees": 30870, "apy": 4.04 }
      }
    }
  ],
  "timestamp": 1711900000
}
```

## Adding More Pools

在 `config.py` 的 `POOLS` 列表中添加新池子即可：

```python
POOLS = [
    # ... existing pools ...
    {
        "name": "WETH / USDC",
        "address": "0x...",  # 从 GeckoTerminal 查找池子地址
        "fee_tier": 0.05,
        "token0": "WETH",
        "token1": "USDC",
    },
]
```
