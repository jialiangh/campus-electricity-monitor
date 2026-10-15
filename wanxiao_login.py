# -*- coding: utf-8 -*-
"""完美校园（wanxiao）服务器端登录模块 —— 自造 CloudPayment 所需的 sessionId/token。

协议链（已实测被服务端接受）：
  1. 本地生成 RSA-1024 密钥对 -> POST exchangeSecretkey.action {"key": <SPKI DER base64>}
  2. 用私钥 RSA PKCS#1 v1.5 解密响应 -> {"session": uuid, "key": <32 hex>}
  3. appKey = key[:24]，其后全部业务体走 3DES-CBC(IV="66666666", PKCS#5) + base64
  4. 账密登录 loginnew.action：password 字段是**逐字符**分别 3DES 加密后的 base64 数组
  5. 新设备会被 code_=5 拦下 -> gainMatrixCaptcha.action 发短信 ->
     registerUsersByTelAndLoginNew.action 用验证码登录，同时把本机 deviceId 绑成受信设备。
     注意发短信必须用带 543 后缀的那个变体，见 send_sms() 里的注释。
  6. 外层信封恒为 {"session": sid, "data": <3DES b64>}，请求头 campusSign =
     sha256(请求体原文).hexdigest()

状态文件（deviceId + token + 取到 token 的时间）默认落在脚本同目录的
`wanxiao_state.json`，换机器部署时把 deviceId 一起带走即可保持绑定。

CLI：
  python wanxiao_login.py status              # 看当前 token / deviceId / 绑定情况
  python wanxiao_login.py login               # 纯账密登录（不消耗短信）
  python wanxiao_login.py sms-send            # 给手机发短信验证码
  python wanxiao_login.py sms-code 123456     # 提交验证码完成绑定登录
  python wanxiao_login.py sms                 # 上两条合一（会现场等你输入验证码）
  python wanxiao_login.py token               # 只打印一个可用 token（供采集器调用）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar

# TripleDES 在 cryptography 45+ 被挪进了 decrepit 包，老版本（如 Ubuntu 自带的
# 41.x）只在 primitives 里，两个位置都试一遍，并且屏蔽它的弃用告警。
import warnings as _warnings

try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore")
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE = "https://app.17wanxiao.com/campus"
IV = b"66666666"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("WANXIAO_STATE") or os.path.join(HERE, "wanxiao_state.json")
CONFIG_PATH = (os.environ.get("DIANFEI_CONFIG") or
               os.environ.get("WANXIAO_CONFIG") or
               os.path.join(HERE, "config.json"))

DEFAULT_UA = ("Dalvik/2.1.0 (Linux; U; Android 12; 24031PN0DC Build/ccbda31.0)")
DEFAULT_MODEL = "24031PN0DC"
DEFAULT_VERSION = 10589101

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- crypto ---
def pad8(data: bytes) -> bytes:
    n = 8 - len(data) % 8
    return data + bytes([n]) * n


def unpad8(data: bytes) -> bytes:
    n = data[-1]
    if n < 1 or n > 8 or data[-n:] != bytes([n]) * n:
        raise ValueError("bad PKCS5 padding")
    return data[:-n]


def _des3(key_text: str):
    try:
        algo = TripleDES(key_text.encode("ascii"), adjust_parity=True)
    except TypeError:
        algo = TripleDES(key_text.encode("ascii"))
    return algo


def des3_encrypt(plain: bytes, key_text: str) -> str:
    enc = Cipher(_des3(key_text), modes.CBC(IV)).encryptor()
    return base64.b64encode(enc.update(pad8(plain)) + enc.finalize()).decode("ascii")


def des3_decrypt(b64_text: str, key_text: str) -> bytes:
    raw = base64.b64decode(b64_text)
    dec = Cipher(_des3(key_text), modes.CBC(IV)).decryptor()
    return unpad8(dec.update(raw) + dec.finalize())


def spki_body_b64(priv) -> str:
    der = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    s = base64.b64encode(der).decode("ascii")
    return "\n".join(s[i:i + 64] for i in range(0, len(s), 64)) + "\n"


# ------------------------------------------------------------- transport ---
def _decode_body(raw_bytes, encoding):
    """部分接口返回 gzip/deflate，urllib 不会自动解压。"""
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            import gzip
            return gzip.decompress(raw_bytes)
        if "deflate" in enc:
            import zlib
            try:
                return zlib.decompress(raw_bytes)
            except zlib.error:
                return zlib.decompress(raw_bytes, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw_bytes


class WanxiaoError(RuntimeError):
    pass


class Wanxiao:
    """一个握手会话 = 一个 session/appKey；token 登录后才有。"""

    def __init__(self, phone, password, device_id, model=DEFAULT_MODEL,
                 ua=DEFAULT_UA, version=DEFAULT_VERSION, verbose=True):
        self.phone = phone
        self.password = password
        self.device_id = device_id
        self.model = model
        self.ua = ua
        self.version = version
        self.verbose = verbose
        self.session = None
        self.app_key = None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            urllib.request.HTTPSHandler(context=CTX))

    def log(self, *a):
        if self.verbose:
            print(*a)

    def _post(self, path, body_text, headers=None):
        h = {"Content-Type": "application/text", "User-Agent": self.ua}
        if headers:
            h.update(headers)
        req = urllib.request.Request(BASE + path, data=body_text.encode("utf-8"),
                                     headers=h, method="POST")
        try:
            with self.opener.open(req, timeout=30) as r:
                return _decode_body(
                    r.read(), r.headers.get("Content-Encoding")).decode(
                    "utf-8", "replace")
        except urllib.error.HTTPError as e:
            return _decode_body(
                e.read(), e.headers.get("Content-Encoding")).decode("utf-8",
                                                                    "replace")

    def _envelope(self, path, args):
        """把业务参数按官方信封发出请求，返回解好的 dict（或原始文本）。"""
        body = json.dumps({"session": self.session,
                           "data": des3_encrypt(json.dumps(args).encode("utf-8"),
                                                self.app_key)})
        sign = hashlib.sha256(body.encode("utf-8")).hexdigest()
        raw = self._post(path, body, {"campusSign": sign})
        try:
            resp = json.loads(raw)
        except Exception:
            raise WanxiaoError("%s 返回非 JSON: %s" % (path, raw[:160]))
        if resp.get("data"):
            try:
                resp["_plain"] = json.loads(
                    des3_decrypt(resp["data"], self.app_key).decode("utf-8")
                    .replace("\r", ""))
            except Exception:
                pass
        return resp

    def _base_args(self, request_method, **kw):
        args = {
            "appCode": "M002",
            "deviceId": self.device_id,
            "netWork": "wifi",
            "qudao": "guanwang",
            "requestMethod": request_method,
            "shebeixinghao": self.model,
            "systemType": "android",
            "telephoneInfo": "12",
            "telephoneModel": self.model,
        }
        args.update(kw)
        return args

    # ------------------------------------------------------------ steps ---
    def handshake(self):
        priv = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        raw = self._post("/cam_iface46/exchangeSecretkey.action",
                         json.dumps({"key": spki_body_b64(priv)}))
        blob = base64.b64decode(raw.strip())
        info = json.loads(priv.decrypt(blob, padding.PKCS1v15()).decode("utf-8"))
        self.session = info["session"]
        self.app_key = info["key"][:24]
        self.log("[握手] session=%s appKey=%s" % (self.session, self.app_key))
        return self.session

    def password_login(self):
        """返回 (ok, token_or_reason)。code_=5 且提示新设备时 ok=False、值为 NEW_DEVICE。"""
        args = self._base_args(
            "cam_iface46/loginnew.action",
            password=[des3_encrypt(ch.encode("utf-8"), self.app_key)
                      for ch in self.password],
            type="1",
            userName=self.phone,
            wanxiaoVersion=self.version,
        )
        resp = self._envelope("/cam_iface46/loginnew.action", args)
        msg = resp.get("message_", "")
        if resp.get("result_"):
            return True, self._token_from(resp)
        self.log("[账密] 失败 code_=%s %s" % (resp.get("code_"), msg))
        if "新设备" in msg:
            return False, "NEW_DEVICE"
        if "密码错误" in msg:
            return False, "BAD_PASSWORD:" + msg
        return False, msg

    def check_phone(self):
        args = self._base_args("cam_iface46/gainMatrixCaptcha.action",
                               action="registAndLogin", confirm=False,
                               mobile=self.phone, type="sms")
        resp = self._envelope("/cam_iface46/checkBeforeSendRegisterCode.action", args)
        self.log("[查号] %s" % resp.get("message_"))
        return resp.get("result_"), resp.get("message_", "")

    def send_sms(self):
        # 543 变体才会真的往手机发短信；不带 543 的那个走聚合图形码，
        # 提交时永远对不上号。
        args = self._base_args("cam_iface46/gainMatrixCaptcha543.action",
                               action="registAndLogin", confirm=False,
                               mobile=self.phone, type="sms")
        resp = self._envelope("/cam_iface46/gainMatrixCaptcha543.action", args)
        self.log("[发短信] code_=%s %s" % (resp.get("code_"), resp.get("message_")))
        return bool(resp.get("result_")), resp.get("message_", "")

    def sms_login(self, code):
        # 参数集与两个参考实现逐字对齐：这里**不能**带 action / confirm，
        # 多了字段会被服务端当成注册流程去校验验证码，于是报“短信验证码错误”。
        args = self._base_args(
            "cam_iface46/registerUsersByTelAndLoginNew.action",
            mobile=self.phone, sms=str(code), type="2",
            wanxiaoVersion=self.version)
        resp = self._envelope("/cam_iface46/registerUsersByTelAndLoginNew.action", args)
        if resp.get("result_"):
            return True, self._token_from(resp)
        msg = resp.get("message_", "")
        self.log("[验证码登录] 失败 code_=%s %s" % (resp.get("code_"), msg))
        return False, msg

    def _token_from(self, resp):
        plain = resp.get("_plain") or {}
        u = (plain.get("user") or {}) if isinstance(plain, dict) else {}
        # CloudPayment 认的是**这次登录所用的握手 session**（抓包铁证：app 登录包
        # 信封里的 session=<本次握手 UUID> 随后原样出现在 pay.do?token=<同一个> 上），
        # 响应体里的 user.token 前 36 位是账号级固定值，拿去 SSO 会被拒。
        tok = self.session
        info = {
            "token": tok,
            "session": self.session,
            "user_token": str(u.get("token") or ""),
            "handshake_session_is_token": tok == self.session,
            "obtained_at": int(time.time()),
            "user": {k: u.get(k) for k in
                     ("id", "customId", "ecardCustomerid", "name", "realName",
                      "authStuNo", "mobileSm3") if u.get(k) is not None},
        }
        self.log("[登录成功] token(=握手session)=%s" % tok)
        if info["user"]:
            self.log("             %s" % json.dumps(info["user"], ensure_ascii=False))
        return info


# ------------------------------------------------------------- keystore ---
def load_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def load_creds():
    """账号密码优先读环境变量，其次读 config.json。"""
    phone = os.environ.get("WANXIAO_PHONE")
    pwd = os.environ.get("WANXIAO_PASSWORD")
    dev = os.environ.get("WANXIAO_DEVICEID")
    if not (phone and pwd):
        try:
            cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
            phone = phone or cfg.get("phone")
            pwd = pwd or cfg.get("password")
            dev = dev or cfg.get("deviceId")
        except Exception:
            pass
    return phone, pwd, dev


def build(phone=None, pwd=None):
    cfg_phone, cfg_pwd, cfg_dev = load_creds()
    phone = phone or cfg_phone
    pwd = pwd or cfg_pwd
    if not phone or not pwd:
        raise SystemExit("缺少账号密码：设 WANXIAO_PHONE / WANXIAO_PASSWORD "
                         "或写 %s" % CONFIG_PATH)
    st = load_state()
    dev = cfg_dev or st.get("deviceId")
    if not dev:
        dev = str(uuid.uuid4())
        st["deviceId"] = dev
        save_state(st)
        print("生成新的 deviceId（换机器请保留它）: %s" % dev)
    return Wanxiao(phone, pwd, dev), st


def fresh_token(max_age=5400, force=False):
    """采集器入口：拿一个尽量新的 token，必要时自动账密重登。"""
    wx, st = build()
    if not force:
        tok = st.get("token")
        if tok and time.time() - st.get("obtained_at", 0) < max_age:
            return tok
    ok, res = wx.password_login() if _handshake(wx, st) else (False, "HANDSHAKE_FAIL")
    if ok:
        st.update(res)
        save_state(st)
        return res["token"]
    if res == "NEW_DEVICE":
        raise SystemExit("deviceId %s 未绑定受信设备，请跑 sms-send + sms-code 绑一次。"
                         % wx.device_id)
    raise SystemExit("登录失败：%s" % res)


def _handshake(wx, st):
    try:
        wx.handshake()
        return True
    except Exception as e:
        print("握手失败:", e)
        return False


def _reuse_or_handshake(wx, st):
    """验证码是绑在发起那次握手 session 上的，能复用就别重新握手。"""
    pend = st.get("pending_sms") or {}
    if pend.get("session") and pend.get("app_key") \
            and time.time() - pend.get("at", 0) < 600:
        wx.session = pend["session"]
        wx.app_key = pend["app_key"]
        print("复用发短信时的 session: %s（%.0f 秒前）"
              % (wx.session, time.time() - pend["at"]))
        return True
    return _handshake(wx, st)


def main(argv):
    cmd = argv[0] if argv else "status"
    wx, st = build()

    if cmd == "status":
        age = time.time() - st.get("obtained_at", 0) if st.get("obtained_at") else None
        print("deviceId : %s" % wx.device_id)
        print("phone    : %s" % wx.phone)
        print("token    : %s" % st.get("token"))
        print("token 年龄: %s" % ("%.0f 分钟前" % (age / 60) if age else "无"))
        print("state    : %s" % STATE_PATH)
        return 0

    if cmd == "token":
        print(fresh_token(force=("--force" in argv)))
        return 0

    if cmd == "login":
        if not _handshake(wx, st):
            return 1
        ok, res = wx.password_login()
        print("=> %s %s" % ("成功" if ok else "失败",
                            json.dumps(res, ensure_ascii=False) if ok else res))
        if ok:
            st.update(res)
            save_state(st)
        return 0 if ok else 1

    if cmd == "sms-send":
        if not _handshake(wx, st):
            return 1
        wx.check_phone()
        ok, msg = wx.send_sms()
        if ok:
            st["pending_sms"] = {"session": wx.session,
                                 "app_key": wx.app_key, "at": int(time.time())}
            save_state(st)
            print("验证码已发往 %s；拿到后执行： python wanxiao_login.py sms-code <验证码>"
                  % wx.phone)
        return 0 if ok else 1

    if cmd == "sms-code":
        if len(argv) < 2:
            raise SystemExit("用法: python wanxiao_login.py sms-code <验证码>")
        if not _reuse_or_handshake(wx, st):
            return 1
        ok, res = wx.sms_login(argv[1])
        print("=> %s %s" % ("成功" if ok else "失败",
                            json.dumps(res, ensure_ascii=False) if ok else res))
        if ok:
            st.update(res)
            st.pop("pending_sms", None)
            save_state(st)
            print("\n现在测纯账密是否已免短信： python wanxiao_login.py login")
        return 0 if ok else 1

    if cmd == "sms":
        """一条命令搞定：发短信 -> 现场读验证码 -> 提交，全程同一个 session。"""
        if not _handshake(wx, st):
            return 1
        wx.check_phone()
        ok, msg = wx.send_sms()
        if not ok:
            return 1
        code = argv[1] if len(argv) > 1 else input("收到验证码: ").strip()
        ok, res = wx.sms_login(code)
        print("=> %s %s" % ("成功" if ok else "失败",
                            json.dumps(res, ensure_ascii=False) if ok else res))
        if ok:
            st.update(res)
            save_state(st)
        return 0 if ok else 1

    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
