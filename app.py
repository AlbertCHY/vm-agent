"""Aria Automation Cloud Ops Agent — 互動式 CLI 入口

用法：
    python app.py                     # 互動對話模式 (Supervisor ReAct Agent)
    python app.py --batch             # 批次處理 ./tickets/ 下所有工單
    python app.py --file SRA001.txt   # 只處理指定工單檔案
"""
import argparse
import json
import sys
import time
import uuid
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import config
from clients.aria_client import aria_client
from subgraphs.vm_provisioning import set_review_handler
from supervisor import build_supervisor_agent, SUPERVISOR_TOOLS
from tools.ticket_tools import list_ticket_files

console = Console()

RECURSION_LIMIT = 60


# ════════════════════════════════════════════════════════════════
# 1. 人工審核介面 (HITL) — 註冊給 VM Provisioning 子圖使用
# ════════════════════════════════════════════════════════════════

def display_spec_table(spec: dict[str, Any]):
    """使用 Rich Table 清晰呈現 16 項規格參數供人工審核 (包含藍圖名稱與專案名稱)"""
    table = Table(title="📋 待審核的虛擬機規格參數清單", show_header=True, header_style="bold magenta")
    table.add_column("參數名稱 (Key)", style="cyan", width=20)
    table.add_column("中文說明", style="dim", width=22)
    table.add_column("設定值 (Value)", style="bold green", width=42)

    # 取得人類易讀的藍圖與專案名稱
    bp_name = (
        spec.get("blueprint_name")
        or aria_client.get_blueprint_name(spec.get("blueprint_id"))
        or spec.get("blueprint_id")
        or "[red]<未填寫>[/red]"
    )
    proj_name = (
        spec.get("project_name")
        or aria_client.get_project_name(spec.get("project_id"))
        or spec.get("project_id")
        or "[red]<未填寫>[/red]"
    )

    field_meta = [
        ("deployment_name", "部署名稱", spec.get("deployment_name")),
        ("blueprint", "藍圖範本名稱", bp_name),
        ("project", "專案名稱", proj_name),
        ("cpu_count", "CPU 核心數 (vCore)", spec.get("cpu_count")),
        ("memory_size", "記憶體容量 (MB)", spec.get("memory_size")),
        ("os_image", "作業系統映像檔", spec.get("os_image")),
        ("sg", "安全群組 (SG)", spec.get("sg")),
        ("network_segment", "網路網段 (Network)", spec.get("network_segment")),
        ("ip_mode", "IP 模式 (DHCP/STATIC)", spec.get("ip_mode")),
        ("cluster", "運算集群 (Cluster)", spec.get("cluster")),
        ("datastore", "存儲位置 (Datastore)", spec.get("datastore")),
        ("case_no", "需求單號 (CaseNo)", spec.get("case_no")),
        ("ap_owner", "應用負責人 (APOwner)", spec.get("ap_owner")),
        ("os_owner", "系統負責人 (OSOwner)", spec.get("os_owner")),
        ("ap_unit", "應用單位 (APUnit)", spec.get("ap_unit")),
        ("description", "用途描述 (Description)", spec.get("description")),
    ]

    for key, desc, val in field_meta:
        val_str = str(val) if val is not None else "[red]<未填寫>[/red]"
        table.add_row(key, desc, val_str)

    console.print(table)


def _parse_edit_input(edit_input: str) -> dict[str, Any]:
    """解析 key=value, key2=value2 形式的修改指令"""
    updated: dict[str, Any] = {}
    for item in edit_input.split(","):
        if "=" not in item:
            continue
        k, v = item.strip().split("=", 1)
        k, v = k.strip(), v.strip()
        # 數值轉換
        if k in ["cpu_count", "memory_size"]:
            try:
                v = int(v)
            except ValueError:
                pass
        # 若修改 os_image，自動比對 image-profiles 中的名稱
        elif k == "os_image":
            matched = aria_client.match_os_image(v)
            if matched:
                v = matched
        # 若修改專案
        elif k in ["project", "project_name"]:
            resolved = aria_client.resolve_project(v)
            if resolved:
                updated["project_id"] = resolved["id"]
                updated["project_name"] = resolved["name"]
            else:
                updated["project_name"] = v
        # 若修改藍圖
        elif k in ["blueprint", "blueprint_name"]:
            resolved = aria_client.resolve_blueprint(v)
            if resolved:
                updated["blueprint_id"] = resolved["id"]
                updated["blueprint_name"] = resolved["name"]
            else:
                updated["blueprint_name"] = v
        updated[k] = v
    return updated


