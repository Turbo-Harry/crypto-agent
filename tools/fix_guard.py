"""
修复经验护栏（2026-08-17 用户问"会积累修复经验吗"）——把已修复缺陷的
【不变量】编码成机器可执行的检查，防止修复被后续改动悄悄破坏。

此前教训是"人看的"（pitfalls.md / anomalies 处置说明 / git），clOrdId 修复
后引擎层仍用连字符格式覆盖适配器修复——正是因为没有机器护栏。本模块让
修复经验变成"每次体检都在验证"的硬约束（health_check H12 消费）。

新增修复后在此登记护栏：一条 (名称, 检查函数) = 一条不会复发的教训。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _scan(pattern, rel_glob_ok):
    """在指定文件里找违反不变量(pattern 命中 = 违规)。"""
    hits = []
    for rel in rel_glob_ok:
        src = _read(rel)
        if not src:
            continue
        for m in re.finditer(pattern, src, flags=re.M):
            line = src[:m.start()].count("\n") + 1
            hits.append(f"{rel}:{line}")
    return hits


GUARDS = [
    # G1 clOrdId 只允许字母数字,全项目唯一生成器(2026-08-17 e841b5a)
    # 只匹配代码形态(自拼连字符生成器),注释里的旧格式说明不算违规
    ("G1 clOrdId 无连字符生成器且 make_cl_ord_id 存在",
     lambda: (not _scan(r'cl_ord_id\s*=\s*f"ca-\{', ["engines/directional_trader.py",
                                                     "exchange/okx_adapter.py"])
              and "def make_cl_ord_id" in _read("exchange/okx_adapter.py"))),
    # G2 OKX bar 参数大小写敏感,禁止 .upper()(2026-08-17 de03fcd,1m→1M 月线)
    ("G2 fetch_candles 不做 bar.upper()",
     lambda: ("bar).upper()" not in _read("exchange/okx_adapter.py")
              and "str(bar).upper" not in _read("exchange/okx_adapter.py"))),
    # G3 transport 必须穿透 sCode/sMsg(2026-08-17 64edb7e)
    ("G3 transport 错误穿透 sCode",
     lambda: "sCode=" in _read("exchange/transport.py")),
    # G4 watchdog tick 判真卡死(2026-08-17 64edb7e)
    ("G4 watchdog tick 进度判死",
     lambda: "TICK_TIMEOUT" in _read("tools/watchdog.py")
             and "tick_" in _read("tools/watchdog.py")),
    # G5 平仓路径账本释放与台账闭环同层(2026-08-17 f07871b,H2)
    ("G5 平仓路径 ledger.release 在 if pos 之外",
     lambda: "ledger.release" in _read("engines/directional_trader.py")
             and "台账闭环就必须释放" in _read("engines/directional_trader.py")),
    # G6 evolver 连亏检查用调用方实时 journal(2026-08-17 f07871b)
    ("G6 decide 支持调用方传入 journal",
     lambda: "journal=None" in _read("decision/self_evolving_trader.py")
             and "journal = journal or self.journal" in _read("decision/self_evolving_trader.py")),
    # G7 长扫描逐币插拍监控(2026-08-17 94169fb)
    ("G7 screen_daily 支持 progress_cb 插拍",
     lambda: "progress_cb" in _read("engines/daily_scan.py")
             and "_long_scan_progress" in _read("engines/directional_trader.py")),
    # G8 教训聚合由数据验证强度驱动(2026-08-17 8af6f43)
    ("G8 教训聚合 evidence_strength 存在",
     lambda: "def evidence_strength" in _read("decision/experience_scoring.py")),
    # G9 连亏冷却已按用户指示移除(2026-08-17)——激进采集期刮损不锁死开仓;
    # 若未来有人把"连亏 N 笔，冷却"拒单逻辑加回来,此处立即报警
    ("G9 连亏 3 笔冷却已移除(用户指示)",
     lambda: "笔，冷却" not in _read("decision/self_evolving_trader.py")),
]


def check_fix_guards():
    """返回违规列表: [(护栏名, 详情), ...]。空列表 = 全部护栏在位。"""
    out = []
    for name, fn in GUARDS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            name = f"{name} (检查异常: {e})"
        if not ok:
            out.append((name, "护栏被破坏——对应修复可能已失效,见 tools/fix_guard.py 注释中的 commit"))
    return out


if __name__ == "__main__":
    bad = check_fix_guards()
    if bad:
        for name, detail in bad:
            print(f"❌ {name}: {detail}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(GUARDS)} 条修复经验护栏在位")
