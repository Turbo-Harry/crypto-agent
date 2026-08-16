# 因子挖掘完善 —— 目标 Prompt（主提示词）

> 用法：把下面【主提示词】整段复制给一个新的 agent（或新会话）使用。
> 它自包含全部上下文：目标、原理、业界标准、现状差距、范围、阶段、验收与红线。

---

## 主提示词

你是一个量化因子研究工程师。任务：**完善 `/Users/wuhai/crypto-agent` 仓库的因子挖掘层，使其产出可被信任、且绝不绕过验证门进入交易决策**。

### 1. 目标（三个层次）

1. **方法层**：把因子挖掘从"挖出即有效"升级为"过门才有效"——补齐业界标准的全链路检验。
2. **消费层**：明确影子政策——任何因子不得未经验证门直接进决策；`factor_top.json` 的现存因子在补验前保持"无消费方"状态。
3. **信任层**：每一次因子试验都入账（试验日志），多重检验校正可追溯。

### 2. 原理与业界标准（先理解，再动手；标准非自定）

因子 = 对未来收益有系统预测力的信号。挖掘 = 候选生成 → 严格检验 → 组合。检验链：

| 环节 | 业界门槛 | 来源 |
|---|---|---|
| IC 检验 | \|IC\| > 0.03~0.05（Spearman 秩相关） | 量化实务共识 |
| IC 稳定性 | ICIR > 0.3~0.5；IC 序列 **t 值显著** | 同上 |
| 多重检验校正 | 新因子 **t > 3.0**（Harvey-Liu-Zhu 检验 316 个已发表因子后的结论）；Deflated Sharpe ≥1；PBO < 0.3 | [Harvey-Liu-Zhu (2016)](https://academic.oup.com/rfs/article-abstract/29/1/5/1843824)、[Deflated Sharpe (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)、[PBO (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)、[pypbo](https://github.com/plaintext-capital/pypbo) |
| 分层单调性 | 按因子值分组，未来收益单调（多空价差为正且显著） | 量化实务共识 |
| 成本与换手 | 扣费后净收益仍为正（默认 taker 0.05% × 双向 × 换手次数） | 实务共识；高频因子常死于成本 |
| 独立性 | 与已接受因子相关性 < 0.7（去冗余） | 实务共识 |
| 经济逻辑 | 每个候选必须有可解释的收益来源（风险补偿/行为偏差/微观结构）；**遗传编程产物无逻辑 → 只能当假设生成器，永不自证** | 防数据挖掘（data snooping） |
| 样本外 | walk-forward（时间序切分 ≥5 折、折间不重叠、防前视泄漏） | Pardo/LdP（见仓库设计文档 S1/S3） |

### 3. 现状（已核实，不要重复调查）

- `factors/factor_mining.py`：单因子 IC/3 层分组检验；**缺** t 值、ICIR 落库、成本、换手、去冗余、walk-forward、试验日志、经济逻辑字段。
- `factors/factor_discovery.py` + `factor_evolution.py`：gplearn 遗传编程海挖表达式；历史结论 15→1（walk-forward 2 折），幸存者 `factor_top.json`（median_oos_ic 0.1046）**无消费方**、表达式不可解释——幸存者偏差的标准样本。
- 相关结论：仓库历史回测"传统技术指标策略全亏或过拟合"；防过拟合是仓库最高哲学（AGENTS.md）。

### 4. 范围（做什么 / 不做什么）

**做**：
1. 新增 `factors/factor_gate.py` 验证门：walk-forward 折内 IC 序列 → t 值；t>3.0 promote / 2.0~3.0 watch / <2.0 reject；成本扣除（换手×双向费）；与已接受因子去冗余（|corr|>0.7 拒）；经济逻辑必填。
2. 试验日志：`storage/db.py` SCHEMA 新增 `factor_trials` 表（id/ts/name/rationale/n_samples/n_folds/mean_ic/icir/ic_tstat/gross_spread/turnover/net_spread/status/expression），每次检验必入账。
3. `factor_mining.py` 接入验证门（main 内 4 个因子改为过门输出；网络数据源保留但验证门必须离线可测）。
4. 离线单测 `tests/test_factor_gate.py`（合成数据：随机因子拒 / 单调因子过 / 高成本拒 / 冗余拒 / 无逻辑降级 / 试验日志落库）。
5. Deflated Sharpe/PBO：本期留**接口钩子 + 文档**（试验日志已含所需字段），不实现全量 CSCV——诚实标注"未实现"。

**不做**：
- 不接任何交易决策（影子政策：消费者必须再过验证门，且必须人工批准）。
- 不改 `factor_top.json` 的消费状态（保持无消费方）。
- 不引入新重依赖（只用 numpy/stdlib，gplearn 仅 discovery 用）。
- 不碰交易引擎/风控代码（零回归红线）。

### 5. 阶段与验收

| 阶段 | 产出 | 验收 |
|---|---|---|
| P1 验证门 | `factor_gate.py` | 单测 6 项全绿（见上） |
| P2 试验日志 | SCHEMA + 落库 | 检验 2 次 → factor_trials 恰 2 行（隔离库） |
| P3 接入 | `factor_mining.py` 过门输出 | 离线跑通、网络失败不崩 |
| P4 文档 | 本 prompt + 更新 docs/README.md / llms.txt / optimization_notes | 索引同步 |

### 6. 红线（违反即任务失败）

1. ❌ 任何因子在通过验证门 + 人工批准前进入决策。
2. ❌ 伪造/美化 IC、回测或样本外结果（AGENTS.md 红线 5）。
3. ❌ 用 GP 表达式自证有效性（无经济逻辑 = 假设，不是结论）。
4. ❌ 改动交易引擎/风控/服务层代码。
5. ❌ 破坏既有全量回归（8 文件 98 项必须保持全绿）。

### 7. 完成后输出

1. 改动文件清单 + 每个验收项的实测证据（测试输出）。
2. factor_trials 示例行（隔离库）。
3. 对 `factor_top.json` 幸存因子的重新判定（过门与否）。
