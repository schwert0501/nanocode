#!/usr/bin/env python3
"""bos - 百度对象存储（BOS）上传客户端，纯标准库实现。

为什么不用 minio / boto3：
- 项目铁律是零第三方依赖（解压即用）。
- 更关键的是实测结论：BOS 的 bj.bcebos.com 端点**不接受 AWS SigV4**
  （直接 400 MissingDateHeader），minio / boto3 这类 S3 客户端签不出它要的签名。
  BOS 用的是自家 `bce-auth-v1` 签名协议。
- 该协议只需 hmac-sha256 + urllib，标准库足够，故在此手写。

签名协议（BCE Authorization V1，已实测验证）：
  authStringPrefix = "bce-auth-v1/{ak}/{utc_iso8601}/{expire_seconds}"
  signingKey       = HMAC-SHA256(sk, authStringPrefix)            -> hex
  canonicalRequest = METHOD \n CanonicalURI \n CanonicalQuery \n CanonicalHeaders
  signature        = HMAC-SHA256(signingKey, canonicalRequest)    -> hex
  Authorization    = "{authStringPrefix}/{signedHeaders}/{signature}"

配置来源（优先级：环境变量 > 项目根 .nanocode_bos.json）：
  BOS_ACCESS_KEY / BOS_SECRET_KEY / BOS_BUCKET / BOS_ENDPOINT
JSON 配置文件示例（.nanocode_bos.json，已在 .gitignore 中，不会入库）：
  {"access_key": "...", "secret_key": "...",
   "bucket": "linshi-bucket", "endpoint": "bj.bcebos.com"}
"""

import datetime
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_BASE_DIR, ".nanocode_bos.json")

# 签名有效期（秒）。BOS 会校验 x-bce-date 与服务端时间的偏差。
_EXPIRE = 1800
_TIMEOUT = 10


def _quote(text):
    """BCE 的 uri-encode：除 A-Za-z0-9-_.~ 外全部转义（含 '/'）"""
    return urllib.parse.quote(str(text), safe="-_.~")


def load_config():
    """读取 BOS 配置。返回 dict，缺字段则值为空串。

    环境变量优先于配置文件，便于 CI / 临时覆盖。
    """
    conf = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                conf = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            conf = {}
    return {
        "access_key": os.environ.get("BOS_ACCESS_KEY") or conf.get("access_key") or "",
        "secret_key": os.environ.get("BOS_SECRET_KEY") or conf.get("secret_key") or "",
        "bucket": os.environ.get("BOS_BUCKET") or conf.get("bucket") or "",
        "endpoint": os.environ.get("BOS_ENDPOINT") or conf.get("endpoint") or "bj.bcebos.com",
    }


def is_configured(conf=None):
    """是否具备上传所需的全部凭据"""
    conf = conf or load_config()
    return bool(conf["access_key"] and conf["secret_key"] and conf["bucket"])


def _sign(method, uri, headers, access_key, secret_key, timestamp):
    """按 bce-auth-v1 协议算出 Authorization 头的值"""
    prefix = "bce-auth-v1/%s/%s/%d" % (access_key, timestamp, _EXPIRE)
    signing_key = hmac.new(
        secret_key.encode("utf-8"), prefix.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    # CanonicalHeaders：按「编码后的小写头名」排序，k:v 各自 uri-encode，换行连接
    items = sorted((k.lower(), str(v).strip()) for k, v in headers.items())
    canonical_headers = "\n".join(
        "%s:%s" % (_quote(k), _quote(v)) for k, v in items
    )
    signed_headers = ";".join(k for k, _ in items)
    # CanonicalQuery 为空（本模块只做无 query 的 PUT object）
    canonical_request = "%s\n%s\n\n%s" % (method, uri, canonical_headers)
    signature = hmac.new(
        signing_key.encode("utf-8"),
        canonical_request.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "%s/%s/%s" % (prefix, signed_headers, signature)


def put_object(key, data, content_type="application/json", conf=None):
    """上传一个对象到 BOS。

    参数：
      key          -- 对象键（如 "problem/2026-01-01/gap-0.json"），不以 / 开头
      data         -- bytes 或 str（str 按 utf-8 编码）
      content_type -- MIME 类型
      conf         -- 可选，load_config() 的结果；省略则现场读取

    返回 (ok: bool, detail: str)。不抛异常，便于调用方 best-effort 使用。
    """
    conf = conf or load_config()
    if not is_configured(conf):
        return False, "BOS 未配置（缺 access_key / secret_key / bucket）"
    if isinstance(data, str):
        data = data.encode("utf-8")

    host = "%s.%s" % (conf["bucket"], conf["endpoint"])
    # CanonicalURI：逐段 uri-encode 后用 / 连接（'/' 本身不被转义）
    uri = "/" + "/".join(_quote(seg) for seg in key.strip("/").split("/"))
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    # 参与签名的头：host 必须；其余按实际发送的头如实填写，否则验签失败
    sign_headers = {
        "host": host,
        "x-bce-date": timestamp,
        "content-type": content_type,
        "content-length": str(len(data)),
    }
    authorization = _sign(
        "PUT", uri, sign_headers, conf["access_key"], conf["secret_key"], timestamp
    )
    # 实际请求头去掉 host（urllib 依据 URL 自动设置，重复设置反而可能不一致）
    send_headers = {k: v for k, v in sign_headers.items() if k != "host"}
    send_headers["Authorization"] = authorization

    url = "https://%s%s" % (host, uri)
    request = urllib.request.Request(url, data=data, method="PUT", headers=send_headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
            return True, "HTTP %d %s" % (resp.status, url)
    except urllib.error.HTTPError as err:
        body = ""
        try:
            body = err.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return False, "HTTP %d %s" % (err.code, body)
    except Exception as err:
        return False, "%s: %s" % (type(err).__name__, err)