def handle_human_review(interrupt_data: dict[str, Any]) -> dict[str, Any]:
    """處理人工審核互動介面 (由 deploy_vm_pipeline 在子圖 interrupt 時回呼)"""
    spec = interrupt_data.get("spec", {})
    console.print("\n[bold yellow]⚠️  觸發人工審查機制 (Human-In-The-Loop Interrupt)[/bold yellow]")
    display_spec_table(spec)

    console.print("\n[bold]請選擇審核操作：[/bold]")
    console.print("[green][A][/green] 確認無誤，核准並送出部署 (Approve)")
    console.print("[yellow][E][/yellow] 修改指定參數後送出 (Edit & Approve)")
    console.print("[red][R][/red] 給予修改指示並退回 (Re-inference with feedback)")
    console.print("[magenta][S][/magenta] 略過/取消此台 VM (Skip)")

    while True:
        choice = Prompt.ask(
            "\n請輸入操作選項", choices=["a", "A", "e", "E", "r", "R", "s", "S"], default="A"
        ).upper()

        if choice == "A":
            console.print("[green]✔ 已核准部署規格！正在送出 Aria Automation 請求...[/green]")
            return {"approved": True}

        if choice == "E":
            console.print("[cyan]請輸入欲修改的參數與數值 (格式: key=value，多個以逗號分隔)[/cyan]")
            console.print("[dim]範例：cpu_count=8, memory_size=16384, project=hqit-HCMBI, ip_mode=STATIC[/dim]")
            console.print("修改完的結果將直接送出")
            updated = _parse_edit_input(Prompt.ask("修改內容"))
            console.print(f"[green]✔ 已更新參數: {updated}，正在送出請求...[/green]")
            return {"approved": True, "updated_params": updated}

        if choice == "R":
            feedback = Prompt.ask("請輸入退回原因或調整需求")
            console.print(f"[red]✖ 已退回，反饋意見: {feedback}[/red]")
            return {"approved": False, "user_feedback": feedback}

        if choice == "S":
            reason = Prompt.ask("請輸入取消原因 (可留空)", default="")
            console.print("[magenta]⏭ 已取消此台 VM 部署[/magenta]")
            return {"approved": False, "aborted": True, "user_feedback": reason}


# ════════════════════════════════════════════════════════════════
# 2. Supervisor 串流輸出呈現
# ════════════════════════════════════════════════════════════════

def _msg_text(msg: Any) -> str:
    """相容字串或 content blocks (Gemini 可能回傳 list[dict])"""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


