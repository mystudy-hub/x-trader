#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex CLI 智能消息驱动自动守护系统 (Codex Watcher)

核心特性:
1. 【最终确认机制】: 
   在 Codex 自身还在尝试中（如 Reconnecting... 1/5 倒计时、Working、esc to interrupt、网络等待）时，
   绝对保持静默，不主动发送消息；
   等到所有尝试彻底结束、最终确认停留在输入提示符后，再自动触发输入。
2. 【基于最后消息决策】: 
   精准提取 Codex CLI 终端当前打印的最后一条消息 (Last Message) 与报错，
   根据消息的语义内容动态决定自动输入什么。
3. 【Rate Limit / 网络异常】: 
   当最终确认出现 "rate limit exceeded: Your requests to ... have exceeded rate limit"、
   传输断开、超时、网络错误等时，自动输入“继续任务”。
4. 【API 400 / 加密内容异常 (invalid_encrypted_content)】:
   当最终确认出现 "bad response status code 400"、"invalid_encrypted_content"、
   "invalid_request_error"、"■ {\"error\":...}" 等 API 报错时，自动输入“继续任务”。
5. 【用户选择 / 权限审批】:
   当最后消息出现选项提示 (如 1) 2)、[1] [2]、Approve app tool call?、Select an option 等) 时，
   自动选择输入“1”。
6. 【询问确认】:
   当最后消息出现“是否继续”、“Do you want to continue”等提问时，自动输入“继续”。
7. 【Pebrel 深度适配】:
   自动检测 Pebrel 运行时，后台静默读取与无感注入，不抢占前台焦点。

使用示例:
    python codex_watcher.py                  # 默认启动
    python codex_watcher.py --dry-run        # 试运行模式，仅检测并显示匹配决策，不实际发送
    python codex_watcher.py --text "继续任务" # 设定错误时的输入内容
    python codex_watcher.py --choice "1"     # 设定选择时的输入内容
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple, Dict

# 强制标准输出使用 UTF-8，杜绝 Windows 控制台中文和特殊 Unicode 符号崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ==============================================================================
# 终端彩色输出
# ==============================================================================
class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

    @classmethod
    def init_windows_vt(cls):
        """启用 Windows 终端 ANSI 虚拟终端序列支持"""
        if os.name == "nt":
            try:
                kernel32 = ctypes.windll.kernel32
                hStdOut = kernel32.GetStdHandle(-11)
                mode = ctypes.c_ulong()
                kernel32.GetConsoleMode(hStdOut, ctypes.byref(mode))
                mode.value |= 0x0004
                kernel32.SetConsoleMode(hStdOut, mode)
            except Exception:
                pass


Color.init_windows_vt()


def log_info(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Color.DIM}[{ts}]{Color.RESET} {Color.CYAN}[信息]{Color.RESET} {msg}")


def log_success(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Color.DIM}[{ts}]{Color.RESET} {Color.GREEN}{Color.BOLD}[成功]{Color.RESET} {msg}")


def log_warn(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Color.DIM}[{ts}]{Color.RESET} {Color.YELLOW}{Color.BOLD}[检测]{Color.RESET} {msg}")


def log_error(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Color.DIM}[{ts}]{Color.RESET} {Color.RED}{Color.BOLD}[错误]{Color.RESET} {msg}")


