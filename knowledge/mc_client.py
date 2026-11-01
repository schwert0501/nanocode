#!/usr/bin/env python3
"""mc_client - 用 MinIO 官方命令行客户端 `mc` 上传对象，Python 侧零第三方依赖。

为什么走 mc 二进制而不是 minio / boto3 这些 pip 包：
- 项目铁律是解压即用、不装 pip 包。mc 是**单个静态二进制**，拿来就能跑，
  比「让用户 pip install minio」迁移性好得多（PEP 668 下 pip 装系统 Python 常被拒）。
- mc 自带 AWS SigV4 实现与重试逻辑，不需要我们手写签名。

关键实测结论（务必保留，否则会走回头路）：
- 百度 BOS 有两个端点，行为不同：
    bj.bcebos.com     -- 原生端点，只认 BOS 自家 bce-auth-v1 签名。
                         mc 用 SigV4 打它会直接 400 MissingDateHeader。
    s3.bj.bcebos.com  -- **S3 兼容端点**，SigV4 正常工作，mc 开箱可用。
  所以本模块会自动把 endpoint 加上 `s3.` 前缀（见 _s3_endpoint）。
  这也更正了 knowledge/bos.py 里「BOS 不兼容 S3 协议」的旧结论——
  那个结论只对原生端点成立。
- MinIO 官方下载站 https://dl.min.io/... 已返回 **HTTP 410 Gone**：
  开源 MinIO Server / mc / KES 项目已归档，不再从该站点分发构建。
  因此只能从 GitHub Release 取二进制（见 _download_url）。
  风险提示：mc 上游已归档，长期不再有安全更新；若哪天 GitHub Release 也下架，
  可退回 knowledge/bos.py 的纯标准库 bce-auth-v1 实现（gaps.py 已做多级降级）。

mc 定位顺序（三级，越靠前越省事）：
  1. 系统已装的 mc（PATH 中），用户自己装的优先，不重复下载。
  2. 本地缓存 ~/.nanocode/bin/mc-<os>-<arch>（上次自动下载的）。
  3. 按当前平台自动下载到缓存目录（需显式允许，见 NANOCODE_MC_AUTO_DOWNLOAD）。

平台支持：仅 macOS 与 Linux（amd64 / arm64），按用户要求不做 Windows。
不支持的平台返回 None，调用方据此降级到 bos.py。

凭据传递用 `MC_HOST_<alias>` 环境变量而非写 ~/.mc/config.json：
不污染用户全局配置，进程退出即消失，AK/SK 不落盘。
"""

import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request

from knowledge import bos

# ---------------------------------------------------------------- 版本与校验

# 固定版本而非每次查 latest：可复现、可校验 sha256、不受上游打新 tag 影响。
# 升级时同步更新下面的 _SHA256 表（取自 Release 里的 .sha256sum 文件）。
MC_RELEASE = "RELEASE.2025-08-13T08-35-41Z"

# <os>-<arch> -> 二进制 sha256。下载后强校验，防镜像投毒 / 半截文件。
_SHA256 = {
    "darwin-amd64": "2862c79cce11b09be9a8911a279b2e9465bebf74b9f01abca9c348a0d795f0cb",
    "darwin-arm64": "a877fd0c183409da9f20f9d6e1811987298bbbca1aa03428eebdffba79fb9445",
    "linux-amd64": "01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891",
    "linux-arm64": "14c8c9616cfce4636add161304353244e8de383b2e2752c0e9dad01d4c27c12c",
}

# 缓存目录：放用户家目录而非项目内，多个 nanocode 副本可共用同一份二进制。
CACHE_DIR = os.path.expanduser(os.environ.get("NANOCODE_BIN_DIR") or "~/.nanocode/bin")

# 自动下载默认开启；设 NANOCODE_MC_AUTO_DOWNLOAD=0 可禁止联网（离线/受控环境）。
_AUTO_DOWNLOAD = (os.environ.get("NANOCODE_MC_AUTO_DOWNLOAD") or "1").strip() not in (
    "0",
    "false",
    "no",
)

_ALIAS = "nanocode-obj"     # mc 内部别名，仅本进程可见
_DL_TIMEOUT = 120           # 二进制约 30MB，给宽松超时
_DL_RETRY = 3               # GitHub 下载偶发 HTTP2 framing / empty reply，必须重试
_RUN_TIMEOUT = 30           # 单次上传超时


# ---------------------------------------------------------------- 平台判定

