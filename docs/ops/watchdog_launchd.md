# launchd 部署模板（R2-4）— 用户手动执行，脚本不自动注册

> 目标：交易进程崩溃/被杀自动拉起（KeepAlive）+ 僵尸进程由 watchdog 心跳超时 kill（StartInterval）。

## 1. 交易进程 plist（各一份）
文件：`~/Library/LaunchAgents/com.crypto.directional.plist`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.crypto.directional</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/wuhai/crypto-agent/engines/directional_trader.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/wuhai/crypto-agent</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/directional_trader.log</string>
  <key>StandardErrorPath</key><string>/tmp/directional_trader.err</string>
</dict>
</plist>
```
套利进程同构：Label=com.crypto.arb、脚本 engines/trading_main.py、日志 /tmp/trading_main.*。

## 2. watchdog plist
文件：`~/Library/LaunchAgents/com.crypto.watchdog.plist`
```xml
<plist version="1.0">
<dict>
  <key>Label</key><string>com.crypto.watchdog</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/wuhai/crypto-agent/tools/watchdog.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/wuhai/crypto-agent</string>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/watchdog.log</string>
</dict>
</plist>
```

## 3. 加载与验证
```bash
launchctl load ~/Library/LaunchAgents/com.crypto.directional.plist
launchctl load ~/Library/LaunchAgents/com.crypto.arb.plist
launchctl load ~/Library/LaunchAgents/com.crypto.watchdog.plist
# 验证僵尸检测：
kill -STOP $(cat directional_trader.pid)   # 挂起不退出
# 等 ≤60s：watchdog 告警 + kill，launchd KeepAlive 自动重启
# 验证崩溃重启：
kill -9 $(cat directional_trader.pid)      # 直接杀
# launchd 自动拉起
```

## 4. 卸载
```bash
launchctl unload ~/Library/LaunchAgents/com.crypto.watchdog.plist
launchctl unload ~/Library/LaunchAgents/com.crypto.directional.plist
launchctl unload ~/Library/LaunchAgents/com.crypto.arb.plist
```

## 注意事项
- 心跳阈值：directional 30s（2s 主循环 ×15）、arb 300s（60s 主循环 ×5）——非数据拟合，运维参数。
- 心跳缺失连续 3 次（3 分钟）才 kill，防磁盘满导致的无限重启循环。
- 上线前先确认交易进程代码已写 PID/心跳（watchdog.py 配套改动）。
