"""飞书通知（共享层）— 各引擎/分析师统一从这里发通知。
避免 decision 反向 import engines（分层违规）。"""
import os
import subprocess

LARK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".lark")
FEISHU_USER_ID = "ou_3c597d18937078f2587b56adb8b960d2"


def notify(msg):
    try:
        subprocess.run([LARK, "im", "+messages-send", "--as", "bot",
                        "--user-id", FEISHU_USER_ID, "--text", msg],
                       capture_output=True, timeout=20)
    except Exception:
        pass
