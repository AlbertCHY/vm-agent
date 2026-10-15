import uuid
import sys
from typing import Any
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import Command

from agent import build_agent_graph
from preprocessor import preprocess_ticket
from ollama_parser import ollama_parser

console = Console()

def display_welcome():
    welcome_text = (
        "[bold cyan]Aria Automation AI Agent 虛擬機自動化部署平台[/bold cyan]\n"
        "[white]架構：StateGraph + Pydantic + Structured Output + Interrupt + Command(resume) + Local Ollama[/white]\n"
        "[dim]支援 [1] 完整工單文字貼上自動預處理與拆解  |  [2] 單筆對話輸入[/dim]"
    )
    console.print(Panel(welcome_text, title="🤖 AI Agent Ready", border_style="cyan"))

def display_spec_table(spec: dict[str, Any]):
    """使用 Rich Table 清晰呈現 16 項規格參數供人工審核 (包含藍圖名稱與專案名稱)"""
    from tools import aria_client

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

def handle_human_review(interrupt_data: dict[str, Any]) -> dict[str, Any]:
    """處理人工審核互動介面"""
    spec = interrupt_data.get("spec", {})
    console.print("\n[bold yellow]⚠️  觸發人工審查機制 (Human-In-The-Loop Interrupt)[/bold yellow]")
    display_spec_table(spec)

    console.print("\n[bold]請選擇審核操作：[/bold]")
    console.print("[green][A][/green] 確認無誤，核准並送出部署 (Approve)")
    console.print("[yellow][E][/yellow] 修改指定參數後送出 (Edit & Approve)")
    console.print("[red][R][/red] 給予修改指示並退回 (Re-inference with feedback)")

    while True:
        choice = Prompt.ask("\n請輸入操作選項", choices=["a", "A", "e", "E", "r", "R"], default="A").upper()

        if choice == "A":
            console.print("[green]✔ 已核准部署規格！正在送出 Aria Automation 請求...[/green]")
            return {"approved": True}

        elif choice == "E":
            console.print("[cyan]請輸入欲修改的參數與數值 (格式: key=value，多個以逗號分隔)[/cyan]")
            console.print("[dim]範例：cpu_count=8, memory_size=16384, project=hqit-HCMBI, ip_mode=STATIC[/dim]")
            console.print("修改完的結果將直接送出")
            edit_input = Prompt.ask("修改內容")
            updated = {}
            for item in edit_input.split(","):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    # 數值轉換
                    if k in ["cpu_count", "memory_size"]:
                        try:
                            v = int(v)
                        except ValueError:
                            pass
                    # 若修改 os_image，自動比對 image-profiles 中的名稱
                    elif k == "os_image":
                        from tools import aria_client
                        matched = aria_client.match_os_image(v)
                        if matched:
                            v = matched
                    # 若修改專案
                    elif k in ["project", "project_name"]:
                        from tools import aria_client
                        resolved = aria_client.resolve_project(v)
                        if resolved:
                            updated["project_id"] = resolved["id"]
                            updated["project_name"] = resolved["name"]
                        else:
                            updated["project_name"] = v
                    # 若修改藍圖
                    elif k in ["blueprint", "blueprint_name"]:
                        from tools import aria_client
                        resolved = aria_client.resolve_blueprint(v)
                        if resolved:
                            updated["blueprint_id"] = resolved["id"]
                            updated["blueprint_name"] = resolved["name"]
                        else:
                            updated["blueprint_name"] = v
                    updated[k] = v

            console.print(f"[green]✔ 已更新參數: {updated}，正在送出請求...[/green]")
            return {"approved": True, "updated_params": updated}

        elif choice == "R":
            feedback = Prompt.ask("請輸入退回原因或調整需求")
            console.print(f"[red]✖ 已退回，反饋意見: {feedback}[/red]")
            return {"approved": False, "user_feedback": feedback}

