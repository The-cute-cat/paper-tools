"""统一的日志配置。控制台带时间戳与级别，支持 UTF-8 输出。"""
import logging
import sys
from typing import Optional


class _TqdmHandler(logging.Handler):
    """把 logging 输出通过 tqdm.write 路由，避免与进度条相互打断。

    tqdm 进度条用 '\\r' 在同一行原地刷新；若 logging 直接 stream.write，
    会把 INFO 行插进进度条行中间，破坏渲染。tqdm.write 会先清掉当前进度
    行、打印消息、再重绘进度条，从而彻底消除交错。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from tqdm import tqdm

            msg = self.format(record)
            tqdm.write(msg)
        except Exception:  # pragma: no cover - 极端情况下退回原始 stderr
            sys.stderr.write(self.format(record) + "\n")


def setup_logging(level: str = "INFO") -> logging.Logger:
    """配置全局 logging 并返回名为 'paper-tools' 的 logger。

    在 Windows 上会把 stdout 强制设为 UTF-8 以避免中文乱码。

    同时把外部库（urllib3 / openai / httpcore）的 INFO 噪音降到 WARNING，
    这些库默认每条 HTTP 请求都会 INFO 一行，会把 tqdm 进度条冲成乱码。

    logging 通过 _TqdmHandler 走 tqdm.write，与 tqdm 进度条（同 stdout 流）
    互不抢占；这样 IDE Run 面板里不会保留"凭空多出的初始帧"残影。
    """
    enc = sys.stdout.encoding
    if enc and "utf-8" not in enc.lower().replace("_", "-"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    handler = _TqdmHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 清掉 basicConfig 可能注入的默认 handler，避免重复打印
    root.handlers = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)]
    root.addHandler(handler)
    root.propagate = False

    # 抑制第三方库的 INFO 噪音（保留 WARNING 及以上：超时/失败/重试仍会显示）
    for noisy in ("urllib3", "openai", "openai._base", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("paper-tools")


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(name or "paper-tools")
