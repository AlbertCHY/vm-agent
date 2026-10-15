import os
import time
import tomllib
from typing import Any, Optional
from dotenv import load_dotenv

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command

from schemas import (
    VMDeploymentParams,
    SpecExtractionResult,
    AgentState
)
from tools import aria_client

# 載入設定與 Prompts
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PROMPTS_PATH = os.path.join(os.path.dirname(__file__), "prompts.toml")
with open(PROMPTS_PATH, "rb") as f:
    PROMPTS = tomllib.load(f)

# 初始化 Gemini LLM
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

llm = ChatGoogleGenerativeAI(
    model=MODEL_NAME,
    google_api_key=GOOGLE_API_KEY
)

# 1. 意圖分析與規格抽取節點
def analyze_and_extract(state: AgentState) -> dict[str, Any]:
    """使用 LLM 結構化輸出解析使用者自然語言，並透過 API 查詢補齊 project_id、os_image (image-profiles) 與 blueprint_id"""
    messages = state.get("messages", [])
    current_spec = dict(state.get("current_spec") or {})

    # 取得系統中所有可用的 OS Image 映射清單 (來自 /iaas/api/image-profiles)
    try:
        available_images = aria_client.get_os_images()
    except Exception as e:
        available_images = {}

    image_keys_str = ", ".join(available_images.keys()) if available_images else "無"

    system_instruction = (
        f"{PROMPTS['agent']['system']}\n\n"
        f"{PROMPTS['extraction']['system']}\n\n"
        f"【系統目前支援的 OS Image 映像檔名稱清單 (image-profiles)】:\n{image_keys_str}\n\n"
        f"注意：os_image 必須為上述清單中的具體名稱。\n\n"
        f"目前已知/已累積的規格參數: {current_spec}"
    )

    structured_llm = llm.with_structured_output(SpecExtractionResult)
    
    # 組合對話歷史傳給模型
    prompt_messages = [SystemMessage(content=system_instruction)] + messages
    result: SpecExtractionResult = structured_llm.invoke(prompt_messages)

    # 合併提取到的新欄位
    extracted_dict = result.extracted_spec.model_dump(exclude_none=True)
    current_spec.update(extracted_dict)

    # 1.1 若需要查詢專案 ID
    if result.project_name_query and not current_spec.get("project_id"):
        projects = aria_client.get_projects(name_filter=result.project_name_query)
        if projects:
            matched_proj = projects[0]
            current_spec["project_id"] = matched_proj["id"]
            current_spec["project_name"] = matched_proj["name"]
            proj_name = matched_proj["name"]
            
            # 嘗試從專案 description (例如 'C25033100002/王興勝/HCM BI Dashboard') 自動輔助填補
            desc = matched_proj.get("description", "")
            parts = desc.split("/")
            if len(parts) >= 1 and not current_spec.get("case_no") and parts[0].strip():
                current_spec["case_no"] = parts[0].strip()
            if len(parts) >= 2 and not current_spec.get("ap_owner") and parts[1].strip():
                current_spec["ap_owner"] = parts[1].strip()
            if len(parts) >= 3 and not current_spec.get("description") and parts[2].strip():
                current_spec["description"] = parts[2].strip()

    if current_spec.get("project_id") and not current_spec.get("project_name"):
        current_spec["project_name"] = aria_client.get_project_name(current_spec["project_id"])

    if current_spec.get("project_name") and not current_spec.get("network_segment"):
        # 用專案名稱查詢對應網段
        current_spec["network_segment"] = aria_client.get_network_segment(current_spec["project_name"])

    # 1.2 處理 OS Image (從 image-profiles 選取) 與 藍圖 ID
    os_query = result.blueprint_os_query or current_spec.get("os_image")
    if available_images and (current_spec.get("os_image") not in available_images or not current_spec.get("os_image")):
        if os_query:
            matched_img = aria_client.match_os_image(os_query, available_images)
            if matched_img:
                current_spec["os_image"] = matched_img

    # 根據 os_image 或 os_query 比對合適的藍圖模板
    if not current_spec.get("blueprint_id"):
        bp_query = "Windows"
        current_img = current_spec.get("os_image", "")
        if current_img.startswith("win"):
            bp_query = "Windows"
        elif "ubuntu" in current_img.lower():
            bp_query = "Ubuntu"
        elif "debian" in current_img.lower():
            bp_query = "Debian"
        elif "rocky" in current_img.lower():
            bp_query = "Rocky"
        elif os_query:
            bp_query = os_query

        bps = aria_client.get_blueprints(os_hint=bp_query)
        if bps:
            selected_bp = bps[0]
            for bp in bps:
                if "v2.0" in bp["name"]:
                    selected_bp = bp
                    break
            current_spec["blueprint_id"] = selected_bp["id"]
            current_spec["blueprint_name"] = selected_bp["name"]

    if current_spec.get("blueprint_id") and not current_spec.get("blueprint_name"):
        current_spec["blueprint_name"] = aria_client.get_blueprint_name(current_spec["blueprint_id"])

    # 1.3 補足常規基礎預設值 (若尚未設定)
    if not current_spec.get("ip_mode"):
        current_spec["ip_mode"] = "DHCP"
    if not current_spec.get("cluster"):
        current_spec["cluster"] = "twhy-w01-cl08"
    if not current_spec.get("datastore"):
        current_spec["datastore"] = "nimble-vm-datastore"
    if not current_spec.get("sg"):
        current_spec["sg"] = "ap"
    if not current_spec.get("network_segment"):
        current_spec["network_segment"] = "HQIT_108"
    if not current_spec.get("os_owner"):
        current_spec["os_owner"] = "Default_OS_Owner"
    if not current_spec.get("ap_unit") and current_spec.get("ap_owner"):
        current_spec["ap_unit"] = "Default_AP_Unit"

    # 若未指定 deployment_name，自動生成一個易辨識的名稱
    if not current_spec.get("deployment_name"):
        base_name = "vm"
        if result.project_name_query:
            base_name = result.project_name_query.lower()
        suffix = int(time.time()) % 10000
        sg_tag = current_spec.get("sg", "ap").upper()
        current_spec["deployment_name"] = f"{base_name}-{sg_tag}-{suffix:04d}"

    return {
        "current_spec": current_spec,
        "action_type": result.action_type
    }


