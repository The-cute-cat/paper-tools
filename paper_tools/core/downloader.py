"""通用下载工具：下载网页与资源文件。"""
import time
from pathlib import Path
from typing import Optional

import requests

from ..config import get_settings
from ..logging_setup import get_logger

logger = get_logger()


def _download_proxies() -> dict[str, str] | None:
    """按配置构造 requests 的 proxies 参数；未配置代理时返回 None。

    - 显式配置了 download_proxy（或环境变量 PAPER_TOOLS_PROXY）则统一用作
      http/https 代理。
    - 否则返回 None，由 requests 自行读取 HTTP_PROXY/HTTPS_PROXY 环境变量
      （requests 默认行为，保留对系统代理的兼容）。
    部分网络对 arxiv.org 等在 TLS 握手阶段直接 RST，必须走代理/VPN 才能下载。
    """
    cfg = get_settings()
    proxy = (cfg.download_proxy or "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _cors_proxy_base() -> str | None:
    """返回 CORS 改写代理的基础 URL（含 ?url=），未配置返回 None。

    该代理为「URL 改写 / CORS 转发」类型（典型如部署在 Cloudflare Workers 上的
    轻量代理，形如 https://xxx.workers.dev/?url=<目标>）：把目标 URL 拼到 ?url=
    之后直接请求代理域名，由代理服务端代为 fetch。它**不经过** requests 的 CONNECT
    隧道，因此适用于不支持 CONNECT 隧道、但有服务侧出网能力的轻量代理。
    """
    cfg = get_settings()
    proxy = (cfg.cors_proxy or "").strip()
    if not proxy:
        return None
    lowered = proxy.lower()
    if "?url=" in lowered:
        return proxy if proxy.endswith("=") else proxy + "="
    if lowered.endswith("?"):
        return proxy + "url="
    return proxy.rstrip("/") + "/?url="


def _text_request(url: str) -> tuple[str, dict | None]:
    """构造文本下载请求：(实际请求URL, requests 的 proxies 参数)。

    - 配置了 cors_proxy（CORS 改写代理）→ 改写 URL、不走 CONNECT 隧道。
    - 否则 → 走标准 download_proxy（可能为空 = 直连）。
    """
    cors_base = _cors_proxy_base()
    if cors_base:
        return cors_base + url, None
    return url, _download_proxies()


def _binary_request(url: str) -> tuple[str, dict | None]:
    """构造二进制下载请求：(实际请求URL, requests 的 proxies 参数)。

    二进制（图片等）下载统一走标准 CONNECT 代理 download_proxy；
    CORS 改写代理通常不支持文件下载，故不用于二进制。
    """
    return url, _download_proxies()


def download_text(url: str) -> str:
    """下载文本（如 HTML）并返回内容。

    自带重试机制以抵抗网络波动（如 arxiv 偶发的连接重置 WinError 10054）：
    失败后按指数退避（1s, 2s, 4s ...）重试，默认次数取自配置 download_max_retries。
    """
    cfg = get_settings()
    fetch_url, proxies = _text_request(url)
    last_err: Exception | None = None
    retries = cfg.download_max_retries
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                fetch_url, timeout=cfg.download_timeout, headers=cfg.download_headers,
                proxies=proxies,
            )
            resp.raise_for_status()
            # arxiv HTML 通常是 UTF-8
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < retries:
            backoff = min(2 ** attempt, 30)  # 指数退避，封顶 30s
            logger.warning(
                f"  文本下载异常 ({last_err})，第 {attempt + 1}/{retries + 1} 次尝试后重试 {url}（{backoff}s）"
            )
            _safe_sleep(backoff)
    # 连接被重置 / TLS 中途 EOF 通常是网络层对目标域名的封锁（如 SNI 重置），
    # 此时仅靠伪装请求头无法绕过，需走代理/VPN。给出明确提示，避免反复重试无效。
    hint = ""
    err_str = str(last_err)
    if "ConnectionReset" in err_str or "UNEXPECTED_EOF" in err_str or "SSLError" in err_str \
            or "10054" in err_str:
        if not ((cfg.download_proxy or "").strip() or (cfg.cors_proxy or "").strip()):
            hint = ("\n  ⚠ 检测到连接被网络层重置/TLS 中断（常见于对 arxiv.org 的封锁）。"
                    " 伪装请求头无法绕过，请配置代理后重试：\n"
                    "    • 标准 CONNECT 代理（图片下载）：在 .env 设置 PAPER_TOOLS_PROXY=http://127.0.0.1:7890\n"
                    "    • 轻量 CORS 改写代理（文本/HTML 下载）：在 .env 设置 "
                    "PAPER_TOOLS_CORS_PROXY=https://你的worker.workers.dev/?url=\n"
                    "  若使用 TUN/全局模式 VPN，请确认其已实际接管 arxiv.org 的流量。")
    raise RuntimeError(f"文本下载最终失败: {url} -> {last_err}{hint}")


def _safe_sleep(seconds: float) -> None:
    try:
        time.sleep(seconds)
    except Exception:  # noqa: BLE001
        pass


def download_binary(url: str, dest: Path, *, max_retries: Optional[int] = None) -> bool:
    """下载二进制资源到 dest，成功返回 True。

    自带重试机制以抵抗网络波动：失败后按指数退避（1s, 2s, 4s ...）重试，
    默认重试次数取自配置 download_max_retries。
    仅对可重试的错误重试（连接/超时/5xx），4xx 视为不可达直接放弃。
    """
    cfg = get_settings()
    retries = max_retries if max_retries is not None else cfg.download_max_retries
    fetch_url, proxies = _binary_request(url)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                fetch_url, timeout=cfg.download_timeout, headers=cfg.download_headers,
                stream=True, proxies=proxies,
            )
            if resp.status_code == 200:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                return True
            # 4xx（除 429 限流）视为不可达，不再重试
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                logger.warning(f"  下载失败 ({resp.status_code} 不可达): {url}")
                return False
            last_err = RuntimeError(f"HTTP {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            last_err = e
        # 还有重试机会
        if attempt < retries:
            backoff = min(2 ** attempt, 30)  # 指数退避，封顶 30s
            logger.warning(
                f"  下载异常 ({last_err})，第 {attempt + 1}/{retries + 1} 次尝试后重试 {url}（{backoff}s）"
            )
            _safe_sleep(backoff)
    logger.warning(f"  下载最终失败: {url} -> {last_err}")
    return False