def log_action(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{Color.DIM}[{ts}]{Color.RESET} {Color.MAGENTA}{Color.BOLD}[决策动作]{Color.RESET} {msg}")


def strip_ansi(text: str) -> str:
    """去除终端 ANSI 控制序列"""
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


# ==============================================================================
# Pebrel 后端交互
# ==============================================================================
class PebrelBackend:
    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("pebrel") is not None or os.environ.get("PEBREL_CLI") is not None

    @classmethod
    def get_cli_path(cls) -> str:
        return os.environ.get("PEBREL_CLI") or shutil.which("pebrel") or "pebrel"

    @classmethod
    def list_codex_panes(cls) -> List[Tuple[int, str, str]]:
        cli = cls.get_cli_path()
        try:
            res = subprocess.run(
                [cli, "agent", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if res.returncode != 0:
                return []
            data = json.loads(res.stdout)
            candidates = []
            for a in data.get("result", {}).get("agents", []):
                agent_info = a.get("agent", {})
                pane_id = a.get("pane_id")
                title = a.get("title", "")
                kind = agent_info.get("kind", "")
                state = a.get("task_state", "")
                if kind == "codex" or "codex" in title.lower() or "落实修改建议" in title:
                    candidates.append((pane_id, title, state))
            return candidates
        except Exception:
            return []

    @classmethod
    def read_pane_tail(cls, pane_id: int, lines: int = 50) -> str:
        cli = cls.get_cli_path()
        try:
            res = subprocess.run(
                [cli, "pane", "read", str(pane_id), "--lines", str(lines)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if res.returncode != 0:
                return ""
            data = json.loads(res.stdout)
            return data.get("result", {}).get("text", "")
        except Exception:
            return ""

    @classmethod
    def send_to_pane(cls, pane_id: int, text: str) -> bool:
        cli = cls.get_cli_path()
        try:
            res = subprocess.run(
                [cli, "pane", "send", str(pane_id), text],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            return res.returncode == 0
        except Exception as e:
            log_error(f"Pebrel 发送失败: {e}")
            return False


# ==============================================================================
# Codex Session 日志审计
# ==============================================================================
class CodexSessionAuditor:
    def __init__(self):
        self.codex_home = os.path.expanduser("~/.codex")
        self.sessions_dir = os.path.join(self.codex_home, "sessions")
        self.last_handled_turn_id: Optional[str] = None

    def find_latest_session_file(self) -> Optional[str]:
        if not os.path.exists(self.sessions_dir):
            return None
        files = glob.glob(os.path.join(self.sessions_dir, "**", "*.jsonl"), recursive=True)
        if not files:
            return None
        files.sort(key=os.path.getmtime, reverse=True)
        return files[0]

    def inspect_latest_status(self) -> Tuple[Optional[str], Optional[dict], bool, bool]:
        """
        返回: (turn_id, error_dict, is_completed_with_error, is_in_progress)
        """
        latest_file = self.find_latest_session_file()
        if not latest_file or not os.path.exists(latest_file):
            return None, None, False, False

        try:
            with open(latest_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-30:]

            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                payload = data.get("payload", {})
                ptype = payload.get("type")

                # 如果最新事件显示正在进行中
                if ptype == "task_started":
                    return payload.get("turn_id"), None, False, True

                # 如果最新事件是完成带错误
                if ptype == "task_complete":
                    turn_id = payload.get("turn_id")
                    err = payload.get("error")
                    if err:
                        return turn_id, err, True, False
                    return turn_id, None, False, False

                if ptype == "turn_aborted":
                    turn_id = payload.get("turn_id")
                    reason = payload.get("reason", "aborted")
                    return turn_id, {"message": f"turn aborted: {reason}"}, True, False

            return None, None, False, False
        except Exception:
            return None, None, False, False


# ==============================================================================
# Windows 窗口备用后端
# ==============================================================================
class WindowsWindowBackend:
    @classmethod
    def find_codex_window(cls) -> Optional[int]:
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        matched_hwnd = None

        def enum_proc(hwnd, lParam):
            nonlocal matched_hwnd
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.lower()
            if any(t in title for t in ["codex", "qh_trader", "pebrel"]):
                matched_hwnd = hwnd
                return False
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        return matched_hwnd

    @classmethod
    def send_keys(cls, hwnd: int, text: str) -> bool:
        if os.name != "nt":
            return False
        try:
            escaped = text.replace("{", "{{}").replace("}", "{}}")
            ps_cmd = (
                f"$wshell = New-Object -ComObject WScript.Shell; "
                f"[void]$wshell.AppActivate({hwnd}); "
                f"Start-Sleep -Milliseconds 200; "
                f"$wshell.SendKeys('{escaped}{{ENTER}}')"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
            return True
        except Exception:
            return False


# ==============================================================================
# 屏幕状态与最后消息提取器 (Last Message & State Extractor)
# ==============================================================================
@dataclass
class ScreenStatus:
    is_attempting: bool          # 是否还在尝试中 (Reconnecting / Working / esc to interrupt)
    attempt_detail: str          # 尝试中的具体描述 (如 Reconnecting... 1/5 (9m 31s))
    is_ready_prompt: bool        # 是否停留在最终输入提示符
    last_message: str            # 提取出的最后一条消息


class LastMessageExtractor:
    """精准解析屏幕状态，区分“还在尝试中”与“最终确认停止”"""

    @classmethod
    def inspect(cls, screen_text: str) -> ScreenStatus:
        clean = strip_ansi(screen_text)
        raw_lines = [l.strip() for l in clean.splitlines() if l.strip()]
        tail_text = "\n".join(raw_lines[-30:]) if raw_lines else ""

        # -------------------------------------------------------------
        # 1. 检测是否还在尝试中 (In-progress / Attempting)
        # 只要存在以下标志，说明 Codex 自带机制或模型正在运行，绝不打扰！
        # -------------------------------------------------------------
        is_attempting = False
        attempt_detail = ""

        # 1.1 检测重连中 (Reconnecting... 1/5 等)
        m_reconn = re.search(r"Reconnecting\.\.\.\s*\d+/\d+\s*\([^\)]*\)", tail_text, re.IGNORECASE)
        if m_reconn:
            is_attempting = True
            attempt_detail = m_reconn.group(0)
        elif re.search(r"Reconnecting\.\.\.", tail_text, re.IGNORECASE):
            is_attempting = True
            attempt_detail = "正在重连中 (Reconnecting...)"
        # 1.2 检测等待网络 (Waiting for network)
        elif re.search(r"waiting\s+for\s+network", tail_text, re.IGNORECASE):
            is_attempting = True
            attempt_detail = "正在等待网络恢复 (Waiting for network)"
        # 1.3 检测 Working / esc to interrupt
        elif re.search(r"esc\s+to\s+interrupt", tail_text, re.IGNORECASE):
            is_attempting = True
            m_work = re.search(r"(?:Working|Reconnecting)[^\)]*esc\s+to\s+interrupt", tail_text, re.IGNORECASE)
            attempt_detail = m_work.group(0) if m_work else "正在运行中 (esc to interrupt)"
        elif re.search(r"◦\s*Working", tail_text, re.IGNORECASE):
            is_attempting = True
            attempt_detail = "正在工作/思考生成中 (Working)"
        # 1.4 检测是否有已排队的消息
        elif re.search(r"Messages\s+to\s+be\s+submitted", tail_text, re.IGNORECASE):
            is_attempting = True
            attempt_detail = "已有排队消息待提交"

        # -------------------------------------------------------------
        # 2. 检测是否处于最终输入提示符 (Ready Prompt)
        # -------------------------------------------------------------
        is_ready = False
        for l in raw_lines[-6:]:
            if re.search(r"›\s*Ask\s+Codex\s+to\s+do\s+anything", l, re.IGNORECASE):
                is_ready = True
                break
            if re.match(r"^[›>]\s*$", l):
                is_ready = True
                break

        # -------------------------------------------------------------
        # 3. 提取最后一条消息 (Last Message)
        # -------------------------------------------------------------
        filtered_lines = []
        for l in raw_lines:
            # 过滤常驻 UI 边框与输入提示符
            if re.match(r"^[›>]\s*$", l):
                continue
            if re.search(r"›\s*Ask\s+Codex\s+to\s+do\s+anything", l, re.IGNORECASE):
                continue
            if re.search(r"gpt-6-astra\s+max\s+·", l, re.IGNORECASE):
                continue
            # 通用过滤底部状态栏 (无论何种模型与窗口状态)
            if re.search(r"(?:Context\s+\d+%\s+left|\b\d+K\s+window\b)", l, re.IGNORECASE):
                continue
            if re.match(r"^[─━\-_=]{5,}$", l):
                continue
            filtered_lines.append(l)

        last_message = ""
        if filtered_lines:
            # 按消息分块 (•, ■, ◦, ›, {"error":, 边框线)
            block_starter = re.compile(r"^\s*(?:[•■◦›]\s*|■|\{\"error\"|[─━\-_=]{5,})")
            blocks = []
            current_block = []

            for line in filtered_lines:
                if block_starter.match(line):
                    if current_block:
                        blocks.append("\n".join(current_block).strip())
                        current_block = []
                current_block.append(line)

            if current_block:
                blocks.append("\n".join(current_block).strip())

            # 取最后一个非用户输入的块 (过滤以 › 开头的用户输入回显)
            for b in reversed(blocks):
                if re.match(r"^\s*›\s*(?:Ask\s+Codex|[\u4e00-\u9fa5\w]+)", b):
                    continue
                last_message = b
                break

            if not last_message and blocks:
                last_message = blocks[-1]

        return ScreenStatus(
            is_attempting=is_attempting,
            attempt_detail=attempt_detail,
            is_ready_prompt=is_ready,
            last_message=last_message,
        )


# ==============================================================================
# 智能规则决策引擎 (Message Decision Engine)
# ==============================================================================
@dataclass
class Decision:
    should_act: bool
    input_text: str = ""
    reason: str = ""
    delay_seconds: float = 0.0
    matched_rule: str = ""


class MessageDecisionEngine:
    def __init__(
        self,
        default_retry_text: str = "继续任务",
        default_choice_text: str = "1",
        rate_limit_delay: float = 5.0,
        custom_rules: Optional[Dict[str, str]] = None,
    ):
        self.default_retry_text = default_retry_text
        self.default_choice_text = default_choice_text
        self.rate_limit_delay = rate_limit_delay
        self.custom_rules = custom_rules or {}

    def decide(self, status: ScreenStatus, session_error: Optional[dict] = None) -> Decision:
        # 闸门 1: 如果 Codex 还在尝试中，绝对不主动发送任何消息！
        if status.is_attempting:
            return Decision(
                should_act=False,
                reason=f"Codex 仍在尝试中 ({status.attempt_detail})，保持等待，不打扰",
            )

        # 闸门 2: 必须处于可输入的空闲提示符状态
        if not status.is_ready_prompt and not session_error:
            return Decision(
                should_act=False,
                reason="当前终端未停留在输入提示符，不触发输入",
            )

        # 获取用于决策的目标文本（优先取屏幕最后消息）
        msg = status.last_message.strip()
        decision = self._match_rules(msg) if msg else Decision(should_act=False, reason="无有效消息")

        # 若屏幕最后消息未匹配到动作，且 session 日志存在未处理的报错，检查 session 报错作为备选
        if not decision.should_act and session_error:
            s_msg = session_error.get("message", "")
            if s_msg and s_msg != msg:
                s_decision = self._match_rules(s_msg)
                if s_decision.should_act:
                    return s_decision

        return decision

    def _match_rules(self, msg: str) -> Decision:
        if not msg:
            return Decision(should_act=False, reason="无有效消息")

        # 0. 优先匹配用户自定义规则
        for pattern, response in self.custom_rules.items():
            if re.search(pattern, msg, re.IGNORECASE):
                return Decision(
                    should_act=True,
                    input_text=response,
                    reason=f"匹配用户自定义规则: [{pattern}]",
                    delay_seconds=1.0,
                    matched_rule="CUSTOM_RULE",
                )

        # 1. 匹配 Rate Limit 最终确认错误 (尝试已结束)
        rate_limit_patterns = [
            r"rate\s*limit\s*exceeded",
            r"exceeded\s*rate\s*limit",
            r"requests\s+to\s+.*\s+have\s+exceeded\s+rate\s+limit",
            r"rate_limit_exceeded",
            r"429\s+Too\s+Many\s+Requests",
        ]
        for p in rate_limit_patterns:
            if re.search(p, msg, re.IGNORECASE):
                return Decision(
                    should_act=True,
                    input_text=self.default_retry_text,
                    reason=f"Codex 尝试结束，最终确认 Rate Limit 错误 ({re.search(p, msg, re.IGNORECASE).group(0)})",
                    delay_seconds=self.rate_limit_delay,
                    matched_rule="RATE_LIMIT_ERROR",
                )

        # 2. 匹配 API 响应异常、400 报错与加密内容失效 (如 bad response status code 400, invalid_encrypted_content)
        api_error_patterns = [
            r"invalid_encrypted_content",
            r"bad\s+response\s+status\s+code(?:\s+\d+)?",
            r"invalid_request_error",
            r"status\s+code\s+400",
            r"■\s*\{[\s\S]*?\"error\"[\s\S]*?\}",
            r'\{[\s\S]*?"error"[\s\S]*?"code"[\s\S]*?"invalid_encrypted_content"',
            r'\{[\s\S]*?"error"[\s\S]*?"message"[\s\S]*?bad\s+response\s+status\s+code',
            r'\{[\s\S]*?"error"[\s\S]*?"type"[\s\S]*?"invalid_request_error"',
        ]
        for p in api_error_patterns:
            if re.search(p, msg, re.IGNORECASE):
                m_req = re.search(r"request\s+id:\s*([0-9a-zA-Z]+)", msg, re.IGNORECASE)
                req_str = f" (request id: {m_req.group(1)})" if m_req else ""
                return Decision(
                    should_act=True,
                    input_text=self.default_retry_text,
                    reason=f"Codex 尝试结束，最终确认 API 400 异常/加密内容错误{req_str} (code: invalid_encrypted_content)",
                    delay_seconds=1.5,
                    matched_rule="API_ERROR",
                )

        # 3. 匹配网络与异常断开错误 (尝试已结束)
        network_error_patterns = [
            r"stream\s+disconnected\s+before\s+completion",
            r"transport\s+error",
            r"network\s+error",
            r"error\s+decoding\s+response\s+body",
            r"connection\s+failed",
            r"error\s+sending\s+request",
            r"connection\s+reset",
            r"timed?\s*out",
            r"conversation\s+interrupted",
            r"tell\s+the\s+model\s+what\s+to\s+do\s+differently",
            r"something\s+went\s+wrong",
            r"internal\s+server\s+error",
            r"server\s+error",
            r"service\s+unavailable",
        ]
        for p in network_error_patterns:
            if re.search(p, msg, re.IGNORECASE):
                return Decision(
                    should_act=True,
                    input_text=self.default_retry_text,
                    reason=f"Codex 尝试结束，最终确认异常错误 ({re.search(p, msg, re.IGNORECASE).group(0)})",
                    delay_seconds=2.0,
                    matched_rule="NETWORK_ERROR",
                )

        # 4. 匹配工具权限审批与命令确认 (Approve Tool / Command)
        approval_patterns = [
            r"approve\s+(app\s+)?tool\s+call\?",
            r"approval\s+required",
            r"approve\s+command\?",
            r"run\s+the\s+tool\s+and\s+continue",
            r"allow\s+and\s+continue",
            r"allow\s+for\s+this\s+session",
        ]
        for p in approval_patterns:
            if re.search(p, msg, re.IGNORECASE):
                return Decision(
                    should_act=True,
                    input_text=self.default_choice_text,
                    reason="最后消息要求权限或命令审批，自动选择第 1 项批准",
                    delay_seconds=0.5,
                    matched_rule="APPROVAL_CHOICE",
                )

        # 5. 匹配多项选择提示 (1) 2) 或 [1] [2] 等)
        has_opt1 = bool(re.search(r"(?:^|\s)(?:1\)|\[1\]|1\.)\s+", msg))
        has_opt2 = bool(re.search(r"(?:^|\s)(?:2\)|\[2\]|2\.)\s+", msg))
        choice_keywords = bool(re.search(r"select\s+an\s+option|choice\s*\[\d+\]|choose\s*\[\d+|请选择", msg, re.IGNORECASE))

        if (has_opt1 and has_opt2) or choice_keywords:
            return Decision(
                should_act=True,
                input_text=self.default_choice_text,
                reason="最后消息出现编号选项菜单，自动选择第 1 项",
                delay_seconds=0.5,
                matched_rule="NUMBERED_CHOICE",
            )

        # 6. 匹配模型提问“是否继续推进”
        continue_question_patterns = [
            r"是否.*?继续",
            r"需要.*?继续",
            r"是否.*?推进",
            r"是否.*?确认",
            r"是否.*?开始",
            r"do\s+you\s+want\s+to\s+continue",
            r"should\s+i\s+continue",
            r"proceed\?",
        ]
        for p in continue_question_patterns:
            if re.search(p, msg, re.IGNORECASE):
                return Decision(
                    should_act=True,
                    input_text="继续",
                    reason="最后消息询问是否继续推进，自动输入“继续”",
                    delay_seconds=1.0,
                    matched_rule="CONFIRM_CONTINUE",
                )

        # 7. 是非选择提示 [y/N]
        if re.search(r"\[y/n\]|\(y/n\)", msg, re.IGNORECASE):
            return Decision(
                should_act=True,
                input_text="y",
                reason="最后消息包含 [y/n] 确认，自动输入 'y'",
                delay_seconds=0.5,
                matched_rule="YES_NO_PROMPT",
            )

        return Decision(should_act=False, reason="最后消息属于正常输出，无需自动干涉")


# ==============================================================================
# 监控调度器 (CodexWatcher)
# ==============================================================================
class CodexWatcher:
    def __init__(
        self,
        pane_id: Optional[int] = None,
        auto_text: str = "继续任务",
        choice_text: str = "1",
        rate_limit_delay: float = 5.0,
        interval: float = 1.5,
        custom_rules: Optional[Dict[str, str]] = None,
        dry_run: bool = False,
    ):
        self.pane_id = pane_id
        self.interval = interval
        self.dry_run = dry_run

        self.engine = MessageDecisionEngine(
            default_retry_text=auto_text,
            default_choice_text=choice_text,
            rate_limit_delay=rate_limit_delay,
            custom_rules=custom_rules,
        )
        self.session_auditor = CodexSessionAuditor()

        self.cooldown_until = 0.0
        self.last_handled_fingerprint = ""

        self.start_time = datetime.now()
        self.total_retries = 0
        self.total_choices = 0

    def resolve_target(self) -> bool:
        if PebrelBackend.is_available():
            if self.pane_id is not None:
                log_info(f"绑定指定的 Pebrel Pane ID: {Color.BOLD}{self.pane_id}{Color.RESET}")
                return True
            panes = PebrelBackend.list_codex_panes()
            if panes:
                self.pane_id = panes[0][0]
                title = panes[0][1]
                log_success(
                    f"已自动发现运行中的 Codex Pane: {Color.BOLD}{self.pane_id}{Color.RESET} ({title})"
                )
                return True

        hwnd = WindowsWindowBackend.find_codex_window()
        if hwnd:
            log_success(f"已发现 Codex Windows 窗口 HWND: {hwnd}")
            return True

        log_warn("未找到活跃的 Pebrel Pane 或窗口，将使用会话文件监听。")
        return True

    def run(self):
        print(f"\n{Color.BOLD}{Color.CYAN}{'='*68}{Color.RESET}")
        print(f"{Color.BOLD}{Color.CYAN}       Codex CLI 最终确认守护系统已启动 (Wait for Attempts Finished){Color.RESET}")
        print(f"{Color.BOLD}{Color.CYAN}{'='*68}{Color.RESET}\n")

        print(f"  • 异常恢复指令 : {Color.GREEN}{Color.BOLD}{self.engine.default_retry_text}{Color.RESET}")
        print(f"  • 选项默认选择 : {Color.GREEN}{Color.BOLD}{self.engine.default_choice_text}{Color.RESET}")
        print(f"  • Rate Limit 冷却等待: {Color.YELLOW}{self.engine.rate_limit_delay} 秒{Color.RESET}")
        print(f"  • 尝试中策略   : {Color.MAGENTA}保持静默等待，绝不打扰重试{Color.RESET}")
        print(f"  • 触发时机     : {Color.CYAN}尝试彻底结束、最终确认停留在输入框时触发{Color.RESET}")
        print(f"  • 检测轮询周期 : {self.interval} 秒")
        print(f"  • 试运行模式(dry-run): {Color.MAGENTA}{self.dry_run}{Color.RESET}")
        print(f"  • 退出快捷键   : {Color.BOLD}Ctrl + C{Color.RESET}\n")

        if not self.resolve_target():
            log_error("未能定位监控目标，退出。")
            return

        log_info("正在实时监测 Codex CLI 运行状态...\n")

        try:
            while True:
                now = time.time()
                if now < self.cooldown_until:
                    remain = int(self.cooldown_until - now)
                    sys.stdout.write(f"\r{Color.DIM}[冷却中] 距离解除还剩 {remain} 秒...{Color.RESET}   ")
                    sys.stdout.flush()
                    time.sleep(1.0)
                    continue

                self._check_and_act()
                time.sleep(self.interval)

        except KeyboardInterrupt:
            self._print_summary()

    def _check_and_act(self):
        screen_text = ""
        if self.pane_id is not None and PebrelBackend.is_available():
            screen_text = PebrelBackend.read_pane_tail(self.pane_id, lines=45)

        # 1. 提取屏幕状态与最后消息
        status = LastMessageExtractor.inspect(screen_text)

        # 2. 会话日志审计 (检查底层真实完成状态)
        s_turn_id, s_err, s_err_completed, s_in_progress = self.session_auditor.inspect_latest_status()

        # 如果会话还在 task_started 阶段，也视为进行中
        if s_in_progress:
            status.is_attempting = True
            if not status.attempt_detail:
                status.attempt_detail = "底层任务进行中 (task_started)"

        # 3. 如果还在尝试中，控制台单行显示进度并直接返回，绝对不发送任何消息！
        if status.is_attempting:
            detail_info = status.attempt_detail[:45]
            sys.stdout.write(
                f"\r{Color.YELLOW}[尝试中]{Color.RESET} {Color.DIM}Codex 正在自动尝试: {detail_info} - 保持等待中...{Color.RESET}      "
            )
            sys.stdout.flush()
            self.last_handled_fingerprint = ""
            return

        # 4. 尝试已彻底结束，传入智能规则引擎进行决策
        unhandled_session_err = None
        if s_err_completed and s_err and s_turn_id != self.session_auditor.last_handled_turn_id:
            unhandled_session_err = s_err

        decision = self.engine.decide(status, session_error=unhandled_session_err)

        if not decision.should_act:
            # 正常空闲
            sys.stdout.write(f"\r{Color.DIM}[状态] 正常空闲 (无待恢复异常){Color.RESET}                              ")
            sys.stdout.flush()
            return

        # 5. 防抖去重：避免同一最终错误重复发送，同时确保连续出现的新报错（不同 request id / turn_id）不被误拦截
        normalized_msg = " ".join(l.strip() for l in status.last_message.strip().splitlines() if l.strip())
        fingerprint = f"{decision.matched_rule}:{s_turn_id or ''}:{normalized_msg[:120]}"
        if fingerprint == self.last_handled_fingerprint:
            return

        # 6. 最终确认触发动作！
        display_msg = status.last_message if status.last_message else (unhandled_session_err.get("message", "") if unhandled_session_err else "")
        self._execute_decision(display_msg, decision, fingerprint)

    def _execute_decision(self, last_msg: str, decision: Decision, fingerprint: str):
        print()  # 换行打印详细卡片
        print(f"{Color.CYAN}┌────────────────────────────────────────────────────────────┐{Color.RESET}")
        print(f"{Color.CYAN}│  [检测到 Codex 尝试已结束 - 最终确认消息]                 │{Color.RESET}")
        lines = last_msg.strip().splitlines() if last_msg else [decision.reason]
        for line in lines[:6]:
            truncated = line[:56]
            print(f"{Color.CYAN}│{Color.RESET}  {Color.WHITE}{truncated:<56}{Color.RESET}{Color.CYAN}│{Color.RESET}")
        if len(lines) > 6:
            print(f"{Color.CYAN}│  ... (余下行已省略)                                        │{Color.RESET}")
        print(f"{Color.CYAN}└────────────────────────────────────────────────────────────┘{Color.RESET}")

        log_warn(f"决策原因: {Color.YELLOW}{decision.reason}{Color.RESET}")

        if decision.delay_seconds > 0:
            log_info(f"等待冷却缓冲 {Color.YELLOW}{decision.delay_seconds}{Color.RESET} 秒...")
            if not self.dry_run:
                time.sleep(decision.delay_seconds)

        log_action(f"根据最终确认结果，自动输入: {Color.GREEN}{Color.BOLD}{decision.input_text}{Color.RESET}")

        if self.dry_run:
            log_info("[Dry-Run 试运行] 跳过实际发送。")
        else:
            success = self._send_input(decision.input_text)
            if success:
                if "CHOICE" in decision.matched_rule:
                    self.total_choices += 1
                else:
                    self.total_retries += 1
                log_success(f"已成功发送输入: “{decision.input_text}”！")
            else:
                log_error("发送失败，请检查目标终端或窗口连接。")

        self.last_handled_fingerprint = fingerprint
        self.cooldown_until = time.time() + 4.0

        # 更新 session auditor 的处理标记
        s_turn_id, _, _, _ = self.session_auditor.inspect_latest_status()
        if s_turn_id:
            self.session_auditor.last_handled_turn_id = s_turn_id

    def _send_input(self, text: str) -> bool:
        if self.pane_id is not None and PebrelBackend.is_available():
            return PebrelBackend.send_to_pane(self.pane_id, text)

        hwnd = WindowsWindowBackend.find_codex_window()
        if hwnd:
            return WindowsWindowBackend.send_keys(hwnd, text)

        return False

    def _print_summary(self):
        print(f"\n\n{Color.BOLD}{Color.CYAN}{'='*68}{Color.RESET}")
        print(f"{Color.BOLD}{Color.CYAN}                 Codex Watcher 运行报表{Color.RESET}")
        print(f"{Color.BOLD}{Color.CYAN}{'='*68}{Color.RESET}")
        elapsed = datetime.now() - self.start_time
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{hours}小时 {minutes}分 {seconds}秒"

        print(f"  • 守护时长     : {duration_str}")
        print(f"  • 自动恢复异常 : {Color.GREEN}{self.total_retries}{Color.RESET} 次")
        print(f"  • 自动处理选择 : {Color.GREEN}{self.total_choices}{Color.RESET} 次")
        print(f"{Color.BOLD}{Color.CYAN}{'='*68}{Color.RESET}\n")
        log_info("监控守护已停止。")


# ==============================================================================
# 命令行入口
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Codex CLI 智能消息驱动自动守护工具 (最终确认后自动输入)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python codex_watcher.py                     # 自动监测并在重试结束后自动处理
  python codex_watcher.py --dry-run           # 试运行模式，仅打印决策，不发送输入
  python codex_watcher.py --pane 5            # 指定监控 Pebrel Pane 5
  python codex_watcher.py --text "继续任务"   # 遇到最终错误时输入的内容 (默认: 继续任务)
  python codex_watcher.py --choice "1"        # 遇到选项提示时输入的内容 (默认: 1)
        """,
    )

    parser.add_argument(
        "--pane",
        type=int,
        default=None,
        help="显式指定 Pebrel Pane ID（默认自动寻找正在运行 Codex 的 Pane）",
    )
    parser.add_argument(
        "--text",
        type=str,
        default="继续任务",
        help="遇到最终错误后自动输入的文字（默认: '继续任务'）",
    )
    parser.add_argument(
        "--choice",
        type=str,
        default="1",
        help="出现选项或审批提示时自动输入的选项（默认: '1'）",
    )
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        default=5.0,
        help="命中 Rate Limit 错误时的等待冷却秒数（默认: 5.0 秒）",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.5,
        help="状态与消息检测轮询周期秒数（默认: 1.5 秒）",
    )
    parser.add_argument(
        "--rule",
        action="append",
        dest="rules",
        default=[],
        help="添加自定义匹配规则，格式为 '关键词正则:自动输入内容'，可多次使用",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行模式，仅检测最后消息与决策，不向终端发送任何实际输入",
    )

    args = parser.parse_args()

    custom_rules_map = {}
    for r in args.rules:
        if ":" in r:
            k, v = r.split(":", 1)
            custom_rules_map[k.strip()] = v.strip()

    watcher = CodexWatcher(
        pane_id=args.pane,
        auto_text=args.text,
        choice_text=args.choice,
        rate_limit_delay=args.rate_limit_delay,
        interval=args.interval,
        custom_rules=custom_rules_map,
        dry_run=args.dry_run,
    )
    watcher.run()


if __name__ == "__main__":
    main()
