"""VM Provisioning 子圖：編譯包含 interrupt() (HITL) 的 StateGraph"""
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, START, END

from schemas import AgentState
from subgraphs.vm_provisioning.nodes import (
    analyze_and_extract,
    check_completeness,
    route_completeness,
    ask_clarification,
    human_review,
    route_review_result,
    submit_request,
)


def build_vm_provisioning_graph(checkpointer: Optional[BaseCheckpointSaver] = None):
    """建立 LangGraph 工作流 (規格抽取 → 完整性檢查 → 人工中斷審核 → 送出請求)"""
    builder = StateGraph(AgentState)

    # 加入節點
    builder.add_node("analyze_and_extract", analyze_and_extract)
    builder.add_node("check_completeness", check_completeness)
    builder.add_node("ask_clarification", ask_clarification)
    builder.add_node("human_review", human_review)
    builder.add_node("submit_request", submit_request)

    # 加入邊
    builder.add_edge(START, "analyze_and_extract")
    builder.add_edge("analyze_and_extract", "check_completeness")

    # 條件邊：檢查參數完整性
    builder.add_conditional_edges(
        "check_completeness",
        route_completeness,
        {
            "ask_clarification": "ask_clarification",
            "human_review": "human_review"
        }
    )

    # 若缺少參數發問後即結束本輪，等待使用者補充
    builder.add_edge("ask_clarification", END)

    # 條件邊：人工審核結果 (核准 → 送出；退回 → 重新推論；取消 → 結束)
    builder.add_conditional_edges(
        "human_review",
        route_review_result,
        {
            "submit_request": "submit_request",
            "analyze_and_extract": "analyze_and_extract",
            END: END
        }
    )

    # 提交完成結束
    builder.add_edge("submit_request", END)

    # 搭配 MemorySaver 支援狀態保留與中斷恢復
    return builder.compile(checkpointer=checkpointer or MemorySaver())
