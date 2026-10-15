# -*- coding: utf-8 -*-
"""推送与统计逻辑自检：不联网、不真推送。

跑法： python tests/selftest.py
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["DIANFEI_DB"] = os.path.join(HERE, "selftest.db")
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ["DIANFEI_CONFIG"] = os.path.join(ROOT, "config.example.json")
sys.path.insert(0, ROOT)
import electricity_monitor as em  # noqa: E402

fails = []


def memdb():
    m = sqlite3.connect(":memory:")
    m.execute("CREATE TABLE samples(ts INTEGER PRIMARY KEY, kwh REAL, unit TEXT)")
    return m


def check(name, got, want):
    ok = got == want
    print("%-34s %s  %r" % (name, "OK " if ok else "FAIL", got))
    if not ok:
        print("     期望 %r" % (want,))
        fails.append(name)


# 1) 跨天/跨时段的区间切分
base = datetime(2026, 9, 7, 0, 0).timestamp()
check("split_interval 跨 0 点",
      em.split_interval(base + 23 * 3600, base + 25 * 3600),
      [("2026-09-07", 2, 3600.0), ("2026-09-08", 0, 3600.0)])
check("split_interval 跨 8 点",
      em.split_interval(base + 7 * 3600, base + 9 * 3600),
      [("2026-09-07", 0, 3600), ("2026-09-07", 1, 3600)])
check("split_interval 整周",
      sum(d for _, _, d in em.split_interval(base, base + 7 * 86400)),
      7 * 86400)

# 2) 用量统计：三天线性下降，总量必须精确守恒
c = sqlite3.connect(":memory:")
c.execute("CREATE TABLE samples(ts INTEGER PRIMARY KEY, kwh REAL, unit TEXT)")
c.execute("CREATE TABLE events(ts INTEGER PRIMARY KEY, kind TEXT, detail TEXT)")
c.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
for h in range(72 + 1):
    c.execute("INSERT INTO samples VALUES(?,?,?)",
              (int(base + h * 3600), 100 - h, "度"))
st = em.usage_breakdown(c, datetime(2026, 9, 7), datetime(2026, 9, 10))
check("总量守恒（72 小时 72 度）", st["total"], 72.0)
check("三天分桶之和 = 总量",
      round(sum(sum(v) for v in st["per_day"].values()), 6), 72.0)
check("每天 24 度", sorted(round(sum(v), 6) for v in st["per_day"].values()),
      [24.0, 24.0, 24.0])
check("每段 8 度",
      sorted({round(x, 6) for v in st["per_day"].values() for x in v}), [8.0])
check("窗口内样本数", st["n_in_window"], 72)
check("周报标题", em.build_weekly(c, datetime(2026, 9, 7),
                                 datetime(2026, 9, 14))[0], "本周用电72度")

# 3) 窗口外的差值不能漏进本周
pre = memdb()
for t, k in ((base - 3600, 103.0), (base, 100.0), (base + 3600, 99.0)):
    pre.execute("INSERT INTO samples VALUES(?,?,?)", (int(t), k, "度"))
st2 = em.usage_breakdown(pre, datetime(2026, 9, 7), datetime(2026, 9, 10))
check("起点前的历史跳变不漏进本周", st2["total"], 1.0)
check("窗口外那一跳不计入", st2["n_in_window"], 2)

# 4) 充值：读数上升记 0，并单独统计
rc = memdb()
for h, k in ((10, 50.0), (11, 49.0), (12, 69.0), (13, 68.0)):
    rc.execute("INSERT INTO samples VALUES(?,?,?)",
               (int(base + (24 + h) * 3600), k, "度"))
st3 = em.usage_breakdown(rc, datetime(2026, 9, 7), datetime(2026, 9, 10))
check("充值不计入用电量", st3["total"], 2.0)
check("充值量被单独记录", st3["recharges"], 20.0)
check("用电落在 8-16 段",
      [round(x, 6) for x in st3["per_day"]["2026-09-08"]], [0.0, 2.0, 0.0])

# 5) 低电量 3 小时节流
mem = sqlite3.connect(":memory:")
mem.execute("CREATE TABLE samples(ts INTEGER PRIMARY KEY, kwh REAL, unit TEXT)")
mem.execute("CREATE TABLE events(ts INTEGER PRIMARY KEY, kind TEXT, detail TEXT)")
mem.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
n0 = []
orig_push = em.push
em.push = lambda t, x, dry=False: n0.append(t)
em.check_low(mem, 8.4)
check("首次低于 10 度即推送", len(n0), 1)
em.check_low(mem, 8.1)
check("3 小时内不重复", len(n0), 1)
mem.execute("DELETE FROM meta")
em.meta_set(mem, "last_low_alert", int(time.time()) - 4 * 3600)
em.check_low(mem, 8.1)
check("超过 3 小时再次提醒", len(n0), 2)
em.check_low(mem, 10.4)
check("回到 10 度以上不推送", len(n0), 2)
em.meta_set(mem, "low_armed", "0")
em.check_low(mem, 0.0)
check("电量耗尽后停发", len(n0), 2)
em.push = orig_push

check("低电量标题", em.build_low(8.4)[0], "电费不足")
check("低电量正文", "%s还剩8.4度电" % em.CFG["room"]["label"]
      in em.build_low(8.4)[1], True)

print("\n%s" % ("全部通过" if not fails else "失败项: %s" % fails))
sys.exit(1 if fails else 0)