# 2. 完整性檢查與路由節點
def check_completeness(state: AgentState) -> dict[str, Any]:
    """檢查 16 個必填欄位是否完整"""
    current_spec = state.get("current_spec") or {}
    
    required_fields = [
        "deployment_name", "blueprint_id", "project_id", "cpu_count",
        "memory_size", "os_image", "sg", "network_segment", "ip_mode",
        "cluster", "datastore", "case_no", "ap_owner", "os_owner",
        "ap_unit", "description"
    ]
    
    missing = [f for f in required_fields if not current_spec.get(f)]
    return {"missing_fields": missing}


def route_completeness(state: AgentState) -> str:
    """若有核心欄位缺失則發問，齊全則進入人工審查"""
    missing = state.get("missing_fields", [])
    # 核心不可缺少的關鍵欄位：專案、藍圖/OS、CPU、記憶體、單號、負責人
    critical_missing = [f for f in missing if f in ["project_id", "blueprint_id", "cpu_count", "memory_size", "case_no", "ap_owner"]]
    
    if critical_missing:
        return "ask_clarification"
    return "human_review"


# 3. 補充詢問節點
def ask_clarification(state: AgentState) -> dict[str, Any]:
    """向使用者詢問缺少的必要資訊"""
    missing = state.get("missing_fields", [])
    
    field_labels = {
        "project_id": "Aria 專案名稱 (如 hqit-HCMBI)",
        "blueprint_id": "藍圖範本 (如 Windows, Ubuntu, Debian)",
        "os_image": "作業系統版本 / 映像檔 (如 Windows Server 2022, Ubuntu 22.04)",
        "cpu_count": "CPU 核心數 (例如: 4)",
        "memory_size": "記憶體容量 GB (例如: 16)",
        "case_no": "需求單號 (如 C25033100002)",
        "ap_owner": "AP 負責人姓名",
        "os_owner": "OS 負責人姓名",
        "ap_unit": "應用單位",
        "description": "用途說明",
        "deployment_name": "部署名稱",
        "sg": "NSX-T 安全群組 (如 APP, WEB, DB)",
        "network_segment": "網段 (如 VLAN100)",
        "ip_mode": "IP 模式 (DHCP 或 STATIC)",
        "cluster": "運算集群 (如 Cluster-01)",
        "datastore": "存儲空間 (如 Datastore-01)"
    }

    missing_labels = [f"- {field_labels.get(f, f)}" for f in missing]
    missing_text = "\n".join(missing_labels)
    msg = PROMPTS["clarification"]["prompt"].format(missing_fields_text=missing_text)

    return {
        "messages": [AIMessage(content=msg)]
    }