def run_vm_deployment_flow(user_input: str, agent_graph, thread_id: str) -> bool:
    """執行單一 VM 的 LangGraph 部署流程 (規格抽取 -> 完整性檢查 -> 人工中斷審核 -> 送出請求)"""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        events = agent_graph.stream(
            {"messages": [HumanMessage(content=user_input)]},
            config=config
        )

        for event in events:
            for node_name, node_output in event.items():
                if node_name == "__interrupt__":
                    continue
                if isinstance(node_output, dict) and "messages" in node_output:
                    for msg in node_output["messages"]:
                        if isinstance(msg, AIMessage):
                            console.print(f"\n[bold green]AI Agent:[/bold green]\n{msg.content}\n")

        # 檢查是否觸發 interrupt (使用 while 迴圈支援審查退回後的多次修正與重新審核)
        state = agent_graph.get_state(config)
        while state.tasks and state.tasks[0].interrupts:
            interrupt_item = state.tasks[0].interrupts[0]
            interrupt_val = interrupt_item.value

            # 進入人工審查互動
            review_payload = handle_human_review(interrupt_val)

            # 透過 Command(resume=...) 喚醒 Graph
            resume_events = agent_graph.stream(
                Command(resume=review_payload),
                config=config
            )

            for event in resume_events:
                for node_name, node_output in event.items():
                    if isinstance(node_output, dict) and "messages" in node_output:
                        for msg in node_output["messages"]:
                            if isinstance(msg, AIMessage):
                                console.print(f"\n[bold green]AI Agent:[/bold green]\n{msg.content}\n")

            state = agent_graph.get_state(config)

        final_state = agent_graph.get_state(config)
        exec_res = final_state.values.get("execution_result") if final_state.values else None
        if exec_res and exec_res.get("success"):
            return True
        return False
    except Exception as e:
        console.print(f"[bold red]執行發生錯誤: {e}[/bold red]")
        return False

def display_ticket_summary(ticket_info: dict[str, Any]):
    """使用 Rich Table 清晰呈現預處理後的工單結構化摘要"""
    table = Table(title="📄 申請工單結構化預處理摘要", show_header=True, header_style="bold cyan")
    table.add_column("項目", style="cyan", width=18)
    table.add_column("解析內容", style="bold white", width=58)

    table.add_row("工單單號", ticket_info.get("單號", "未提供"))
    applicant_str = f"{ticket_info.get('申請人', '未提供')} (工號: {ticket_info.get('申請人工號', '未提供')}, 單位: {ticket_info.get('申請單位', '未提供')})"
    table.add_row("申請人資訊", applicant_str)
    table.add_row("專案名稱", ticket_info.get("專案名稱", "未提供"))
    table.add_row("專案背景", ticket_info.get("專案背景", "無"))

    comments = ticket_info.get("審核覆寫意見", [])
    comments_str = "\n".join(f"• {c}" for c in comments) if comments else "無"
    table.add_row("有效簽核覆寫意見", comments_str)

    res = ticket_info.get("資源總計", {})
    res_str = f"VM: {res.get('VM總數', 1)} 台 | vCore: {res.get('vCore總數', 0)} | RAM: {res.get('Memory總數GB', 0)} GB | SSD: {res.get('SSD總數GB', 0)} GB | HDD: {res.get('HDD總數GB', 0)} GB"
    table.add_row("資源總計", res_str)

    console.print(table)

def display_parsed_vms(vms: list[str]):
    """使用 Rich Table 呈現拆解後的每台 VM 規格描述清單"""
    table = Table(title="🤖 本地 Ollama 拆解之 VM 規格清單", show_header=True, header_style="bold green")
    table.add_column("項次", style="cyan", width=6)
    table.add_column("VM 規格需求描述", style="white", width=72)

    for idx, desc in enumerate(vms, 1):
        table.add_row(f"#{idx}", desc)

    console.print(table)