def _platform_tag():
    """返回 "<os>-<arch>"（如 darwin-arm64），不支持的平台返回 None。

    machine() 的叫法各家不一（arm64/aarch64、x86_64/amd64），统一归一化。
    """
    system = platform.system().lower()
    if system not in ("darwin", "linux"):
        return None
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch = "amd64"
    else:
        return None
    tag = "%s-%s" % (system, arch)
    return tag if tag in _SHA256 else None


def _download_url(tag):
    """GitHub Release 的资产 URL。官方 dl.min.io 已 410，只能走这里。"""
    return "https://github.com/minio/mc/releases/download/%s/mc.%s.%s" % (
        MC_RELEASE,
        tag,
        MC_RELEASE,
    )


# ---------------------------------------------------------------- mc 定位

def _cached_path(tag):
    return os.path.join(CACHE_DIR, "mc-%s" % tag)


def _is_runnable(path):
    """能执行且 `mc --version` 正常退出，才算一个可用的 mc"""
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _sha256_file(path):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(tag):
    """下载对应平台的 mc 到缓存目录并校验 sha256。返回 (path, None) 或 (None, 原因)。

    先写临时文件再原子 rename：避免下载中断留下半截的"可执行"文件，
    也避免两个 nanocode 进程同时下载时互相写坏。
    """
    url = _download_url(tag)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError as err:
        return None, "无法创建缓存目录 %s: %s" % (CACHE_DIR, err)

    last = ""
    for _ in range(_DL_RETRY):
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".mc-dl-", dir=CACHE_DIR)
            req = urllib.request.Request(url, headers={"User-Agent": "nanocode"})
            with urllib.request.urlopen(req, timeout=_DL_TIMEOUT) as resp, os.fdopen(
                fd, "wb"
            ) as out:
                shutil.copyfileobj(resp, out, length=1 << 20)
            got = _sha256_file(tmp_path)
            if got != _SHA256[tag]:
                last = "sha256 校验失败（期望 %s… 实际 %s…）" % (
                    _SHA256[tag][:12],
                    got[:12],
                )
                os.unlink(tmp_path)
                continue
            os.chmod(tmp_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
            target = _cached_path(tag)
            os.replace(tmp_path, target)
            return target, None
        except Exception as err:
            last = "%s: %s" % (type(err).__name__, err)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    return None, "下载 mc 失败（%s）：%s" % (url, last)


def locate_mc(auto_download=None):
    """找到一个可用的 mc。返回 (path, None) 或 (None, 原因)。

    顺序：显式指定 > 系统 PATH > 本地缓存 > 自动下载。
    """
    explicit = os.environ.get("NANOCODE_MC_PATH")
    if explicit:
        if _is_runnable(explicit):
            return explicit, None
        return None, "NANOCODE_MC_PATH=%s 不可执行" % explicit

    system_mc = shutil.which("mc")
    if system_mc and _is_runnable(system_mc):
        return system_mc, None

    tag = _platform_tag()
    if not tag:
        return None, "当前平台不支持（仅 macOS / Linux 的 amd64 / arm64）：%s %s" % (
            platform.system(),
            platform.machine(),
        )

    cached = _cached_path(tag)
    if _is_runnable(cached):
        return cached, None

    if auto_download is None:
        auto_download = _AUTO_DOWNLOAD
    if not auto_download:
        if _AUTO_DOWNLOAD:
            # 探测模式（status / is_configured）不联网，但真上传时会下载
            return None, "未找到 mc，将在首次上传时自动下载（约 30MB）"
        return None, "未找到 mc，且自动下载已禁用（NANOCODE_MC_AUTO_DOWNLOAD=0）"
    return _download(tag)


# ---------------------------------------------------------------- 配置

def _s3_endpoint(conf):
    """把配置里的 endpoint 换成 S3 兼容端点。

    BOS 的原生端点 bj.bcebos.com 不认 SigV4，必须用 s3.bj.bcebos.com。
    用户配置里一般写的是原生端点，这里自动补 `s3.` 前缀；
    已经带 s3. 的、或用 BOS_S3_ENDPOINT 显式指定的，原样使用。
    """
    override = os.environ.get("BOS_S3_ENDPOINT")
    if override:
        return override.strip()
    endpoint = (conf.get("endpoint") or "bj.bcebos.com").strip()
    endpoint = endpoint.split("://", 1)[-1].strip("/")
    if endpoint.startswith("s3."):
        return endpoint
    return "s3." + endpoint


def load_config():
    """复用 bos.py 的配置来源（环境变量 > .nanocode_bos.json），避免两套配置。"""
    return bos.load_config()


def is_configured(conf=None):
    """凭据齐备 **且** mc 可用（已装/已缓存，或允许自动下载），才算这条通道可用。

    这里不真的触发下载（locate_mc(auto_download=False)）：仅作可用性探测时
    不该偷偷联网。真正上传时 put_object 会按需下载。
    """
    conf = conf or load_config()
    if not bos.is_configured(conf):
        return False
    path, _ = locate_mc(auto_download=False)
    if path:
        return True
    # 还没下载，但平台支持且允许联网 —— 首次上传时会自动补齐
    return bool(_AUTO_DOWNLOAD and _platform_tag())


# ---------------------------------------------------------------- 上传

def _env_for(conf, config_dir):
    """构造 mc 子进程的环境变量。

    AK/SK 要塞进 URL 的 userinfo 部分，含 @ : / 等字符必须先 URL 编码，
    否则 mc 会把 URL 解析错（本项目当前凭据无特殊字符，但不能赌）。
    """
    env = dict(os.environ)
    ak = urllib.parse.quote(conf["access_key"], safe="")
    sk = urllib.parse.quote(conf["secret_key"], safe="")
    env["MC_HOST_%s" % _ALIAS] = "https://%s:%s@%s" % (ak, sk, _s3_endpoint(conf))
    # 隔离配置目录：不读写用户的 ~/.mc/，也不留下 alias 记录
    env["MC_CONFIG_DIR"] = config_dir
    return env


def put_object(key, data, content_type="application/json", conf=None):
    """用 mc 上传一个对象。返回 (ok: bool, detail: str)，不抛异常。

    参数与 bos.put_object 保持一致，方便两条通道互为降级。

    走 `mc pipe` 从 stdin 直传，不落临时文件——缺口记录是内存里的 JSON，
    写磁盘只会多一次凭据/内容落盘风险。
    注意 content_type 对 `mc pipe` 无效（mc 不提供设置入口），这里只做签名兼容，
    BOS 侧会按扩展名推断；缺口对象都是 .json，实际影响为零。
    """
    conf = conf or load_config()
    if not bos.is_configured(conf):
        return False, "BOS 未配置（缺 access_key / secret_key / bucket）"

    mc_path, err = locate_mc()
    if not mc_path:
        return False, err or "未找到 mc"

    if isinstance(data, str):
        data = data.encode("utf-8")

    target = "%s/%s/%s" % (_ALIAS, conf["bucket"], key.strip("/"))
    config_dir = None
    try:
        # 每次用一个临时 config 目录，用完即删，杜绝凭据残留
        config_dir = tempfile.mkdtemp(prefix="nanocode-mc-")
        proc = subprocess.run(
            [mc_path, "--no-color", "pipe", target],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env_for(conf, config_dir),
            timeout=_RUN_TIMEOUT,
        )
        if proc.returncode == 0:
            # 成败只看 exit code：mc 的 stdout 含进度条/表格，解析文本不可靠
            return True, "mc pipe -> %s/%s" % (_s3_endpoint(conf), target.split("/", 1)[1])
        message = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        return False, "mc 退出码 %d: %s" % (proc.returncode, message[:300])
    except subprocess.TimeoutExpired:
        return False, "mc 上传超时（%ds）" % _RUN_TIMEOUT
    except Exception as err:
        return False, "%s: %s" % (type(err).__name__, err)
    finally:
        if config_dir:
            shutil.rmtree(config_dir, ignore_errors=True)


# ---------------------------------------------------------------- 诊断

def status():
    """供 /gaps 等命令展示的通道状态，不触发下载。"""
    conf = load_config()
    path, err = locate_mc(auto_download=False)
    if not path:
        source = ""
    elif os.path.realpath(path).startswith(os.path.realpath(CACHE_DIR) + os.sep):
        source = "缓存 %s" % path
    elif path == (os.environ.get("NANOCODE_MC_PATH") or "").strip():
        source = "NANOCODE_MC_PATH %s" % path
    elif path == shutil.which("mc"):
        source = "系统 PATH"
    else:
        source = path
    return {
        "platform": _platform_tag() or "unsupported",
        "mc_path": path or "",
        "mc_source": source,
        "mc_reason": "" if path else (err or ""),
        "release": MC_RELEASE,
        "auto_download": _AUTO_DOWNLOAD,
        "endpoint": _s3_endpoint(conf),
        "bucket": conf["bucket"] if bos.is_configured(conf) else "",
        "ready": is_configured(conf),
    }


if __name__ == "__main__":
    # 自检：python3 -m knowledge.mc_client
    print(json.dumps(status(), ensure_ascii=False, indent=2))
