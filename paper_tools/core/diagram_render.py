"""把「图即代码」的围栏代码块渲染为 PNG（供 Word / PDF 内嵌）。

覆盖范围刻意收窄为**主流 Markdown 编辑器（Typora 等）会原生渲染**的三种围栏，
因为只有这些代码块在原文里「本应是图」——导出成 Word/PDF 时若原样输出源码，
就是明显的错误（其余语言编辑器根本不渲染，按普通代码块处理才对）：

* ```` ```mermaid ````  —— Mermaid（Typora / Obsidian / GitHub / GitLab / Notion）
* ```` ```flow ````     —— flowchart.js（Typora / Mark Text）
* ```` ```sequence ```` —— js-sequence-diagrams（Typora / Mark Text）

后两者没有可用的在线渲染服务，且语法分别源自 Mermaid / PlantUML 系，
因此这里做**尽力而为的机械转译**成 Mermaid，再复用同一套渲染后端：

* local  —— 本地 mermaid-cli（``mmdc``）：完全离线、不外传内容、质量最好。
* online —— 在线渲染服务（默认 mermaid.ink）：零本地依赖，但**会把图表源码
             发送到第三方服务器**；涉密/内网文档请改用 local 或 off。

渲染结果按内容哈希缓存到 ``out_dir``，docx / pdf 两轮渲染只算一次；
转译失败、渲染失败等任何异常都返回 None，调用方退化为「按普通代码块输出」。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import zlib
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger

logger = get_logger()

# 支持渲染的围栏信息串（小写）→ 内部类别
DIAGRAM_LANGS = {
    "mermaid": "mermaid", "mmd": "mermaid",
    "flow": "flow",
    "sequence": "sequence",
}

_DEFAULT_ENDPOINT = "https://mermaid.ink"
_DEFAULT_WIDTH = 1600          # 在线渲染默认输出宽度（像素），保证打印清晰
_LOCAL_TIMEOUT = 180           # mmdc 首次经 npx 下载依赖时可能很慢
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def diagram_kind(lang: str) -> str:
    """围栏信息串 → 内部类别（mermaid/flow/sequence）；不支持则返回空串。"""
    return DIAGRAM_LANGS.get((lang or "").strip().lower(), "")


# ──────────────────────────────────────────────
# 语块转译：flowchart.js / js-sequence-diagrams → Mermaid
# ──────────────────────────────────────────────

# flowchart.js 节点类型 → Mermaid 形状（文案统一用双引号包裹，避免括号/引号破坏语法）
_FLOW_SHAPE = {
    "start": '(["{}"])',
    "end": '(["{}"])',
    "operation": '["{}"]',
    "subroutine": '[["{}"]]',
    "condition": '{{"{}"}}',
    "inputoutput": '[/"{}"/]',
    "input": '[/"{}"/]',
    "output": '[/"{}"/]',
    "parallel": '[["{}"]]',
}
_FLOW_DECL_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*=>\s*([A-Za-z]+)\s*:\s*(.*)$")
_FLOW_DIR_WORDS = {"right", "left", "top", "bottom", "up", "down"}
_FLOW_TRAIL_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _flow_label(text: str) -> str:
    """节点/边文案归一：``\\n`` 换行 → ``<br/>``，双引号降级为单引号。"""
    return (text.replace("\\n", "<br/>").replace("\r", "")
            .replace('"', "'").strip())


def _flow_edge(line: str) -> str:
    """``a->b->c`` / ``cond(yes)->e`` → ``a --> b -->|yes| e``。

    注意 flowchart.js 的边标签挂在**左侧**节点上（``cond(yes)->io`` 表示
    「cond 经 yes 到 io」），而括号里也可能是 ``(right)`` 这类方向提示，
    按关键词区分。
    """
    line = line.replace("@>", "->")
    line = re.sub(r"\(\{[^}]*\}\)", "", line)        # 去掉 @> 后的样式参数
    parts = [p for p in (p.strip() for p in line.split("->")) if p]
    if len(parts) < 2:
        return line
    refs: list[tuple[str, str]] = []
    for part in parts:
        label = ""
        m = _FLOW_TRAIL_PAREN_RE.search(part)
        if m:
            label = m.group(0).strip().strip("()").strip()
            part = part[:m.start()].strip()
        refs.append((part, label))
    out = refs[0][0]
    for i in range(1, len(refs)):
        node = refs[i][0]
        label = refs[i - 1][1]           # 边标签来自左侧节点
        if label and label.lower() not in _FLOW_DIR_WORDS:
            out += f" -->|{_flow_label(label)}| {node}"
        else:
            out += f" --> {node}"
    return out


def _flow_to_mermaid(code: str) -> str:
    """flowchart.js → Mermaid flowchart（机械翻译，尽力而为）。"""
    nodes: list[str] = []
    edges: list[str] = []
    for raw in code.splitlines():
        s = raw.strip()
        if not s or s.startswith(("//", "%%", "#")):
            continue
        m = _FLOW_DECL_RE.match(s)
        if m:
            nid, ntype, text = m.group(1), m.group(2).lower(), m.group(3)
            shape = _FLOW_SHAPE.get(ntype, '["{}"]')
            nodes.append(f"    {nid}{shape.format(_flow_label(text))}")
        elif "->" in s:
            edges.append("    " + _flow_edge(s))
    if not nodes and not edges:
        return code
    return "flowchart TD\n" + "\n".join(nodes + edges)


# js-sequence-diagrams 的箭头 → Mermaid 箭头（长模式排在前面，避免被短模式抢先匹配）
_SEQ_ARROW_RE = re.compile(r"-->>|--x|->>|-->|->|-x")
_SEQ_ARROW_MAP = {"->": "->>", "-->": "-->>", "->>": "->>", "-->>": "-->>",
                  "-x": "-x", "--x": "--x"}
_SEQ_TITLE_RE = re.compile(r"^title\s*:\s*(.*)$", re.I)


def _sequence_to_mermaid(code: str) -> str:
    """js-sequence-diagrams → Mermaid sequenceDiagram（机械翻译，尽力而为）。"""
    out: list[str] = []
    for raw in code.splitlines():
        s = raw.strip()
        if not s or s.startswith(("//", "%%", "#")):
            continue
        m = _SEQ_TITLE_RE.match(s)
        if m:
            out.append(f"    title {m.group(1).strip()}")
            continue
        out.append("    " + _SEQ_ARROW_RE.sub(
            lambda mm: _SEQ_ARROW_MAP[mm.group(0)], s))
    if not out:
        return code
    return "sequenceDiagram\n" + "\n".join(out)


def _to_mermaid(kind: str, code: str) -> str:
    if kind == "flow":
        return _flow_to_mermaid(code)
    if kind == "sequence":
        return _sequence_to_mermaid(code)
    return code


# ──────────────────────────────────────────────
# 后端选择
# ──────────────────────────────────────────────

def _endpoint(settings) -> str:
    ep = str(getattr(settings, "md_mermaid_endpoint", "") or "").strip()
    return (ep or _DEFAULT_ENDPOINT).rstrip("/")


def _pick_backend(renderer: str, settings) -> tuple[str, str] | None:
    """返回 (backend, target)；backend ∈ online/local，target 为地址或 mmdc 路径。

    renderer=auto 时优先本地 mmdc（PATH 上有才用，避免 npx 悄悄下载依赖），
    否则走在线服务。
    """
    mmdc = str(getattr(settings, "md_mermaid_mmdc", "") or "").strip()
    if renderer in ("online", "web", "remote", "api"):
        return "online", _endpoint(settings)
    if renderer in ("local", "mmdc", "cli"):
        return "local", mmdc
    # auto
    if mmdc or shutil.which("mmdc"):
        return "local", mmdc
    return "online", _endpoint(settings)


# ──────────────────────────────────────────────
# 在线渲染
# ──────────────────────────────────────────────

def _mermaid_ink_url(endpoint: str, code: str, theme: str, width: int) -> str:
    """mermaid.ink 的 ``/img/pako:<b64>`` 形式（deflate 压缩，显著缩短 URL）。"""
    payload: dict = {"code": code}
    if theme:
        payload["mermaid"] = {"theme": theme}
    comp = zlib.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"), 9)
    b64 = base64.urlsafe_b64encode(comp).decode("ascii")
    base = endpoint if endpoint.endswith("/img") else endpoint + "/img"
    url = f"{base}/pako:{b64}?type=png&bgColor=FFFFFF"
    if width > 0:
        url += f"&width={width}"
    return url


def _proxy_map(settings) -> dict | None:
    proxy = (getattr(settings, "download_proxy", "") or "").strip()
    return {"http": proxy, "https": proxy} if proxy else None


def _save_png(resp, dest: Path) -> bool:
    """把响应体落盘为 PNG；服务返回 jpeg/webp 时用 Pillow 转 PNG。"""
    data = resp.content
    if data.startswith(_PNG_MAGIC):
        dest.write_bytes(data)
        return True
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype.startswith("image/"):
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                im.convert("RGBA" if "A" in im.mode or im.mode == "P" else "RGB") \
                  .save(dest, "PNG")
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def _online_render(endpoint: str, code: str, theme: str, width: int,
                   dest: Path, settings) -> bool:
    import time

    import requests

    if "kroki" in endpoint.lower():
        url = endpoint if endpoint.endswith("/png") else endpoint + "/mermaid/png"
        method, headers, data = "POST", {"Content-Type": "text/plain"}, code.encode("utf-8")
    else:
        url = _mermaid_ink_url(endpoint, code, theme, width)
        method, headers, data = "GET", None, None
    if len(url) > 8000:
        logger.warning(f"  图表渲染 URL 过长（{len(url)} 字符），"
                       "在线服务可能拒绝，图表可能较大")

    proxies = _proxy_map(settings)
    retries = int(getattr(settings, "download_max_retries", 3) or 0)
    timeout = int(getattr(settings, "download_timeout", 60) or 60)
    last: object = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, timeout=timeout,
                                    proxies=proxies, headers=headers, data=data)
            if resp.status_code == 200:
                if _save_png(resp, dest):
                    return True
                body = resp.text[:120].replace("\n", " ")
                logger.warning(f"  在线图表渲染返回了非图片内容: {body!r}")
                return False
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                body = resp.text[:160].replace("\n", " ")
                logger.warning(f"  图表渲染失败 (HTTP {resp.status_code}): {body}")
                return False
            last = f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < retries:
            backoff = min(2 ** attempt, 30)
            logger.warning(f"  图表渲染异常 ({last})，"
                           f"第 {attempt + 1}/{retries + 1} 次尝试后重试（{backoff}s）")
            time.sleep(backoff)
    logger.warning(f"  在线图表渲染失败: {last}")
    return False


# ──────────────────────────────────────────────
# 本地渲染（mermaid-cli）
# ──────────────────────────────────────────────

def _mmdc_command(mmdc: str) -> list[str] | None:
    """构造 mmdc 调用命令；找不到 mmdc 时退回 npx。"""
    if mmdc:
        return [mmdc]
    exe = shutil.which("mmdc")
    if exe:
        return [exe]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "@mermaid-js/mermaid-cli"]
    return None


def _local_render(code: str, theme: str, scale: float, dest: Path,
                  mmdc: str) -> bool:
    cmd = _mmdc_command(mmdc)
    if cmd is None:
        logger.warning("  本地渲染图表需要 mermaid-cli（mmdc）或 Node.js 的 npx，"
                       "均未找到：请安装 @mermaid-js/mermaid-cli，"
                       "或把 PAPER_TOOLS_MD_MERMAID 设为 online")
        return False

    work = dest.parent / "_diagram_src"
    work.mkdir(parents=True, exist_ok=True)
    src = work / f"{dest.stem}.mmd"
    src.write_text(code, encoding="utf-8")
    # Windows / 容器里 Chromium 常因沙箱崩溃，显式 --no-sandbox（仅本地进程）
    pup = work / f"{dest.stem}.puppeteer.json"
    pup.write_text(json.dumps({"args": ["--no-sandbox"]}), encoding="utf-8")

    args = [*cmd, "-i", str(src), "-o", str(dest), "-b", "white",
            "-p", str(pup), "-s", str(scale)]
    if theme:
        args += ["-t", theme]
    if os.name == "nt" and Path(cmd[0]).suffix.lower() in (".cmd", ".bat"):
        args = ["cmd", "/c", *args]  # CreateProcess 无法直接执行 .cmd/.bat
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=_LOCAL_TIMEOUT)
    except FileNotFoundError:
        logger.warning(f"  本地图表渲染器不可执行: {cmd[0]}")
        return False
    except subprocess.TimeoutExpired:
        logger.warning(f"  本地图表渲染超时（>{_LOCAL_TIMEOUT}s）")
        return False
    if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 100:
        return True
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    logger.warning(f"  本地图表渲染失败"
                   f"{': ' + err[-1] if err else f'（退出码 {proc.returncode}）'}")
    return False


# ──────────────────────────────────────────────
# 对外入口
# ──────────────────────────────────────────────

def render_diagram_to_png(kind: str, code: str, out_dir: Path,
                          settings=None) -> Path | None:
    """渲染图表（mermaid/flow/sequence）为 PNG，返回路径；失败或关闭时返回 None。"""
    code = (code or "").strip()
    if not code:
        return None
    settings = settings or get_settings()
    renderer = str(getattr(settings, "md_mermaid_renderer", "auto")
                   or "auto").strip().lower()
    if renderer in ("off", "none", "no", "disable", "disabled", "false", "0"):
        return None

    source = _to_mermaid(kind, code)
    picked = _pick_backend(renderer, settings)
    if picked is None:
        return None
    backend, target = picked

    width = int(getattr(settings, "md_mermaid_width", _DEFAULT_WIDTH) or 0)
    theme = str(getattr(settings, "md_mermaid_theme", "") or "").strip()
    scale = max(1.0, width / 800) if width > 0 else 2.0

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{backend}|{target}|{width}|{theme}|{source}".encode("utf-8")).hexdigest()[:16]
    png = out_dir / f"diagram_{key}.png"
    if png.exists() and png.stat().st_size > 100:
        return png

    label = {"mermaid": "Mermaid", "flow": "flowchart.js",
             "sequence": "js-sequence"}.get(kind, kind)
    logger.info(f"  渲染 {label} 图表（{backend}）...")
    ok = (_local_render(source, theme, scale, png, target) if backend == "local"
          else _online_render(target, source, theme, width, png, settings))
    if ok and png.exists() and png.stat().st_size > 100:
        return png
    png.unlink(missing_ok=True)
    return None
