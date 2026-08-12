"""通用下载工具：下载网页与资源文件。"""
import time
from pathlib import Path
from typing import Optional

import requests

from ..config import get_settings
from ..logging_setup import get_logger

logger = get_logger()


def download_text(url: str) -> str:
    """下载文本（如 HTML）并返回内容。"""
    cfg = get_settings()
    resp = requests.get(url, timeout=cfg.download_timeout, headers=cfg.download_headers)
    resp.raise_for_status()
    # arxiv HTML 通常是 UTF-8
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


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
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url, timeout=cfg.download_timeout, headers=cfg.download_headers, stream=True
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
