# -*- coding: utf-8 -*-
"""
🎮 虚拟炒股模拟器（零风险练手版）
==================================
专为不敢投入真金白银的投资新手设计：
  * 200,000 元虚拟启动资金
  * 8 只模拟股票，行情由"随机游走 + 趋势 + 波动"模型生成
  * 支持买卖、K线图、均线、成交量、手续费模拟（A股规则）
  * 支持存档/读档、自动播报、成绩排行榜

运行方式：双击 start_game.bat 或执行  python stock_sim.py

免责声明：本游戏行情为程序模拟生成，仅供学习练手，不构成任何投资建议。
"""

import datetime as dt
import json
import math
import os
import random
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# ============================================================
# 全局配置
# ============================================================
INIT_CASH = 200_000.0            # 初始虚拟资金（约可买1手茅台）
HISTORY_DAYS = 60                # 初始可见的历史K线天数
INIT_DAYS = 900                  # 初始模拟天数（用完后自动扩展，永不结束）
DAYS_PER_EXTEND = 300            # 每次自动扩展的天数
LOT_SIZE = 100                   # A股一手 = 100股
COMMISSION_RATE = 0.00025        # 佣金 万2.5（双向，最低5元）
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.0005          # 印花税 万分之5（仅卖出）
TRANSFER_FEE_RATE = 0.00001      # 过户费 十万分之1（双向）

# ---- 日内 tick 级行情（秒级/毫秒级买卖 + 时间流速） ----
TICK_MS = 1000                 # 每 tick 模拟时长（毫秒）：1000=秒级, 100=0.1秒, 10=10毫秒, 1=毫秒级
INTRADAY_SAMPLES = 480         # 分时图最大采样点数（内存恒定，粒度再细也不怕）
FRAME_MS = 33                  # 自动交易时 UI 刷新帧间隔（毫秒，约 30fps）
DEFAULT_SPEED = 50             # 默认流速：每秒推进的 tick 数
MAX_SPEED = 10000              # 流速滑块上限（tick/秒）
SESSION_MS = 4 * 3600 * 1000   # 单日交易时段总毫秒数（A股 4 小时：09:30-11:30 + 13:00-15:00）

SAVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "save_game.json")
LEADER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard.json")

# 真实行情数据目录候选（用户下载的A股行情CSV）
REAL_DATA_DIRS = [
    r"C:\Users\<用户名>\Desktop\deepseek_chat_ui\client\workspace\workspace\a股大盘股行情",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "a股大盘股行情"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "a股大盘股行情"),
    "a股大盘股行情",
]

# 股票定义：(代码, 名称, 初始价格)
STOCK_DEFS = [
    ("600519", "贵州茅台", 1450.0),
    ("300750", "宁德时代", 185.0),
    ("002594", "比亚迪",   245.0),
    ("601318", "中国平安",  52.0),
    ("600036", "招商银行",  34.0),
    ("000858", "五粮液",   128.0),
    ("601012", "隆基绿能",  18.5),
    ("600276", "恒瑞医药",  45.0),
]

MARKET_NEWS = [
    "📈 央行宣布降准，市场情绪回暖",
    "📉 国际油价大涨，能源板块承压",
    "🏭 某上市公司披露重大合同公告",
    "🔍 北向资金今日净流入约 30 亿元",
    "⚠️ 监管部门提示市场波动风险",
    "🚀 科技板块获主力资金追捧",
    "😱 市场恐慌指数显著上升",
    "💰 多家上市公司宣布股份回购计划",
    "🏦 存款利率下调，资金流向股市",
    "🌐 海外市场普涨，A股高开",
    "📊 经济数据好于预期，信心提振",
    "🛑 部分高位股遭获利回吐",
]


# ============================================================
# 工具函数
# ============================================================
def trading_days(start: dt.date, count: int):
    """生成 count 个交易日（跳过周末）。"""
    days, d = [], start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def t_noise(df: int = 4) -> float:
    """标准化 Student-t 分布采样（方差=1，df 自由度）。

    A股日收益呈尖峰厚尾，t分布（df=3~4）比正态产生更多极端值，
    可模拟“突发跳水”和“直线拉升”。这里用 Z/sqrt(chi2/df) 构造。
    """
    z = random.gauss(0, 1)
    v = sum(random.gauss(0, 1) ** 2 for _ in range(df))
    return z / math.sqrt(v / df) * math.sqrt((df - 2) / df)


# ============================================================
# 市场环境模型：A股特征化行情生成器
# ============================================================
class MarketEnv:
    """市场共同因子，融合 A 股五大特征：

    1. Student-t(df=4) 厚尾噪声         —— 尖峰厚尾，突发大阳大阴
    2. EWMA 波动率聚集                  —— σ²ₜ=0.94σ²ₜ₋₁+0.06r²ₜ₋₁，恐慌期巨震/平静期缩量
    3. 复合泊松跳跃（每月1~2次）        —— 跳空缺口，跳幅 N(±2%, 1%)
    4. 涨跌停截断（±10%）              —— 极端日封板
    5. 两状态马尔可夫链（牛/熊）        —— 牛市漂移+0.06%/天，熊市-0.06%/天且波动×1.2，
                                           每 20~60 个交易日切换

    mode="sim"  : 纯模拟模式，默认参数
    mode="real" : 真实行情续接模式，参数由真实数据校准
    """

    def __init__(self, mode: str = "sim", n_days: int = 0, params: dict = None):
        self.mode = mode
        if mode == "real" and params:
            self.mu_m = params["mu_m"]                 # 真实市场长期漂移
            self.base_sigma = params["base_sigma"]     # 真实市场基准日波动
            self.drift_bull = params.get("drift_bull", self.mu_m + 0.0006)
            self.drift_bear = params.get("drift_bear", self.mu_m - 0.0006)
        else:
            self.mu_m = 0.0001
            self.base_sigma = 0.015
            self.drift_bull = 0.0006
            self.drift_bear = -0.0006
        self.state = 1 if random.random() < 0.5 else 0   # 1=牛市 0=熊市
        self.state_remaining = random.randint(20, 60)
        self.ewma_var = self.base_sigma ** 2
        self.days_to_jump = random.randint(15, 35)
        self.last_ret = 0.0
        self.series = []
        for _ in range(n_days):
            self.next()

    def next(self) -> float:
        # 1) 马尔可夫牛熊状态切换
        self.state_remaining -= 1
        if self.state_remaining <= 0:
            self.state = 1 - self.state
            self.state_remaining = random.randint(20, 60)
        # 2) 漂移：牛市为正，熊市为负（波动放大1.2倍）
        drift = self.drift_bull if self.state == 1 else self.drift_bear
        # 3) EWMA 波动率聚集（平静期缩量 / 恐慌期巨震）
        self.ewma_var = 0.94 * self.ewma_var + 0.06 * self.last_ret ** 2
        sigma_t = math.sqrt(self.ewma_var) * (1.2 if self.state == 0 else 1.0)
        sigma_t = max(0.004, min(0.05, sigma_t))
        # 4) t分布厚尾噪声（市场层用 df=3，尾部更接近真实A股）
        noise = sigma_t * t_noise(3)
        # 5) 复合泊松跳跃（跳空缺口）
        jump = 0.0
        self.days_to_jump -= 1
        if self.days_to_jump <= 0:
            self.days_to_jump = random.randint(15, 35)
            jump = random.choice((-1.0, 1.0)) * abs(random.gauss(0.025, 0.012))
        # 6) 涨跌停截断
        ret = drift + noise + jump
        ret = max(-0.10, min(0.10, ret))
        self.last_ret = ret
        self.series.append(ret)
        return ret

    def to_dict(self):
        return {"mode": self.mode, "mu_m": self.mu_m, "base_sigma": self.base_sigma,
                "drift_bull": self.drift_bull, "drift_bear": self.drift_bear,
                "state": self.state, "state_remaining": self.state_remaining,
                "ewma_var": self.ewma_var, "days_to_jump": self.days_to_jump,
                "last_ret": self.last_ret, "series": self.series}

    @classmethod
    def from_dict(cls, d):
        # 注意：不能用 cls(...)，否则会消耗随机数导致读档后行情不一致
        m = cls.__new__(cls)
        m.mode = d.get("mode", "sim")
        m.mu_m = d.get("mu_m", 0.0001)
        m.base_sigma = d.get("base_sigma", 0.015)
        m.drift_bull = d.get("drift_bull", m.mu_m + 0.0006)
        m.drift_bear = d.get("drift_bear", m.mu_m - 0.0006)
        m.state = d.get("state", 1)
        m.state_remaining = d.get("state_remaining", 30)
        m.ewma_var = d.get("ewma_var", m.base_sigma ** 2)
        m.days_to_jump = d.get("days_to_jump", 20)
        m.last_ret = d.get("last_ret", 0.0)
        m.series = d.get("series", [])
        return m


# ============================================================
# 真实行情数据加载与校准
# ============================================================
def find_real_data_dir() -> str:
    """在候选路径中查找真实行情目录。"""
    for p in REAL_DATA_DIRS:
        if os.path.isdir(p):
            return p
    return None


