import os
import re
import json
import requests
import tomllib
from typing import Optional, Any
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PROMPTS_PATH = os.path.join(os.path.dirname(__file__), "prompts.toml")
with open(PROMPTS_PATH, "rb") as f:
    PROMPTS = tomllib.load(f)

class OllamaTicketParser:
    """負責與本機 Ollama 服務溝通，將工單資訊拆解為每台 VM 的規格需求清單"""

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.configured_model = model or os.getenv("OLLAMA_MODEL", "gemma3:1b")
        self._active_model = None

    def get_available_models(self) -> list[str]:
        """向 Ollama 查詢本地已下載的模型清單"""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            pass
        return []

    def get_active_model(self) -> str:
        """決定實際使用的模型 (若配置之模型未下載，則智慧適配本地現存模型)"""
        if self._active_model:
            return self._active_model

        available = self.get_available_models()
        if not available:
            self._active_model = self.configured_model
            return self._active_model

        # 1. 完全比對或前綴比對
        target = self.configured_model.lower()
        for m in available:
            if m.lower() == target or m.lower().startswith(target):
                self._active_model = m
                return self._active_model

        # 2. 若配置的模型尚未下載，自動選取本地現有模型 (優先 qwen 或 gemma)
        for m in available:
            if "gemma" in m.lower() or "qwen" in m.lower():
                self._active_model = m
                return self._active_model

        self._active_model = available[0]
        return self._active_model

    def parse_ticket_to_vms(self, ticket_info: dict[str, Any]) -> list[str]:
        """
        將預處理後的工單資料傳給 Ollama 模型，並解析為每台 VM 的規格描述字串清單。
        """
        model = self.get_active_model()
        system_prompt = PROMPTS.get("ollama", {}).get("system", "")
        user_prompt_template = PROMPTS.get("ollama", {}).get("user_prompt", "")

        # 準備填入 Template 的變數
        case_no = ticket_info.get("單號", "未提供")
        applicant = ticket_info.get("申請人", "未提供")
        applicant_id = ticket_info.get("申請人工號", "未提供")
        unit = ticket_info.get("申請單位", "未提供")
        project_name = ticket_info.get("專案名稱", "未提供")
        project_background = ticket_info.get("專案背景", "無")

        comments_list = ticket_info.get("審核覆寫意見", [])
        approval_comments = "; ".join(comments_list) if comments_list else "無"

        res = ticket_info.get("資源總計", {})
        vm_total = res.get("VM總數", 1)
        vcore_total = res.get("vCore總數", 0)
        mem_total = res.get("Memory總數GB", 0)
        hdd_total = res.get("HDD總數GB", 0)
        ssd_total = res.get("SSD總數GB", 0)

        vm_table = ticket_info.get("VM表格", "").strip() or "無主機表格內容"

        formatted_user_prompt = user_prompt_template.format(
            case_no=case_no,
            applicant=applicant,
            applicant_id=applicant_id,
            unit=unit,
            project_name=project_name,
            project_background=project_background,
            approval_comments=approval_comments,
            vm_total=vm_total,
            vcore_total=vcore_total,
            mem_total=mem_total,
            hdd_total=hdd_total,
            ssd_total=ssd_total,
            vm_table=vm_table
        )

        payload = {
            "model": model,
            "system": system_prompt,
            "prompt": formatted_user_prompt,
            "stream": False,
            "format": "json"
        }

        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=300
            )
            resp.raise_for_status()
            raw_response = resp.json().get("response", "").strip()

            # 解析 JSON
            print(raw_response)
            vms = self._extract_json_vms(raw_response)
            if vms:
                return vms

        except Exception as e:
            print(f"⚠️ Ollama 模型推理或連線異常: {e}")

        # 若模型解析失敗或回傳非預期，啟動規則回退保護機制
        # return self._fallback_rule_based_parser(ticket_info)
        return []

    def _extract_json_vms(self, text: str) -> list[str]:
        """清洗並提取 JSON 中的 vms 欄位"""
        if not text:
            return []

        # 1. 移除 Markdown codeblock 標籤
        clean_text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        clean_text = re.sub(r"\s*```$", "", clean_text, flags=re.MULTILINE).strip()

        try:
            data = json.loads(clean_text)
            if isinstance(data, dict) and "vms" in data and isinstance(data["vms"], list):
                return [str(item).strip() for item in data["vms"] if str(item).strip()]
            if isinstance(data, list):
                return [str(item).strip() for item in data if str(item).strip()]
        except json.JSONDecodeError:
            pass

        # 2. 嘗試透過正則匹配 {"vms": [...]}
        match = re.search(r'\{\s*"vms"\s*:\s*\[(.*?)\]\s*\}', clean_text, re.DOTALL)
        if match:
            items_str = match.group(1)
            items = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', items_str)
            if items:
                return [it.encode().decode('unicode_escape', 'ignore').strip() for it in items if it.strip()]

        return []

    # def _fallback_rule_based_parser(self, ticket_info: dict[str, Any]) -> list[str]:
    #     """當 Ollama 模型無法產出標準 JSON 時的確定性規則解析回退方案"""
    #     project = ticket_info.get("專案名稱", "未指定專案")
    #     case_no = ticket_info.get("單號", "未指定單號")
    #     owner = ticket_info.get("申請人", "IT")
    #     comments = "; ".join(ticket_info.get("審核覆寫意見", []))
    #     vm_table = ticket_info.get("VM表格", "")
    #     total_vms = ticket_info.get("資源總計", {}).get("VM總數", 1)

    #     lines = [line.strip() for line in vm_table.split("\n") if line.strip()]
    #     # 過濾掉表頭
    #     data_rows = [l for l in lines if not any(h in l for h in ["OS版本", "記憶體GB", "數據硬碟"])]

    #     vms = []
    #     for idx, row in enumerate(data_rows, 1):
    #         parts = re.split(r"[\t, ]+", row)
    #         os_ver = parts[0] if len(parts) > 0 else "Linux"
    #         cpu = parts[1] if len(parts) > 1 else "4"
    #         mem = parts[2] if len(parts) > 2 else "16"
    #         disk = parts[3] if len(parts) > 3 else "100"
    #         disk_type = parts[4] if len(parts) > 4 else "SSD"
    #         usage = parts[5] if len(parts) > 5 else "伺服器"
    #         env = parts[6] if len(parts) > 6 else "測試"
    #         note = parts[-1] if len(parts) > 7 else ""

    #         desc = (
    #             f"請在 {project} 專案開立 1 台 {os_ver} 虛擬機，配置 {cpu} vCore，{mem}GB 記憶體，"
    #             f"{disk}GB {disk_type}，用途為 {usage}，環境為 {env}，單號 {case_no}，AP負責人 {owner}"
    #         )
    #         if comments:
    #             desc += f"，審核指示: {comments}"
    #         if note:
    #             desc += f"，備註: {note}"
    #         vms.append(desc)

    #     if not vms:
    #         # 若表格完全為空，產生總量描述
    #         for i in range(1, total_vms + 1):
    #             desc = (
    #                 f"請在 {project} 專案開立 1 台 VM (第{i}台)，單號 {case_no}，AP負責人 {owner}"
    #             )
    #             if comments:
    #                 desc += f"，審核指示: {comments}"
    #             vms.append(desc)

    #     return vms

# 全域預設實例
ollama_parser = OllamaTicketParser()
