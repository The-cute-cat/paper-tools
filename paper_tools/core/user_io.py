"""用户终端交互：输出文件覆盖确认。

html-translate / pdf-translate 在写出结果前共用 confirm_overwrite：
  * overwrite_mode=always —— 直接覆盖；
  * overwrite_mode=never  —— 跳过写入（保留现有文件）；
  * overwrite_mode=ask（默认）—— 终端询问。注意不用 isatty() 预判
    （PyCharm 等 IDE 运行终端 isatty()=False 但允许输入），直接尝试读取，
    真正读不到输入（CI 管道 / stdin 关闭）时退化为「跳过写入」。
"""

from pathlib import Path

from ..config import AppSettings, get_settings
from ..logging_setup import get_logger


def confirm_overwrite(path: str | Path, *, settings: AppSettings | None = None,
                      logger=None) -> bool:
    """目标文件已存在时按 overwrite_mode 决定是否覆盖。返回 True=允许写入。

    文件不存在时直接返回 True（无覆盖问题）。
    """
    settings = settings or get_settings()
    log = logger or get_logger()
    mode = (settings.overwrite_mode or "ask").strip().lower()

    if mode in ("always", "overwrite", "force", "yes"):
        return True
    target = Path(path)
    if not target.exists():
        return True
    if mode in ("never", "skip", "no"):
        log.warning(f"目标文件已存在且 overwrite_mode=never，跳过写入: {target}")
        return False

    # ask：终端询问。注意：不用 isatty() 预判——PyCharm 等 IDE 运行终端的
    # isatty() 返回 False 但实际允许输入。直接尝试读取；只有真正读不到
    # （stdin 关闭/EOF，如 CI 管道）时才退化为跳过。
    while True:
        try:
            ans = input(
                f"\n目标文件已存在：{target}\n"
                f"请选择：[y] 覆盖 / [n] 跳过该文件 / [q] 退出：").strip().lower()
        except (EOFError, OSError, RuntimeError):
            log.warning(
                f"目标文件已存在，但无法从终端读取输入（无交互终端），跳过覆盖: {target}\n"
                "  （如需强制覆盖，请设置 PAPER_TOOLS_OVERWRITE=always 或使用 --overwrite）")
            return False
        except KeyboardInterrupt:
            log.warning("读取用户输入被中断，跳过覆盖。")
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            log.info(f"已选择跳过写入，保留现有文件: {target}")
            return False
        if ans in ("q", "quit", "exit"):
            log.info("用户选择退出，未覆盖任何文件。")
            raise SystemExit(0)
        print("无效输入，请输入 y / n / q。")
