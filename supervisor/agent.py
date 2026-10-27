"""頂層 ReAct Supervisor：整合唯讀工具、工單解析工具與業務子圖工具"""
from typing import Optional

from langchain_core.tools import BaseTool
from langchain.agents import create_agent
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

import config
from clients.llm import get_llm
from subgraphs.vm_provisioning import deploy_vm_pipeline
from tools import (
    query_vm_by_ip,
    list_project_deployments,
    check_vm_disk_usage,
    scan_and_parse_tickets,
)

# 新增業務子圖 (例如 vm_decommission_flow) 時，只要在這裡註冊其工具即可
SUPERVISOR_TOOLS: list[BaseTool] = [
    # 維運查詢工具組 (Ops & Diag)
    query_vm_by_ip,
    list_project_deployments,
    check_vm_disk_usage,
    # 工單處理工具組 (Ticket Subsystem)
    scan_and_parse_tickets,
    # 業務執行子圖 (Workflows)
    deploy_vm_pipeline,
]

SUPERVISOR_SYSTEM_PROMPT = config.PROMPTS["supervisor"]["system"]

def build_supervisor_agent(checkpointer: Optional[BaseCheckpointSaver] = None):
    """
    建立 Supervisor ReAct Agent (已編譯的 LangGraph)。
    """
    llm = get_llm()
    checkpointer = checkpointer or MemorySaver()

    return create_agent(
        model=llm,
        tools=SUPERVISOR_TOOLS,
        system_prompt=SUPERVISOR_SYSTEM_PROMPT,
        checkpointer=checkpointer,
        name="cloud_ops_supervisor",
    )