def load_real_data(data_dir: str) -> list:
    """读取目录下所有 *_股票名.csv（跳过 all_ 开头的汇总文件），
    返回 [(code, name, rows)]，rows 为按日期升序的 OHLCV 列表。"""
    import csv
    result = []
    for fn in sorted(os.listdir(data_dir)):
        if not (fn.endswith(".csv") and "_" in fn and not fn.startswith("all_")):
            continue
        path = os.path.join(data_dir, fn)
        rows = []
        try:
            with open(path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    try:
                        rows.append({
                            "date": row["日期"].strip(),
                            "open": float(row["开盘"]),
                            "high": float(row["最高"]),
                            "low": float(row["最低"]),
                            "close": float(row["收盘"]),
                            "volume": int(float(row.get("成交量(手)", 0) or 0)),
                        })
                    except (ValueError, KeyError, TypeError):
                        continue
        except (OSError, UnicodeDecodeError):
            continue
        if len(rows) >= 30:
            stem = fn.rsplit(".", 1)[0]
            code, name = stem.split("_", 1)
            result.append((code.strip(), name.replace(" ", "").strip(), rows))
    return result


def calibrate_stocks(stocks: list) -> dict:
    """从真实日收益序列校准单因子模型参数。

    个股收益 r_i = alpha + beta × 市场收益 + 噪声
    返回市场续接参数 {mu_m, beta_m, noise_sigma}。
    """
    n = min(len(s.history) for s in stocks)
    rets = []
    for s in stocks:
        closes = [s.history[i]["close"] for i in range(n)]
        rets.append([math.log(closes[i] / closes[i - 1])
                     for i in range(1, n)])
    m = len(rets[0])
    market = [sum(rets[j][i] for j in range(len(rets))) / len(rets)
              for i in range(m)]
    mu_m = sum(market) / m
    var_m = sum((x - mu_m) ** 2 for x in market) / m
    if var_m < 1e-12:
        var_m = 1e-12
    std_m = math.sqrt(var_m)

    for j, s in enumerate(stocks):
        r = rets[j]
        mean_r = sum(r) / m
        cov = sum((r[i] - mean_r) * (market[i] - mu_m) for i in range(m)) / m
        s.beta = cov / var_m
        s.alpha = mean_r - s.beta * mu_m
        resid = [r[i] - s.alpha - s.beta * market[i] for i in range(m)]
        s.sigma = math.sqrt(sum(x * x for x in resid) / m) or 0.01

    # 续接市场：波动水平保留真实市场的标准差，牛熊漂移围绕真实均值对称
    return {"mu_m": mu_m, "base_sigma": std_m,
            "drift_bull": mu_m + 0.0006, "drift_bear": mu_m - 0.0006}


# ============================================================
# 股票类：行情模型
# ============================================================
class Stock:
    """单只股票：市场因子 + 个股特征生成每日 OHLCV。

    日收益 = alpha(个股漂移) + beta(市场敏感度) × 市场收益 + sigma(个股噪声)
    """

    def __init__(self, code: str, name: str, init_price: float,
                 market_series: list = None):
        self.code = code
        self.name = name
        self.init_price = init_price
        # 个股特征：alpha 长期接近 0（年化约 5%），beta 对市场敏感度，sigma 个股波动
        self.alpha = random.uniform(-0.0002, 0.0006)
        self.beta = random.uniform(0.7, 1.3)
        self.sigma = random.uniform(0.012, 0.028)
        self.ewma_var = self.sigma ** 2      # 个股波动率聚集状态
        # 长期基准成交量（手）：高价股成交少、低价股成交多
        self.base_volume = int(random.uniform(1.5, 3.5) * 2_000_000
                               / max(1.0, init_price / 10))
        self.history = []          # 列表：[{date, open, high, low, close, volume}]
        self._seed_history(HISTORY_DAYS, market_series or [])
        # ---- 日内 tick 级行情（秒级/毫秒级买卖） ----
        self.tick_ms = TICK_MS       # 当前 tick 粒度（模拟毫秒/tick）
        self.tick_price = None       # 当前 tick 成交价（盘中实时价）
        self.tick_idx = 0            # 当日已推进的 tick 序号（0 起）
        self.intraday = []           # 当日分时采样 [(tick_idx, price)]，供分时图
        self.day_open = None         # 当日开盘价（tick 路径起点）
        self.day_close = None        # 当日收盘价（tick 路径终点 = 日K close）

    # ----- 历史数据生成 -----
    def _seed_history(self, n: int, market_series: list):
        price = self.init_price
        volume = self.base_volume
        for i in range(n):
            mret = market_series[i] if i < len(market_series) else 0.0
            price = self._next_close(price, mret)
            volume = self._next_volume(volume, 0.0)
            o, c = price * random.uniform(0.995, 1.005), price
            hi = max(o, c) * random.uniform(1.0, 1.02)
            lo = min(o, c) * random.uniform(0.98, 1.0)
            self.history.append({
                "date": "", "open": o, "high": hi, "low": lo,
                "close": c, "volume": int(volume),
            })

    def _next_close(self, prev_close: float, market_ret: float) -> float:
        """A股式个股日收益：t分布厚尾 + EWMA波动聚集 + 涨跌停截断。"""
        # EWMA 波动率聚集：昨日收益越大，今日波动越大
        if len(self.history) >= 2:
            prev_ret = ((self.history[-1]["close"] - self.history[-2]["close"])
                        / self.history[-2]["close"])
        else:
            prev_ret = 0.0
        self.ewma_var = 0.94 * self.ewma_var + 0.06 * prev_ret ** 2
        sigma_t = max(0.004, min(0.06, math.sqrt(self.ewma_var)))
        # t分布厚尾噪声（df=4）+ 市场贝塔
        ret = self.alpha + self.beta * market_ret + sigma_t * t_noise(4)
        # 涨跌停截断（主板 ±10%）
        ret = max(-0.10, min(0.10, ret))
        price = prev_close * (1 + ret)
        return max(0.5, round(price, 2))

    def _next_volume(self, prev_volume: int, ret: float) -> int:
        """成交量围绕长期基准波动：大涨放量（最多约3倍）、缩量横盘，不无限膨胀。"""
        noise = math.exp(0.3 * random.gauss(0, 1))
        vol_ratio = 1.0 + min(abs(ret), 0.10) / 0.02 * 2.0   # 单日收益2%≈放量2倍
        v = self.base_volume * noise * vol_ratio
        v = prev_volume * 0.5 + v * 0.5                      # 与昨日平滑过渡
        return max(500, int(v))

    def next_day(self, date: dt.date, market_ret: float):
        """生成下一个交易日行情（开盘考虑跳空缺口，涨跌停日一字板）。"""
        prev = self.history[-1]
        close = self._next_close(prev["close"], market_ret)
        ret = (close - prev["close"]) / prev["close"] if prev["close"] else 0.0
        if abs(ret) >= 0.099:
            # 涨停/跌停一字板：开=高=低=收
            open_ = hi = lo = close
        else:
            # 开盘价吸收部分当日收益（跳跃日自然形成跳空缺口）
            open_ = round(prev["close"] * (1 + ret * random.uniform(0.15, 0.5)), 2)
            hi = round(max(open_, close) * random.uniform(1.0, 1.018), 2)
            lo = round(min(open_, close) * random.uniform(0.982, 1.0), 2)
        volume = self._next_volume(prev["volume"], ret)
        self.history.append({
            "date": date.strftime("%Y-%m-%d"), "open": open_, "high": hi,
            "low": lo, "close": close, "volume": int(volume),
        })
        # 初始化当日 tick 路径（一字板时全天价格不变）
        self.tick_idx = 0
        self.day_open = open_
        self.day_close = close
        self.tick_price = open_
        self.intraday = [(0, open_)]

    # ----- 日内 tick 级行情（秒级/毫秒级） -----
    def _limit_board(self) -> bool:
        """当日是否一字板（开=高=低=收）：盘中价格全程不动。"""
        k = self.history[-1] if self.history else None
        return bool(k and k["open"] == k["high"] == k["low"] == k["close"])

    def advance_tick(self):
        """推进一个日内 tick：沿「开盘→收盘」随机桥路径走一步。

        惰性生成（不预存整条路径）：无论粒度细到秒还是毫秒，内存恒定。
        """
        n = max(1, SESSION_MS // max(1, self.tick_ms))
        i = self.tick_idx + 1
        if i >= n:
            return
        if self.day_open is None:
            self.day_open = self.day_close = (
                self.history[-1]["close"] if self.history else self.init_price)
        if self.tick_price is None:
            self.tick_price = self.day_open
        if self._limit_board():
            price = self.day_close
        else:
            prev = self.tick_price
            remaining = n - i
            # 日波动按 sqrt(1/n) 拆到每个 tick；t 分布厚尾保留盘中异动
            sigma_tick = self.sigma * math.sqrt(1.0 / n)
            noise = sigma_tick * t_noise(4) * prev
            drift = (self.day_close - prev) / remaining     # 线性桥：逐渐趋向收盘价
            price = prev + drift + noise
            if i >= n - 1:
                price = self.day_close                      # 最后一 tick 精确收盘
            # 涨跌停限制（相对昨收 ±10%，主板）
            pc = self.prev_close()
            price = max(pc * 0.9, min(pc * 1.1, price))
            price = round(max(0.5, price), 2)
        self.tick_price = price
        self.tick_idx = i
        # 分时采样（抽稀，控制点数，内存恒定）
        every = max(1, n // INTRADAY_SAMPLES)
        if i % every == 0 or i >= n - 1:
            self.intraday.append((i, price))
        if len(self.intraday) > INTRADAY_SAMPLES + 8:
            self.intraday = self.intraday[-(INTRADAY_SAMPLES + 8):]

    def fast_forward(self):
        """快进到当日收盘：跳过剩余 tick，价格直接落在收盘价。"""
        n = max(1, SESSION_MS // max(1, self.tick_ms))
        self.tick_idx = n - 1
        self.tick_price = (self.day_close if self.day_close is not None
                           else (self.history[-1]["close"] if self.history else self.init_price))
        if not self.intraday or self.intraday[-1][0] < n - 1:
            self.intraday.append((n - 1, self.tick_price))

    # ----- 行情查询 -----
    def current_price(self) -> float:
        """当前成交价：盘中取最新 tick 价，未开盘/收盘取最新日K收盘。"""
        if self.tick_price is not None:
            return self.tick_price
        return self.history[-1]["close"] if self.history else self.init_price

    def prev_close(self) -> float:
        """上一交易日收盘价（昨日收盘）。"""
        if len(self.history) >= 2:
            return self.history[-2]["close"]
        return self.history[-1]["close"] if self.history else self.init_price

    def change_pct(self) -> float:
        p, pc = self.current_price(), self.prev_close()
        return (p - pc) / pc * 100 if pc else 0.0

    def to_dict(self):
        return {"code": self.code, "name": self.name,
                "alpha": self.alpha, "beta": self.beta, "sigma": self.sigma,
                "history": self.history,
                "tick_ms": self.tick_ms, "tick_price": self.tick_price,
                "tick_idx": self.tick_idx, "intraday": self.intraday,
                "day_open": self.day_open, "day_close": self.day_close}

    @classmethod
    def from_dict(cls, d):
        s = cls.__new__(cls)
        s.code, s.name = d["code"], d["name"]
        # 兼容旧版存档（只有 mu/sigma）
        s.alpha = d.get("alpha", d.get("mu", 0.0002))
        s.beta = d.get("beta", 1.0)
        s.sigma = d.get("sigma", d.get("mu", 0.02))
        s.ewma_var = s.sigma ** 2
        vols = [k["volume"] for k in d["history"][-20:]] or [50000]
        s.base_volume = int(sum(vols) / len(vols))
        s.init_price = d["history"][0]["close"] if d["history"] else 100.0
        s.history = d["history"]
        # 日内 tick 状态（旧版存档无此字段，自动补默认值）
        s.tick_ms = d.get("tick_ms", TICK_MS)
        s.tick_price = d.get("tick_price")
        s.tick_idx = d.get("tick_idx", 0)
        s.intraday = d.get("intraday", [])
        s.day_open = d.get("day_open")
        s.day_close = d.get("day_close")
        return s

    @classmethod
    def from_real(cls, code: str, name: str, rows: list):
        """用真实行情数据构造股票（参数稍后由 calibrate_stocks 校准）。"""
        s = cls.__new__(cls)
        s.code = code
        s.name = name
        s.init_price = rows[0]["close"] if rows else 100.0
        s.alpha = 0.0
        s.beta = 1.0
        s.sigma = 0.02
        s.ewma_var = s.sigma ** 2
        vols = [k["volume"] for k in rows[-20:]] or [50000]
        s.base_volume = int(sum(vols) / len(vols))
        s.history = rows
        # 日内 tick 状态（真实数据回放完成后由 next_day 初始化）
        s.tick_ms = TICK_MS
        s.tick_price = None
        s.tick_idx = 0
        s.intraday = []
        s.day_open = None
        s.day_close = None
        return s


# ============================================================
# 游戏核心：资金 / 持仓 / 交易 / 存读档
# ============================================================
class Game:
    def __init__(self, cash: float = INIT_CASH, load: bool = False, mode: str = None):
        if load and os.path.exists(SAVE_FILE):
            self._load()
        elif mode == "real" or (mode is None and find_real_data_dir()):
            self._init_real(cash)
        else:
            self._init_sim(cash)

    # ---------- 初始化：纯模拟模式 ----------
    def _init_sim(self, cash: float):
        self.mode = "sim"
        self.market = MarketEnv("sim", HISTORY_DAYS)
        self.stocks = [Stock(c, n, p, self.market.series) for c, n, p in STOCK_DEFS]
        self.cash = cash
        self.holdings = {}          # code -> {"shares": int, "cost": float(每股成本含费)}
        self.trades = []            # 最近交易记录（新的在前）
        self.start_date = dt.date(2023, 1, 3)
        self.days = trading_days(self.start_date, INIT_DAYS)
        self.day_idx = HISTORY_DAYS  # 当前处于第 day_idx 个交易日（已有 HISTORY_DAYS 天历史）
        self.days_extended = False    # 标记：本轮是否扩展了时间线
        for i in range(HISTORY_DAYS):
            d = self.days[i]
            for st in self.stocks:
                st.history[-1]["date"] = d.strftime("%Y-%m-%d")
        for st in self.stocks:
            st.history = st.history[:HISTORY_DAYS]
        self.initial_cash = cash
        self.best_assets = cash
        self.assets_history = [float(cash)]   # 每个交易日的总资产（收益曲线数据）
        # 直接开盘：生成「今天」的日K与日内 tick 路径，盘中行情立即可买卖
        self.tick_ms = TICK_MS
        self.open_new_day()

    # ---------- 初始化：真实行情模式 ----------
    def _init_real(self, cash: float):
        self.mode = "real"
        data_dir = find_real_data_dir()
        data = load_real_data(data_dir) if data_dir else []
        if not data:
            # 真实数据缺失时回退到纯模拟
            return self._init_sim(cash)
        self.stocks = [Stock.from_real(code, name, rows) for code, name, rows in data]
        market_params = calibrate_stocks(self.stocks)
        self.market = MarketEnv("real", 0, market_params)
        self.cash = cash
        self.holdings = {}
        self.trades = []
        # 日期线：真实数据的交易日（含节假日休市）+ 之后模拟生成的交易日
        real_dates = [dt.date.fromisoformat(r["date"])
                      for r in self.stocks[0].history]
        self.start_date = real_dates[0]
        future = trading_days(real_dates[-1] + dt.timedelta(days=1), INIT_DAYS)
        self.days = real_dates + future
        self.day_idx = len(real_dates) - 1   # 停在真实数据最后一天
        self.days_extended = False
        self.initial_cash = cash
        self.best_assets = cash
        self.assets_history = [float(cash)]
        # 直接开盘：真实数据最后一天之后进入模拟交易日盘中
        self.tick_ms = TICK_MS
        self.open_new_day()

    # ---------- 基础查询 ----------
    def stock(self, code: str) -> Stock:
        for st in self.stocks:
            if st.code == code:
                return st
        raise KeyError(code)

    def current_date(self) -> dt.date:
        return self.days[min(self.day_idx, len(self.days) - 1)]

    def price_of(self, code: str) -> float:
        return self.stock(code).current_price()

    def market_value(self, code: str) -> float:
        h = self.holdings.get(code)
        return h["shares"] * self.price_of(code) if h else 0.0

    def total_market_value(self) -> float:
        return sum(self.market_value(c) for c in self.holdings)

    def total_assets(self) -> float:
        return self.cash + self.total_market_value()

    def total_profit(self) -> float:
        return self.total_assets() - self.initial_cash

    def return_rate(self) -> float:
        return self.total_profit() / self.initial_cash * 100 if self.initial_cash else 0.0

    def floating_profit(self, code: str) -> float:
        h = self.holdings.get(code)
        if not h:
            return 0.0
        return (self.price_of(code) - h["cost"]) * h["shares"]

    # ---------- 费用计算 ----------
    @staticmethod
    def buy_fee(amount: float) -> float:
        return max(MIN_COMMISSION, amount * COMMISSION_RATE) + amount * TRANSFER_FEE_RATE

    @staticmethod
    def sell_fee(amount: float) -> float:
        return (max(MIN_COMMISSION, amount * COMMISSION_RATE)
                + amount * STAMP_TAX_RATE + amount * TRANSFER_FEE_RATE)

    # ---------- 交易 ----------
    def max_buyable(self, code: str) -> int:
        """当前资金最多可买几手（100股整数倍）。"""
        price = self.price_of(code)
        if price <= 0:
            return 0
        # 估算：每手成本 = price*100 + 手续费
        per_lot = price * LOT_SIZE + self.buy_fee(price * LOT_SIZE)
        return int(self.cash // per_lot) * LOT_SIZE

    def buy(self, code: str, shares: int):
        """买入（A股规则：100股整数倍）。返回成交信息 dict。"""
        st = self.stock(code)
        if shares <= 0:
            raise ValueError("买入数量必须大于 0")
        if shares % LOT_SIZE != 0:
            raise ValueError(f"买入数量须为 {LOT_SIZE} 股的整数倍（一手）")
        price = st.current_price()
        amount = price * shares
        fee = self.buy_fee(amount)
        total = amount + fee
        if total > self.cash + 1e-6:
            raise ValueError(f"资金不足！需要 {fmt_money(total)} 元，可用 {fmt_money(self.cash)} 元")
        self.cash -= total
        h = self.holdings.setdefault(code, {"shares": 0, "cost": 0.0})
        new_shares = h["shares"] + shares
        h["cost"] = (h["cost"] * h["shares"] + amount + fee) / new_shares
        h["shares"] = new_shares
        self.assets_history[-1] = self.total_assets()   # 当天资产即时更新
        self.trades.insert(0, {
            "date": f"{self.current_date().strftime('%Y-%m-%d')} {self.current_tick_time()}",
            "code": code, "name": st.name, "action": "买入", "price": price,
            "shares": shares, "amount": amount, "fee": fee, "profit": 0.0,
        })
        return {"price": price, "amount": amount, "fee": fee, "shares": shares}

    def sell(self, code: str, shares: int):
        """卖出（不足一手的零股可一次性卖出）。"""
        st = self.stock(code)
        h = self.holdings.get(code)
        if not h or h["shares"] <= 0:
            raise ValueError("没有该股票持仓")
        if shares <= 0:
            raise ValueError("卖出数量必须大于 0")
        if shares > h["shares"]:
            raise ValueError(f"持仓不足！当前持有 {h['shares']} 股")
        if shares % LOT_SIZE != 0 and shares != h["shares"]:
            raise ValueError("卖出须为整手，或一次性清仓剩余零股")
        price = st.current_price()
        amount = price * shares
        fee = self.sell_fee(amount)
        profit = (price - h["cost"]) * shares - fee
        self.cash += amount - fee
        h["shares"] -= shares
        if h["shares"] == 0:
            del self.holdings[code]
        self.assets_history[-1] = self.total_assets()   # 当天资产即时更新
        self.trades.insert(0, {
            "date": f"{self.current_date().strftime('%Y-%m-%d')} {self.current_tick_time()}",
            "code": code, "name": st.name, "action": "卖出", "price": price,
            "shares": shares, "amount": amount, "fee": fee,
            "profit": round(profit, 2),
        })
        return {"price": price, "amount": amount, "fee": fee,
                "shares": shares, "profit": profit}

    def sell_all(self, code: str):
        h = self.holdings.get(code)
        if h and h["shares"] > 0:
            return self.sell(code, h["shares"])

    def liquidate(self):
        """一键清仓。"""
        for code in list(self.holdings.keys()):
            self.sell_all(code)

    # ---------- 日内 tick 推进（秒级/毫秒级买卖） ----------
    def ticks_per_day(self) -> int:
        """一天内的 tick 总数（4小时交易时段 ÷ 每 tick 模拟毫秒）。"""
        return max(1, SESSION_MS // max(1, self.tick_ms))

    def open_new_day(self):
        """进入下一个交易日：生成当日日K与日内 tick 路径起点（开盘）。"""
        if self.day_idx >= len(self.days) - 1:
            self._extend_days()
        self.day_idx += 1
        d = self.days[self.day_idx]
        mret = self.market.next()
        for st in self.stocks:
            st.tick_ms = self.tick_ms
            st.next_day(d, mret)
        self.best_assets = max(self.best_assets, self.total_assets())
        self.assets_history.append(self.total_assets())

    def advance_tick(self) -> bool:
        """推进一个行情 tick（盘中价格跳动一步）。

        返回 True 表示当日已收盘（还需调用 open_new_day() 进入次日）。
        """
        st0 = self.stocks[0]
        if st0.tick_idx >= self.ticks_per_day() - 1:
            self.assets_history[-1] = self.total_assets()   # 收盘资产入曲线
            return True
        for st in self.stocks:
            st.advance_tick()
        self.best_assets = max(self.best_assets, self.total_assets())
        return False

    def fast_forward_day(self):
        """快进：跳过当日剩余 tick 直接收盘（『下一交易日』按钮用）。"""
        for st in self.stocks:
            st.fast_forward()
        self.assets_history[-1] = self.total_assets()

    def set_tick_ms(self, ms: int):
        """切换行情粒度（1000=秒级 … 1=毫秒级），即时生效，不重置进度。"""
        ms = max(1, min(1000, int(ms)))
        if ms == self.tick_ms:
            return
        old_n = self.ticks_per_day()
        old_idx = self.stocks[0].tick_idx
        self.tick_ms = ms
        new_n = self.ticks_per_day()
        # 重新标定 tick 序号与分时采样坐标：保持当前模拟时刻不变
        new_idx = min(new_n - 1, max(0, int(round(old_idx * new_n / old_n))))
        for st in self.stocks:
            st.tick_ms = ms
            st.tick_idx = new_idx
            st.intraday = [(min(new_n - 1, int(round(i * new_n / old_n))), p)
                           for i, p in st.intraday]

    def current_tick_time(self) -> str:
        """当前模拟盘中时间：秒级显示 HH:MM:SS，毫秒级显示 HH:MM:SS.mmm。"""
        n = self.ticks_per_day()
        idx = max(0, min(self.stocks[0].tick_idx, n - 1))
        half = n // 2
        if idx < half:
            t = 9 * 3600 * 1000 + 30 * 60 * 1000 + idx * self.tick_ms      # 09:30 起
        else:
            t = 13 * 3600 * 1000 + (idx - half) * self.tick_ms             # 13:00 起
        hh, rem = divmod(max(0, t), 3600 * 1000)
        mm, rem = divmod(rem, 60 * 1000)
        ss, mmm = divmod(rem, 1000)
        if self.tick_ms >= 1000:
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return f"{hh:02d}:{mm:02d}:{ss:02d}.{mmm:03d}"

    def advance_day(self) -> bool:
        """快进到下一个交易日开盘（跳过今日剩余 tick）。时间线自动扩展，永不结束。"""
        self.fast_forward_day()
        return True

    def _extend_days(self):
        """在时间线末尾追加新的交易日。"""
        last = self.days[-1]
        self.days.extend(trading_days(last + dt.timedelta(days=1), DAYS_PER_EXTEND))
        self.days_extended = True

    def is_game_over(self) -> bool:
        """破产判定：无持仓且资金 < 1 元。"""
        return self.cash < 1.0 and not self.holdings

    # ---------- 存读档 ----------
    def save(self, path: str = SAVE_FILE):
        data = {
            "mode": self.mode,
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "day_idx": self.day_idx,
            "best_assets": self.best_assets,
            "start_date": self.start_date.isoformat(),
            "days": [d.isoformat() for d in self.days],
            "stocks": [st.to_dict() for st in self.stocks],
            "holdings": {c: {"shares": h["shares"], "cost": h["cost"]}
                         for c, h in self.holdings.items()},
            "trades": self.trades,
            "assets_history": self.assets_history,
            "market": self.market.to_dict(),
            "tick_ms": self.tick_ms,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    def _load(self, path: str = SAVE_FILE):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.stocks = [Stock.from_dict(d) for d in data["stocks"]]
        self.cash = data["cash"]
        self.initial_cash = data.get("initial_cash", INIT_CASH)
        self.day_idx = data["day_idx"]
        self.best_assets = data.get("best_assets", self.cash)
        self.start_date = dt.date.fromisoformat(data["start_date"])
        self.days = [dt.date.fromisoformat(s) for s in data["days"]]
        self.holdings = {c: {"shares": h["shares"], "cost": h["cost"]}
                         for c, h in data["holdings"].items()}
        self.trades = data["trades"]
        self.days_extended = False
        # 兼容旧存档：没有收益曲线数据时从当前资产开始
        self.assets_history = data.get("assets_history", [self.total_assets()])
        # 恢复市场环境（旧存档没有则用空序列，行情从头生成）
        self.mode = data.get("mode", "sim")
        if "market" in data:
            self.market = MarketEnv.from_dict(data["market"])
        else:
            self.market = MarketEnv("sim", 0)
            self.market.series = data.get("market_series", [])
        # 日内 tick 状态：新档直接恢复；旧档（无 tick_ms）打开新的一天进入盘中
        self.tick_ms = data.get("tick_ms", TICK_MS)
        for st in self.stocks:
            st.tick_ms = self.tick_ms
        if "tick_ms" not in data or self.stocks[0].tick_price is None:
            self.open_new_day()


# ============================================================
# 排行榜
# ============================================================
def load_leaderboard() -> list:
    if os.path.exists(LEADER_FILE):
        try:
            with open(LEADER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_leaderboard(entry: dict):
    board = load_leaderboard()
    board.append(entry)
    board.sort(key=lambda e: e["rate"], reverse=True)
    board = board[:20]
    with open(LEADER_FILE, "w", encoding="utf-8") as f:
        json.dump(board, f, ensure_ascii=False, indent=1)


# ============================================================
# K线图绘制（Canvas 自绘，红涨绿跌）
# ============================================================
UP_COLOR = "#e0473c"     # 涨：红
DOWN_COLOR = "#18a05e"   # 跌：绿
FLAT_COLOR = "#888888"
BG_COLOR = "#10141c"
GRID_COLOR = "#262c38"
TEXT_COLOR = "#c8d0dc"
AXIS_COLOR = "#4a5568"
MA5_COLOR = "#f5b942"
MA10_COLOR = "#4a9eff"
MA20_COLOR = "#c06ee8"
VOL_COLOR = "#2a3140"


class Chart:
    """在 Canvas 上绘制蜡烛图 + 均线 + 成交量，支持缩放（count）与平移（offset）。"""

    def __init__(self, canvas: tk.Canvas, width: int, height: int):
        self.canvas = canvas
        self.w, self.h = width, height
        self.price_h = int(height * 0.68)      # K线区域高度
        self.vol_h = height - self.price_h - 34  # 成交量区域高度
        self.left, self.right = 46, 8
        self.top, self.bottom = 12, 30

    def draw(self, stock: Stock, count: int = 60, offset: int = 0):
        """count: 显示K线数量；offset: 从最新一天往左偏移的天数（0=最新）。"""
        cv = self.canvas
        cv.delete("all")
        cv.create_rectangle(0, 0, self.w, self.h, fill=BG_COLOR, outline=BG_COLOR)

        end = max(0, len(stock.history) - offset)
        start = max(0, end - count)
        if end == 0 and len(stock.history) > 0:
            # 平移到最左端：显示最早的一批K线
            start = 0
            end = min(count, len(stock.history))
        hist = stock.history[start:end]
        if len(hist) < 2:
            return
        # 坐标范围
        lo = min(min(k["low"] for k in hist), min(k["open"] for k in hist), min(k["close"] for k in hist))
        hi = max(max(k["high"] for k in hist), max(k["open"] for k in hist), max(k["close"] for k in hist))
        pad = (hi - lo) * 0.06 or hi * 0.01
        lo, hi = lo - pad, hi + pad
        pw = self.w - self.left - self.right
        ph = self.price_h - self.top - self.bottom
        slot = pw / len(hist)
        cw = max(2.0, slot * 0.62)              # 蜡烛宽度

        def px(i): return self.left + slot * i + slot / 2
        def py(v): return self.top + ph * (1 - (v - lo) / (hi - lo))

        # 网格与价格刻度
        n_grid = 5
        for g in range(n_grid + 1):
            v = lo + (hi - lo) * g / n_grid
            y = py(v)
            cv.create_line(self.left, y, self.w - self.right, y, fill=GRID_COLOR)
            cv.create_text(self.left - 5, y, text=f"{v:.2f}", anchor="e",
                           fill=TEXT_COLOR, font=("Microsoft YaHei", 8))

        # 蜡烛 + 影线
        closes = [k["close"] for k in hist]
        for i, k in enumerate(hist):
            up = k["close"] >= k["open"]
            color = UP_COLOR if up else DOWN_COLOR
            x = px(i)
            cv.create_line(x, py(k["high"]), x, py(k["low"]), fill=color, width=1)
            y1, y2 = py(k["open"]), py(k["close"])
            if y1 == y2:
                y2 += 1
            cv.create_rectangle(x - cw / 2, y1, x + cw / 2, y2,
                                fill=color, outline=color)

        # 均线
        self._draw_ma(hist, closes, 5, MA5_COLOR, px, py, slot)
        self._draw_ma(hist, closes, 10, MA10_COLOR, px, py, slot)
        self._draw_ma(hist, closes, 20, MA20_COLOR, px, py, slot)

        # 日期轴
        step = max(1, len(hist) // 6)
        for i in range(0, len(hist), step):
            cv.create_text(px(i), self.price_h + 10,
                           text=hist[i]["date"][5:], anchor="n",
                           fill=TEXT_COLOR, font=("Microsoft YaHei", 8))

        # 成交量（下方区域）
        vol_top = self.price_h + 26
        vmax = max(k["volume"] for k in hist) or 1
        vh = self.vol_h - 6
        for i, k in enumerate(hist):
            up = k["close"] >= k["open"]
            color = UP_COLOR if up else DOWN_COLOR
            x = px(i)
            hh = vh * k["volume"] / vmax
            cv.create_rectangle(x - cw / 2, vol_top + vh - hh, x + cw / 2,
                                vol_top + vh, fill=color, outline=color)
        cv.create_line(self.left, vol_top + vh, self.w - self.right, vol_top + vh,
                       fill=AXIS_COLOR)

        # 标题信息
        p = stock.current_price()
        chg = stock.change_pct()
        d0 = hist[0]["date"] or "--"
        d1 = hist[-1]["date"] or "--"
        title = (f"{stock.code} {stock.name}    现价 {p:.2f}    "
                 f"涨跌 {chg:+.2f}%")
        cv.create_text(self.left + 4, 4, text=title, anchor="nw",
                       fill=UP_COLOR if chg >= 0 else DOWN_COLOR,
                       font=("Microsoft YaHei", 10, "bold"))
        # 视图范围 + 操作提示
        cv.create_text(self.left + 4, self.price_h - 14,
                       text=f"近{len(hist)}日  {d0} ~ {d1}   |   滚轮缩放 · 拖动平移 · 双击复位",
                       anchor="w", fill="#8fa1b8", font=("Microsoft YaHei", 8))
        # 均线图例
        legend = "MA5   MA10   MA20"
        cv.create_text(self.w - self.right - 2, 4, text=legend, anchor="ne",
                       fill=TEXT_COLOR, font=("Microsoft YaHei", 9))
        # 最新价虚线
        cv.create_line(self.left, py(p), self.w - self.right, py(p),
                       fill=AXIS_COLOR, dash=(3, 3))
        cv.create_text(self.w - self.right + 2, py(p), text=f"{p:.2f}",
                       anchor="w", fill=TEXT_COLOR, font=("Microsoft YaHei", 8))

    # ----- 当日分时图（秒级/毫秒级行情可视化） -----
    def draw_intraday(self, stock: Stock):
        """绘制当日分时：价格折线 + 昨收虚线 + 最新价标注 + 成交量采样。"""
        cv = self.canvas
        cv.delete("all")
        cv.create_rectangle(0, 0, self.w, self.h, fill=BG_COLOR, outline=BG_COLOR)
        pts = stock.intraday
        if len(pts) < 2:
            cv.create_text(self.w // 2, self.h // 2, text="⏱ 当日分时数据生成中…",
                           fill=TEXT_COLOR, font=("Microsoft YaHei", 10))
            return
        pc = stock.prev_close()
        lo = min(min(p for _, p in pts), pc) * 0.999
        hi = max(max(p for _, p in pts), pc) * 1.001
        pad = (hi - lo) * 0.08 or hi * 0.01
        lo, hi = lo - pad, hi + pad
        pw = self.w - self.left - self.right
        ph = self.price_h - self.top - self.bottom
        n = max(pts[-1][0] + 1, SESSION_MS // max(1, stock.tick_ms))

        def px(i): return self.left + pw * i / max(1, n - 1)
        def py(v): return self.top + ph * (1 - (v - lo) / (hi - lo))

        # 网格 + 价格刻度
        for g in range(5):
            v = lo + (hi - lo) * g / 4
            y = py(v)
            cv.create_line(self.left, y, self.w - self.right, y, fill=GRID_COLOR)
            cv.create_text(self.left - 5, y, text=f"{v:.2f}", anchor="e",
                           fill=TEXT_COLOR, font=("Microsoft YaHei", 8))
        # 昨收基准线
        y0 = py(pc)
        cv.create_line(self.left, y0, self.w - self.right, y0, fill="#5a6a80", dash=(4, 3))
        cv.create_text(self.w - self.right + 4, y0, text=f"昨收 {pc:.2f}", anchor="w",
                       fill="#8fa1b8", font=("Microsoft YaHei", 8))
        # 分时价格线 + 最新价圆点
        color = UP_COLOR if pts[-1][1] >= pc else DOWN_COLOR
        last_pt = None
        for i, p in pts:
            x, y = px(i), py(p)
            if last_pt:
                cv.create_line(last_pt[0], last_pt[1], x, y, fill=color, width=1.4)
            last_pt = (x, y)
        cv.create_oval(last_pt[0] - 3, last_pt[1] - 3, last_pt[0] + 3, last_pt[1] + 3,
                       fill=color, outline="#ffffff")
        # 时间轴（A股交易时段）
        for frac, label in ((0.0, "09:30"), (0.25, "10:00"), (0.5, "11:30/13:00"),
                            (0.75, "14:00"), (1.0, "15:00")):
            cv.create_text(self.left + pw * frac, self.price_h + 10, text=label,
                           anchor="n", fill=TEXT_COLOR, font=("Microsoft YaHei", 8))
        # 成交量采样条（下方区域）
        vol_top = self.price_h + 26
        vh = self.vol_h - 6
        base = max(pc, 1e-9)
        for i, p in pts:
            bar = max(2, int(round(vh * 0.12 * (0.5 + (p / base - 1) * 20))))
            x = px(i)
            cv.create_line(x, vol_top + vh, x, vol_top + vh - min(bar, vh),
                           fill=color, width=1)
        # 标题
        cv.create_text(self.left + 2, 2, anchor="nw",
                       text=f"⏱ 当日分时 {stock.name}  最新 {pts[-1][1]:.2f}",
                       fill="#c8d0dc", font=("Microsoft YaHei", 9, "bold"))

    def _draw_ma(self, hist, closes, n, color, px, py, slot):
        if len(closes) < n:
            return
        pts = []
        for i in range(n - 1, len(closes)):
            ma = sum(closes[i - n + 1: i + 1]) / n
            pts.append((px(i), py(ma)))
        if len(pts) >= 2:
            for j in range(1, len(pts)):
                self.canvas.create_line(pts[j - 1][0], pts[j - 1][1],
                                        pts[j][0], pts[j][1], fill=color, width=1)


class AssetChart:
    """历史收益曲线图：绘制总资产随交易日的变化。"""

    MAX_POINTS = 300   # 最多显示最近 300 个交易日

    def __init__(self, canvas: tk.Canvas, width: int, height: int):
        self.canvas = canvas
        self.w, self.h = width, height
        self.left, self.right, self.top, self.bottom = 64, 8, 22, 20

    def draw(self, assets: list, initial_cash: float):
        cv = self.canvas
        cv.delete("all")
        cv.create_rectangle(0, 0, self.w, self.h, fill=BG_COLOR, outline=BG_COLOR)
        data = assets[-self.MAX_POINTS:]
        if not data:
            return

        lo = min(min(data), initial_cash)
        hi = max(max(data), initial_cash)
        if hi - lo < 1:
            hi = lo + 1
        pad = (hi - lo) * 0.10
        lo, hi = lo - pad, hi + pad

        pw = self.w - self.left - self.right
        ph = self.h - self.top - self.bottom
        n = len(data)

        def px(i): return self.left + pw * i / (n - 1) if n > 1 else self.left + pw / 2
        def py(v): return self.top + ph * (1 - (v - lo) / (hi - lo))

        # 网格 + 金额刻度
        for g in range(4):
            v = lo + (hi - lo) * g / 3
            y = py(v)
            cv.create_line(self.left, y, self.w - self.right, y, fill=GRID_COLOR)
            cv.create_text(self.left - 4, y, text=f"{v:,.0f}", anchor="e",
                           fill=TEXT_COLOR, font=("Microsoft YaHei", 8))

        # 初始资金基准线（虚线）
        y0 = py(initial_cash)
        cv.create_line(self.left, y0, self.w - self.right, y0, fill="#5a6a80",
                       dash=(4, 3))
        cv.create_text(self.w - self.right + 3, y0, text="初始资金", anchor="w",
                       fill="#8fa1b8", font=("Microsoft YaHei", 8))

        # 折线 + 底部填充
        pts = [(px(i), py(v)) for i, v in enumerate(data)]
        up = data[-1] >= initial_cash
        color = "#3ddc84" if up else "#ff6b5e"
        fill_color = "#14402f" if up else "#451f1c"
        base_y = self.top + ph
        cv.create_polygon(pts + [(self.w - self.right, base_y), (self.left, base_y)],
                          fill=fill_color, outline="")
        for i in range(1, len(pts)):
            cv.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1],
                           fill=color, width=2)

        # 最新点标记
        cv.create_oval(pts[-1][0] - 3, pts[-1][1] - 3, pts[-1][0] + 3, pts[-1][1] + 3,
                       fill=color, outline="#ffffff")

        # 标题与当前收益
        cur = data[-1]
        rate = (cur - initial_cash) / initial_cash * 100 if initial_cash else 0.0
        cv.create_text(self.left + 2, 4, anchor="nw",
                       text=f"📈 历史收益曲线（近{len(data)}个交易日）", fill="#c8d0dc",
                       font=("Microsoft YaHei", 9, "bold"))
        cv.create_text(self.w - self.right - 2, 4, anchor="ne",
                       text=f"当前 {cur:,.2f} 元  ({rate:+.2f}%)", fill=color,
                       font=("Microsoft YaHei", 9, "bold"))


# ============================================================
# 主界面
# ============================================================
class GameApp:
    def __init__(self):
        self.game = Game(load=True) if os.path.exists(SAVE_FILE) else Game()
        self.desired_mode = self.game.mode
        self.root = tk.Tk()
        self.root.title("🎮 虚拟炒股模拟器 —— 零风险练手")
        self.root.geometry("1340x950")
        self.root.minsize(1200, 860)
        self.root.configure(bg="#151a23")
        self.auto_play = False
        self.after_id = None
        self.quant_win = None
        self.selected_code = self.game.stocks[0].code
        self.speed = DEFAULT_SPEED          # 时间流速：每秒推进的 tick 数
        self.tick_ms = TICK_MS              # 行情粒度（模拟毫秒/tick）
        self.chart_mode = "k"               # "k"=日K线, "intraday"=当日分时

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background="#1b2230", fieldbackground="#1b2230",
                        foreground="#d5dce8", rowheight=26, font=("Microsoft YaHei", 9))
        style.configure("Treeview.Heading", background="#232c3d", foreground="#e8edf5",
                        font=("Microsoft YaHei", 9, "bold"))
        style.map("Treeview", background=[("selected", "#2e5c9e")])
        style.configure("TButton", font=("Microsoft YaHei", 10), padding=4)
        style.configure("TLabel", background="#151a23", foreground="#d5dce8")

        self._build_ui()
        self._show_disclaimer()
        self._update_mode_label()
        self.refresh_all()

    # ---------------- 界面构建 ----------------
    def _build_ui(self):
        root = self.root
        # 顶部标题栏
        head = tk.Frame(root, bg="#1b2230")
        head.pack(fill="x")
        tk.Label(head, text="🎮 虚拟炒股模拟器", bg="#1b2230", fg="#e8c95a",
                 font=("Microsoft YaHei", 14, "bold")).pack(side="left", padx=14, pady=8)
        self.lbl_date = tk.Label(head, text="", bg="#1b2230", fg="#9fb2c8",
                                 font=("Microsoft YaHei", 11))
        self.lbl_date.pack(side="left", padx=20)
        self.lbl_mode = tk.Label(head, text="", bg="#1b2230",
                                 font=("Microsoft YaHei", 10, "bold"))
        self.lbl_mode.pack(side="right", padx=10)
        self.lbl_rank = tk.Label(head, text="", bg="#1b2230", fg="#7ec699",
                                 font=("Microsoft YaHei", 10))
        self.lbl_rank.pack(side="right", padx=14)

        # 状态栏（总资产/资金/市值/盈亏/收益率）
        stat = tk.Frame(root, bg="#20283a")
        stat.pack(fill="x", padx=8, pady=(8, 4))
        self.stat_labels = {}
        order = [("总资产", "assets"), ("可用资金", "cash"), ("持仓市值", "mv"),
                 ("浮动盈亏", "profit"), ("总收益率", "rate"), ("累计最高资产", "best")]
        for i, (title, key) in enumerate(order):
            box = tk.Frame(stat, bg="#20283a")
            box.pack(side="left", expand=True, fill="x")
            tk.Label(box, text=title, bg="#20283a", fg="#8fa1b8",
                     font=("Microsoft YaHei", 9)).pack()
            self.stat_labels[key] = tk.Label(box, text="", bg="#20283a", fg="#ffffff",
                                             font=("Microsoft YaHei", 14, "bold"))
            self.stat_labels[key].pack()

        # 主体区域
        body = tk.Frame(root, bg="#151a23")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        # ===== 左列：股票列表 + K线 =====
        left = tk.Frame(body, bg="#151a23")
        left.pack(side="left", fill="both", expand=True)
        title_row = tk.Frame(left, bg="#151a23")
        title_row.pack(fill="x", pady=(0, 2))
        tk.Label(title_row, text="📋 自选股行情（点击行查看K线/分时）", bg="#151a23",
                 fg="#e8c95a", font=("Microsoft YaHei", 10, "bold")).pack(side="left")
        self.btn_chart_mode = tk.Button(title_row, text="⏱ 切换分时图", command=self._toggle_chart_mode,
                                        bg="#39465f", fg="white", font=("Microsoft YaHei", 8),
                                        bd=0, padx=8, activebackground="#4a5a78")
        self.btn_chart_mode.pack(side="right")

        cols = ("code", "name", "price", "pct", "open", "high", "low", "vol")
        self.stock_tree = ttk.Treeview(left, columns=cols, show="headings", height=8)
        heads = [("code", "代码", 70), ("name", "名称", 90), ("price", "现价", 85),
                 ("pct", "涨跌幅", 85), ("open", "今开", 85), ("high", "最高", 85),
                 ("low", "最低", 85), ("vol", "成交量(手)", 110)]
        for c, t, w in heads:
            self.stock_tree.heading(c, text=t)
            self.stock_tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.stock_tree.yview)
        self.stock_tree.configure(yscrollcommand=vsb.set)
        self.stock_tree.pack(fill="x")
        vsb.pack(side="right", fill="y", before=self.stock_tree)
        self.stock_tree.bind("<<TreeviewSelect>>", self._on_select_stock)
        self.stock_tree.tag_configure("up", foreground="#ff6b5e")
        self.stock_tree.tag_configure("down", foreground="#3ddc84")

        # K线图
        self.chart_frame = tk.Frame(left, bg="#10141c")
        self.chart_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.chart_canvas = tk.Canvas(self.chart_frame, bg="#10141c", highlightthickness=0)
        self.chart_canvas.pack(fill="both", expand=True)
        self.chart_canvas.bind("<Configure>", lambda e: self._redraw_chart())
        # K线交互：滚轮缩放 / 拖拽平移 / 双击复位
        self.chart_canvas.bind("<MouseWheel>", self._on_chart_wheel)
        self.chart_canvas.bind("<Button-4>", self._on_chart_wheel)
        self.chart_canvas.bind("<Button-5>", self._on_chart_wheel)
        self.chart_canvas.bind("<ButtonPress-1>", self._on_chart_press)
        self.chart_canvas.bind("<B1-Motion>", self._on_chart_drag)
        self.chart_canvas.bind("<Double-Button-1>", self._on_chart_reset)
        self.chart = Chart(self.chart_canvas, 900, 520)
        self.chart_view = 60      # K线显示天数（可缩放 10~500）
        self.chart_offset = 0     # 从最新K线往左偏移的天数（平移）
        self._drag_x = None
        self._drag_off = 0

        # 历史收益曲线（位于行情表与K线图之间）
        asset_box = tk.Frame(left, bg="#10141c")
        asset_box.pack(fill="x", pady=(6, 0))
        self.asset_canvas = tk.Canvas(asset_box, bg="#10141c", highlightthickness=0,
                                      height=112)
        self.asset_canvas.pack(fill="x")
        self.asset_canvas.bind("<Configure>", lambda e: self._redraw_asset_chart())
        self.asset_chart = AssetChart(self.asset_canvas, 900, 112)

        # ===== 右列：买卖 / 持仓 / 交易记录 =====
        right = tk.Frame(body, bg="#151a23", width=430)
        right.pack(side="right", fill="y", padx=(8, 0))
        right.pack_propagate(False)

        # --- 买卖面板 ---
        trade_box = tk.LabelFrame(right, text="💰 买卖操作（A股规则：一手=100股）",
                                  bg="#1b2230", fg="#e8c95a",
                                  font=("Microsoft YaHei", 10, "bold"))
        trade_box.pack(fill="x", pady=(0, 6))
        self.trade_box = trade_box

        r1 = tk.Frame(trade_box, bg="#1b2230")
        r1.pack(fill="x", padx=8, pady=4)
        tk.Label(r1, text="选择股票:", bg="#1b2230", fg="#c8d0dc",
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self.stock_combo = ttk.Combobox(r1, state="readonly", width=24,
                                        font=("Microsoft YaHei", 9))
        self.stock_combo.pack(side="left", padx=6)
        self.stock_combo.bind("<<ComboboxSelected>>", self._on_combo)

        self.lbl_price = tk.Label(trade_box, text="现价: --", bg="#1b2230", fg="#ffffff",
                                  font=("Microsoft YaHei", 12, "bold"))
        self.lbl_price.pack(anchor="w", padx=10)

        r2 = tk.Frame(trade_box, bg="#1b2230")
        r2.pack(fill="x", padx=8, pady=4)
        tk.Label(r2, text="数量(股):", bg="#1b2230", fg="#c8d0dc",
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self.spin_qty = tk.Spinbox(r2, from_=100, to=10_000_000, increment=100,
                                   width=12, font=("Microsoft YaHei", 10))
        self.spin_qty.pack(side="left", padx=6)
        tk.Button(r2, text="最多可买", command=self._fill_max_buy, bg="#2e5c9e",
                  fg="white", font=("Microsoft YaHei", 8), bd=0, padx=6,
                  activebackground="#3a6fbf").pack(side="left")

        r3 = tk.Frame(trade_box, bg="#1b2230")
        r3.pack(fill="x", padx=8, pady=2)
        for label, frac in (("1/4仓", 0.25), ("1/2仓", 0.5), ("全仓", 1.0)):
            tk.Button(r3, text=label,
                      command=lambda f=frac: self._fill_fraction(f),
                      bg="#39465f", fg="white", font=("Microsoft YaHei", 9),
                      bd=0, padx=10, activebackground="#4a5a78").pack(side="left", padx=2)

        self.lbl_est = tk.Label(trade_box, text="预计花费: --", bg="#1b2230",
                                fg="#9fb2c8", font=("Microsoft YaHei", 9))
        self.lbl_est.pack(anchor="w", padx=10)

        r4 = tk.Frame(trade_box, bg="#1b2230")
        r4.pack(fill="x", padx=8, pady=6)
        tk.Button(r4, text="🟢 买入", command=lambda: self._do_trade("buy"),
                  bg="#c0392b", fg="white", font=("Microsoft YaHei", 11, "bold"),
                  bd=0, padx=22, pady=4, activebackground="#a93226").pack(side="left", padx=4)
        tk.Button(r4, text="🔴 卖出", command=lambda: self._do_trade("sell"),
                  bg="#1e8449", fg="white", font=("Microsoft YaHei", 11, "bold"),
                  bd=0, padx=22, pady=4, activebackground="#186a3b").pack(side="left", padx=4)
        tk.Button(r4, text="一键清仓", command=self._do_liquidate,
                  bg="#6c3483", fg="white", font=("Microsoft YaHei", 9),
                  bd=0, padx=10, pady=4, activebackground="#5b2c6f").pack(side="left", padx=4)

        # --- 持仓列表 ---
        hold_label = tk.Label(right, text="📦 我的持仓", bg="#151a23", fg="#e8c95a",
                              font=("Microsoft YaHei", 10, "bold"))
        hold_label.pack(anchor="w", pady=(6, 2))
        self.hold_tree = ttk.Treeview(right, columns=("code", "name", "shares", "cost",
                                                      "price", "mv", "profit"),
                                      show="headings", height=5)
        for c, t, w in [("code", "代码", 55), ("name", "名称", 75), ("shares", "持股", 60),
                        ("cost", "成本价", 70), ("price", "现价", 70),
                        ("mv", "市值", 80), ("profit", "浮动盈亏", 90)]:
            self.hold_tree.heading(c, text=t)
            self.hold_tree.column(c, width=w, anchor="center")
        self.hold_tree.pack(fill="x")
        self.hold_tree.tag_configure("up", foreground="#ff6b5e")
        self.hold_tree.tag_configure("down", foreground="#3ddc84")

        # --- 交易记录 ---
        tk.Label(right, text="📜 交易记录（最近50条）", bg="#151a23", fg="#e8c95a",
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(6, 2))
        self.trade_text = tk.Text(right, height=12, bg="#1b2230", fg="#c8d0dc",
                                  font=("Microsoft YaHei", 9), state="disabled",
                                  relief="flat", wrap="none")
        self.trade_text.pack(fill="both", expand=True)

        # --- 日内交易控制条（秒级/毫秒级 + 时间流速） ---
        tickbar = tk.Frame(root, bg="#20283a")
        tickbar.pack(fill="x", padx=8, pady=(4, 0))
        self.btn_next_tick = tk.Button(tickbar, text="⏱ 下一Tick", command=self._next_tick,
                                       bg="#1e8449", fg="white", font=("Microsoft YaHei", 9, "bold"),
                                       bd=0, padx=10, pady=3, activebackground="#186a3b")
        self.btn_next_tick.pack(side="left", padx=4)
        self.btn_auto = tk.Button(tickbar, text="▶ 自动交易", command=self._toggle_auto,
                                  bg="#7d6608", fg="white", font=("Microsoft YaHei", 9, "bold"),
                                  bd=0, padx=10, pady=3, activebackground="#9a7d0a")
        self.btn_auto.pack(side="left", padx=4)
        tk.Label(tickbar, text="流速:", bg="#20283a", fg="#9fb2c8",
                 font=("Microsoft YaHei", 9)).pack(side="left", padx=(10, 2))
        self.scale_speed = tk.Scale(tickbar, from_=1, to=MAX_SPEED, orient="horizontal",
                                    length=200, showvalue=False, bg="#20283a", fg="#d5dce8",
                                    highlightthickness=0, troughcolor="#39465f",
                                    command=lambda v: self._on_speed_change(v))
        self.scale_speed.set(self.speed)
        self.scale_speed.pack(side="left")
        self.lbl_speed = tk.Label(tickbar, text=f"{self.speed} tick/秒", bg="#20283a",
                                  fg="#e8c95a", font=("Microsoft YaHei", 9, "bold"))
        self.lbl_speed.pack(side="left", padx=4)
        for v in (1, 10, 60, 300, 1000, 5000):
            tk.Button(tickbar, text=f"{v}x", command=lambda vv=v: self._set_speed(vv),
                      bg="#39465f", fg="white", font=("Microsoft YaHei", 8), bd=0,
                      padx=6, activebackground="#4a5a78").pack(side="left", padx=2)
        tk.Label(tickbar, text="粒度:", bg="#20283a", fg="#9fb2c8",
                 font=("Microsoft YaHei", 9)).pack(side="left", padx=(10, 2))
        for label, ms in (("秒级", 1000), ("100ms", 100), ("10ms", 10), ("毫秒级", 1)):
            tk.Button(tickbar, text=label, command=lambda ms=ms: self._set_tick_ms(ms),
                      bg="#39465f", fg="white", font=("Microsoft YaHei", 8), bd=0,
                      padx=6, activebackground="#4a5a78").pack(side="left", padx=2)
        self.lbl_ticktime = tk.Label(tickbar, text="", bg="#20283a", fg="#7ec699",
                                     font=("Microsoft YaHei", 9, "bold"))
        self.lbl_ticktime.pack(side="right", padx=10)

        # --- 底部控制条 ---
        bar = tk.Frame(root, bg="#1b2230")
        bar.pack(fill="x", padx=8, pady=(4, 8))
        self.btn_next = tk.Button(bar, text="⏭ 下一交易日", command=self._next_day,
                                  bg="#2e5c9e", fg="white", font=("Microsoft YaHei", 10, "bold"),
                                  bd=0, padx=16, pady=5, activebackground="#3a6fbf")
        self.btn_next.pack(side="left", padx=4)
        for text, cmd in (("💾 存盘", self._save), ("📂 读盘", self._load),
                          ("🔄 重新开局", self._reset), ("📖 帮助", self._help)):
            tk.Button(bar, text=text, command=cmd, bg="#39465f", fg="white",
                      font=("Microsoft YaHei", 10), bd=0, padx=14, pady=5,
                      activebackground="#4a5a78").pack(side="left", padx=4)
        tk.Button(bar, text="🤖 量化实验室", command=self._open_quant,
                  bg="#1e8449", fg="white", font=("Microsoft YaHei", 10, "bold"),
                  bd=0, padx=14, pady=5, activebackground="#186a3b").pack(side="right", padx=4)
        self.btn_mode = tk.Button(bar, text="切换行情模式", command=self._toggle_mode,
                                  bg="#39465f", fg="white",
                                  font=("Microsoft YaHei", 9), bd=0, padx=10, pady=5,
                                  activebackground="#4a5a78")
        self.btn_mode.pack(side="right", padx=4)

        # 新闻滚动条
        self.lbl_news = tk.Label(root, text="", bg="#141a24", fg="#7ec699",
                                 font=("Microsoft YaHei", 9), anchor="w")
        self.lbl_news.pack(fill="x", padx=8, pady=(0, 6))

        # 底部绑定关闭事件
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 交互事件 ----------------
    def _on_select_stock(self, _e=None):
        sel = self.stock_tree.selection()
        if sel:
            self.selected_code = self.stock_tree.item(sel[0], "values")[0]
            self._sync_combo()
            self.chart_offset = 0    # 切换股票后回到最新行情
            self._redraw_chart()
            self._update_trade_panel()

    def _on_combo(self, _e=None):
        code = self.stock_combo.get().split()[0]
        self.selected_code = code
        self._update_trade_panel()

    def _sync_combo(self):
        st = self.game.stock(self.selected_code)
        self.stock_combo.set(f"{st.code} {st.name}")

    def _show_disclaimer(self):
        messagebox.showinfo(
            "⚠️ 风险提示（必读）",
            "欢迎来到虚拟炒股模拟器！\n\n"
            "1. 你获得 200,000 元虚拟资金，可放心大胆练习，亏光也不心疼；\n"
            "2. 行情由程序随机模拟生成，与真实市场无关；\n"
            "3. 已按 A 股规则模拟：一手 100 股、佣金万2.5（最低5元）、\n"
            "   卖出印花税万分之5、过户费十万分之1；\n"
            "4. 本游戏仅供学习练手，不构成任何投资建议。\n\n"
            "祝你在游戏中积累经验，将来在真实市场更加从容！")

    # ---------------- 数据刷新 ----------------
    def refresh_all(self):
        self._refresh_stats()
        self._refresh_stock_table()
        self._refresh_holdings()
        self._refresh_trades()
        self._update_trade_panel()
        self._redraw_chart()
        self._redraw_asset_chart()
        self._update_date()

    def _update_date(self):
        d = self.game.current_date()
        t = self.game.current_tick_time()
        self.lbl_date.config(text=f"📅 {d.strftime('%Y-%m-%d')}  ⏱ {t}")
        self.lbl_ticktime.config(
            text=f"tick {self.game.stocks[0].tick_idx + 1}/{self.game.ticks_per_day()}")
        board = load_leaderboard()
        if board:
            self.lbl_rank.config(text=f"🏆 排行榜最佳收益率: {board[0]['rate']:+.2f}%")

    def _update_mode_label(self):
        if self.game.mode == "real":
            n = len(self.game.stocks[0].history)
            self.lbl_mode.config(
                text=f"📊 真实行情（{self.game.start_date} 起，前{n}天为真实数据）",
                fg="#7ec699")
        else:
            self.lbl_mode.config(text="🎲 纯模拟行情（程序生成）", fg="#f5b942")

    def _toggle_mode(self):
        """在真实行情与纯模拟行情之间切换（重新开局）。"""
        self.desired_mode = "sim" if self.desired_mode == "real" else "real"
        self._reset(confirm=False)
        self._update_mode_label()

    def _refresh_stats(self):
        g = self.game
        assets = g.total_assets()
        profit = g.total_profit()
        rate = g.return_rate()
        up = profit >= 0
        color = "#3ddc84" if up else "#ff6b5e"
        self.stat_labels["assets"].config(text=fmt_money(assets), fg="#ffffff")
        self.stat_labels["cash"].config(text=fmt_money(g.cash), fg="#ffffff")
        self.stat_labels["mv"].config(text=fmt_money(g.total_market_value()), fg="#ffffff")
        self.stat_labels["profit"].config(text=f"{profit:+,.2f}", fg=color)
        self.stat_labels["rate"].config(text=f"{rate:+.2f}%", fg=color)
        self.stat_labels["best"].config(text=fmt_money(g.best_assets), fg="#e8c95a")

    def _refresh_stock_table(self):
        tree = self.stock_tree
        tree.delete(*tree.get_children())
        for st in self.game.stocks:
            chg = st.change_pct()
            tag = "up" if chg >= 0 else "down"
            k = st.history[-1]
            prev = st.history[-2] if len(st.history) > 1 else k
            tree.insert("", "end", iid=st.code, values=(
                st.code, st.name, f"{st.current_price():.2f}", f"{chg:+.2f}%",
                f"{k['open']:.2f}", f"{k['high']:.2f}", f"{k['low']:.2f}",
                f"{k['volume'] // 100:,}"), tags=(tag,))
        tree.selection_set(self.selected_code)

    def _refresh_holdings(self):
        tree = self.hold_tree
        tree.delete(*tree.get_children())
        g = self.game
        for code, h in g.holdings.items():
            st = g.stock(code)
            price = st.current_price()
            profit = g.floating_profit(code)
            tag = "up" if profit >= 0 else "down"
            tree.insert("", "end", values=(
                code, st.name, h["shares"], f"{h['cost']:.2f}", f"{price:.2f}",
                fmt_money(h["shares"] * price), f"{profit:+,.2f}"), tags=(tag,))

    def _refresh_trades(self):
        self.trade_text.config(state="normal")
        self.trade_text.delete("1.0", "end")
        if not self.game.trades:
            self.trade_text.insert("end", "（暂无交易记录）\n")
        for t in self.game.trades[:50]:
            line = (f"{t['date']}  {t['name']}({t['code']})  "
                    f"{'🟢买' if t['action'] == '买入' else '🔴卖'}  "
                    f"{t['price']:.2f} × {t['shares']}  "
                    f"金额 {t['amount']:,.2f}  费 {t['fee']:.2f}")
            if t["action"] == "卖出":
                line += f"  已实现盈亏 {t['profit']:+,.2f}"
            self.trade_text.insert("end", line + "\n")
        self.trade_text.config(state="disabled")

    def _update_trade_panel(self):
        try:
            st = self.game.stock(self.selected_code)
        except KeyError:
            return
        p = st.current_price()
        chg = st.change_pct()
        self.lbl_price.config(text=f"现价: {p:.2f}  ({chg:+.2f}%)",
                              fg="#ff6b5e" if chg >= 0 else "#3ddc84")
        self.lbl_est.config(text=self._est_text())

    def _est_text(self) -> str:
        try:
            st = self.game.stock(self.selected_code)
            qty = int(float(self.spin_qty.get()))
        except Exception:
            return "预计花费: --"
        price = st.current_price()
        amount = price * qty
        if qty > 0:
            fee = Game.buy_fee(amount)
            return (f"预计花费: {fmt_money(amount + fee)} 元"
                    f"（成交 {fmt_money(amount)} + 手续费 {fee:.2f}）")
        return "预计花费: --"

    # ---------------- 交易动作 ----------------
    def _fill_max_buy(self):
        qty = self.game.max_buyable(self.selected_code)
        self.spin_qty.delete(0, "end")
        self.spin_qty.insert(0, str(qty))
        self.lbl_est.config(text=self._est_text())

    def _fill_fraction(self, frac: float):
        price = self.game.price_of(self.selected_code)
        if price <= 0:
            return
        lots = int(self.game.cash * frac / (price * LOT_SIZE * 1.001))
        qty = max(0, lots) * LOT_SIZE
        self.spin_qty.delete(0, "end")
        self.spin_qty.insert(0, str(qty))
        self.lbl_est.config(text=self._est_text())

    def _do_trade(self, action: str):
        try:
            qty = int(float(self.spin_qty.get()))
        except Exception:
            messagebox.showwarning("输入错误", "请输入有效的买入数量（100的整数倍）")
            return
        try:
            st = self.game.stock(self.selected_code)
            if action == "buy":
                info = self.game.buy(self.selected_code, qty)
                msg = (f"✅ 买入成功：{st.name} {info['shares']}股 @ {info['price']:.2f}，"
                       f"成交 {fmt_money(info['amount'])} 元，手续费 {info['fee']:.2f} 元")
            else:
                info = self.game.sell(self.selected_code, qty)
                msg = (f"✅ 卖出成功：{st.name} {info['shares']}股 @ {info['price']:.2f}，"
                       f"成交 {fmt_money(info['amount'])} 元，手续费 {info['fee']:.2f} 元")
                if info.get("profit") is not None:
                    msg += f"，已实现盈亏 {info['profit']:+,.2f} 元"
        except ValueError as e:
            messagebox.showwarning("⚠️ 无法成交", str(e))
            return
        self.lbl_news.config(text=msg)
        self.refresh_all()
        self._check_game_over()

    def _do_liquidate(self):
        if not self.game.holdings:
            messagebox.showinfo("提示", "当前没有持仓，无需清仓。")
            return
        if messagebox.askyesno("确认清仓", "确定要按当前价卖出所有持仓吗？"):
            self.game.liquidate()
            self.refresh_all()

    def _check_game_over(self):
        g = self.game
        if g.is_game_over():
            if messagebox.askyesno("💸 破产了！",
                                   f"你的虚拟资金已亏光（最终资产 {fmt_money(g.total_assets())} 元）。\n"
                                   "要把这次成绩写入排行榜吗？"):
                self._record_score()
            if messagebox.askyesno("重新开始", "要重新开局再来一次吗？"):
                self._reset(confirm=False)
        elif g.return_rate() >= 100:
            messagebox.showinfo("🎉 恭喜翻倍！", "你的总资产已翻倍，虚拟炒股大成功！\n"
                                "可以继续挑战更高收益，也可以存盘后去真实市场小试身手（注意风险）。")

    def _record_score(self):
        g = self.game
        save_leaderboard({
            "date": dt.date.today().strftime("%Y-%m-%d %H:%M"),
            "rate": round(g.return_rate(), 2),
            "assets": round(g.total_assets(), 2),
            "days": g.day_idx,
        })
        self._update_date()

    # ---------------- 时间推进（tick 级） ----------------
    def _run_ticks(self, n: int):
        """连续推进 n 个 tick；跨天时自动结算（量化按收盘价运行）并进入次日。"""
        for _ in range(n):
            if self.game.advance_tick():
                self._on_day_ended()
                self.game.open_new_day()
                self._update_date()

    def _next_tick(self):
        """手动推进一个 tick（秒级/毫秒级买入卖出的节奏控制）。"""
        self._run_ticks(1)
        self.refresh_all()
        self._check_game_over()

    def _next_day(self):
        """快进到下一个交易日开盘（跳过今日剩余 tick）。"""
        self.game.fast_forward_day()
        self._on_day_ended()
        self.game.open_new_day()
        if self.game.days_extended:
            self.lbl_news.config(text="🚀 已越过初始模拟终点，交易日已自动扩展，本局可继续玩下去！")
            self.game.days_extended = False
        else:
            self.lbl_news.config(text=random.choice(MARKET_NEWS))
        self._update_date()
        self.refresh_all()
        self._check_game_over()

    def _on_day_ended(self):
        """当日收盘回调：量化策略在收盘价结算；手动模式下播报一条新闻。"""
        self._notify_quant()
        if not self.auto_play:
            self.lbl_news.config(text=random.choice(MARKET_NEWS))

    def _toggle_chart_mode(self):
        self.chart_mode = "intraday" if self.chart_mode == "k" else "k"
        self.btn_chart_mode.config(
            text="📊 切换日K线" if self.chart_mode == "intraday" else "⏱ 切换分时图")
        self._redraw_chart()

    def _set_speed(self, v: int):
        self.speed = max(1, min(MAX_SPEED, int(v)))
        self.scale_speed.set(self.speed)
        self.lbl_speed.config(text=f"{self.speed} tick/秒")

    def _on_speed_change(self, v):
        self.speed = max(1, min(MAX_SPEED, int(float(v))))
        self.lbl_speed.config(text=f"{self.speed} tick/秒")

    def _set_tick_ms(self, ms: int):
        self.tick_ms = ms
        self.game.set_tick_ms(ms)
        self._update_date()
        self.refresh_all()

    # ---------------- 量化实验室 ----------------
    def _open_quant(self):
        if self.quant_win is not None:
            try:
                if self.quant_win.win.winfo_exists():
                    self.quant_win.win.lift()
                    self.quant_win.win.focus_force()
                    return
            except tk.TclError:
                pass
        self.quant_win = QuantWindow(self)

    def _close_quant(self):
        if self.quant_win is not None:
            try:
                self.quant_win.win.destroy()
            except Exception:
                pass
            self.quant_win = None

    def _notify_quant(self):
        """主界面推进一个交易日后，同步量化实验室。"""
        if self.quant_win is None:
            return
        try:
            if self.quant_win.win.winfo_exists():
                self.quant_win.on_main_day_advanced()
            else:
                self.quant_win = None
        except tk.TclError:
            self.quant_win = None

    def _toggle_auto(self):
        self.auto_play = not self.auto_play
        self.btn_auto.config(text="⏸ 暂停" if self.auto_play else "▶ 自动交易")
        if self.auto_play:
            self._auto_tick()
        elif self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None

    def _auto_tick(self):
        """帧驱动自动交易：按设定流速推进 tick，每帧刷新界面（约 30fps）。"""
        if not self.auto_play:
            return
        n = max(1, int(round(self.speed * FRAME_MS / 1000.0)))
        for _ in range(n):
            if self.game.advance_tick():
                self._on_day_ended()
                self.game.open_new_day()
                self._update_date()
            if self.game.is_game_over():
                break
        if self.game.is_game_over():
            self._toggle_auto()
            self._check_game_over()
            return
        self.refresh_all()
        self.after_id = self.root.after(FRAME_MS, self._auto_tick)

    # ---------------- 存盘 / 读盘 / 重置 ----------------
    def _save(self):
        try:
            self.game.save()
            messagebox.showinfo("💾 存盘成功", f"游戏进度已保存到:\n{SAVE_FILE}")
        except Exception as e:
            messagebox.showerror("存盘失败", str(e))

    def _load(self):
        if not os.path.exists(SAVE_FILE):
            messagebox.showinfo("提示", "还没有存档文件。")
            return
        if messagebox.askyesno("读盘确认", "读取存档将覆盖当前进度，确定继续吗？"):
            try:
                self._close_quant()
                self.game = Game(load=True)
                self.desired_mode = self.game.mode
                self.refresh_all()
                self._update_mode_label()
                messagebox.showinfo("📂 读盘成功", "存档已载入！")
            except Exception as e:
                messagebox.showerror("读盘失败", str(e))

    def _reset(self, confirm=True):
        if confirm and not messagebox.askyesno("重新开局", "将清空当前进度并回到 200,000 元初始资金，确定吗？"):
            return
        try:
            if os.path.exists(SAVE_FILE):
                os.remove(SAVE_FILE)
        except OSError:
            pass
        self._close_quant()
        self.game = Game(mode=self.desired_mode)
        self.selected_code = self.game.stocks[0].code
        self.refresh_all()
        self._update_mode_label()
        if self.game.mode == "real":
            self.lbl_news.config(text=f"📊 真实行情已加载：{len(self.game.stocks)} 只股票，"
                                      f"共 {len(self.game.stocks[0].history)} 个真实交易日")
        else:
            self.lbl_news.config(text="🔄 新游戏开始！200,000 元虚拟资金已到账（纯模拟行情）。")

    def _help(self):
        messagebox.showinfo(
            "📖 使用帮助",
            "【怎么玩】\n"
            "1. 在左侧行情表中点击股票，查看K线（含MA5/10/20均线）；\n"
            "2. 右侧输入买入数量（100的整数倍）→ 点【买入】；\n"
            "3. 持仓后可随时卖出或一键清仓；\n"
            "4. 秒级/毫秒级交易：点【下一Tick】单步推进，或开启【自动交易】\n"
            "   按设定流速连续运行，盘中可随时买卖；\n"
            "5. 随时存盘，下次打开可继续；破产或想结算时可写入排行榜。\n\n"
            "【秒级/毫秒级交易】\n"
            "• 行情以 tick 为单位跳动：1 tick = 1 模拟秒（秒级），\n"
            "   可切换 100ms / 10ms / 1ms（毫秒级），粒度越细价格跳动越快；\n"
            "• 流速滑块（1~10000 tick/秒）控制自动交易速度，档位按钮一键切换；\n"
            "• 点击【⏱ 切换分时图】查看当日逐 tick 走势（红涨绿跌）；\n"
            "• 每个交易日 4 小时交易时段（09:30-11:30 / 13:00-15:00），\n"
            "   一天 = 14400 tick（秒级）~ 1440万 tick（毫秒级）。\n\n"
            "【K线图操作】\n"
            "• 鼠标滚轮：缩放K线数量（10~500根）\n"
            "• 按住左键拖动：平移查看历史行情\n"
            "• 双击：复位到最近60日视图\n\n"
            "【交易规则（模拟A股）】\n"
            "• 买入 100 股起，须为整手；卖出可留零股或一次性清仓\n"
            "• 佣金：成交额 0.025%，最低 5 元（买卖双向）\n"
            "• 印花税：卖出 0.05%（2023年8月起减半后的标准）\n"
            "• 过户费：成交额 0.001%（双向）\n\n"
            "【小贴士】\n"
            "• 关注均线：价格站上 MA20 通常是强势信号\n"
            "• 分散持仓，不要全仓押注一只股票\n"
            "• 设好止盈止损，克服“不舍得卖”的侥幸心理\n\n"
            "⚠️ 行情为模拟数据，不构成投资建议！")

    def _on_close(self):
        if messagebox.askyesno("退出", "退出前要保存当前进度吗？"):
            self._save()
        if self.after_id:
            self.root.after_cancel(self.after_id)
        self.root.destroy()

    def _redraw_chart(self):
        try:
            st = self.game.stock(self.selected_code)
        except KeyError:
            return
        # 视图边界约束
        max_off = max(0, len(st.history) - self.chart_view)
        self.chart_offset = max(0, min(self.chart_offset, max_off))
        w = max(300, self.chart_canvas.winfo_width())
        h = max(240, self.chart_canvas.winfo_height())
        self.chart = Chart(self.chart_canvas, w, h)
        if self.chart_mode == "intraday":
            self.chart.draw_intraday(st)
        else:
            self.chart.draw(st, self.chart_view, self.chart_offset)

    # ---------- K线交互：缩放 / 平移 / 复位 ----------
    def _on_chart_wheel(self, event):
        """滚轮缩放：向上放大（K线变少），向下缩小（K线变多）。"""
        if self.chart_mode != "k":
            return
        delta = getattr(event, "delta", 0)
        if delta == 0:                     # Linux Button-4/5
            delta = 120 if event.num == 4 else -120
        if delta > 0:
            self.chart_view = max(10, int(self.chart_view / 1.2))
        else:
            self.chart_view = min(500, int(self.chart_view * 1.2))
        self._redraw_chart()

    def _on_chart_press(self, event):
        if self.chart_mode != "k":
            return
        self._drag_x = event.x
        self._drag_off = self.chart_offset

    def _on_chart_drag(self, event):
        if self.chart_mode != "k" or self._drag_x is None:
            return
        st = self.game.stock(self.selected_code)
        w = max(300, self.chart_canvas.winfo_width())
        slot = max(2.0, (w - 46 - 8) / self.chart_view)   # 每根K线占的像素
        dx = event.x - self._drag_x
        # 向右拖 = 查看更早历史（offset 增大）
        self.chart_offset = int(self._drag_off + dx / slot)
        max_off = max(0, len(st.history) - self.chart_view)
        self.chart_offset = max(0, min(self.chart_offset, max_off))
        self._redraw_chart()

    def _on_chart_reset(self, _event):
        """双击复位为默认近60日视图。"""
        self.chart_view = 60
        self.chart_offset = 0
        self._redraw_chart()

    def _redraw_asset_chart(self):
        w = max(240, self.asset_canvas.winfo_width())
        h = max(80, self.asset_canvas.winfo_height())
        self.asset_chart = AssetChart(self.asset_canvas, w, h)
        self.asset_chart.draw(self.game.assets_history, self.game.initial_cash)


# ============================================================
# 🤖 量化策略模块：让程序自动炒股
# ============================================================
class Strategy:
    """量化策略基类：独立模拟账户，按收盘价自动交易。"""

    name = "基础策略"
    desc = ""

    def __init__(self, cash: float = INIT_CASH):
        self.initial = cash
        self.cash = cash
        self.holdings = {}      # code -> shares
        self.trades = 0
        self.fees = 0.0
        self.days_run = 0

    def on_day(self, game: Game):
        raise NotImplementedError

    def assets(self, game: Game) -> float:
        return self.cash + sum(sh * game.price_of(c) for c, sh in self.holdings.items())

    def _buy(self, game: Game, code: str, budget: float):
        """按当前价买入，尽量用 budget 预算，不足一手则跳过。"""
        price = game.price_of(code)
        shares = int(budget // price // LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            return
        amount = price * shares
        fee = Game.buy_fee(amount)
        if amount + fee > self.cash:
            shares -= LOT_SIZE
            if shares <= 0:
                return
            amount = price * shares
            fee = Game.buy_fee(amount)
        self.cash -= amount + fee
        self.holdings[code] = self.holdings.get(code, 0) + shares
        self.trades += 1
        self.fees += fee

    def _sell_all(self, game: Game, code: str):
        shares = self.holdings.pop(code, 0)
        if shares <= 0:
            return
        amount = game.price_of(code) * shares
        fee = Game.sell_fee(amount)
        self.cash += amount - fee
        self.trades += 1
        self.fees += fee


class DualMAStrategy(Strategy):
    """双均线趋势：MA5>MA20 视为多头买入，MA5<MA20 卖出。"""

    name = "双均线趋势"
    desc = "经典趋势跟踪：5日均线上穿/高于20日均线买入，下穿/低于卖出"

    def _bull(self, game: Game, code: str) -> bool:
        closes = [k["close"] for k in game.stock(code).history[-21:]]
        if len(closes) < 21:
            return False
        return sum(closes[-5:]) / 5 > sum(closes[-20:]) / 20

    def on_day(self, game: Game):
        # 卖出走弱持仓
        for code in list(self.holdings):
            if not self._bull(game, code):
                self._sell_all(game, code)
        # 买入走强股票（均分现金）
        bulls = [st for st in game.stocks
                 if st.code not in self.holdings and self._bull(game, st.code)]
        if bulls and self.cash > 2000:
            per = self.cash / len(bulls)
            for st in bulls:
                self._buy(game, st.code, per)


class RSIStrategy(Strategy):
    """RSI 超卖反转：RSI14<30 买入，RSI14>70 卖出。"""

    name = "RSI超卖反转"
    desc = "均值回归思路：相对强弱指标低于30超卖买入，高于70超买卖出"

    @staticmethod
    def _rsi(closes, n=14) -> float:
        if len(closes) < n + 1:
            return 50.0
        gains = losses = 0.0
        for i in range(-n, 0):
            d = closes[i] - closes[i - 1]
            gains += max(d, 0.0)
            losses += max(-d, 0.0)
        if losses == 0:
            return 100.0
        return 100 - 100 / (1 + gains / losses)

    def on_day(self, game: Game):
        for code in list(self.holdings):
            closes = [k["close"] for k in game.stock(code).history]
            if self._rsi(closes) > 70:
                self._sell_all(game, code)
        oversold = [st for st in game.stocks
                    if st.code not in self.holdings
                    and self._rsi([k["close"] for k in st.history]) < 30]
        if oversold and self.cash > 2000:
            per = self.cash / len(oversold)
            for st in oversold:
                self._buy(game, st.code, per)


class MomentumStrategy(Strategy):
    """动量轮动：每20个交易日买入近20日涨幅前2的股票。"""

    name = "动量轮动"
    desc = "追涨杀跌：每20个交易日轮动，重仓近20日涨幅最强的2只股票"

    @staticmethod
    def _ret20(game: Game, code: str) -> float:
        h = game.stock(code).history
        if len(h) < 21:
            return 0.0
        return (h[-1]["close"] - h[-21]["close"]) / h[-21]["close"]

    def on_day(self, game: Game):
        self.days_run += 1
        if self.days_run % 20 != 0:
            return
        for code in list(self.holdings):
            self._sell_all(game, code)
        top = sorted(game.stocks, key=lambda st: self._ret20(game, st.code),
                     reverse=True)[:2]
        if self.cash > 2000:
            per = self.cash / len(top)
            for st in top:
                self._buy(game, st.code, per)


class DCAStrategy(Strategy):
    """智能定投：每5个交易日把现金的30%等权买入全部股票。"""

    name = "智能定投"
    desc = "定期定额：每5个交易日拿出现金的30%等权买入所有股票，摊平成本"

    def on_day(self, game: Game):
        self.days_run += 1
        if self.days_run % 5 != 0:
            return
        if self.cash < 5000:
            return
        per = self.cash * 0.3 / len(game.stocks)
        for st in game.stocks:
            self._buy(game, st.code, per)


STRATEGY_CLASSES = {s.name: s for s in
                    (DualMAStrategy, RSIStrategy, MomentumStrategy, DCAStrategy)}


class Benchmark:
    """买入持有基准：开局按等权买入全部股票并长期持有。"""

    def __init__(self, cash: float, game: Game):
        self.initial = cash
        self.cash = cash
        self.holdings = {}
        per = cash / len(game.stocks)
        for st in game.stocks:
            price = st.current_price()
            shares = int(per // price // LOT_SIZE) * LOT_SIZE
            if shares <= 0:
                continue
            amount = price * shares
            fee = Game.buy_fee(amount)
            if amount + fee <= self.cash:
                self.cash -= amount + fee
                self.holdings[st.code] = shares

    def assets(self, game: Game) -> float:
        return self.cash + sum(sh * game.price_of(c)
                               for c, sh in self.holdings.items())


class QuantLab:
    """量化实验室：管理策略账户、基准账户，记录每日资产曲线。"""

    def __init__(self, game: Game, strat_name: str, cash: float = INIT_CASH):
        self.game = game
        self.strat_name = strat_name
        self.strategy = STRATEGY_CLASSES[strat_name](cash)
        self.benchmark = Benchmark(cash, game)
        self.started = False
        self.history = []   # [(date, 策略资产, 基准资产, 玩家资产)]

    def start(self):
        if self.started:
            return
        self.started = True
        self.history = []
        self.record()

    def stop(self):
        self.started = False

    def advance(self):
        if not self.started:
            return
        self.strategy.on_day(self.game)
        self.record()

    def record(self):
        self.history.append((
            self.game.current_date().strftime("%Y-%m-%d"),
            self.strategy.assets(self.game),
            self.benchmark.assets(self.game),
            self.game.total_assets(),
        ))

    def series(self, idx: int):
        """取第 idx 列序列（1=策略, 2=基准, 3=玩家）。"""
        return [h[idx] for h in self.history]


class MultiLineChart:
    """多序列收益率对比图：策略 vs 基准 vs 玩家。"""

    def __init__(self, canvas: tk.Canvas, width: int, height: int):
        self.canvas = canvas
        self.w, self.h = width, height
        self.left, self.right, self.top, self.bottom = 58, 10, 26, 22

    def draw(self, series: list, labels: dict):
        """series: [(name, color, [values])]，归一化为收益率百分比。"""
        cv = self.canvas
        cv.delete("all")
        cv.create_rectangle(0, 0, self.w, self.h, fill=BG_COLOR, outline=BG_COLOR)
        norm = []
        for name, color, vals in series:
            if not vals:
                continue
            base = vals[0] or 1.0
            norm.append((name, color, [v / base * 100 - 100 for v in vals]))
        if not norm:
            return
        all_v = [v for _, _, vs in norm for v in vs]
        lo, hi = min(all_v), max(all_v)
        if hi - lo < 0.5:
            hi, lo = lo + 0.5, lo - 0.5
        pad = (hi - lo) * 0.12
        lo, hi = lo - pad, hi + pad
        pw = self.w - self.left - self.right
        ph = self.h - self.top - self.bottom
        n = max(len(vs) for _, _, vs in norm)

        def px(i): return self.left + pw * i / (n - 1) if n > 1 else self.left + pw / 2
        def py(v): return self.top + ph * (1 - (v - lo) / (hi - lo))

        # 网格与刻度（百分比）
        for g in range(5):
            v = lo + (hi - lo) * g / 4
            y = py(v)
            cv.create_line(self.left, y, self.w - self.right, y, fill=GRID_COLOR)
            cv.create_text(self.left - 4, y, text=f"{v:+.1f}%", anchor="e",
                           fill=TEXT_COLOR, font=("Microsoft YaHei", 8))
        # 0% 基准线
        y0 = py(0)
        cv.create_line(self.left, y0, self.w - self.right, y0, fill="#5a6a80", dash=(4, 3))
        cv.create_text(self.w - self.right + 3, y0, text="0%", anchor="w",
                       fill="#8fa1b8", font=("Microsoft YaHei", 8))

        # 各序列曲线
        for name, color, vals in norm:
            pts = [(px(i), py(v)) for i, v in enumerate(vals)]
            for i in range(1, len(pts)):
                cv.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1],
                               fill=color, width=2)
            cv.create_oval(pts[-1][0] - 3, pts[-1][1] - 3, pts[-1][0] + 3,
                           pts[-1][1] + 3, fill=color, outline="#ffffff")

        # 标题 + 图例
        cv.create_text(self.left + 2, 4, anchor="nw",
                       text="📊 收益率对比（%）", fill="#c8d0dc",
                       font=("Microsoft YaHei", 9, "bold"))
        x = self.left
        for name, color, vals in norm:
            label = f"{name} {vals[-1]:+.2f}%" if vals else name
            cv.create_line(x, 12, x + 14, 12, fill=color, width=3)
            cv.create_text(x + 18, 12, text=label, anchor="w", fill=color,
                           font=("Microsoft YaHei", 8, "bold"))
            x += 22 + len(label) * 7.5


class QuantWindow:
    """量化实验室窗口。"""

    def __init__(self, app: "GameApp"):
        self.app = app
        self.lab = None
        self.win = tk.Toplevel(app.root)
        self.win.title("🤖 量化实验室 —— 让程序替你炒股")
        self.win.geometry("940x660")
        self.win.configure(bg="#151a23")
        self._build_ui()
        self._refresh()

    # ---------- 界面 ----------
    def _build_ui(self):
        w = self.win
        head = tk.Frame(w, bg="#1b2230")
        head.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(head, text="选择策略:", bg="#1b2230", fg="#e8c95a",
                 font=("Microsoft YaHei", 10, "bold")).pack(side="left", padx=6)
        self.combo = ttk.Combobox(head, state="readonly", width=16,
                                  values=list(STRATEGY_CLASSES.keys()),
                                  font=("Microsoft YaHei", 10))
        self.combo.current(0)
        self.combo.pack(side="left", padx=4)
        self.combo.bind("<<ComboboxSelected>>", lambda e: self._show_desc())
        self.lbl_desc = tk.Label(head, text="", bg="#1b2230", fg="#9fb2c8",
                                 font=("Microsoft YaHei", 9), wraplength=380,
                                 justify="left")
        self.lbl_desc.pack(side="left", padx=10)
        self.btn_start = tk.Button(head, text="▶ 启动策略", command=self._start,
                                   bg="#1e8449", fg="white",
                                   font=("Microsoft YaHei", 10, "bold"),
                                   bd=0, padx=14, pady=4, activebackground="#186a3b")
        self.btn_start.pack(side="right", padx=4)
        self.btn_stop = tk.Button(head, text="⏸ 暂停", command=self._stop,
                                  bg="#7d6608", fg="white",
                                  font=("Microsoft YaHei", 9), bd=0, padx=10,
                                  pady=4, activebackground="#9a7d0a")
        self.btn_stop.pack(side="right", padx=4)
        self.btn_reset = tk.Button(head, text="↺ 重置", command=self._reset_lab,
                                   bg="#39465f", fg="white",
                                   font=("Microsoft YaHei", 9), bd=0, padx=10,
                                   pady=4, activebackground="#4a5a78")
        self.btn_reset.pack(side="right", padx=4)
        self._show_desc()

        # 曲线图
        self.canvas = tk.Canvas(w, bg="#10141c", highlightthickness=0, height=320)
        self.canvas.pack(fill="x", padx=8, pady=4)
        self.canvas.bind("<Configure>", lambda e: self._refresh())
        self.chart = MultiLineChart(self.canvas, 920, 320)

        # 状态表格
        self.tree = ttk.Treeview(w, columns=("item", "assets", "rate", "extra"),
                                 show="headings", height=4)
        for c, t, wd in [("item", "对比项", 140), ("assets", "总资产", 160),
                         ("rate", "收益率", 120), ("extra", "备注", 460)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=wd, anchor="center")
        self.tree.pack(fill="x", padx=8, pady=4)

        note = ("💡 提示：策略与基准各用 200,000 元独立虚拟资金，每个交易日收盘时"
                "（自动交易跨日 / 点【下一交易日】）按收盘价自动运行一次。"
                "行情为模拟数据，仅供学习参考。")
        tk.Label(w, text=note, bg="#151a23", fg="#7ec699", wraplength=900,
                 justify="left", font=("Microsoft YaHei", 9)).pack(anchor="w", padx=12)

    def _show_desc(self):
        name = self.combo.get()
        cls = STRATEGY_CLASSES[name]
        self.lbl_desc.config(text=cls.desc)

    # ---------- 运行 ----------
    def _start(self):
        name = self.combo.get()
        if self.lab is None or self.lab.strat_name != name:
            self.lab = QuantLab(self.app.game, name)
        self.lab.start()
        self.btn_start.config(text="▶ 运行中…", state="disabled")
        self._refresh()

    def _stop(self):
        if self.lab:
            self.lab.stop()
            self.btn_start.config(text="▶ 继续运行", state="normal")

    def _reset_lab(self):
        self.lab = None
        self.btn_start.config(text="▶ 启动策略", state="normal")
        self._refresh()

    def on_main_day_advanced(self):
        """主界面每推进一个交易日时调用。"""
        if self.lab and self.lab.started:
            self.lab.advance()
        self._refresh()

    def _refresh(self):
        self._refresh_table()
        self._redraw_chart()

    def _redraw_chart(self):
        if not self.lab or not self.lab.history:
            self.canvas.delete("all")
            self.canvas.create_text(self.canvas.winfo_width() // 2 + 20,
                                    self.canvas.winfo_height() // 2,
                                    text="启动策略后，收益曲线将在此显示（需推进交易日）",
                                    fill="#8fa1b8", font=("Microsoft YaHei", 10))
            return
        w = max(300, self.canvas.winfo_width())
        h = max(240, self.canvas.winfo_height())
        self.chart = MultiLineChart(self.canvas, w, h)
        self.chart.draw([
            (f"🤖 {self.lab.strat_name}", "#3ddc84", self.lab.series(1)),
            ("📈 买入持有基准", "#8a93a3", self.lab.series(2)),
            ("🧑 你的手动操作", "#4a9eff", self.lab.series(3)),
        ], {})

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        g = self.app.game
        if not self.lab or not self.lab.history:
            self.tree.insert("", "end", values=(
                "🤖 策略（未启动）", "-", "-", "点击【启动策略】开始，之后正常推进交易日即可"))
            self.tree.insert("", "end", values=(
                "📈 买入持有基准", "-", "-", "等权买入全部 8 只股票并持有不动"))
            self.tree.insert("", "end", values=(
                "🧑 你的手动操作", fmt_money(g.total_assets()),
                f"{g.return_rate():+.2f}%", "主界面账户"))
            return
        s = self.lab.strategy
        b = self.lab.benchmark
        sa, ba, pa = self.lab.series(1)[-1], self.lab.series(2)[-1], self.lab.series(3)[-1]
        self.tree.insert("", "end", values=(
            f"🤖 {self.lab.strat_name}", fmt_money(sa), f"{sa / s.initial * 100 - 100:+.2f}%",
            f"交易 {s.trades} 次，手续费 {s.fees:,.0f} 元，运行 {len(self.lab.history)} 个交易日"))
        self.tree.insert("", "end", values=(
            "📈 买入持有基准", fmt_money(ba), f"{ba / b.initial * 100 - 100:+.2f}%",
            "等权买入全部 8 只股票并持有不动"))
        self.tree.insert("", "end", values=(
            "🧑 你的手动操作", fmt_money(pa), f"{g.return_rate():+.2f}%",
            "主界面账户（与策略同日起点对比）"))
        excess = (sa - s.initial) - (ba - b.initial)
        tag = "跑赢" if excess >= 0 else "跑输"
        self.tree.insert("", "end", values=(
            "⚖️ 策略 vs 基准", f"{excess:+,.2f} 元",
            f"{excess / b.initial * 100:+.2f}%", f"策略{tag}基准 {abs(excess):,.0f} 元"))


def main():
    random.seed()  # 每次开局行情都不同
    app = GameApp()
    app.root.mainloop()


if __name__ == "__main__":
    main()
