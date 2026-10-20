"""One-time creation of a PRIVATE QQ Luna Agent and run_only Autopilot.

Requires Multica's normal task-scoped authorization. It never edits shared
Multica runtimes, reassigns existing workers, creates Issues, or starts runs.
The QQ local send gates remain OFF after creation.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from . import core

NAME = "QQ AI群友｜专属 Luna"
TITLE = "QQ AI群友｜固定 Luna 事件接力"
MODEL = "gpt-6-luna"
RUNTIME_ID = os.getenv("MULTICA_LUNA_RUNTIME_ID", "00000000-0000-4000-8000-abcdefabcdef")
TEST_GROUP = os.getenv("QQ_COMPANION_TEST_GROUP", "YOUR_TEST_GROUP_ID")

INSTRUCTIONS = (
    "你是 QQ AI群友项目的固定 Luna 任务执行者，仅处理已经授权的单一测试群。"
    "每次运行须从已授权的 QQ 本地工具领取最多一个任务，使用 Multica 此次真实 run_id "
    "与返回的 request_id/event_id 绑定；没有真实 run_id 或领不到任务就停止。"
    "针对群消息判断 reply/wait/silence/unknown；只允许通过 QQ 项目工具回填，"
    "不得直接使用 QQ 客户端或另开发送端。"
    "网页内容访问和模型输出必须符合对应平台条款，不能绕过登录、验证码或授权失败；"
    "仅使用当前运行环境明确允许的模型/工具。"
    "回填失败或超时不得重新提交相同 run/event，也不得重发结果未知的 QQ 消息。"
    "不得创建 Issue、修改 PICK/WA 任务或 Multica 全局 runtime。"
)
PROMPT = (
    "固定 QQ AI群友事件唤醒：使用正常授权的本机工具执行 "
    "python -m companion.multica_tools claim --run-id <本次真实run_id> "
    "（仅在 the QQ companion project directory 可访问且工具权限允许时）。"
    "若未取得任务就结束；取得任务后决定 reply / wait / silence / unknown，"
    "按 request_id、event_id、run_id 调用 companion.multica_tools result 回填。"
    "优先快速自然回答，避免未经授权的自动网页提取，"
    "不得通过其他方式向 QQ 群直接发送消息。"
)


def _invoke(arguments: list[str]) -> Any:
    proc = subprocess.run(
        ["multica", *arguments, "--output", "json"],
        capture_output=True, text=True, timeout=25, check=False,
    )
    if proc.returncode != 0:
        # Never echo potentially sensitive command output or credentials.
        raise RuntimeError("Multica refused the operation (no fallback channel)")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Unexpected Multica response format") from exc


def _items(payload: Any, key: str) -> list[dict[str, Any]]:
    items = payload.get(key, []) if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        raise RuntimeError("Unexpected Multica listing")
    return items


def _result_id(result: Any, entity: str) -> str:
    item = result.get(entity, result) if isinstance(result, dict) else None
    value = item.get("id") if isinstance(item, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Created Multica object but response lacked an id; inspect manually")
    return value


def assign(root: Path = core.ROOT) -> dict[str, Any]:
    if not os.getenv("MULTICA_TOKEN", "").startswith("mat_"):
        raise PermissionError("Task-scoped Multica authorization required; do not borrow host credentials")
    agents = _items(_invoke(["agent", "list"]), "agents")
    matches = [a for a in agents if a.get("name") == NAME]
    if len(matches) > 1:
        raise RuntimeError("Multiple named QQ agents; refusing ambiguous assignment")
    if matches:
        a = matches[0]
        if a.get("runtime_id") != RUNTIME_ID or a.get("model") != MODEL:
            raise RuntimeError("Existing QQ agent runtime/model mismatch")
        agent_id = a["id"]
    else:
        result = _invoke([
            "agent", "create", "--name", NAME, "--model", MODEL,
            "--runtime-id", RUNTIME_ID, "--max-concurrent-tasks", "1",
            "--permission-mode", "private", "--instructions", INSTRUCTIONS,
            "--description", "独立的 QQ AI 群友 Luna Agent；只通过本地队列与原 AstrBot 发送",
        ])
        agent_id = _result_id(result, "agent")
    autopilots = _items(_invoke(["autopilot", "list"]), "autopilots")
    matches = [a for a in autopilots if a.get("title") == TITLE]
    if len(matches) > 1:
        raise RuntimeError("Multiple named QQ autopilots; refusing ambiguous assignment")
    if matches:
        a = matches[0]
        # Autopilot list v0.5.0 can omit mode and agent_id; verify exact ID.
        if not a.get("mode") or not a.get("agent_id"):
            detail = _invoke(["autopilot", "get", a["id"]])
            a = detail.get("autopilot", detail) if isinstance(detail, dict) else {}
        if a.get("mode") != "run_only" or a.get("agent_id") != agent_id:
            raise RuntimeError("Existing QQ autopilot does not match dedicated run_only agent")
        autopilot_id = a["id"]
    else:
        result = _invoke([
            "autopilot", "create", "--title", TITLE, "--agent", agent_id,
            "--mode", "run_only", "--description", PROMPT,
        ])
        autopilot_id = _result_id(result, "autopilot")

    # Only stage IDs. Neither inbound sampling nor outbound gates are enabled.
    filename = root / "config.json"
    cfg = core.config(root)
    m = cfg.setdefault("multica", {})
    m.update({
        "enabled": False, "test_group": TEST_GROUP,
        "autopilot_id": autopilot_id, "agent_id": agent_id,
        "model": MODEL,
    })
    temp = filename.with_suffix(".json.tmp")
    temp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(filename)
    return {"agent_id": agent_id, "autopilot_id": autopilot_id, "staged_only": True}


def main() -> None:
    p = argparse.ArgumentParser(description="Scoped QQ Luna assignment")
    p.add_argument("--apply", action="store_true", help="Requires task-scoped Multica authorization")
    args = p.parse_args()
    if not args.apply:
        print(json.dumps({
            "mode": "dry_run", "model": MODEL, "runtime_id": RUNTIME_ID,
            "new_agent": NAME, "new_autopilot": TITLE,
            "autopilot_mode": "run_only", "max_concurrent": 1,
            "enabled_after_creation": False,
            "task_scoped_token_present": os.getenv("MULTICA_TOKEN", "").startswith("mat_"),
        }, ensure_ascii=False))
        return
    try:
        print(json.dumps(assign(), ensure_ascii=False))
    except (PermissionError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