def display_ticket_results(raw: str):
    """將 scan_and_parse_tickets 的 JSON 結果渲染為表格"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        console.print(f"[dim]{escape(str(raw)[:500])}[/dim]")
        return

    for t in data.get("tickets", []):
        if "error" in t:
            console.print(f"[red]📄 {escape(str(t.get('file')))}: {escape(str(t['error']))}[/red]")
            continue
        table = Table(title=f"📄 工單解析結果：{escape(str(t.get('file')))}", show_header=False, title_justify="left")
        table.add_column("項目", style="cyan", width=14)
        table.add_column("內容", style="white", width=80)
        table.add_row("工單單號", escape(str(t.get("ticket_id"))))
        table.add_row("工單類型", f"[bold yellow]{escape(str(t.get('ticket_type')))}[/bold yellow]")
        table.add_row("申請人 / 單位", escape(f"{t.get('applicant')} / {t.get('department')}"))
        table.add_row("需求摘要", escape(str(t.get("summary"))))
        table.add_row("簽核意見", escape(str(t.get("approval_notes") or "無")))
        for idx, item in enumerate(t.get("action_items", []), 1):
            table.add_row(f"動作 #{idx}", escape(str(item.get("description", item))))
        console.print(table)


def _print_tool_result(msg: ToolMessage):
    name = msg.name or "tool"
    text = _msg_text(msg)
    if name == "scan_and_parse_tickets":
        display_ticket_results(text)
        return
    try:
        preview = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        preview = " ".join(text.split())
    style = "red" if text.lstrip().startswith("❌") else "dim"
    console.print(f"[{style}]  ↳ {name}: {escape(preview[:160])}{'…' if len(preview) > 160 else ''}[/{style}]", highlight=False)


def run_supervisor_turn(agent, thread_id: str, user_text: str) -> str:
    """送出一輪使用者輸入給 Supervisor，邊串流邊顯示工具呼叫與結果，回傳最終 AI 回覆文字"""
    run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    final_text = ""
    try:
        for update in agent.stream({"messages": [HumanMessage(content=user_text)]},
                                   config=run_config, stream_mode="updates"):
            for node_output in update.values():
                if not isinstance(node_output, dict):
                    continue
                for msg in node_output.get("messages", []) or []:
                    if isinstance(msg, AIMessage):
                        for tc in msg.tool_calls or []:
                            args = json.dumps(tc.get("args", {}), ensure_ascii=False)
                            console.print(f"[cyan]🔧 {tc.get('name')}[/cyan] [dim]{escape(args[:200])}[/dim]", highlight=False)
                        text = _msg_text(msg).strip()
                        if text and not msg.tool_calls:
                            final_text = text
                    elif isinstance(msg, ToolMessage):
                        _print_tool_result(msg)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中斷本輪執行。[/yellow]")
        return final_text
    except Exception as e:
        console.print(f"[bold red]Supervisor 執行發生錯誤: {type(e).__name__}: {e}[/bold red]")
        return final_text

    if final_text:
        console.print(Panel(Markdown(final_text), title="🤖 Supervisor", border_style="green"))
    return final_text


# ════════════════════════════════════════════════════════════════
# 3. 互動模式與批次模式
# ════════════════════════════════════════════════════════════════

HELP_TEXT = (
    "[bold]指令[/bold]\n"
    "  [cyan]/tickets[/cyan]   列出 tickets 資料夾中的工單檔案\n"
    "  [cyan]/batch[/cyan]     批次處理資料夾中所有工單\n"
    "  [cyan]/paste[/cyan]     貼上整份工單純文字 (存入 tickets 資料夾後交給 Agent 處理)\n"
    "  [cyan]/new[/cyan]       開啟新的對話 Session\n"
    "  [cyan]/help[/cyan]      顯示說明\n"
    "  [cyan]exit[/cyan]       離開程式\n\n"
    "[bold]範例問句[/bold]\n"
    "  [dim]• 10.51.8.239 是哪台機器？同一個 deployment 還有哪些資源？[/dim]\n"
    "  [dim]• 列出 hqit-alberttest 專案的所有 deployment[/dim]\n"
    "  [dim]• 幫我看 10.51.8.239 的磁碟空間，特別是 /data[/dim]\n"
    "  [dim]• 處理工單資料夾裡的工單 / 處理 SRA26091700001.txt[/dim]\n"
    "  [dim]• 在 hqit-alberttest 開一台 Ubuntu 24 VM，4核16G，單號 N/A，AP負責人 Albert，單位 HQIT，用途是測試agent[/dim]"
)


def display_welcome():
    tool_names = ", ".join(t.name for t in SUPERVISOR_TOOLS)
    welcome_text = (
        "[bold cyan]Aria Automation Cloud Ops Agent[/bold cyan]\n"
        "[white]架構：Supervisor (ReAct) + Tools + VM Provisioning Subgraph (HITL) + Local Ollama 工單解析[/white]\n"
        f"[dim]已載入工具：{tool_names}[/dim]\n"
        f"[dim]工單資料夾：{config.TICKETS_DIR}[/dim]"
    )
    console.print(Panel(welcome_text, title="🤖 AI Agent Ready", border_style="cyan"))


def show_ticket_files():
    files = list_ticket_files()
    if not files:
        console.print(f"[yellow]📭 {config.TICKETS_DIR} 中沒有工單檔案 ({'/'.join(config.TICKET_EXTENSIONS)})。[/yellow]")
        return
    table = Table(title="📂 工單檔案", show_header=True, header_style="bold cyan")
    table.add_column("#", width=4)
    table.add_column("檔名", style="white")
    table.add_column("大小", justify="right")
    table.add_column("修改時間", style="dim")
    for i, p in enumerate(files, 1):
        st = p.stat()
        table.add_row(str(i), p.name, f"{st.st_size:,} B", time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))
    console.print(table)


def paste_ticket() -> str | None:
    """讀取多行貼上的工單文字並存成 tickets/ 下的檔案，回傳檔名"""
    console.print(Panel(
        "[bold yellow]請貼上整份工單純文字內容。[/bold yellow]\n"
        "[dim]貼上完成後，在新的一行輸入 [bold green]END[/bold green] 送出；輸入 [bold red]CANCEL[/bold red] 取消。[/dim]",
        title="📋 工單貼上", border_style="yellow"
    ))
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        if line.strip().upper() == "CANCEL":
            console.print("[yellow]已取消。[/yellow]")
            return None
        lines.append(line)

    raw_text = "\n".join(lines).strip()
    if not raw_text:
        console.print("[red]⚠️ 輸入內容為空。[/red]")
        return None

    config.TICKETS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"pasted-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    (config.TICKETS_DIR / filename).write_text(raw_text, encoding="utf-8")
    console.print(f"[green]✔ 已儲存為 {config.TICKETS_DIR / filename}[/green]")
    return filename


def process_ticket_file(agent, filename: str, interactive_followup: bool = True):
    """以獨立 Session 交給 Supervisor 處理單一工單，並允許使用者就該工單繼續對話"""
    thread_id = f"ticket-{filename}-{uuid.uuid4().hex[:6]}"
    console.print(f"\n[bold magenta]{'═' * 64}[/bold magenta]")
    console.print(f"[bold cyan]▶ 處理工單：{filename}[/bold cyan]  [dim](session {thread_id})[/dim]")
    run_supervisor_turn(agent, thread_id, f"請處理工單檔案「{filename}」：先解析，再依工單類型調度後續流程。")

    if not interactive_followup:
        return
    while True:
        reply = Prompt.ask("[bold blue]回覆 Agent[/bold blue] [dim](直接 Enter 結束此工單)[/dim]", default="").strip()
        if not reply:
            break
        run_supervisor_turn(agent, thread_id, reply)


def run_batch(agent, filenames: list[str] | None = None, interactive_followup: bool = True):
    files = filenames or [p.name for p in list_ticket_files()]
    if not files:
        show_ticket_files()
        return
    console.print(f"[bold]📦 批次處理 {len(files)} 張工單：{', '.join(files)}[/bold]")
    for idx, name in enumerate(files, 1):
        console.print(f"[dim]({idx}/{len(files)})[/dim]")
        try:
            process_ticket_file(agent, name, interactive_followup=interactive_followup)
        except KeyboardInterrupt:
            console.print("\n[yellow]已中斷批次處理。[/yellow]")
            return
    console.print("\n[bold green]✔ 批次工單處理結束！[/bold green]")


def run_interactive(agent):
    display_welcome()
    console.print(Panel(HELP_TEXT, title="💬 對話模式", border_style="cyan"))
    thread_id = f"chat-{uuid.uuid4().hex[:6]}"
    console.print(f"[dim]對話 Session ID: {thread_id}[/dim]\n")

    while True:
        try:
            user_input = Prompt.ask("[bold blue]使用者[/bold blue]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[cyan]感謝使用，再見！[/cyan]")
            break
        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("exit", "quit", "/exit", "/quit", "q"):
            console.print("[cyan]感謝使用，再見！[/cyan]")
            break
        if cmd == "/help":
            console.print(Panel(HELP_TEXT, border_style="cyan"))
        elif cmd == "/tickets":
            show_ticket_files()
        elif cmd == "/batch":
            run_batch(agent)
        elif cmd == "/paste":
            filename = paste_ticket()
            if filename:
                process_ticket_file(agent, filename)
        elif cmd == "/new":
            thread_id = f"chat-{uuid.uuid4().hex[:6]}"
            console.print(f"[dim]已開啟新對話 Session ID: {thread_id}[/dim]")
        else:
            run_supervisor_turn(agent, thread_id, user_input)


def main():
    parser = argparse.ArgumentParser(description="Aria Automation Cloud Ops Agent CLI")
    parser.add_argument("--batch", action="store_true", help="批次處理 tickets 資料夾中的所有工單後結束")
    parser.add_argument("--file", action="append", metavar="NAME", help="只處理指定工單檔名 (可重複指定)")
    parser.add_argument("--no-followup", action="store_true", help="批次模式下不進入每張工單的追問對話")
    args = parser.parse_args()

    # 將 Rich CLI 審核介面註冊給 VM Provisioning 子圖
    set_review_handler(handle_human_review)
    config.TICKETS_DIR.mkdir(parents=True, exist_ok=True)

    agent = build_supervisor_agent()

    if args.batch or args.file:
        display_welcome()
        run_batch(agent, filenames=args.file, interactive_followup=not args.no_followup)
        return

    run_interactive(agent)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]程式已中斷[/yellow]")
        sys.exit(130)
