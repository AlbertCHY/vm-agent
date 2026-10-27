"""將 VM Provisioning 子圖包裝為 Supervisor 可呼叫的標準工具 deploy_vm_pipeline

HITL 設計：
    子圖內的 human_review 節點以 interrupt() 暫停；本工具負責「驅動」子圖：
    偵測到中斷 → 呼叫已註冊的 review handler (例如 app.py 的 Rich CLI 審核介面) 取得審核結果
    → 以 Command(resume=...) 喚醒子圖，直到子圖結束。
    介面層 (CLI / Web) 只需透過 set_review_handler() 註冊自己的審核函式，子圖本身與 UI 完全解耦。
"""
import threading
import uuid
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.types import Command

from subgraphs.vm_provisioning.graph import build_vm_provisioning_graph

# 審核函式：輸入 interrupt payload {"action", "spec", "message"}，
# 回傳 {"approved": bool, "updated_params"?: dict, "user_feedback"?: str, "aborted"?: bool}
ReviewHandler = Callable[[dict[str, Any]], dict[str, Any]]

_review_handler: Optional[ReviewHandler] = None
_graph = None
_graph_lock = threading.Lock()
# 同一時間只允許一條開機管線執行，避免平行 tool call 時多個人工審核畫面交錯
_pipeline_lock = threading.Lock()

# 防止退回 → 重新推論 → 再審核的無限迴圈
MAX_REVIEW_ROUNDS = 10


def set_review_handler(handler: Optional[ReviewHandler]) -> None:
    """由介面層註冊人工審核函式 (傳 None 可解除)"""
    global _review_handler
    _review_handler = handler


def get_vm_provisioning_graph():
    """Lazy 編譯子圖 (整個行程共用一份，靠 thread_id 區隔各台 VM 的狀態)"""
    global _graph
    if _graph is None:
        with _graph_lock:
            if _graph is None:
                _graph = build_vm_provisioning_graph()
    return _graph


def _pending_interrupts(graph, config: dict) -> list[Any]:
    state = graph.get_state(config)
    return [itr for task in state.tasks for itr in (task.interrupts or ())]


def run_vm_pipeline(requirement_description: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """
    執行一次 VM 開機子圖並回傳結構化結果 (供工具與批次程式共用)。

    Returns:
        {"status": submitted|failed|needs_clarification|aborted|review_unavailable|incomplete,
         "session_id": str, "message": str, "spec": dict, "execution_result": dict|None}
    """
    graph = get_vm_provisioning_graph()
    session_id = session_id or f"vmprov-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 60}

    # 每次呼叫都清掉上一輪的審核/執行結果，避免沿用同一 session 時誤判
    graph.invoke(
        {
            "messages": [HumanMessage(content=requirement_description)],
            "human_feedback": None,
            "execution_result": None,
        },
        config=config,
    )

    rounds = 0
    interrupts = _pending_interrupts(graph, config)
    while interrupts:
        payload = interrupts[0].value
        if _review_handler is None:
            return {
                "status": "review_unavailable",
                "session_id": session_id,
                "message": "規格已組合完成，但目前執行環境沒有註冊人工審查介面，無法送出部署。",
                "spec": payload.get("spec", {}),
                "execution_result": None,
            }
        rounds += 1
        if rounds > MAX_REVIEW_ROUNDS:
            return {
                "status": "incomplete",
                "session_id": session_id,
                "message": f"人工審查已退回超過 {MAX_REVIEW_ROUNDS} 次，停止本次流程。",
                "spec": payload.get("spec", {}),
                "execution_result": None,
            }
        review_result = _review_handler(payload)
        graph.invoke(Command(resume=review_result), config=config)
        interrupts = _pending_interrupts(graph, config)

    values = graph.get_state(config).values or {}
    exec_res = values.get("execution_result") or None
    ai_msgs = [m for m in values.get("messages", []) if isinstance(m, AIMessage)]
    last_msg = ai_msgs[-1].content if ai_msgs else ""

    if exec_res and exec_res.get("success"):
        status = "submitted"
    elif exec_res and exec_res.get("aborted"):
        status = "aborted"
    elif exec_res and exec_res.get("error"):
        status = "failed"
    elif values.get("missing_fields") and not exec_res:
        status = "needs_clarification"
    else:
        status = "incomplete"

    return {
        "status": status,
        "session_id": session_id,
        "message": last_msg,
        "spec": values.get("current_spec") or {},
        "missing_fields": values.get("missing_fields") or [],
        "execution_result": exec_res,
    }


@tool
def deploy_vm_pipeline(requirement_description: str, session_id: Optional[str] = None) -> str:
    """
    【僅限開立 VM】傳入單一 VM 的完整需求描述，啟動嚴謹的 16 欄位自動補齊、人工審查中斷 (HITL) 與 Aria 提交流程。
    一次只處理一台 VM；多台 VM 請依序逐台呼叫，不要同時平行呼叫。
    參數 requirement_description: 單一 VM 的自然語言需求 (專案、作業系統、VM 名稱、單號、CPU、記憶體、負責人、單位、用途等)。
    參數 session_id: (選填) 若上一次呼叫回傳「需要補充資訊」，請帶入當時回傳的 session_id，
                     並在 requirement_description 只填使用者補充的內容，即可延續先前已解析的規格。
    """
    with _pipeline_lock:
        try:
            result = run_vm_pipeline(requirement_description, session_id=session_id)
        except Exception as e:
            return f"❌ VM 開機子圖執行失敗：{type(e).__name__}: {e}"

    sid = result["session_id"]
    status = result["status"]
    msg = result.get("message") or ""

    if status == "submitted":
        return f"[status=submitted][session_id={sid}]\n{msg}"
    if status == "needs_clarification":
        return (
            f"[status=needs_clarification][session_id={sid}]\n"
            f"開機流程缺少關鍵欄位，已暫停。請將以下問題轉達使用者，取得答案後再以相同 session_id 呼叫 deploy_vm_pipeline：\n{msg}"
        )
    if status == "aborted":
        return f"[status=aborted][session_id={sid}]\n使用者於人工審查時取消了此 VM 的部署。{msg}"
    if status == "failed":
        return f"[status=failed][session_id={sid}]\n{msg}"
    if status == "review_unavailable":
        return f"[status=review_unavailable][session_id={sid}]\n{msg}\n已組合規格：{result.get('spec')}"
    return f"[status={status}][session_id={sid}]\n{msg or '流程未完成，請確認需求後重試。'}"
