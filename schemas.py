from typing import Literal, Optional, Any, Annotated, List
from pydantic import BaseModel, Field
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

# ════════════════════════════════════════════════════════════════
# A. 通用工單資料模型 (Ticket Subsystem ⇄ Supervisor ⇄ Subgraphs)
# ════════════════════════════════════════════════════════════════

# 工單類型列舉 (為未來彈性預留)
TicketType = Literal[
    "CREATE_VM",          # 建立虛擬機
    "EXPAND_RESOURCE",    # 資源擴容 (未來)
    "DECOMMISSION_VM",    # 註銷/下線虛擬機 (未來)
    "NETWORK_ADJUSTMENT", # 網路變更 (未來)
    "UNKNOWN"             # 未知/人工處理
]


class ParsedTicket(BaseModel):
    """統一工單封裝模型 (Ollama 解析產出)"""
    ticket_id: str = Field(description="工單單號，如 SRA26100500001")
    applicant: str = Field(description="申請人姓名/工號")
    department: str = Field(description="申請人部門單位")
    ticket_type: TicketType = Field(description="識別出的工單類型")
    summary: str = Field(description="工單需求摘要說明")
    approval_notes: Optional[str] = Field(default="", description="簽核審批意見")

    # 根據不同 ticket_type 存放具體的負載陣列
    # 例如 CREATE_VM 時，items 存放各台機器的描述文字或規格字典
    action_items: List[dict[str, Any]] = Field(
        default_factory=list,
        description="拆解出的具體動作清單 (每筆項目可獨立交由對應 Subgraph 處理)"
    )
    raw_content: Optional[str] = Field(default="", description="原始工單文本")


# ════════════════════════════════════════════════════════════════
# B. VM Provisioning 子圖資料模型
# ════════════════════════════════════════════════════════════════

# 1. 完整的 VM 建立必要參數模型
class VMDeploymentParams(BaseModel):
    deployment_name: str = Field(description="部署名稱 (英文數字底線破折號)")
    blueprint_id: str = Field(description="藍圖範本 ID (UUID)")
    project_id: str = Field(description="Aria Automation 專案 ID (UUID)")
    blueprint_name: Optional[str] = Field(default=None, description="藍圖範本名稱 (方便人工辨識)")
    project_name: Optional[str] = Field(default=None, description="專案名稱 (方便人工辨識)")
    cpu_count: int = Field(ge=1, le=128, description="CPU 核心數 (vCore)")
    memory_size: int = Field(ge=1, le=65536, description="記憶體大小 (MB)")
    os_image: str = Field(description="作業系統映像檔在系統 image-profiles 中的特定名稱 (例如 win2022dc-cht, ubuntu2204-50G)")
    sg: Literal["ap", "db", "web"] = Field(description="NSX-T 安全群組 Application Type (ap, db, web)")
    network_segment: str = Field(description="網段選項/Network (例如 HQIT_108, HQIT_106)")
    ip_mode: Literal["DHCP", "STATIC"] = Field(default="DHCP", description="IP 取得模式 (DHCP 或 STATIC)")
    cluster: str = Field(description="機器建立運算群組位置 (Cluster)")
    datastore: Literal["nimble-vm-datastore", "hqithyvsan"] = Field(description="虛擬機存放存儲位置 (Datastore)")
    case_no: str = Field(description="需求單號或工單號 (CaseNo)")
    ap_owner: str = Field(description="應用系統負責人 (APOwner)")
    os_owner: str = Field(description="作業系統負責人 (OSOwner)")
    ap_unit: str = Field(description="應用單位或部門 (APUnit)")
    description: str = Field(default="", description="用途或備註描述")

    def to_aria_request_payload(self) -> dict[str, Any]:
        """轉換為 Aria Automation /blueprint/api/blueprint-requests 所需的 JSON body"""
        return {
            "deploymentName": self.deployment_name,
            "blueprintId": self.blueprint_id,
            "projectId": self.project_id,
            "inputs": {
                "cpuCount": self.cpu_count,
                "memorySize": self.memory_size,
                "OSimage": self.os_image,
                "sg": self.sg,
                "NetworkSegment": self.network_segment,
                "IPmode": self.ip_mode,
                "Cluster": self.cluster,
                "Datastore": self.datastore,
                "CaseNo": self.case_no,
                "APOwner": self.ap_owner,
                "OSOwner": self.os_owner,
                "APUnit": self.ap_unit,
                "DBOwner": "",
                "Description": self.description
            },
            "reason": self.description or "Automated deployment by Aria Agent"
        }

