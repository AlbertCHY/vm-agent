"""VMware Aria Automation REST API Client (無狀態封裝，原 tools.py 遷移至此)"""
import time
from typing import Optional, Any

import requests
import urllib3

import config


class AriaAutomationClient:
    """VMware Aria Automation API Client"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        refresh_token: Optional[str] = None,
        verify_ssl: Optional[bool] = None
    ):
        self.base_url = (base_url or config.AUTOMATION_URL).rstrip("/")
        self.refresh_token = refresh_token or config.AUTOMATION_API_TOKEN
        self.verify_ssl = verify_ssl if verify_ssl is not None else config.AUTOMATION_VERIFY_SSL
        self.timeout = config.AUTOMATION_HTTP_TIMEOUT

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

        self.os_images = None  # 暫存 OS Image 映射清單，避免重複查詢

    # ────────────────────────────── 認證與共用 HTTP ──────────────────────────────

    def login(self) -> str:
        """使用 refresh_token 向 /iaas/api/login 換取 access_token"""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        url = f"{self.base_url}/iaas/api/login"
        payload = {"refreshToken": self.refresh_token}
        headers = {"Content-Type": "application/json"}

        response = requests.post(url, json=payload, headers=headers, verify=self.verify_ssl, timeout=self.timeout)
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

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """共用 GET 請求 (自動帶入 Bearer Token)"""
        url = f"{self.base_url}{path}"
        response = requests.get(
            url, headers=self._get_auth_headers(), params=params,
            verify=self.verify_ssl, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def _get_all_pages(self, path: str, params: Optional[dict[str, Any]] = None,
                       page_size: int = 100, max_pages: int = 20) -> list[dict[str, Any]]:
        """處理 Deployment API (page/size) 分頁，回傳所有 content 項目"""
        items: list[dict[str, Any]] = []
        base_params = dict(params or {})
        for page in range(max_pages):
            data = self._get(path, params={**base_params, "page": page, "size": page_size})
            content = data.get("content", [])
            items.extend(content)
            total_pages = data.get("totalPages")
            if data.get("last") is True or not content:
                break
            if total_pages is not None and page + 1 >= total_pages:
                break
        return items

    # ────────────────────────────── 專案 / 藍圖 / 映像檔 ──────────────────────────────

    def get_projects(self, name_filter: Optional[str] = None) -> list[dict[str, Any]]:
        """取得專案列表，可依名稱模糊比對"""
        data = self._get("/iaas/api/projects")
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
        data = self._get("/blueprint/api/blueprints")
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

        data = self._get("/iaas/api/image-profiles")
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

    # ────────────────────────────── 部署請求 ──────────────────────────────

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

    # ────────────────────────────── 維運查詢 (唯讀) ──────────────────────────────

    @staticmethod
    def _pick(source: dict[str, Any], *keys: str) -> Any:
        """依序從頂層與 customProperties 取第一個非空值 (不同 Aria 版本欄位位置不一)"""
        custom = source.get("customProperties") or {}
        props = source.get("properties") or {}
        for k in keys:
            for container in (source, custom, props):
                val = container.get(k)
                if val not in (None, ""):
                    return val
        return None

    @staticmethod
    def _to_int(val: Any) -> Optional[int]:
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _summarize_resource(cls, res: dict[str, Any]) -> dict[str, Any]:
        """將 Deployment resource 精簡為關鍵欄位，依類型萃取重點屬性"""
        rtype = res.get("type", "")
        props = res.get("properties") or {}
        summary: dict[str, Any] = {
            "id": res.get("id"),
            "name": props.get("resourceName"),
            "type": rtype,
            "state": res.get("state"),
        }
        t = rtype.lower()
        if "machine" in t:
            summary.update({
                "address": props.get("address"),
                # "cpu_count": cls._to_int(props.get("cpuCount")),
                # "memory_mb": cls._to_int(props.get("totalMemoryMB") or props.get("memoryInMB")),
                "power_state": props.get("powerState"),
                # "os_image": props.get("image") or props.get("imageRef"),
                "dependency": props.get("dependsOn")
            })
        # elif "disk" in t or "volume" in t:
        #     summary.update({
        #         "capacity_gb": props.get("capacityGb"),
        #         "persistent": props.get("persistent"),
        #         "datastore": props.get("datastoreName") or props.get("storage"),
        #     })
        # elif "network" in t:
        #     summary.update({
        #         "network_type": props.get("networkType"),
        #         "cidr": props.get("networkCidr") or props.get("cidr"),
        #         "network_name": props.get("resourceName") or props.get("name"),
        #     })
        # elif "security" in t:
        #     summary.update({
        #         "security_group_type": props.get("securityGroupType"),
        #         "rules_count": len(props.get("rules") or []),
        #     })
        return {k: v for k, v in summary.items() if v not in (None, "")}

    def get_deployment(self, deployment_id: str) -> dict[str, Any]:
        """GET /deployment/api/deployments/{id} 取得 Deployment 基本資訊"""
        return self._get(f"/deployment/api/deployments/{deployment_id}")

    def get_deployment_resources(self, deployment_id: str) -> list[dict[str, Any]]:
        """GET /deployment/api/deployments/{id}/resources 取得 Deployment 下全部元件"""
        return self._get_all_pages(f"/deployment/api/deployments/{deployment_id}/resources")

    def query_vm_by_ip(self, ip: str) -> dict[str, Any]:
        """
        以 IP 位址反查 VM 規格、所屬專案與同 Deployment 下的全部元件 (Disks / Networks / Security Groups)。
        找不到時回傳 {"found": False, ...}。
        """
        ip = ip.strip()
        data = self._get("/iaas/api/machines", params={"$filter": f"address eq '{ip}'"})
        machines = data.get("content", [])
        if not machines:
            return {"found": False, "ip": ip, "message": f"找不到 IP 為 {ip} 的虛擬機"}

        m = machines[0]
        project_id = m.get("projectId")
        deployment_id = m.get("deploymentId")

        result: dict[str, Any] = {
            "found": True,
            "ip": ip,
            "machine": {
                "id": m.get("id"),
                "name": m.get("name"),
                "address": m.get("address"),
                "power_state": m.get("powerState"),
                "cpu_count": self._to_int(self._pick(m, "cpuCount")),
                "memory_gb": self._to_int(self._pick(m, "memoryGB")),
                "os_type": self._pick(m, "image"),
                "created_at": m.get("createdAt"),
            },
            "project_id": project_id,
            "project_name": self.get_project_name(project_id) if project_id else None,
            "deployment_id": deployment_id,
        }
        if len(machines) > 1:
            result["other_matches"] = [{"id": x.get("id"), "name": x.get("name")} for x in machines[1:]]

        if deployment_id:
            try:
                dep = self.get_deployment(deployment_id)
                result["deployment"] = {
                    "name": dep.get("name"),
                    "status": dep.get("status"),
                    "created_by": dep.get("createdBy"),
                    "created_at": dep.get("createdAt"),
                }
            except requests.RequestException as e:
                result["deployment"] = {"error": f"無法取得 Deployment 資訊: {e}"}

            try:
                resources = self.get_deployment_resources(deployment_id)
                grouped: dict[str, list[dict[str, Any]]] = {
                    "machines": [], "disks": [], "networks": [], "security_groups": [], "others": []
                }
                for r in resources:
                    s = self._summarize_resource(r)
                    t = (r.get("type") or "").lower()
                    if "machine" in t:
                        grouped["machines"].append(s)
                    elif "disk" in t or "volume" in t:
                        grouped["disks"].append(s)
                    elif "network" in t:
                        grouped["networks"].append(s)
                    elif "security" in t:
                        grouped["security_groups"].append(s)
                    else:
                        grouped["others"].append(s)
                result["deployment_resources"] = {k: v for k, v in grouped.items() if v}
            except requests.RequestException as e:
                result["deployment_resources"] = {"error": f"無法取得 Deployment 資源: {e}"}

        return result

    def list_deployments_by_project(self, project_name: str) -> list[dict[str, Any]]:
        """
        列出指定專案下所有 Deployment 的名稱、狀態、建立者與機器清單。
        專案不存在時拋出 ValueError。
        """
        project = self.resolve_project(project_name)
        if not project:
            raise ValueError(f"找不到名稱或 ID 為 '{project_name}' 的專案")

        deployments = self._get_all_pages(
            "/deployment/api/deployments",
            params={"projects": project["id"], "expand": "resources"}
        )

        results: list[dict[str, Any]] = []
        for d in deployments:
            machines = [
                self._summarize_resource(r)
                for r in (d.get("resources") or [])
                if "machine" in (r.get("type") or "").lower()
            ]
            results.append({
                "id": d.get("id"),
                "name": d.get("name"),
                "status": d.get("status"),
                "created_by": d.get("createdBy"),
                "created_at": d.get("createdAt"),
                "project_id": project["id"],
                "project_name": project["name"],
                "machines": machines,
            })
        return results


# 全域實例供 Subgraph 與 Tools 調用
aria_client = AriaAutomationClient()