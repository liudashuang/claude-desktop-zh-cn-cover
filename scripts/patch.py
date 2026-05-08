#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-click zh-CN patcher for Claude Desktop on macOS.

What it does:
1. Copies /Applications/Claude.app to a temporary working app.
2. Adds zh-CN to Claude Desktop's language whitelist.
3. Installs Chinese desktop-shell and frontend i18n resources.
4. Sets the current user's Claude config locale to zh-CN.
5. Moves the original app to a timestamped backup and installs the patched app.

Run from this folder:
    sudo /usr/bin/python3 scripts/patch_claude_zh_cn.py --user-home "$HOME"
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any


APP_DEFAULT = Path("/Applications/Claude.app")
LANG_CODE = "zh-CN"
ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "resources"

FRONTEND_TRANSLATION = RESOURCES / "frontend-zh-CN.json"
DESKTOP_TRANSLATION = RESOURCES / "desktop-zh-CN.json"
LOCALIZABLE_STRINGS = RESOURCES / "Localizable.strings"

APP_ASAR_REL = Path("Contents/Resources/app.asar")
FRONTEND_I18N_REL = Path("Contents/Resources/ion-dist/i18n")
FRONTEND_ASSETS_REL = Path("Contents/Resources/ion-dist/assets/v1")
DESKTOP_RESOURCES_REL = Path("Contents/Resources")
ASAR_PATCH_TARGET = ".vite/build/index.js"
ASAR_INTEGRITY_BLOCK_SIZE = 4 * 1024 * 1024

LANG_LIST_RE = re.compile(
    r'\["en-US","de-DE","fr-FR","ko-KR","ja-JP","es-419","es-ES","it-IT","hi-IN","pt-BR","id-ID"(.*?)\]'
)


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def require_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Missing required file: {path}")


def read_entitlements(path: Path) -> str:
    return run(["codesign", "-d", "--entitlements", "-", str(path)], check=False).stdout


