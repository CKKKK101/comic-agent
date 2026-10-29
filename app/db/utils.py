"""通用时间工具。

全项目统一使用中国时区(Asia/Shanghai)的 naive 本地时间:
- 中国无夏令时,naive 本地时间不会漂移;
- 直接落库/展示,无需再转换;
- 若未来需要多时区,再改为 aware UTC 并在展示层转换。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def china_now() -> datetime:
    """当前中国本地时间(naive)。"""
    return datetime.now(CHINA_TZ).replace(tzinfo=None)