# 2. 暫存或提取中的部分參數 (欄位皆為 Optional，供模型逐步抽取)
class PartialVMDeploymentParams(BaseModel):
    deployment_name: Optional[str] = Field(default=None, description="部署名稱")
    blueprint_id: Optional[str] = Field(default=None, description="藍圖範本 ID")
    project_id: Optional[str] = Field(default=None, description="專案 ID")
    blueprint_name: Optional[str] = Field(default=None, description="藍圖範本名稱")
    project_name: Optional[str] = Field(default=None, description="專案名稱")
    cpu_count: Optional[int] = Field(default=None, description="CPU核心數")
    memory_size: Optional[int] = Field(default=None, description="記憶體大小 (MB)")
    os_image: Optional[str] = Field(default=None, description="作業系統映像檔")
    sg: Optional[Literal["ap", "db", "web"]] = Field(default="ap", description="安全群組 (ap, db, web)")
    network_segment: Optional[str] = Field(default=None, description="網段")
    ip_mode: Optional[Literal["DHCP", "STATIC"]] = Field(default="DHCP", description="IP模式")
    cluster: Optional[str] = Field(default=None, description="運算集群")
    datastore: Optional[Literal["nimble-vm-datastore", "hqithyvsan"]] = Field(default=None, description="存儲集")
    case_no: Optional[str] = Field(default=None, description="工單號")
    ap_owner: Optional[str] = Field(default=None, description="AP負責人")
    os_owner: Optional[str] = Field(default=None, description="OS負責人")
    ap_unit: Optional[str] = Field(default=None, description="AP單位")
    description: Optional[str] = Field(default=None, description="用途說明")

# 3. 結構化提取輸出模型
class SpecExtractionResult(BaseModel):
    action_type: Literal["create_deployment", "expand_vm", "delete_vm", "query_status", "unknown"] = Field(
        default="create_deployment",
        description="使用者的意圖動作類型"
    )
    extracted_spec: PartialVMDeploymentParams = Field(
        description="從使用者對話中抽取的規格參數"
    )
    project_name_query: Optional[str] = Field(
        default=None,
        description="使用者提及但需要工具查詢 project_id 的專案名稱"
    )
    blueprint_os_query: Optional[str] = Field(
        default=None,
        description="使用者提及但需要工具查詢 blueprint_id 的作業系統關鍵字"
    )
    needs_user_input: bool = Field(
        default=False,
        description="是否仍缺少關鍵資訊需要使用者補充"
    )
    clarification_message: Optional[str] = Field(
        default=None,
        description="若缺少關鍵資訊，向使用者詢問補充的訊息"
    )

# 4. 人工審核的回應酬載
class HumanReviewPayload(BaseModel):
    approved: bool = Field(description="是否核准送出")
    updated_params: Optional[dict[str, Any]] = Field(default=None, description="修改後的參數鍵值對")
    user_feedback: Optional[str] = Field(default=None, description="退回或修正的意見回饋")

# 5. LangGraph State 定義
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    action_type: str
    current_spec: Optional[dict[str, Any]]
    missing_fields: list[str]
    human_feedback: Optional[dict[str, Any]]
    execution_result: Optional[dict[str, Any]]
