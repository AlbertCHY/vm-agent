"""本地 Ollama API 封裝：通用工單解析引擎

流程： 原始工單文字 ─► (可選) preprocessor 去雜訊 ─► Ollama 判斷 ticket_type 並拆解 action_items ─► ParsedTicket
不再預設「工單一定是開 VM」，由模型先分類，再依類型輸出對應的負載結構。
"""
import json
import re
from typing import Any, Optional, get_args

import requests

import config
from schemas import ParsedTicket, TicketType

try:
    # preprocessor 針對公司「伺服器資源申請與歸還」表單格式；非該格式時欄位會是「未提供」
    from preprocessor import preprocess_ticket
except ImportError:  # pragma: no cover
    preprocess_ticket = None

VALID_TICKET_TYPES: tuple[str, ...] = get_args(TicketType)
_MISSING = ("", "未提供", "無", "N/A", "n/a", None)


class TicketParseError(RuntimeError):
    """Ollama 無法產出可用的工單解析結果"""


class OllamaClient:
    """負責與本機 Ollama 服務溝通 (模型自動適配 + JSON 結構化輸出)"""

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        self.base_url = (base_url or config.OLLAMA_BASE_URL).rstrip("/")
        self.configured_model = model or config.OLLAMA_MODEL
        self.timeout = config.OLLAMA_TIMEOUT
        self.num_ctx = config.OLLAMA_NUM_CTX
        self._active_model: Optional[str] = None

    # ────────────────────────────── 模型管理 ──────────────────────────────

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

    # ────────────────────────────── 底層呼叫 ──────────────────────────────

    def generate_json(self, system: str, prompt: str, schema: Optional[dict[str, Any]] = None) -> str:
        """
        呼叫 /api/generate 並要求 JSON 輸出。
        優先使用 JSON Schema 結構化輸出 (Ollama >= 0.5)，若伺服器不支援則退回 format="json"。
        """
        payload: dict[str, Any] = {
            "model": self.get_active_model(),
            "system": system,
            "prompt": prompt,
            "stream": False,
            "format": schema or "json",
            "options": {"num_ctx": self.num_ctx, "temperature": 0.1},
        }
        url = f"{self.base_url}/api/generate"
        resp = requests.post(url, json=payload, timeout=self.timeout)
        if resp.status_code >= 400 and schema is not None:
            payload["format"] = "json"
            resp = requests.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
        """清洗 Markdown codeblock 並解析出第一個 JSON 物件"""
        if not text:
            return None
        clean = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
        clean = re.sub(r"\s*```$", "", clean, flags=re.MULTILINE).strip()
        try:
            data = json.loads(clean)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
        # 退而求其次：抓最外層大括號
        start, end = clean.find("{"), clean.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(clean[start:end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    # ────────────────────────────── 工單解析 ──────────────────────────────

    @staticmethod
    def _llm_output_schema() -> dict[str, Any]:
        """提供給 Ollama 的 JSON Schema (ParsedTicket 去掉 raw_content)"""
        return {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "applicant": {"type": "string"},
                "department": {"type": "string"},
                "ticket_type": {"type": "string", "enum": list(VALID_TICKET_TYPES)},
                "summary": {"type": "string"},
                "approval_notes": {"type": "string"},
                "action_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"description": {"type": "string"}},
                        "required": ["description"],
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["ticket_id", "applicant", "department", "ticket_type", "summary", "action_items"],
        }

    @staticmethod
    def _build_structured_hint(raw_text: str, info: dict[str, Any]) -> str:
        """將 preprocessor 結果整理成提示區塊；表單格式不符時回報未辨識"""
        if not info or info.get("單號") in _MISSING:
            return "（未辨識為標準申請單格式，請直接閱讀原文）"

        m = re.search(r"申請類別[\t ]+([^\n\t]+)", raw_text)
        apply_category = m.group(1).strip() if m else "未提供"
        comments = info.get("審核覆寫意見") or []
        res = info.get("資源總計") or {}
        return (
            f"【工單單號】：{info.get('單號', '未提供')}\n"
            f"【申請類別】：{apply_category}\n"
            f"【申請人】：{info.get('申請人', '未提供')} (工號: {info.get('申請人工號', '未提供')}, "
            f"單位: {info.get('申請單位', '未提供')})\n"
            f"【專案名稱】：{info.get('專案名稱', '未提供')}\n"
            f"【專案背景】：{info.get('專案背景', '無')}\n"
            f"【有效簽核意見】：{'; '.join(comments) if comments else '無'}\n"
            f"【資源總計】：VM總數: {res.get('VM總數', 1)}, vCore總數: {res.get('vCore總數', 0)}, "
            f"Memory總數: {res.get('Memory總數GB', 0)}GB, HDD總數: {res.get('HDD總數GB', 0)}GB, "
            f"SSD總數: {res.get('SSD總數GB', 0)}GB\n"
            f"【VM 主機規格表格內容】：\n{(info.get('VM表格') or '').strip() or '無主機表格內容'}"
        )

    @staticmethod
    def _normalize_action_items(items: Any) -> list[dict[str, Any]]:
        """確保 action_items 為 list[dict] 且每筆都有 description"""
        if not isinstance(items, list):
            return []
        normalized: list[dict[str, Any]] = []
        for it in items:
            if isinstance(it, str) and it.strip():
                normalized.append({"description": it.strip()})
            elif isinstance(it, dict) and it:
                item = {k: v for k, v in it.items() if v not in (None, "")}
                if not item.get("description"):
                    # 由結構化欄位合成描述，確保後續子圖有自然語言可用
                    item["description"] = "，".join(f"{k}: {v}" for k, v in item.items())
                normalized.append(item)
        return normalized

    def parse_ticket(self, raw_text: str, source_name: Optional[str] = None) -> ParsedTicket:
        """
        解析任意工單文字為通用 ParsedTicket。

        Raises:
            TicketParseError: Ollama 連線失敗或回傳內容無法解析。
        """
        if not raw_text or not raw_text.strip():
            raise TicketParseError("工單內容為空")

        prompts = config.PROMPTS.get("ollama", {})
        system_prompt = prompts.get("system")
        user_template = prompts.get("reply")
        if not system_prompt or not user_template:
            raise TicketParseError("prompts.toml 缺少 [ollama] system / reply 設定")

        info: dict[str, Any] = {}
        if preprocess_ticket is not None:
            try:
                info = preprocess_ticket(raw_text) or {}
            except Exception:
                info = {}

        truncated = raw_text.strip()
        if len(truncated) > config.OLLAMA_MAX_RAW_CHARS:
            truncated = truncated[:config.OLLAMA_MAX_RAW_CHARS] + "\n...(以下內容過長已截斷)"

        user_prompt = user_template.format(
            source_name=source_name or "使用者輸入",
            structured_hint=self._build_structured_hint(raw_text, info),
            raw_text=truncated,
        )

        try:
            raw_response = self.generate_json(system_prompt, user_prompt, schema=self._llm_output_schema())
        except requests.RequestException as e:
            raise TicketParseError(f"Ollama 連線或推理失敗 ({self.base_url}, model={self.get_active_model()}): {e}") from e

        data = self._extract_json_object(raw_response)
        if not data:
            raise TicketParseError(f"Ollama 回傳內容非有效 JSON：{raw_response[:300]}")

        # ── 正規化與以 preprocessor 結果補值 (避免小模型漏填) ──
        def pick(llm_key: str, info_key: Optional[str] = None, default: str = "未提供") -> str:
            val = data.get(llm_key)
            if isinstance(val, str) and val.strip() not in _MISSING:
                return val.strip()
            if info_key and info.get(info_key) not in _MISSING:
                return str(info[info_key]).strip()
            return default

        ticket_type = str(data.get("ticket_type", "UNKNOWN")).strip().upper()
        if ticket_type not in VALID_TICKET_TYPES:
            ticket_type = "UNKNOWN"

        approval = data.get("approval_notes")
        if isinstance(approval, list):
            approval = "; ".join(str(a) for a in approval)
        if not approval and info.get("審核覆寫意見"):
            approval = "; ".join(info["審核覆寫意見"])

        try:
            return ParsedTicket(
                ticket_id=pick("ticket_id", "單號", default=source_name or "未提供"),
                applicant=pick("applicant", "申請人"),
                department=pick("department", "申請單位"),
                ticket_type=ticket_type,
                summary=pick("summary", default="(模型未提供摘要)"),
                approval_notes=str(approval or ""),
                action_items=self._normalize_action_items(data.get("action_items")),
                raw_content=raw_text,
            )
        except Exception as e:  # pydantic ValidationError 等
            raise TicketParseError(f"解析結果不符合 ParsedTicket 結構：{e}") from e


# 全域預設實例
ollama_client = OllamaClient()
