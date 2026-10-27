"""工單處理工具：掃描 ./tickets/ 並呼叫本地 Ollama 解析為通用 ParsedTicket

核心解耦：此工具只負責「讀檔 + 分類 + 拆解」，不直接觸發任何變更流程；
後續要開 VM 或做其他處置，一律由 Supervisor 依 ticket_type 決定。
"""
import json
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import tool

import config
from clients.ollama_client import ollama_client, TicketParseError
from schemas import ParsedTicket

# 解析結果快取：key = (絕對路徑, mtime_ns)，避免同一份未變更的工單重複送 Ollama
_PARSE_CACHE: dict[tuple[str, int], ParsedTicket] = {}

# 各類型工單建議的下一步 (提供給 Supervisor 作為路由提示)
NEXT_STEP_HINTS: dict[str, str] = {
    "CREATE_VM": "逐筆將 action_items[i].description 作為 requirement_description 呼叫 deploy_vm_pipeline (一次一台、依序執行)。",
    "EXPAND_RESOURCE": "目前尚無擴容子圖。可先用 query_vm_by_ip 查詢目標主機現況，再告知使用者需人工處理。",
    "DECOMMISSION_VM": "目前尚無下線子圖。可先用 query_vm_by_ip 確認目標主機與其 Deployment，再告知使用者需人工處理。",
    "NETWORK_ADJUSTMENT": "目前尚無網路變更子圖。請整理變更項目並告知使用者需人工處理。",
    "UNKNOWN": "無法自動判斷類型，請將摘要呈現給使用者並請其確認處置方式。",
}


def list_ticket_files() -> list[Path]:
    """列出 tickets 目錄下所有支援的工單檔案 (依檔名排序)"""
    tickets_dir = config.TICKETS_DIR
    if not tickets_dir.is_dir():
        return []
    return sorted(
        (p for p in tickets_dir.iterdir()
         if p.is_file() and p.suffix.lower() in config.TICKET_EXTENSIONS),
        key=lambda p: p.name.lower(),
    )


def resolve_ticket_file(filename: str) -> Path:
    """
    將使用者提供的檔名解析為 tickets 目錄內的實際檔案 (支援省略副檔名)。
    拒絕任何指向 tickets 目錄以外的路徑。
    """
    tickets_dir = config.TICKETS_DIR.resolve()
    name = filename.strip().strip("\"'")
    candidate = (tickets_dir / name).resolve()

    if tickets_dir not in candidate.parents and candidate != tickets_dir:
        raise ValueError(f"不允許存取 tickets 目錄以外的檔案: {filename}")

    if candidate.is_file():
        return candidate
    for ext in config.TICKET_EXTENSIONS:
        with_ext = candidate.with_name(candidate.name + ext)
        if with_ext.is_file():
            return with_ext
    raise FileNotFoundError(f"在 {tickets_dir} 中找不到工單檔案: {filename}")


def _read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def parse_ticket_file(path: Path, use_cache: bool = True) -> ParsedTicket:
    """讀取單一工單檔案並交由 Ollama 解析 (帶快取)"""
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    if use_cache and key in _PARSE_CACHE:
        return _PARSE_CACHE[key]
    ticket = ollama_client.parse_ticket(_read_text(path), source_name=path.name)
    _PARSE_CACHE[key] = ticket
    return ticket


def ticket_to_summary(ticket: ParsedTicket, source: Optional[str] = None) -> dict[str, Any]:
    """輸出給 Supervisor 的工單摘要 (不含原始全文以節省 token)"""
    data = ticket.model_dump(exclude={"raw_content"})
    if source:
        data = {"file": source, **data}
    data["action_item_count"] = len(ticket.action_items)
    data["next_step_hint"] = NEXT_STEP_HINTS.get(ticket.ticket_type, NEXT_STEP_HINTS["UNKNOWN"])
    return data


@tool
def scan_and_parse_tickets(filename: Optional[str] = None) -> str:
    """掃描本機 tickets 工單資料夾 (*.txt, *.md)，呼叫本地 Ollama 解析工單，回傳每張工單的類型 (ticket_type)、單號、申請人、摘要、簽核意見與拆解後的動作清單 (action_items)。
    此工具只做解析、不會執行任何變更；請依回傳的 ticket_type 自行決定後續流程 (例如 CREATE_VM 時呼叫 deploy_vm_pipeline)。
    參數 filename: (選填) 只解析指定檔名 (可省略副檔名)；不填則解析資料夾內全部工單。"""
    try:
        if filename:
            files = [resolve_ticket_file(filename)]
        else:
            files = list_ticket_files()
    except (ValueError, FileNotFoundError) as e:
        available = [p.name for p in list_ticket_files()]
        return f"❌ {e}\n目前可用的工單檔案：{available or '（無）'}"

    if not files:
        return f"📭 工單資料夾 {config.TICKETS_DIR} 中沒有任何 {'/'.join(config.TICKET_EXTENSIONS)} 檔案。"

    results: list[dict[str, Any]] = []
    for path in files:
        try:
            ticket = parse_ticket_file(path)
            results.append(ticket_to_summary(ticket, source=path.name))
        except TicketParseError as e:
            results.append({"file": path.name, "error": f"解析失敗: {e}"})
        except OSError as e:
            results.append({"file": path.name, "error": f"讀檔失敗: {e}"})

    return json.dumps(
        {"tickets_dir": str(config.TICKETS_DIR), "total": len(results), "tickets": results},
        ensure_ascii=False, indent=2, default=str,
    )