def load_entitlements(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = plistlib.loads(result.stdout)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def require_virtualization_entitlement(app: Path) -> None:
    entitlements = read_entitlements(app)
    if "com.apple.security.virtualization" not in entitlements:
        raise SystemExit(
            "Claude.app does not have the required virtualization entitlement. "
            "Restore or reinstall the official Claude.app first, then run this patcher again."
        )


def quit_claude() -> None:
    run(["osascript", "-e", 'tell application "Claude" to quit'], check=False)


def copy_app(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    print(f"Copying app to temporary workspace: {dst}")
    run(["ditto", str(src), str(dst)])


def patch_language_whitelist(app: Path) -> Path:
    assets_dir = app / FRONTEND_ASSETS_REL
    candidates = sorted(assets_dir.glob("index-*.js"))
    if not candidates:
        raise SystemExit(f"Cannot find frontend index bundle in {assets_dir}")

    for path in candidates:
        text = path.read_text(encoding="utf-8")
        if '"zh-CN"' in text:
            print(f"Language whitelist already contains zh-CN: {path.name}")
            return path
        if LANG_LIST_RE.search(text):
            patched = LANG_LIST_RE.sub(
                '["en-US","de-DE","fr-FR","ko-KR","ja-JP","es-419","es-ES","it-IT","hi-IN","pt-BR","id-ID","zh-CN"]',
                text,
                count=1,
            )
            path.write_text(patched, encoding="utf-8")
            print(f"Patched language whitelist: {path.name}")
            return path

    raise SystemExit("Could not patch language whitelist. Claude's bundle format may have changed.")


def patch_hardcoded_frontend_strings(app: Path) -> None:
    assets_dir = app / FRONTEND_ASSETS_REL
    replacements = {
        '"Sent on every inference and `/v1/models` discovery request (joined into the CLI\'s `ANTHROPIC_CUSTOM_HEADERS`).\\n\\nUse this for fleet-wide constants. For per-user or per-session values, have the **credential helper script** emit JSON with a `headers` field — those are merged over these static entries (helper wins on conflict)."': '"每次推理和 `/v1/models` 发现请求都会发送此信息（已添加到 CLI 的 `ANTHROPIC_CUSTOM_HEADERS` 中）。\\n\\n此信息用于全局常量。对于每个用户或每个会话的值，请让**凭证辅助脚本**生成带有 `headers` 字段的 JSON——这些值会与这些静态条目合并（冲突时以辅助脚本的值为准）。"',

        '\'Claude runs the executable with no arguments and reads **stdout** (trimmed). Exit code must be `0`; any output on **stderr** is logged but ignored. **Stdout must be the credential only** — no banners, prompts, or log lines.\\n\\n**Output format** — either:\\n- a single bare token (the API key / bearer token), or\\n- a JSON object `{"token": "...", "headers": {"Name": "Value", ...}}` when per-request headers are needed (gateway provider only; merged over **Gateway extra headers**, helper wins on conflict)\\n\\nResult is cached for the TTL below. On TTL expiry the helper is re-invoked transparently — no user prompt, no relaunch.\\n\\n**Typical use:** a shell script that pulls from Keychain, 1Password CLI, or an internal secret broker. Example:\\n\\n`security find-generic-password -s anthropic-api -w`\\n\\nIf this field is set, static credential fields (API key, bearer token) are ignored. The helper always wins.\'': '\'Claude 运行该可执行文件时不带任何参数，并读取 **stdout**（已精简）。退出代码必须为 `0`；**stderr** 上的任何输出都会被记录但会被忽略。**stdout 必须仅包含凭据**——不包含横幅、提示或日志行。\\n\\n**输出格式**——可以是：\\n- 单个裸令牌（API 密钥/持有者令牌），或者\\n- 当需要每个请求的标头时，可以使用 JSON 对象 `{"token": "...", "headers": {"Name": "Value", ...}}`（仅限网关提供商；会与 **网关额外标头** 合并，冲突时辅助程序优先）\\n\\n结果会缓存以下 TTL 值。TTL 到期后，辅助程序会透明地重新调用——无需用户提示，也无需重新启动。\\n\\n**典型用途：** 从 Keychain、1Password CLI 或内部密钥代理拉取凭据的 shell 脚本。示例：\\n\\n`security find-generic-password -s anthropic-api -w`\\n\\n如果设置了此字段，则会忽略静态凭据字段（API 密钥、持有者令牌）。辅助方法始终优先。\'',

        '"Only affects **tool calls** — inference and MCP traffic are covered by their own allowlists elsewhere.\\n\\nWhen unset, only the inference endpoint is reachable from the sandbox; the agent\'s package installs (pip/npm) and web fetches will fail with a 403.\\n\\nAccepts exact hostnames (`api.github.com`), wildcards (`*.corp.com` matches one subdomain level), and `*` to allow all.\\n\\nWildcards don\'t cross schemes. `*.corp.com` matches `docs.corp.com` but not `corp.com` itself — add both if you need the apex.\\n\\nIP literals and localhost always resolve regardless of this list; this is a public-egress filter, not a sandbox.\\n\\nHosts you add here also need to be open on your network firewall — see **Egress Requirements** for the full allowlist."': '"仅影响**工具调用**——推理和 MCP 流量由它们各自的允许列表在其他地方进行管理。\\n\\n如果未设置，则只有推理端点可从沙箱访问；代理的软件包安装（pip/npm）和 Web 获取将失败并返回 403 错误。\\n\\n接受精确主机名（例如 `api.github.com`）、通配符（例如 `*.corp.com` 匹配一个子域名级别）以及 `*` 以允许所有访问。\\n\\n通配符不会跨越协议。`*.corp.com` 匹配 `docs.corp.com`，但不匹配 `corp.com` 本身——如果需要根域名，请同时添加两者。\\n\\n无论此列表如何，IP 地址字面值和 localhost 始终可以解析；这是一个公共出口过滤器，而不是沙箱。\\n\\n您在此处添加的主机还需要在您的网络防火墙上开放——有关完整的允许列表，请参阅**出口要求**。"',

        '{body:"\\"Essential\\" means the signals Anthropic needs to keep your deployment working: **crash stacks**, **startup failure reasons**, and **version/OS metadata**. No prompts, completions, file contents, or identifiers beyond a random install ID.\\n\\n**What you lose when this is on:** when a Cowork build hits a bug that only reproduces on your OS version or locale, Anthropic can\'t see it unless a user manually reports. Fixes ship slower.\\n\\n**Why this is discouraged, not blocked:** some air-gapped environments require zero outbound telemetry as a matter of policy. The switch exists for them — if you don\'t have that constraint, leave it off."': '{body:"\\"必要\\"指的是 Anthropic 维持部署正常运行所需的信号：**崩溃堆栈**、**启动失败原因**和**版本/操作系统元数据**。除了随机安装 ID 之外，不会收集任何提示、补全信息、文件内容或标识符。\\n\\n**启用此功能后会丢失什么：**当 Cowork 构建遇到仅在您的操作系统版本或语言环境中才会出现的错误时，除非用户手动报告，否则 Anthropic 将无法检测到该错误。修复程序的发布速度会变慢。\\n\\n**不建议启用此功能，但并非完全禁用：**某些物理隔离环境出于策略考虑，要求完全禁止对外传输遥测数据。您可以为此启用此功能——如果您没有此类限制，请将其关闭。"',

        'help:{body:\'"Nonessential" covers two things: **product-usage analytics** (which features get used, navigation patterns — no prompts or completions) and the **Send** action in Help → Generate Diagnostic Report. Turning this on stops both.\\n\\nDestination for both: `claude.ai`. Already listed under Egress Requirements → Nonessential telemetry.\'': 'help:{body:\'"非必要"涵盖两项内容：**产品使用情况分析**（哪些功能被使用、导航模式——不包括提示或自动完成）以及"帮助"→"生成诊断报告"中的**发送**操作。启用此选项将停止这两项操作。\\n\\n两者的目标地址均为：`claude.ai`。已列于"出口要求"→"非必要遥测"下。\'',


        '"Desktop extensions (Python runtime)"': '"桌面扩展（Python 运行时）"',
        '"Auto-updates"': '"自动更新"',
        '"Core (VM bundle + Claude CLI binary)"': '"核心（VM 包 + Claude CLI 二进制文件）"',
        '"Per-user soft cap, counted client-side over the duration below. Not a server-enforced quota."': '"每个用户的软上限，在以下时间段内由客户端计算。并非服务器强制执行的配额。"',
        '"Max tokens per window"': '"每个窗口的最大令牌数"',
        'title:"Usage limits"': 'title:"使用限制"',
        '"This disables artifact previews and connector icons. Artifacts will not render in conversations."': '""',
        '"Block nonessential services"': '"这将禁用工件预览和连接器图标。工件将不会在对话中显示。"',
        '"Usage analytics help us prioritize improvements for third-party inference. Diagnostic-report uploads will also be blocked. No message content is included in either."': '"使用情况分析有助于我们优先改进第三方推理功能。诊断报告上传也将被阻止。以上两项措施均不包含任何消息内容。"',
        '"Block nonessential telemetry"': '"阻止非必要的遥测"',
        '"Crash and error reports are how we diagnose failures specific to your inference setup. Support turnaround will be slower without them."': '"崩溃和错误报告是我们诊断特定推理设置故障的关键。如果没有这些报告，支持响应速度将会变慢。"',
        '"Block essential telemetry"': '"阻止必要的遥测"',
        '"Anthropic telemetry"': '"Anthropic 遥测"',
        '"Hours before a downloaded update force-installs. Blank = 72-hour default."': '"下载的更新会在几小时内强制安装。空白处表示默认的 72 小时。"',
        ',suffix:"hours",': ',suffix:"小时",',
        '"Auto-update enforcement window"': '"自动更新强制窗口"',
        '"Stop Cowork from fetching updates. You\'ll need to push new versions yourself."': '"停止 Cowork 获取更新。您需要自行推送新版本。"',
        '"Block auto-updates"': '"阻止自动更新"',
        ',group:"Updates"': ',group:"更新"',
        '"Extra resource attributes to attach to every span/metric."': '"附加到每个 span/metric 的额外资源属性。"',
        '"OpenTelemetry resource attributes"': '"OpenTelemetry 资源属性"',
        '"Optional auth headers for the collector."': '"收集器的可选身份验证标头。"',
        '"OpenTelemetry exporter headers"': '"OpenTelemetry导出器头"',
        '"grpc or http/protobuf."': '"grpc 或 http/protobuf。"',
        '"OpenTelemetry exporter protocol"': '"OpenTelemetry导出器协议"',
        '"Where Cowork sends OpenTelemetry logs and metrics. Leave blank to disable."': '"Cowork 会将 OpenTelemetry 日志和指标发送到此处。留空则禁用此功能。"',
        '"OpenTelemetry collector endpoint"': '"OpenTelemetry 收集器端点"',
        '"OpenTelemetry"': '"开放遥测"',
        '"Prompts, completions, and your data are never sent to Anthropic — telemetry covers crash and usage signals only."': '"提示、完成情况和您的数据永远不会发送给 Anthropic——遥测数据仅涵盖崩溃和使用情况信号。"',
        '"Reject desktop extensions that are not signed by a trusted publisher."': '"拒绝安装未经可信发布者签名的桌面扩展程序。"',
        '"Require signed extensions"': '"需要签署的扩展"',
        '"The in-app catalogue of installable extensions. Hide to allow sideload only."': '"应用内可安装扩展程序目录。隐藏后仅允许侧载。"',
        '"Show extension directory"': '"显示扩展目录"',
        '".dxt and .mcpb installs."': '".dxt 和 .mcpb 安装。"',
        '"Allow desktop extensions"': '"允许桌面扩展"',
        ',group:"Extensions"': ',group:"扩展"',
        '"Local stdio servers added via the Developer settings. Remote servers come from the managed list above, or plugins mounted to a user\'s computer by an organization admin."': '"本地 stdio 服务器通过开发者设置添加。远程服务器来自上述托管列表，或由组织管理员挂载到用户计算机上的插件。"',
        '"Allow user-added MCP servers"': '"允许用户添加的 MCP 服务器"',
        '"Org-pushed remote MCP servers. May embed bearer tokens."': '"组织推送的远程 MCP 服务器。可能嵌入持有者令牌。"',
        '"Managed MCP servers"': '"托管 MCP 服务器"',
        '"MCP servers"': '"MCP 服务器"',
        '"Folders users may attach as a workspace. Leave unset for unrestricted access."': '"用户可将文件夹附加为工作区。如需无限制访问，请勿设置。"',
        '"Allowed workspace folders"': '"允许的工作区文件夹"',
        '"Built-in tools removed from Cowork."': '"Cowork 中已移除内置工具。"',
        '"Disabled built-in tools"': '"已禁用内置工具"',
        '"Domains Cowork\'s tools may reach during a turn. Also surfaced under Egress Requirements."': '"Cowork 工具在一次回合中可访问的域名。也会显示在“出站要求”中。"',
        '"Show the Code tab (terminal-based coding sessions). Sessions run on the host, not inside the VM."': '"显示“代码”选项卡（基于终端的编码会话）。会话在主机上运行，而不是在虚拟机内部运行。"',
        '"Allowed egress hosts"': '"允许的出口主机"',
        '"Allow Claude Code tab"': '"允许 Claude 代码选项卡"',
        '"Go straight to this provider at launch — users won\'t see the option to sign in to Anthropic instead."': '"上线后请直接访问此提供商——用户不会看到登录 Anthropic 的选项。"',
        '"Skip login-mode chooser"': '"跳过登录模式选择器"',

        '"Absolute path to an executable that prints the credential."': '"打印凭据的可执行文件的绝对路径。"',
        '"Credential helper script"': '"凭据辅助脚本"',
        '"Tags telemetry events with your org so support can find them. Not used for auth."': '"使用组织名称标记遥测事件，以便支持人员能够找到它们。不用于身份验证。"',
        '"Organization UUID"': '"组织 UUID"',
        '"Offer 1M-context variant"': '"提供 1M 上下文变体"',
        '"Model ID"': '"模型ID"',
        '"First entry is the picker default. Aliases like sonnet, opus accepted. Optional for gateway — when set, the picker shows exactly this list instead of /v1/models discovery. Turn on 1M context only for models your provider actually serves with the extended window."': '"第一个条目是选择器的默认值。接受 sonnet、opus 等别名。网关可选——启用后，选择器将显示此列表，而不是 /v1/models 发现列表。仅对提供商实际提供的、具有扩展窗口的模型启用 1M 上下文。"',
        '"Model list"': '"模型列表"',
        '"Identity & models"': '"身份与模型"',
        '"Gateway API key"': '"网关 API key"',
        '"Gateway auth scheme"': '"网关认证方案"',
        '"Gateway extra headers"': '"网关额外标头"',
        '"Full URL of the inference gateway endpoint."': '"推理网关端点的完整URL。"',
        ',title:"Gateway base URL"': ',title:"网关基础 URL"',

        '"Extra headers sent to the gateway. One value per header name. For tenant routing, org IDs, etc."': '"发送到网关的额外标头。每个标头名称对应一个值。用于租户路由、组织 ID 等。"',
        '"Hide Anthropic sign-in"': '"隐藏 Anthropic 登录"',
        '"Users see only this provider at the login screen — the option to sign in to Anthropic is hidden."': '"用户在登录界面只能看到此提供商——登录 Anthropic 的选项被隐藏了。"',
        '"Bearer (default) sends Authorization: Bearer. x-api-key is for the Anthropic API directly — auto-selected when the URL is *.anthropic.com."': '"Bearer（默认）发送 Authorization: Bearer。x-api-key 直接用于 Anthropic API — 当 URL 为 *.anthropic.com 时自动选择。"',
        '"Choose where Claude Desktop sends inference requests."':'"选择 Claude Desktop 向何处发送推理请求。"',
        ':{title:"Connection",':':{title:"连接",',
        '{title:"Sandbox & workspace"}':'{title:"沙盒和工作区"}',
        '{title:"Connectors & extensions"}':'{title:"连接器和扩展"}',
        ':{title:"Telemetry & updates",':':{title:"遥测和更新",',
        '{title:"Egress Requirements",':'{title:"出站要求",',
        '{title:"Usage limits"}':'{title:"使用限制"}',
        '{title:"Plugins & skills",':'{title:"插件和技能",',
        'Plugins and skills aren\'t set in this configuration. Mount plugin bundles to the folder below using your device-management tool and Cowork will load them at launch.':'此配置中未设置插件和技能。请使用设备管理工具将插件包挂载到以下文件夹，Cowork 将在启动时加载它们。',
        '"Drop plugin folders here. Read-only to the app."':'"将插件文件夹拖放到此处。对应用程序只读。"',
        '"Hosts your network firewall must allow, derived from your current settings. This list is read-only and updates as you make changes. Traffic is HTTPS on port 443 unless a custom port is specified (OTLP, gateway, or MCP server URLs)."':'"此列表包含您的网络防火墙必须允许的主机，其值取决于您当前的设置。此列表为只读，会随着您的更改而更新。除非指定自定义端口（例如 OTLP、网关或 MCP 服务器 URL），否则流量将通过 443 端口上的 HTTPS 协议传输。"',
        'e.length?"All"': 'e.length?"全部"',
        ',["0","All"]]': ',["0","全部"]]',
        ',Al="Local",Dl="Cloud",El="Remote Control",zl="All"': ',Al="本地",Dl="云端",El="远程控制",zl="全部"',
        'children:"All projects"': 'children:"所有项目"',
        '[["active","Active"],["archived","Archived"],["all","All"]]': '[["active","活跃"],["archived","已存档"],["all","全部"]]',
        '[["date","Date"]': '[["date","日期"]',
        '[["state","State"]]': '[["state","状态"]]',
        '["none","None"]]': '["none","无"]]',
        '[["project","Project"]]': '[["project","项目"]]',
        '[["environment","Environment"]]': '[["environment","环境"]]',
        'label:"Sort by",': 'label:"排序方式",',
        '{title:"Projects",': '{title:"项目",',
        '="New project",': '="新建项目",',
        ',placeholder:"Search projects"}': ',placeholder:"搜索项目"}',
        '={recent:"Recent",created:"Created",alphabetical:"Alphabetical"}': '={recent:"按最近使用",created:"按创建时间",alphabetical:"按字母顺序"}',
        '[["alpha","Alphabetically"],["created","Created time"],["recency","Recency"]]': '[["alpha","按字母顺序"],["created","按创建时间"],["recency","按最近使用"]]',
        ',children:"Clear filters"': ',children:"清除筛选"',
        ',tooltip:"Search",tooltipKeyboardShortcut': ',tooltip:"搜索",tooltipKeyboardShortcut',
        ',tooltip:"Collapse sidebar"': ',tooltip:"折叠侧边栏"',
        'label:"Environment",': 'label:"环境",',
        'label:"Last activity",': 'label:"最后活动",',
        ',children:"Project"': ',children:"项目"',
        'label:"Group by",': 'label:"按组",',
        'label:"Status",': 'label:"状态",',
        ',children:"New task"': ',children:"新建任务"',
        ',placeholder:"Filter scheduled tasks"}': ',placeholder:"筛选计划任务"}',
        '={nextRun:"Next run",name:"Name"}': '={nextRun:"按下次运行",name:"按名称"}',
        ',label:"New session"': ',label:"新建会话"',
        ',message:"Scheduled tasks only run while your computer is awake.",': ',message:"计划任务仅在计算机处于唤醒状态时运行。",',
        '}),"No scheduled tasks yet."]}': '}),"尚无计划任务。"]}',
        ',children:"Run tasks on a schedule or whenever you need them. Type /schedule in any existing task to set one up."}': ',children:"按计划或在需要时运行任务。在任何现有任务中键入 /schedule 来设置一项。"}',
        ',{title:"Scheduled tasks",': ',{title:"计划任务",',
        'const Ql={chat:"New chat",cowork:"New task",code:"New session",operon:"New session",home:"New"}': 'const Ql={chat:"新建对话",cowork:"新建任务",code:"新建会话",operon:"新建会话",home:"New"}',
        ',children:"Pinned"': ',children:"已置顶"',
        'const Jl="Recents"': 'const Jl="最近使用"',
        ',label:"Projects"': ',label:"项目"',
        ',label:"Scheduled"': ',label:"计划任务"',
        ',label:"Customize"': ',label:"自定义"',
        ',name:"Customize"': ',name:"自定义"',
        '"Drag to pin"': '"拖到此处固定"',
        '"Drop here"': '"拖到此处"',
        '"Let go"': '"松开"',
    }
    patched_files = 0
    patched_strings = 0

    for path in sorted(assets_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        patched = text
        count = 0
        for source, target in replacements.items():
            occurrences = patched.count(source)
            if occurrences:
                patched = patched.replace(source, target)
                count += occurrences
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            patched_files += 1
            patched_strings += count

    print(f"Patched hardcoded frontend strings: {patched_strings} replacements in {patched_files} files")


def align4(value: int) -> int:
    return value + ((4 - (value % 4)) % 4)


def read_asar_header(data: bytes, path: Path) -> tuple[int, str, dict[str, Any]]:
    if len(data) < 16:
        raise SystemExit(f"Unsupported app.asar header in {path}")

    size_pickle_payload = struct.unpack_from("<I", data, 0)[0]
    header_size = struct.unpack_from("<I", data, 4)[0]
    if size_pickle_payload != 4 or header_size <= 0 or len(data) < 8 + header_size:
        raise SystemExit(f"Unsupported app.asar size pickle in {path}")

    header_pickle = data[8 : 8 + header_size]
    header_payload_size = struct.unpack_from("<I", header_pickle, 0)[0]
    header_string_size = struct.unpack_from("<i", header_pickle, 4)[0]
    expected_payload_size = align4(4 + header_string_size)
    if header_payload_size != expected_payload_size or header_size != 4 + header_payload_size:
        raise SystemExit(f"Unsupported app.asar header pickle in {path}")

    header_start = 8
    header_end = header_start + header_string_size
    header_string = header_pickle[header_start:header_end].decode("utf-8")
    header = json.loads(header_string)
    if not isinstance(header, dict):
        raise SystemExit(f"Unsupported app.asar header JSON in {path}")
    return header_size, header_string, header


def encode_asar_header(header_string: str, expected_header_size: int) -> bytes:
    header_bytes = header_string.encode("utf-8")
    header_payload_size = align4(4 + len(header_bytes))
    header_pickle = (
        struct.pack("<I", header_payload_size)
        + struct.pack("<i", len(header_bytes))
        + header_bytes
        + b"\0" * (header_payload_size - 4 - len(header_bytes))
    )
    if len(header_pickle) != expected_header_size:
        raise SystemExit("Internal patch error: app.asar header length changed.")
    return struct.pack("<I", 4) + struct.pack("<I", expected_header_size) + header_pickle


def get_asar_file_entry(header: dict[str, Any], file_path: str) -> dict[str, Any]:
    node: dict[str, Any] = header
    for part in file_path.split("/"):
        files = node.get("files")
        if not isinstance(files, dict) or part not in files:
            raise SystemExit(f"Could not find {file_path} in app.asar header.")
        child = files[part]
        if not isinstance(child, dict):
            raise SystemExit(f"Unsupported app.asar header entry for {file_path}.")
        node = child
    for key in ["size", "offset", "integrity"]:
        if key not in node:
            raise SystemExit(f"Missing {key} for {file_path} in app.asar header.")
    return node


def calculate_file_integrity(data: bytes) -> dict[str, Any]:
    blocks = [
        hashlib.sha256(data[offset : offset + ASAR_INTEGRITY_BLOCK_SIZE]).hexdigest()
        for offset in range(0, len(data), ASAR_INTEGRITY_BLOCK_SIZE)
    ]
    if not blocks:
        blocks.append(hashlib.sha256(data).hexdigest())
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(data).hexdigest(),
        "blockSize": ASAR_INTEGRITY_BLOCK_SIZE,
        "blocks": blocks,
    }


def update_electron_asar_integrity(app: Path, header_string: str) -> None:
    info_plist = app / "Contents/Info.plist"
    require_file(info_plist)
    with info_plist.open("rb") as f:
        info = plistlib.load(f)

    integrity = info.get("ElectronAsarIntegrity")
    if not isinstance(integrity, dict):
        raise SystemExit("Info.plist is missing ElectronAsarIntegrity.")
    app_asar = integrity.get("Resources/app.asar")
    if not isinstance(app_asar, dict) or app_asar.get("algorithm") != "SHA256":
        raise SystemExit("Info.plist has unsupported ElectronAsarIntegrity format.")

    app_asar["hash"] = hashlib.sha256(header_string.encode("utf-8")).hexdigest()
    tmp = info_plist.with_suffix(info_plist.suffix + ".tmp")
    with tmp.open("wb") as f:
        plistlib.dump(info, f, fmt=plistlib.FMT_XML)
    os.replace(tmp, info_plist)


def patch_custom3p_model_validation(app: Path) -> None:
    path = app / APP_ASAR_REL
    require_file(path)

    old_expr = b'process.env.NODE_ENV!=="production"'
    new_expr = b"false"
    replacement = new_expr + b" " * (len(old_expr) - len(new_expr))
    anchor = b"const Hte=" + old_expr + b"||!1,eRt="
    patched = b"const Hte=" + replacement + b"||!1,eRt="
    if len(anchor) != len(patched):
        raise SystemExit("Internal patch error: custom 3P validation replacement changed length.")

    data = bytearray(path.read_bytes())
    header_size, _header_string, header = read_asar_header(data, path)
    entry = get_asar_file_entry(header, ASAR_PATCH_TARGET)
    content_offset = 8 + header_size + int(entry["offset"])
    content_size = int(entry["size"])
    content_end = content_offset + content_size
    if content_offset < 0 or content_end > len(data):
        raise SystemExit(f"Unsupported app.asar file bounds for {ASAR_PATCH_TARGET}.")

    content = bytes(data[content_offset:content_end])
    count = content.count(anchor)
    if count != 1:
        raise SystemExit(
            "Could not patch custom 3P model validation. Claude bundle format may have changed."
        )

    patched_content = content.replace(anchor, patched, 1)
    if len(patched_content) != len(content):
        raise SystemExit("Internal patch error: app.asar length changed during custom 3P patch.")
    data[content_offset:content_end] = patched_content

    entry["integrity"] = calculate_file_integrity(patched_content)
    updated_header_string = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
    updated_header = encode_asar_header(updated_header_string, header_size)
    data[: len(updated_header)] = updated_header

    path.write_bytes(data)
    update_electron_asar_integrity(app, updated_header_string)
    print("Patched custom 3P model-name validation in app.asar")


def merge_frontend_locale(app: Path) -> tuple[int, int, int]:
    source = app / FRONTEND_I18N_REL / "en-US.json"
    target = app / FRONTEND_I18N_REL / "zh-CN.json"
    require_file(source)
    require_file(FRONTEND_TRANSLATION)

    en = load_json(source)
    zh_pack = load_json(FRONTEND_TRANSLATION)
    if not isinstance(en, dict) or not isinstance(zh_pack, dict):
        raise SystemExit("Unsupported frontend i18n JSON shape.")

    merged: dict[str, Any] = {}
    translated = 0
    fallback = 0
    for key, value in en.items():
        if key in zh_pack:
            merged[key] = zh_pack[key]
            if zh_pack[key] != value:
                translated += 1
        else:
            merged[key] = value
            fallback += 1

    save_json(target, merged)
    extra = len(set(zh_pack) - set(en))
    print(f"Installed frontend zh-CN: {translated} translated, {fallback} fallback, {extra} extra old keys ignored")
    return translated, fallback, extra


def install_desktop_locale(app: Path) -> None:
    resources_dir = app / DESKTOP_RESOURCES_REL
    require_file(DESKTOP_TRANSLATION)
    require_file(LOCALIZABLE_STRINGS)

    shutil.copy2(DESKTOP_TRANSLATION, resources_dir / "zh-CN.json")
    for folder in ["zh-CN.lproj", "zh_CN.lproj"]:
        out_dir = resources_dir / folder
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LOCALIZABLE_STRINGS, out_dir / "Localizable.strings")
    print("Installed desktop shell zh-CN resources")


def install_statsig_locale(app: Path) -> None:
    statsig_dir = app / FRONTEND_I18N_REL / "statsig"
    if not statsig_dir.exists():
        return
    target = statsig_dir / "zh-CN.json"
    bundled = RESOURCES / "statsig-zh-CN.json"
    if bundled.exists():
        shutil.copy2(bundled, target)
    elif (statsig_dir / "en-US.json").exists():
        shutil.copy2(statsig_dir / "en-US.json", target)
    print("Installed statsig zh-CN resource")


def sign_path(path: Path, entitlements_dir: Path) -> None:
    entitlements = load_entitlements(path)
    if entitlements:
        # Ad-hoc signatures do not have a real Team ID. Under hardened runtime,
        # Electron's main process otherwise fails library validation when it loads
        # bundled frameworks, even when the whole bundle is signed consistently.
        entitlements["com.apple.security.cs.disable-library-validation"] = True

    cmd = [
        "codesign",
        "--force",
        "--sign",
        "-",
        "--options",
        "runtime",
        "--preserve-metadata=identifier,flags",
    ]
    if entitlements:
        entitlement_path = entitlements_dir / f"{abs(hash(path.as_posix()))}.plist"
        entitlement_path.write_bytes(plistlib.dumps(entitlements, fmt=plistlib.FMT_XML))
        cmd.extend(["--entitlements", str(entitlement_path)])
    cmd.append(str(path))

    result = run(cmd, check=False)
    if result.returncode != 0:
        print(result.stdout, end="")
        raise SystemExit(f"Failed to re-sign: {path}")


def is_signable_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if path.suffix in {".dylib", ".node", ".so"}:
        return True
    return os.access(path, os.X_OK)


def resign_app(app: Path) -> None:
    print("Re-signing patched app with local ad-hoc signature, preserving entitlements")
    contents = app / "Contents"
    entitlements_dir = Path(tempfile.mkdtemp(prefix="claude-zh-cn-entitlements."))
    bundle_targets: list[Path] = []
    file_targets: list[Path] = []

    for root, dirs, files in os.walk(contents):
        root_path = Path(root)
        for dirname in dirs:
            path = root_path / dirname
            if path.suffix in {".app", ".framework"}:
                bundle_targets.append(path)
        for filename in files:
            path = root_path / filename
            if is_signable_file(path):
                file_targets.append(path)

    # Sign nested Mach-O files first, then their containing bundles, then the outer app.
    for path in sorted(file_targets, key=lambda p: len(p.parts), reverse=True):
        sign_path(path, entitlements_dir)
    for path in sorted(bundle_targets, key=lambda p: len(p.parts), reverse=True):
        sign_path(path, entitlements_dir)
    sign_path(app, entitlements_dir)


def clear_quarantine(app: Path) -> None:
    result = run(["xattr", "-dr", "com.apple.quarantine", str(app)], check=False)
    if result.returncode == 0:
        print("Cleared Gatekeeper quarantine attribute")


def set_user_locale(user_home: Path) -> None:
    config = user_home / "Library/Application Support/Claude/config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if config.exists():
        try:
            data = load_json(config)
        except Exception:
            backup = config.with_suffix(".json.bak-invalid")
            shutil.copy2(config, backup)
            print(f"Existing config was not valid JSON; backed up to {backup}")
    data["locale"] = LANG_CODE
    save_json(config, data)

    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        os.chown(config, int(sudo_uid), int(sudo_gid))
    print(f"Set Claude config locale: {config}")


def backup_and_replace(original: Path, patched: Path, dry_run: bool) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = original.with_name(f"Claude.backup-before-zh-CN-{stamp}.app")
    if dry_run:
        print(f"[dry-run] Would move {original} -> {backup}")
        print(f"[dry-run] Would move {patched} -> {original}")
        return backup

    print(f"Backing up current app: {backup}")
    shutil.move(str(original), str(backup))
    print(f"Installing patched app: {original}")
    shutil.move(str(patched), str(original))
    return backup


def verify(app: Path) -> None:
    frontend = app / FRONTEND_I18N_REL / "zh-CN.json"
    data = load_json(frontend)
    values = [v for v in data.values() if isinstance(v, str)]
    chinese = sum(1 for v in values if re.search(r"[\u4e00-\u9fff]", v))
    print(f"Verified frontend zh-CN JSON: {chinese}/{len(values)} strings contain Chinese")

    verify_result = run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=False)
    if verify_result.returncode == 0:
        print("Verified app signature")
    else:
        print("App signature verification failed:")
        print(verify_result.stdout, end="")

    entitlements = read_entitlements(app)
    if "com.apple.security.virtualization" in entitlements:
        print("Verified virtualization entitlement")
    else:
        print("Warning: virtualization entitlement is missing")

    result = run(["codesign", "-dv", str(app)], check=False).stdout
    for line in result.splitlines():
        if line.startswith("TeamIdentifier="):
            print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch Claude Desktop with zh-CN language resources.")
    parser.add_argument("--app", type=Path, default=APP_DEFAULT, help="Path to Claude.app")
    parser.add_argument("--user-home", type=Path, default=Path.home(), help="Home directory whose Claude config should be updated")
    parser.add_argument("--dry-run", action="store_true", help="Prepare and verify a patched temp app, but do not replace /Applications/Claude.app")
    parser.add_argument("--launch", action="store_true", help="Launch Claude after installation")
    args = parser.parse_args()

    require_file(FRONTEND_TRANSLATION)
    require_file(DESKTOP_TRANSLATION)
    require_file(LOCALIZABLE_STRINGS)
    if not args.app.exists():
        raise SystemExit(f"Claude.app not found: {args.app}")
    require_virtualization_entitlement(args.app)

    try:
        in_applications = args.app.resolve().as_posix().startswith("/Applications/")
    except Exception:
        in_applications = str(args.app).startswith("/Applications/")
    if os.geteuid() != 0 and in_applications:
        print("This usually needs sudo because /Applications is protected.", file=sys.stderr)

    if args.dry_run:
        print("[dry-run] Claude will not be quit.")
    else:
        quit_claude()
    tmp_root = Path(tempfile.mkdtemp(prefix="claude-zh-cn-patch."))
    patched_app = tmp_root / "Claude.app"

    copy_app(args.app, patched_app)
    patch_language_whitelist(patched_app)
    patch_hardcoded_frontend_strings(patched_app)
    patch_custom3p_model_validation(patched_app)
    merge_frontend_locale(patched_app)
    install_desktop_locale(patched_app)
    install_statsig_locale(patched_app)
    resign_app(patched_app)
    clear_quarantine(patched_app)
    if args.dry_run:
        print(f"[dry-run] Would set Claude config locale under: {args.user_home}")
    else:
        set_user_locale(args.user_home)
    verify(patched_app)

    backup = backup_and_replace(args.app, patched_app, args.dry_run)
    if not args.dry_run:
        print(f"Backup kept at: {backup}")
        if args.launch:
            run(["open", "-a", str(args.app)], check=False)

    print("Done. Select Language -> 中文（中国） in Claude if it is not already selected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())