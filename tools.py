import os
import time
import urllib3
import requests
from typing import Optional, Any
from dotenv import load_dotenv
from langchain_core.tools import tool

# 載入環境變數
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

class AriaAutomationClient:
    """VMware Aria Automation API Client"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        refresh_token: Optional[str] = None,
        verify_ssl: Optional[bool] = None
    ):
        self.base_url = (base_url or os.getenv("AUTOMATION_URL", "")).rstrip("/")
        self.refresh_token = refresh_token or os.getenv("AUTOMATION_API_TOKEN", "")
        
        ssl_env = os.getenv("AUTOMATION_VERIFY_SSL", "false").lower()
        self.verify_ssl = verify_ssl if verify_ssl is not None else (ssl_env in ("true", "1", "yes"))
        
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

        self.os_images = None  # 暫存 OS Image 映射清單，避免重複查詢

    def login(self) -> str:
        """使用 refresh_token 向 /iaas/api/login 換取 access_token"""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        url = f"{self.base_url}/iaas/api/login"
        payload = {"refreshToken": self.refresh_token}
        headers = {"Content-Type": "application/json"}

        response = requests.post(url, json=payload, headers=headers, verify=self.verify_ssl, timeout=15)
        response.raise_for_status()

        data = response.json()
        self._access_token = data.get("token")
        # 預設有效時間保守估算 25 分鐘 (通常為 30-60 分鐘)
        self._token_expires_at = time.time() + 1500
        return self._access_token

    def _get_auth_headers(self) -> dict[str, str]:
        token = self.login()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def get_projects(self, name_filter: Optional[str] = None) -> list[dict[str, Any]]:
        """取得專案列表，可依名稱模糊比對"""
        url = f"{self.base_url}/iaas/api/projects"
        headers = self._get_auth_headers()
        
        response = requests.get(url, headers=headers, verify=self.verify_ssl, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        projects = data.get("content", [])

        if name_filter:
            kw = name_filter.strip().lower()
            projects = [
                p for p in projects
                if kw in p.get("name", "").lower() or kw in p.get("description", "").lower()
            ]

        return [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "description": p.get("description", "")
            }
            for p in projects
        ]

    def get_blueprints(self, os_hint: Optional[str] = None) -> list[dict[str, Any]]:
        """取得有效藍圖範本清單，可依作業系統關鍵字篩選"""
        url = f"{self.base_url}/blueprint/api/blueprints"
        headers = self._get_auth_headers()

        response = requests.get(url, headers=headers, verify=self.verify_ssl, timeout=15)
        response.raise_for_status()

        data = response.json()
        blueprints = data.get("content", [])

        # 只保留 valid == True 的藍圖
        valid_bps = [bp for bp in blueprints if bp.get("valid", False)]

        if os_hint:
            kw = os_hint.strip().lower()
            matched = [
                bp for bp in valid_bps
                if kw in bp.get("name", "").lower() or kw in bp.get("description", "").lower()
            ]
            if matched:
                valid_bps = matched

        return [
            {
                "id": bp.get("id"),
                "name": bp.get("name"),
                "description": bp.get("description", ""),
                "status": bp.get("status", "")
            }
            for bp in valid_bps
        ]

    def get_project_name(self, project_id: Optional[str]) -> Optional[str]:
        """根據 project_id 取得專案名稱 (name)"""
        if not project_id:
            return None
        projects = self.get_projects()
        for p in projects:
            if p.get("id") == project_id:
                return p.get("name")
        return None

    def get_blueprint_name(self, blueprint_id: Optional[str]) -> Optional[str]:
        """根據 blueprint_id 取得藍圖名稱 (name)"""
        if not blueprint_id:
            return None
        blueprints = self.get_blueprints()
        for b in blueprints:
            if b.get("id") == blueprint_id:
                return b.get("name")
        return None

    def resolve_project(self, identifier: str) -> Optional[dict[str, str]]:
        """根據名稱或 ID 解析專案資訊 (回傳 id 與 name)"""
        if not identifier:
            return None
        projects = self.get_projects()
        target = identifier.strip().lower()
        for p in projects:
            if p.get("id") == identifier.strip():
                return {"id": p["id"], "name": p["name"]}
        for p in projects:
            if target == p.get("name", "").lower():
                return {"id": p["id"], "name": p["name"]}
        for p in projects:
            if target in p.get("name", "").lower():
                return {"id": p["id"], "name": p["name"]}
        return None

    def get_network_segment(self, project_name: Optional[str]) -> str:
        """根據專案名稱取得對應網段"""
        project_name = project_name.strip() if project_name else ""
        if not project_name:
            return ""
        match project_name:
            case "hqit-idpbg": return "Service_142"
            case "hqit-B-Group": return "Service_136"
            case "hqit-cbg_ptcplm": return "Service_138"
            case "hqit-dbg_plm": return "HQIT_108"
            case "hqit-SmartCityDev": return "Service_154"
            case "hqit-NewGenesis": return "Service_701"
            case "hqit-sbg": return "Service_XXX"

        head = project_name[:2].lower()
        match head:
            case "a-": return "Service_142"
            case "b-": return "Service_136"
            case "c-": return "Service_138"
            case "d-": return "Service_140"
            case "e-": return "Service_152"
            case "s-": return "Service_139"
            case "fi": return "Service_156"

        return "HQIT_108"


    def resolve_blueprint(self, identifier: str) -> Optional[dict[str, str]]:
        """根據名稱或 ID 解析藍圖資訊 (回傳 id 與 name)"""
        if not identifier:
            return None
        blueprints = self.get_blueprints()
        target = identifier.strip().lower()
        for b in blueprints:
            if b.get("id") == identifier.strip():
                return {"id": b["id"], "name": b["name"]}
        for b in blueprints:
            if target == b.get("name", "").lower():
                return {"id": b["id"], "name": b["name"]}
        for b in blueprints:
            if target in b.get("name", "").lower():
                return {"id": b["id"], "name": b["name"]}
        return None

    def get_os_images(self) -> dict[str, dict[str, Any]]:
        """取得 /iaas/api/image-profiles 中的所有可用 OS Image 映射清單"""
        if self.os_images is not None:
            return self.os_images

        url = f"{self.base_url}/iaas/api/image-profiles"
        headers = self._get_auth_headers()

        response = requests.get(url, headers=headers, verify=self.verify_ssl, timeout=15)
        response.raise_for_status()

        data = response.json()
        content = data.get("content", [])

        images: dict[str, dict[str, Any]] = {}
        for item in content:
            mappings = item.get("imageMappings", {}).get("mapping", {})
            for img_key, img_info in mappings.items():
                images[img_key] = {
                    "key": img_key,
                    "name": img_info.get("name", img_key),
                    "osFamily": img_info.get("osFamily", ""),
                    "description": img_info.get("description", ""),
                    "externalId": img_info.get("externalId", "")
                }
        self.os_images = images
        return images

    def match_os_image(self, query: str, images_dict: Optional[dict[str, dict[str, Any]]] = None) -> Optional[str]:
        """根據使用者輸入的作業系統需求，在系統可用的 OS Image 中智慧比對並選出最符合的特定名稱"""
        if not query:
            return None

        if images_dict is None:
            images_dict = self.get_os_images()

        if not images_dict:
            return None

        # 1. 直接比對現有 key
        for k in images_dict:
            if k.lower() == query.strip().lower():
                return k

        q = query.lower().replace(" ", "").replace("-", "").replace("_", "")
        is_eng = "eng" in q or "english" in q or "en" in q

        # 2. 針對主要作業系統分類與常見版本進行規則對齊
        if "win" in q:
            if "2019" in q:
                return "win2019dc-eng" if is_eng else "win2019dc-cht"
            if "2016" in q:
                return "win2016dc-eng" if is_eng else "win2016dc-cht"
            if "10" in q:
                return "win10pro-cht"
            # 預設 Windows 使用最新 2022
            return "win2022dc-eng" if is_eng else "win2022dc-cht"

        if "ubuntu" in q:
            if "24" in q:
                return "ubuntu2404-50G"
            if "22" in q:
                return "ubuntu2204-50G"
            if "20" in q:
                return "ubuntu2004"
            return "ubuntu2404-50G"  # 預設 Ubuntu 24.04 LTS

        if "debian" in q:
            if "11" in q:
                return "Debian11.6-50G"
            return "Debian12-50G"  # 預設 Debian 12

        if "rocky" in q:
            if "9.1" in q:
                return "Rocky9.1-50G"
            return "Rocky9.8-50G"

        if "centos" in q:
            if "7" in q:
                return "centos79-50G"
            return "centos9_stream-50G"

        if "rhel" in q or "redhat" in q:
            return "rhel9u7-50G"

        if "oracle" in q:
            if "7" in q:
                return "oracle7.9-50G"
            if "8" in q:
                return "oracle8.10-50G"
            return "oracle9.6-50G"

        if "suse" in q:
            return "suse15sp3"

        # 3. 模糊文字比對 key 或 description
        for k, info in images_dict.items():
            if query.lower() in k.lower() or query.lower() in info.get("description", "").lower():
                return k

        return None
    
    def submit_blueprint_request(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        """發送 POST /blueprint/api/blueprint-requests 提交建立 VM 請求"""
        url = f"{self.base_url}/blueprint/api/blueprint-requests"
        headers = self._get_auth_headers()

        response = requests.post(url, json=request_payload, headers=headers, verify=self.verify_ssl, timeout=20)
        response.raise_for_status()

        data = response.json()
        return {
            "success": True,
            "request_id": data.get("id"),
            "deployment_id": data.get("deploymentId"),
            "deployment_name": data.get("deploymentName"),
            "status": data.get("status"),
            "flow_id": data.get("flowId"),
            "created_at": data.get("createdAt")
        }


# 全域實例供 Agent 與 Tools 調用
aria_client = AriaAutomationClient()

# @tool
# def search_projects(keyword: str) -> str:
#     """根據關鍵字搜尋 Aria Automation 中的專案名稱與對應的 project_id。例如：'hqit-HCMBI' 或 'HCMBI'。"""
#     try:
#         results = aria_client.get_projects(name_filter=keyword)
#         if not results:
#             return f"找不到包含 '{keyword}' 的專案。請確認專案名稱。"
#         return str(results[:5])
#     except Exception as e:
#         return f"查詢專案失敗: {str(e)}"

# @tool
# def search_blueprints(os_name: str) -> str:
#     """根據作業系統關鍵字搜尋適合的藍圖範本與 blueprint_id。例如：'Windows', 'Ubuntu', 'Debian', 'Rocky'。"""
#     try:
#         results = aria_client.get_blueprints(os_hint=os_name)
#         if not results:
#             return f"找不到與作業系統 '{os_name}' 相關的有效藍圖。"
#         return str(results[:5])
#     except Exception as e:
#         return f"查詢藍圖範本失敗: {str(e)}"

# @tool
# def list_available_os_images(filter_os: Optional[str] = None) -> str:
#     """取得系統 image-profiles 中所有可用的 OS Image 名稱清單。可選傳入 filter_os 進行篩選 (如 'Windows', 'Linux')。"""
#     try:
#         images = aria_client.get_os_images()
#         if filter_os:
#             kw = filter_os.lower()
#             filtered = {
#                 k: v for k, v in images.items()
#                 if kw in k.lower() or kw in v.get("osFamily", "").lower()
#             }
#             return str(list(filtered.keys()))
#         return str(list(images.keys()))
#     except Exception as e:
#         return f"查詢可用 OS Image 失敗: {str(e)}"