# 4. 人工審核中斷節點 (HITL)
def human_review(state: AgentState) -> dict[str, Any]:
    """觸發中斷等待人工審查，支援核准、修改參數或退回"""
    spec = dict(state.get("current_spec") or {})

    # 確保 project_name 與 blueprint_name 已解析填補以供檢視辨識
    if spec.get("project_id") and not spec.get("project_name"):
        spec["project_name"] = aria_client.get_project_name(spec["project_id"])
    if spec.get("blueprint_id") and not spec.get("blueprint_name"):
        spec["blueprint_name"] = aria_client.get_blueprint_name(spec["blueprint_id"])

    # 使用 interrupt 暫停 Graph，將包含名稱的完整規格暴露給外部
    human_response: dict = interrupt({
        "action": "review_deployment",
        "spec": spec,
        "message": "VM 部署規格已組合完成，請人工審查確認。"
    })

    # 外部呼叫 Command(resume=payload) 恢復執行時，human_response 即為傳入的 payload
    approved = human_response.get("approved", False)
    updated_params = human_response.get("updated_params") or {}
    user_feedback = human_response.get("user_feedback")

    new_spec = dict(spec)
    if updated_params:
        # 若人工審核時修改了 project 或 project_name
        if "project" in updated_params or "project_name" in updated_params:
            pval = updated_params.get("project") or updated_params.get("project_name")
            resolved_p = aria_client.resolve_project(pval)
            if resolved_p:
                updated_params["project_id"] = resolved_p["id"]
                updated_params["project_name"] = resolved_p["name"]
        # 若人工審核時修改了 blueprint 或 blueprint_name
        if "blueprint" in updated_params or "blueprint_name" in updated_params:
            bval = updated_params.get("blueprint") or updated_params.get("blueprint_name")
            resolved_b = aria_client.resolve_blueprint(bval)
            if resolved_b:
                updated_params["blueprint_id"] = resolved_b["id"]
                updated_params["blueprint_name"] = resolved_b["name"]
        new_spec.update(updated_params)

    if approved:
        return {
            "current_spec": new_spec,
            "human_feedback": human_response
        }
    else:
        return {
            "current_spec": new_spec,
            "human_feedback": human_response,
            "messages": [
                HumanMessage(content=f"人工審查已退回，意見為: {user_feedback or '請調整規格'}")
            ]
        }


def route_review_result(state: AgentState) -> str:
    """根據人工審查結果路由"""
    fb = state.get("human_feedback") or {}
    if fb.get("approved"):
        return "submit_request"
    return "analyze_and_extract"


# 5. 提交請求節點
def submit_request(state: AgentState) -> dict[str, Any]:
    """將驗證後的規格轉為 API 請求並送出至 Aria Automation"""
    spec = state.get("current_spec") or {}

    try:
        validated_params = VMDeploymentParams(**spec)
        payload = validated_params.to_aria_request_payload()

        # 發送至 Aria Automation API
        result = aria_client.submit_blueprint_request(payload)

        msg = (
            f"🎉 **VM 部署請求已成功送出！**\n\n"
            f"- **部署名稱**: `{result.get('deployment_name')}`\n"
            f"- **請求編號 (Request ID)**: `{result.get('request_id')}`\n"
            f"- **部署編號 (Deployment ID)**: `{result.get('deployment_id')}`\n"
            f"- **狀態**: `{result.get('status')}`\n"
            f"- **建立時間**: `{result.get('created_at')}`\n\n"
            f"Aria Automation 已開始在背景建立該虛擬機，您可以登入主控台查看進度。"
        )

        return {
            "execution_result": result,
            "messages": [AIMessage(content=msg)]
        }
    except Exception as e:
        err_msg = f"❌ 送出部署請求失敗: {str(e)}"
        return {
            "execution_result": {"error": str(e)},
            "messages": [AIMessage(content=err_msg)]
        }


# 建立 LangGraph 工作流
def build_agent_graph():
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

    # 條件邊：人工審核結果
    builder.add_conditional_edges(
        "human_review",
        route_review_result,
        {
            "submit_request": "submit_request",
            "analyze_and_extract": "analyze_and_extract"
        }
    )

    # 提交完成結束
    builder.add_edge("submit_request", END)

    # 搭配 MemorySaver 支援狀態保留與中斷恢復
    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)
