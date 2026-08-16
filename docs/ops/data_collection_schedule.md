# 定时采集数据 — 三种方式

采集范围：
- **加密币**：观察池前 N 个（USDT 计价，7×24）
- **美股代币**：OKX 的 tokenized stocks（X 开头，如 XAAPL/XNVDA/XTSLA，约 65 个）

## 方式一：常驻进程（推荐，最省心）

直接跑，进程自己调度（分钟级每 60 秒、日线+美股每天 UTC 0:05）：

```bash
# 前台运行（测试）
python3 data/collect_daemon.py --bar 1m --top 20

# 后台运行
nohup python3 data/collect_daemon.py --bar 1m --top 20 > collect.log 2>&1 &
```

每日 UTC 0:05 会自动执行：
1. 日线采集（加密币观察池）
2. 日线采集（美股代币）
3. 上传当日快照到 COS

## 手动采集命令

```bash
python3 data/collect.py --bar 1D            # 日线（加密币观察池）
python3 data/collect.py --stocks --bar 1D   # 日线（美股代币）
python3 data/collect.py --bar 1m --top 20   # 分钟级（前20加密币）
```

## 方式二：crontab（Linux/macOS 通用）

```bash
crontab -e
```

加入以下两行：

```cron
# 每分钟采集 1 分钟线（前 20 币）
* * * * * cd /Users/wuhai/Desktop/untitled\ folder/crypto-agent && python3 data/collect.py --bar 1m --top 20 >> data/collect.log 2>&1

# 每天 UTC 0:05 采集日线（全部观察池）
5 0 * * * cd /Users/wuhai/Desktop/untitled\ folder/crypto-agent && python3 data/collect.py --bar 1D >> data/collect.log 2>&1
```

> 注意：cron 每分钟启动一个 Python 进程，开销略大但简单可靠。

## 方式三：launchd（macOS 原生，开机自启）

把 `data/com.okx.collect.plist` 复制到 `~/Library/LaunchAgents/`，然后：

```bash
cp data/com.okx.collect.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.okx.collect.plist
launchctl start com.okx.collect
```

## 容量估算

| 周期 | 采集范围 | 条数/天 | 体积/天 | 体积/年 |
|---|---|---|---|---|
| 1m | 前 20 币 | 28,800 | ~2.3MB | ~850MB |
| 1D | 60 币 | 60 | 可忽略 | 可忽略 |

SQLite 单库可承受 GB 级，一年 850MB 没问题；数据多了可归档到 Parquet。

## 查询积累的数据

```python
import sqlite3
conn = sqlite3.connect("data/market.db")
# 某币某周期的K线
rows = conn.execute(
    "SELECT open_time, open, high, low, close FROM klines "
    "WHERE inst_id='BTC-USDT' AND bar='1m' ORDER BY open_time").fetchall()
```

## 数据上传到腾讯云 COS（备份）

采集的数据自动上传到 COS 备份，形成"本地采集 → 云端备份"闭环。

```bash
# 手动上传（market.db）
python3 data/upload.py

# 手动上传（含历史缓存打包）
python3 data/upload.py --full
```

常驻进程（collect_daemon.py）已在每天日线采集后自动上传 market.db。

COS 存储位置：bucket `clawdbot-1300609114`（region ap-beijing），按日期分目录：

```
crypto-data/
├── 2026-08-15/
│   └── market.db           # 每日采集快照（按 UTC 日期分目录）
├── 2026-08-16/
│   └── market.db           # 每天一个独立快照，可回溯、不覆盖
└── history/
    └── cache_okx.tar.gz    # 6年历史日线缓存（一次性）
```

> 注意：
> - tccli 日志写 `~/.tccli/log/` 受沙箱限制，upload.py 用 HOME 重定向解决
>   （凭证已复制到工作目录 `.tccli-home/.tccli/`）。
> - tccli cos 的 upload/list/delete 必须显式 `--region ap-beijing`，否则报 NoSuchBucket。

