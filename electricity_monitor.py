# -*- coding: utf-8 -*-
"""宿舍电量监控：每小时采样 -> 本地台账 -> 周一 00:00 周报 + 低电量每 3 小时提醒。

取数不依赖模拟器：用 wanxiao_login 自造 token，再走 CloudPayment 的四步链路
（pay.do 换 SESSION -> bill/type.do -> querySelfPayList -> selectPayProject
-> getRoomState）。注意 token 用的是**本次登录的握手 session**，不是响应体里的
user.token；且 selectPayProject 之后必须拿它当 Referer，否则 getRoomState 报系统繁忙。

CLI：
  python electricity_monitor.py projects   # 首次配置：列出本校缴费项目
  python electricity_monitor.py rooms ...  # 首次配置：下钻找 roomverify
  python electricity_monitor.py test       # 读一次 + 推一条确认消息
  python electricity_monitor.py sample     # 采一次并入库，顺带做低电量检查
  python electricity_monitor.py now        # 只查一次当前剩余电量，不入库不推送
  python electricity_monitor.py report     # 立刻按周报格式推送一次（--dry 只看不动）
  python electricity_monitor.py status     # 台账概览
  python electricity_monitor.py run        # 常驻循环（不用 cron 时）
  python electricity_monitor.py install    # 打印 cron / 计划任务配置
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sqlite3
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wanxiao_login  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DIANFEI_DB") or os.path.join(HERE, "electricity.db")
CONFIG_PATH = os.environ.get("DIANFEI_CONFIG") or os.path.join(HERE, "config.json")
CFG = json.load(open(CONFIG_PATH, encoding="utf-8"))

CP_BASE = "https://h5cloud.17wanxiao.com:18443/CloudPayment/"
WEB_UA = ("Mozilla/5.0 (Linux; Android 12; 24031PN0DC Build/V417IR; wv) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 "
          "Safari/537.36 Wanxiao/5.8.9 Wmxy/5.8.27")
VERSIONCODE = "10589101"
VERSIONNUM = "1050827101"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

WEEKDAY_CN = "一二三四五六日"
BUCKETS = ("0-8", "8-16", "16-24")


# ------------------------------------------------------- CloudPayment -----
class NoPowerData(RuntimeError):
    pass


class CloudPay:
    """CloudPayment 客户端。

    必须用真正的 CookieJar：pay.do 是先 302 到 bill/type.do 并在这一跳下发
    HttpOnly 的 SESSION，手工解析最终响应的 Set-Cookie 会把它整条丢掉。
    """

    def __init__(self, schoolcode, timeout=25):
        self.schoolcode = str(schoolcode)
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.referer = CP_BASE + "bill/type.do"
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=CTX))

    def _get(self, path, referer=None, json_ok=False, **params):
        url = CP_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        h = {"User-Agent": WEB_UA,
             "Accept": ("application/json, text/javascript, */*; q=0.01"
                        if json_ok else "*/*"),
             "X-Requested-With": "XMLHttpRequest",
             "Referer": referer or self.referer}
        req = urllib.request.Request(url, headers=h)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                return e.read().decode("utf-8", "replace")
            except Exception:
                return ""

    def session_id(self):
        return next((c.value for c in self.jar if c.name == "SESSION"), None)

    def sso(self, token):
        """token 有效才会下发 CloudPayment 的 SESSION cookie。"""
        self._get("user/pay.do", CP_BASE, versionnum=VERSIONNUM,
                  customerId=self.schoolcode, systemType="Android",
                  UAinfo="wanxiao", versioncode=VERSIONCODE, token=token)
        if not self.session_id():
            raise NoPowerData("token 未被 CloudPayment 接受（SSO 未下发 SESSION）")
        return self.session_id()

    def projects(self):
        raw = self._get("bill/querySelfPayList.do", json_ok=True)
        try:
            return json.loads(raw).get("payprolist") or []
        except Exception:
            return []

    def select_project(self, proj):
        params = dict(txcode=proj.get("txcode") or "",
                      interurl=proj.get("interurl") or "",
                      payProId=str(proj["id"]),
                      amtflag=proj.get("amtflag") or "0",
                      payamt=proj.get("payamt") or "0",
                      payproname=proj.get("payproname") or "",
                      img=proj.get("img") or "", subPayProId="")
        # 服务端从 Referer 里的 payProId 反查房间序列号 idserial，
        # 所以后面几个接口必须把这条完整 URL 当 Referer 回传。
        self.referer = (CP_BASE + "bill/selectPayProject.do?" +
                        urllib.parse.urlencode(params))
        self._get("bill/selectPayProject.do", CP_BASE + "bill/type.do",
                  **params)

    def room_state(self, proj, roomverify):
        raw = self._get("user/getRoomState.do", json_ok=True,
                        payProId=str(proj["id"]), schoolcode=self.schoolcode,
                        businesstype=proj.get("businesstype") or "2",
                        roomverify=roomverify)
        try:
            d = json.loads(raw)
        except Exception:
            raise NoPowerData("getRoomState 返回非 JSON: %s" % raw[:120])
        if str(d.get("returncode")) != "100":
            raise NoPowerData("getRoomState: %s" % (d.get("returnmsg") or raw[:120]))
        return d

    def room_level(self, proj, optype, areaid=0, buildid=0, unitid=0,
                   levelid=0):
        """房间树的一层：optype 1 校区 / 2 楼栋 / 3 楼层(单元) / 4 房间。"""
        raw = self._get("user/getRoom.do", json_ok=True,
                        payProId=str(proj["id"]), schoolcode=self.schoolcode,
                        businesstype=str(proj.get("businesstype") or "2"),
                        optype=str(optype), areaid=str(areaid),
                        buildid=str(buildid), unitid=str(unitid),
                        levelid=str(levelid))
        try:
            return json.loads(raw).get("roomlist") or []
        except Exception:
            return []

    def open_project(self, token):
        """SSO + 选中电费项目，返回 (全部项目, 选中的那个)。

        后面的接口全都依赖 select_project 建立的会话上下文，这一步省不得。
        """
        self.sso(token)
        plist = self.projects()
        if not plist:
            # 实测：token 无效时 CloudPayment 照样发 SESSION，但项目列表为空，
            # 所以“项目列表非空”才是 token 真的被认的唯一判据。
            raise NoPowerData("token 已失效（SSO 后项目列表为空）")
        want_name = CFG["payPro"]["electric"].get("name", "用电支出")
        proj = next((p for p in plist if p.get("payproname") == want_name), None)
        if proj is None and plist:
            proj = plist[0]
        proj["businesstype"] = str(CFG["payPro"]["electric"]["businesstype"])
        self.select_project(proj)
        return plist, proj

    def read_kwh(self, token, roomverify):
        """返回 (剩余量 float, 单位, 描述)。"""
        _, proj = self.open_project(token)
        d = self.room_state(proj, roomverify)
        return float(d["quantity"]), d.get("quantityunit", "度"), d.get("description", "")


def read_once(token):
    cp = CloudPay(CFG["schoolcode"])
    return cp.read_kwh(token, CFG["room"]["roomverify"])


def get_kwh(force_login=False):
    """取剩余电量。token 疑似过期时自动重新登录再试一次。"""
    token = wanxiao_login.fresh_token(force=force_login)
    try:
        return read_once(token)
    except NoPowerData as e:
        if force_login:
            raise
        print("[提示] %s，重新登录后重试" % e)
        return read_once(wanxiao_login.fresh_token(force=True))


# ----------------------------------------------------------- 首次配置辅助 ---
def do_projects():
    """列出本校的缴费项目，用来填 config.json 里的 payPro。"""
    cp = CloudPay(CFG["schoolcode"])
    plist, chosen = cp.open_project(wanxiao_login.fresh_token())
    print("schoolcode = %s" % CFG["schoolcode"])
    for p in plist:
        mark = "*" if p is chosen else " "
        print("  %s payProId=%-6s name=%-8s businesstype=%s txcode=%s"
              % (mark, p.get("id"), p.get("payproname"),
                 p.get("businesstype"), p.get("txcode")))
    print("把你要监控的那一项的 id / businesstype 填进 config.json")


def do_rooms(campus=None, building=None, room=None, max_calls=400):
    """下钻 校区 -> 楼栋 -> 楼层 -> 房间，找出 roomverify 该填什么。

    整棵树几百个节点，一定要用 --campus / --building 剪枝，
    否则请求数会撞上限（也容易被 WAF 限流）。
    """
    cp = CloudPay(CFG["schoolcode"])
    _, proj = cp.open_project(wanxiao_login.fresh_token())
    calls = 0
    hits = []
    for c in cp.room_level(proj, 1):
        calls += 1
        if campus and campus not in str(c.get("name")):
            continue
        for b in cp.room_level(proj, 2, areaid=c["id"]):
            calls += 1
            if building and building not in str(b.get("name")):
                continue
            for f in cp.room_level(proj, 3, areaid=c["id"], buildid=b["id"]):
                calls += 1
                for r in cp.room_level(proj, 4, areaid=c["id"],
                                       buildid=b["id"], levelid=f["id"]):
                    calls += 1
                    if room and room not in str(r.get("name")):
                        continue
                    # optype=4 返回的 id 已经是拼好的 roomverify（形如
                    # "101-07--112-308"），中间的 -- 是「单元为空」留下的分隔符。
                    rv = (r["id"] if "--" in str(r.get("id", "")) else
                          "%s-%s--%s-%s" % (c["id"], b["id"], f["id"], r["id"]))
                    hits.append("%s / %s / %s / %s  ->  %s"
                                % (c.get("name"), b.get("name"),
                                   f.get("name"), r.get("name"), rv))
                    if calls > max_calls:
                        raise SystemExit("请求数超过 %d，请用 --campus / "
                                         "--building 缩小范围" % max_calls)
    if not hits:
        print("没找到，试试放宽 --campus / --building / --room")
    for h in hits[:80]:
        print("  " + h)
    print("共 %d 个候选（用了 %d 次请求）" % (len(hits), calls))


# ------------------------------------------------------------ storage -----
def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS samples("
              "ts INTEGER PRIMARY KEY, kwh REAL, unit TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS events("
              "ts INTEGER PRIMARY KEY, kind TEXT, detail TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS meta("
              "k TEXT PRIMARY KEY, v TEXT)")
    return c


def meta_get(c, k, default=None):
    row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(c, k, v):
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) "
              "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def hourly_series(c, start_ts, end_ts):
    """取区间内样本 + 区间起点前最后一个样本，用来算差值。"""
    rows = c.execute("SELECT ts,kwh FROM samples WHERE ts<=? ORDER BY ts",
                     (end_ts,)).fetchall()
    return rows


# ------------------------------------------------------------ analytics ---
def bucket_of(hour):
    return 0 if hour < 8 else (1 if hour < 16 else 2)


def split_interval(t0, t1):
    """把 [t0,t1) 按 0 点 / 8 点 / 16 点切成不跨时段边界的小段。

    返回 [(YYYY-MM-DD, 时段下标, 秒数)]，用来把两次读数的差值按占比摊到
    真正发生的时段，而不是全记在采样那一刻所在的天。
    """
    out = []
    cur = t0
    while cur < t1:
        day0 = datetime.fromtimestamp(cur).replace(
            hour=0, minute=0, second=0, microsecond=0)
        b8 = day0.timestamp() + 8 * 3600
        b16 = b8 + 8 * 3600
        b24 = b16 + 8 * 3600
        nxt = b8 if cur < b8 else (b16 if cur < b16 else b24)
        out.append((day0.strftime("%Y-%m-%d"),
                    0 if cur < b8 else (1 if cur < b16 else 2),
                    min(nxt, t1) - cur))
        cur = min(nxt, t1)
    return out


def usage_breakdown(c, start_dt, end_dt):
    """按天 + 三时段统计用电量（剩余电量下降量，充值造成的上升记 0）。"""
    rows = hourly_series(c, 0, int(end_dt.timestamp()))
    lo = start_dt.strftime("%Y-%m-%d")
    hi = (end_dt - timedelta(seconds=1)).strftime("%Y-%m-%d")
    per_day = {}
    recharges = 0.0
    n_in_window = 0
    prev = None
    for ts, kwh in rows:
        if lo <= datetime.fromtimestamp(ts).strftime("%Y-%m-%d") <= hi:
            n_in_window += 1
        if prev is not None:
            p_ts, p_kwh = prev
            delta = p_kwh - kwh
            span = max(ts - p_ts, 1)
            segs = split_interval(p_ts, ts)
            if delta < 0:
                # 充值：读数上升不算用电，单独记一笔
                if segs and lo <= segs[-1][0] <= hi:
                    recharges += -delta
            elif delta > 0:
                for day, idx, dur in segs:
                    if lo <= day <= hi:
                        per_day.setdefault(
                            day, [0.0, 0.0, 0.0])[idx] += delta * dur / span
        prev = (ts, kwh)
    days = sorted(per_day)
    total = round(sum(sum(v) for v in per_day.values()), 2)
    return {"days": days, "per_day": per_day, "total": total,
            "recharges": round(recharges, 2),
            "n_samples": len(rows), "n_in_window": n_in_window}


def week_range(now=None):
    """刚过完的自然周：周一 00:00 ~ 下周一 00:00。"""
    now = now or datetime.now()
    this_mon = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return this_mon - timedelta(days=7), this_mon


# ---------------------------------------------------------------- push -----
def push(title, content, dry=False):
    token = CFG.get("pushplus_token") or os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        raise SystemExit("config.json 里缺 pushplus_token")
    body = json.dumps({"token": token, "title": title, "content": content,
                       "template": "html", "channel": "wechat"}).encode("utf-8")
    print("[推送] %s" % title)
    if dry:
        print(content)
        return {"code": 0, "msg": "dry-run"}
    req = urllib.request.Request("https://www.pushplus.plus/send", data=body,
                                 headers={"Content-Type":
                                          "application/json; charset=utf-8"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    print("       -> %s" % json.dumps(out, ensure_ascii=False)[:200])
    return out


def build_weekly(c, start_dt, end_dt):
    st = usage_breakdown(c, start_dt, end_dt)
    room = CFG["room"]["label"]
    total = st["total"]
    col_bucket = [0.0, 0.0, 0.0]
    ndays = 0
    tr = []
    # 按周一到周日的时间顺序逐天输出，没有样本的天显式标“无数据”
    for d in range((end_dt - start_dt).days):
        day_dt = start_dt + timedelta(days=d)
        day = day_dt.strftime("%Y-%m-%d")
        label = "%s 周%s" % (day[5:], WEEKDAY_CN[day_dt.weekday()])
        b = st["per_day"].get(day)
        if b is None:
            tr.append("<tr><td>%s</td>"
                      "<td colspan='4' align='center' "
                      "style='color:#bbb'>无数据</td></tr>" % label)
            continue
        ndays += 1
        for i in range(3):
            col_bucket[i] += b[i]
        tr.append("<tr><td>%s</td><td align='right'><b>%.1f</b></td>"
                  "<td align='right'>%.1f</td><td align='right'>%.1f</td>"
                  "<td align='right'>%.1f</td></tr>"
                  % (label, sum(b), b[0], b[1], b[2]))
    tr.append("<tr style='background:#f2f4f7'><td><b>时段合计</b></td>"
              "<td align='right'><b>%.1f</b></td>"
              "<td align='right'>%.1f</td><td align='right'>%.1f</td>"
              "<td align='right'>%.1f</td></tr>"
              % (sum(col_bucket), col_bucket[0], col_bucket[1], col_bucket[2]))
    last = c.execute("SELECT kwh,ts FROM samples ORDER BY ts DESC LIMIT 1").fetchone()

    span = "%s ~ %s" % (start_dt.strftime("%m-%d"),
                        (end_dt - timedelta(seconds=1)).strftime("%m-%d"))
    complete = ndays >= (end_dt - start_dt).days
    head = ("<div style='font-size:15px'>"
            "<p><b>%s %s</b>%s</p>"
            "<p>本周总用电 <b>%.1f 度</b>，日均 %.1f 度（有数据 %d 天）%s</p>"
            "<table width='100%%' cellspacing='0' cellpadding='6' "
            "style='border-collapse:collapse;font-size:14px'>"
            "<tr style='background:#f2f4f7'><th align='left'>日期</th>"
            "<th align='right'>合计(度)</th><th align='right'>0-8</th>"
            "<th align='right'>8-16</th><th align='right'>16-24</th></tr>%s</table>"
            % (room, span, "" if complete else "（数据不完整）", total,
               total / max(ndays, 1), ndays,
               "，期间充值 +%.1f 度" % st["recharges"] if st["recharges"] else "",
               "".join(tr)))
    if last:
        head += ("<p style='color:#666'>当前剩余 %.1f 度（采样于 %s）</p>"
                 % (last[0], datetime.fromtimestamp(last[1]).strftime("%m-%d %H:%M")))
    head += "</div>"
    title = "本周用电%g度" % round(total, 1)
    return title, head, st


def build_low(kwh):
    room = CFG["room"]["label"]
    title = "电费不足"
    content = ("<div style='font-size:16px'><b>%s还剩%g度电</b>"
               "<p style='color:#888;font-size:13px'>采样 %s</p></div>"
               % (room, round(kwh, 1), datetime.now().strftime("%m-%d %H:%M")))
    return title, content


# --------------------------------------------------------------- actions ---
def do_sample(c, dry=False, verbose=True):
    kwh, unit, desc = get_kwh()
    now = int(time.time())
    prev = c.execute("SELECT kwh FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    c.execute("INSERT OR REPLACE INTO samples(ts,kwh,unit) VALUES(?,?,?)",
              (now, kwh, unit))
    if prev and kwh - prev[0] > 0.5:
        c.execute("INSERT OR REPLACE INTO events(ts,kind,detail) VALUES(?,?,?)",
                  (now, "recharge", "+%.1f 度（%.1f -> %.1f）" % (kwh - prev[0], prev[0], kwh)))
        meta_set(c, "low_armed", "1")
        if verbose:
            print("检测到充值 +%.1f 度，低电量提醒重新武装" % (kwh - prev[0]))
    c.commit()
    if verbose:
        print("%s 剩余电量 %.1f%s" % (datetime.now().strftime("%m-%d %H:%M"), kwh, unit))
    check_low(c, kwh, dry=dry)
    return kwh


def check_low(c, kwh, dry=False):
    limit = float(CFG.get("threshold_low", 10))
    if kwh >= limit:
        return
    if meta_get(c, "low_armed", "1") != "1":
        return
    last_alert = int(meta_get(c, "last_low_alert", "0"))
    if time.time() - last_alert < 3 * 3600:
        return
    title, content = build_low(kwh)
    push(title, content, dry=dry)
    meta_set(c, "last_low_alert", int(time.time()))
    if kwh <= 0:
        meta_set(c, "low_armed", "0")
    c.commit()


def do_report(c, dry=False, force=False):
    start, end = week_range()
    st = usage_breakdown(c, start, end)
    # 少于 2 个样本就算不出任何差值，推出去只会是一条「本周用电0度」
    if st["n_in_window"] < 2 and not force:
        print("本周样本不足（%d 个），跳过周报" % st["n_in_window"])
        return None
    title, content, st = build_weekly(c, start, end)
    out = push(title, content, dry=dry)
    if not dry:
        meta_set(c, "last_report", int(time.time()))
        c.commit()
    return out


def do_status(c):
    n = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    first = c.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    last = c.execute("SELECT kwh,ts FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    print("数据库   : %s" % DB_PATH)
    print("样本数   : %d" % n)
    if first:
        print("最早样本 : %s" % datetime.fromtimestamp(first).strftime("%Y-%m-%d %H:%M"))
    if last:
        print("最新读数 : %.1f 度  @ %s" % (last[0], datetime.fromtimestamp(last[1]).strftime("%Y-%m-%d %H:%M")))
    print("上次周报 : %s" % (datetime.fromtimestamp(int(meta_get(c, "last_report", "0"))).strftime("%Y-%m-%d %H:%M") if meta_get(c, "last_report") else "从未"))
    print("上次低电 : %s" % (datetime.fromtimestamp(int(meta_get(c, "last_low_alert", "0"))).strftime("%Y-%m-%d %H:%M") if meta_get(c, "last_low_alert") else "从未"))
    print("低电武装 : %s" % meta_get(c, "low_armed", "1"))
    ev = c.execute("SELECT ts,kind,detail FROM events ORDER BY ts DESC LIMIT 5").fetchall()
    for ts, kind, detail in ev:
        print("  事件 %s %-9s %s" % (datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"), kind, detail))


def do_test(c):
    """部署自测：读一次实时电量，入库，并推一条确认消息。"""
    kwh, unit, desc = get_kwh()
    c.execute("INSERT OR REPLACE INTO samples(ts,kwh,unit) VALUES(?,?,?)",
              (int(time.time()), kwh, unit))
    c.commit()
    room = CFG["room"]["label"]
    limit = float(CFG.get("threshold_low", 10))
    title = "电费监控已上线"
    content = ("<div style='font-size:16px'><b>%s 当前剩余 %g%s电</b>"
               "<p style='color:#888;font-size:13px'>采样 %s · 主机 %s</p>"
               "<p style='font-size:13px;color:#555'>每小时自动采样；"
               "周一 00:05 推送上周汇总；剩余不足 %g%s 时每 3 小时提醒一次</p>"
               "</div>" % (room, round(kwh, 1), unit,
                           datetime.now().strftime("%Y-%m-%d %H:%M"),
                           socket.gethostname(), limit, unit))
    push(title, content)
    print("%.1f %s (%s) 已推送" % (kwh, unit, desc))
    return kwh


def do_run(c, interval=3600):
    print("常驻模式：每 %d 秒采一次，周一 00:00 出周报" % interval)
    while True:
        try:
            do_sample(c, verbose=True)
            now = datetime.now()
            if now.weekday() == 0 and now.hour == 0 and \
                    meta_get(c, "report_week") != now.strftime("%Y-%m-%d"):
                do_report(c)
                meta_set(c, "report_week", now.strftime("%Y-%m-%d"))
                c.commit()
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print("本轮失败: %r" % e)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="宿舍电量监控与微信推送")
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["sample", "now", "report", "status", "run",
                             "install", "test", "projects", "rooms"])
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    ap.add_argument("--force", action="store_true", help="无数据也推送（调试版式）")
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--campus", help="rooms：校区名关键字")
    ap.add_argument("--building", help="rooms：楼栋名关键字")
    ap.add_argument("--room", help="rooms：房间号关键字")
    a = ap.parse_args()
    c = db()

    if a.cmd == "now":
        kwh, unit, desc = get_kwh(force_login=True)
        print("%.1f %s (%s)" % (kwh, unit, desc))
    elif a.cmd == "sample":
        do_sample(c, dry=a.dry)
    elif a.cmd == "report":
        do_report(c, dry=a.dry, force=a.force)
    elif a.cmd == "status":
        do_status(c)
    elif a.cmd == "test":
        do_test(c)
    elif a.cmd == "projects":
        do_projects()
    elif a.cmd == "rooms":
        do_rooms(a.campus, a.building, a.room)
    elif a.cmd == "run":
        do_run(c, a.interval)
    elif a.cmd == "install":
        py = sys.executable.replace("\\", "/")
        me = os.path.abspath(__file__).replace("\\", "/")
        d = os.path.dirname(me)
        print("# Linux cron（服务器上），追加时先用 crontab -l 保住已有条目")
        print("PYTHONIOENCODING=utf-8")
        print("0 * * * * flock -n %s/.lock %s %s sample >> %s/monitor.log 2>&1"
              % (d, py, me, d))
        print("5 0 * * 1 flock -n %s/.lock %s %s report >> %s/monitor.log 2>&1"
              % (d, py, me, d))
        print("# Windows 计划任务：")
        print('schtasks /Create /TN DianfeiHourly /SC HOURLY /MO 1 '
              '/TR "\\"%s\\" \\"%s\\" sample" /F' % (py, me))
        print('schtasks /Create /TN DianfeiWeekly /SC WEEKLY /D MON /ST 00:05 '
              '/TR "\\"%s\\" \\"%s\\" report" /F' % (py, me))
    return 0


if __name__ == "__main__":
    sys.exit(main())
