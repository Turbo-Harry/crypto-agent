"""
service 包 — 交易程序的服务端外壳（FastAPI + uvicorn）。

分层关系（不重写策略逻辑，只在最外层包一层 HTTP）：
  service.main   —— 进程入口：启动 uvicorn + 后台交易线程
  service.app    —— HTTP 接口层（/health /status /signals /pause …）
  service.models —— Pydantic 响应模型（AI 可读 schema，自动进 /docs）
  service.worker —— 后台线程承载 DirectionalTrader 常驻循环 + 暂停开关

依赖方向：service → (directional_trader / exchange / trade_journal …)
交易逻辑零改动；策略代码不 import 任何 web 框架。
"""