def handle_ticket_mode(agent_graph):
    """模式 [1]: 工單文字貼上與本地 Ollama 模型處理流程"""
    console.print(Panel(
        "[bold yellow]請貼上整份申請工單純文字內容。[/bold yellow]\n"
        "[dim]貼上完成後，請在新的一行輸入 [bold green]END[/bold green] 並按 Enter 送出；若要取消請輸入 [bold red]CANCEL[/bold red]：[/dim]",
        title="📋 模式 [1] 工單純文字貼上模式",
        border_style="yellow"
    ))

    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        if line.strip().upper() == "CANCEL":
            console.print("[yellow]已取消工單貼上，返回主選單。[/yellow]")
            return
        lines.append(line)

    raw_text = "\n".join(lines).strip()
    if not raw_text:
        console.print("[red]⚠️ 輸入的工單文字為空，返回主選單。[/red]")
        return

    # 1. 呼叫 preprocessor
    with console.status("[cyan]正在執行工單去雜訊與欄位提取 (preprocessor)...[/cyan]"):
        ticket_info = preprocess_ticket(raw_text)

    display_ticket_summary(ticket_info)

    # 2. 呼叫 Ollama 解析拆解
    active_model = ollama_parser.get_active_model()
    with console.status(f"[cyan]正在調用本地 Ollama 模型 ({active_model}) 拆解每台 VM 規格...[/cyan]"):
        vms = ollama_parser.parse_ticket_to_vms(ticket_info)

    if not vms:
        console.print("[red]⚠️ 未能解析出任何 VM 規格，請檢查工單內容。[/red]")
        return

    display_parsed_vms(vms)

    # 3. 確認是否開始依序部署
    confirm = Prompt.ask(
        f"\n[bold]共拆解出 {len(vms)} 台 VM 需求，是否開始依序執行規格解析與審核部署？[/bold]",
        choices=["y", "Y", "n", "N"],
        default="Y"
    ).upper()

    if confirm != "Y":
        console.print("[yellow]已取消後續部署流程，返回主選單。[/yellow]")
        return

    # 4. 逐一呼叫 LangGraph Agent
    total = len(vms)
    case_no = ticket_info.get("單號", "TICKET")
    for idx, vm_desc in enumerate(vms, 1):
        console.print(f"\n[bold magenta]════════════════════════════════════════════════════════════════[/bold magenta]")
        console.print(f"[bold cyan]▶ 正在處理第 [{idx}/{total}] 台虛擬機需求...[/bold cyan]")
        console.print(f"[dim]{vm_desc}[/dim]\n")

        session_id = f"ticket-{case_no}-vm{idx}-{uuid.uuid4().hex[:4]}"
        success = run_vm_deployment_flow(vm_desc, agent_graph, session_id)

        if not success:
            console.print(f"[red]第 {idx} 台虛擬機器部署流程失敗或中斷。[/red]")

        if idx < total:
            cont = Prompt.ask(
                f"\n[bold]第 {idx} 台虛擬機流程結束。是否繼續處理第 {idx+1} 台？[/bold]",
                choices=["y", "Y", "n", "N"],
                default="Y"
            ).upper()
            if cont != "Y":
                console.print("[yellow]已暫停後續 VM 部署。[/yellow]")
                break

    console.print(f"\n[bold green]✔ 工單 {case_no} 的所有處理已結束！[/bold green]\n")

def handle_freeform_mode(agent_graph):
    """模式 [2]: 自由文字輸入模式"""
    console.print(Panel(
        "[bold cyan]請直接輸入您的開機需求。[/bold cyan]\n"
        "[dim]範例：'請幫我在 hqit-alberttest 專案下開一台 Windows VM，4核 16G 記憶體，單號 N/A，AP負責人Albert，單位 HQIT，用途是跑報表'[/dim]\n"
        "[dim]輸入 'back' 可返回主選單，輸入 'exit' 退出程式。[/dim]",
        title="💬 模式 [2] 自由文字輸入模式",
        border_style="cyan"
    ))

    session_id = f"freeform-{uuid.uuid4().hex[:6]}"
    console.print(f"[dim]對話 Session ID: thread-{session_id}[/dim]\n")

    while True:
        try:
            user_input = Prompt.ask("[bold blue]使用者[/bold blue]").strip()
            if not user_input:
                continue

            if user_input.lower() in ["back", "menu"]:
                console.print("[cyan]返回主選單。[/cyan]")
                break

            if user_input.lower() in ["exit", "quit"]:
                console.print("[cyan]感謝使用，再見！[/cyan]")
                sys.exit(0)

            run_vm_deployment_flow(user_input, agent_graph, f"thread-{session_id}")

        except KeyboardInterrupt:
            console.print("\n[yellow]已中斷當前輸入，返回主選單。[/yellow]")
            break

def main():
    display_welcome()

    # 初始化 LangGraph 編譯物件
    agent_graph = build_agent_graph()

    while True:
        try:
            console.print("\n[bold]請選擇操作模式：[/bold]")
            console.print("[bold green][1][/bold green] 工單純文字貼上模式 (Ticket Raw Text Pipeline)")
            console.print("[bold cyan][2][/bold cyan] 自由文字輸入模式 (Free-form Natural Language)")
            console.print("[bold red][Q][/bold red] 退出程式 (Quit)")

            choice = Prompt.ask("\n請選擇模式", choices=["1", "2", "q", "Q"], default="1").upper()

            if choice == "1":
                handle_ticket_mode(agent_graph)
            elif choice == "2":
                handle_freeform_mode(agent_graph)
            elif choice == "Q":
                console.print("[cyan]感謝使用，再見！[/cyan]")
                break
        except KeyboardInterrupt:
            console.print("\n[yellow]程式已中斷[/yellow]")
            break
        except Exception as e:
            console.print(f"[bold red]主迴圈發生異常: {e}[/bold red]")

if __name__ == "__main__":
    main()
