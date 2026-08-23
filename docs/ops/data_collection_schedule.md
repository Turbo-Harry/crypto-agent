# 严谨行情采集与每日对账

当前研究行情写入 `data/market.db` 的 `klines_v2` 表。旧 `klines` 不删除，状态为
`legacy_unverified`，因为它曾将未收线快照用 `INSERT OR IGNORE` 固化；当前研究不得默认读取。

## 数据契约

- 来源：OKX 公共行情 API；不携带账户凭证。
- 场所：仅 `*-USDT-SWAP`，不混入现货。
- 周期：`1m / 15m / 1H / 4H / 1D`；日线按 UTC 边界。
- 收线：只接收 OKX `confirm=1`，并再次校验 `close_time <= as_of_ms`。
- 身份：`source + venue + inst_id + bar + open_time` 唯一。
- 修订：同一 K 线再次取得不同终值时 UPSERT；相同终值幂等不重复。
- 血缘：保存来源、场所、UTC 时区、收线标志、采集时间、as-of 和原始值哈希。
- 缺口：首次扫描缺少的时间槽会逐点再次查询；仍不存在时写入 `market_data_gaps`，不补零、不
  插值。只有未经二次复核的缺口才令命令返回非零。
- 失败：网络、解析、非法 OHLC、空终值或未解释缺口都会令命令返回非零。

## 常驻调度

macOS launchd 作业名为 `com.okx.collect`，入口是 `data/collect_daemon.py`。守护进程会：

1. 按各周期节奏采集最近已收线 K 线；
2. 每个 UTC 新自然日，对前一 UTC 日的全部观察标的、全部五个周期调用历史接口回补；
3. 对每条序列核对精确预期数量与 OHLC/收线/场所/时区不变量，源端缺失需独立复核并留证；
4. 全部完整后才记成功并备份；失败则保留明细，15 分钟后重试。

查看状态和日志：

```bash
launchctl list | grep com.okx.collect
tail -f data/collect.log
```

## 手动采集与对账

只采最近终值：

```bash
PYTHONPATH=.:lib python3 data/collect.py --bars 1m,15m,1H,4H,1D
PYTHONPATH=.:lib python3 data/collect.py --inst BTC-USDT-SWAP --bars 1m,15m
```

回补并严格审计某个 UTC 自然日：

```bash
PYTHONPATH=.:lib python3 data/collect.py --reconcile-date 2026-08-22 --all
PYTHONPATH=.:lib python3 tools/market_data_audit.py --date 2026-08-22
```

两条命令只要存在失败序列、未解释缺口或坏行都会返回退出码 1，适合接监控。运行证据保存在
`market_collection_runs`；数据集资格保存在 `market_datasets`。

## 查询示例

```python
import sqlite3

conn = sqlite3.connect("data/market.db")
rows = conn.execute(
    "SELECT open_time,open,high,low,close,as_of_ms "
    "FROM klines_v2 WHERE source='okx' AND venue='swap' "
    "AND time_zone='UTC' AND confirmed=1 "
    "AND inst_id='BTC-USDT-SWAP' AND bar='15m' ORDER BY open_time"
).fetchall()
```

不得把旧 `klines` 的历史行直接复制到 `klines_v2`。需要历史数据时必须从 OKX 历史接口重新
获取终值，让确认标志、场所身份和 as-of 可审计。

## 备份

每日严格对账成功后，守护进程调用 `data/upload.py` 上传 `market.db`。手动备份命令：

```bash
python3 data/upload.py
python3 data/upload.py --full
```

备份成功不等于数据完整；数据资格以 `market_collection_runs.status=success` 和
`tools/market_data_audit.py` 的 `complete=true` 为准。
