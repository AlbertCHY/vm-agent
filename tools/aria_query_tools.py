"""Aria Automation 唯讀查詢工具 (IP 反查 VM、專案 Deployment 清單)"""
import ipaddress
import json
from typing import Any

import requests
from langchain_core.tools import tool

from clients.aria_client import aria_client


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _http_error_text(e: requests.RequestException) -> str:
    resp = getattr(e, "response", None)
    if resp is not None:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    return str(e)


@tool
def query_vm_by_ip(ip: str) -> str:
    """查詢指定 IP 虛擬機的規格 (CPU/記憶體/電源狀態)、所在專案，以及所屬 Deployment 的所有關聯資源 (Disks、Networks、Security Groups)。
    參數 ip: 虛擬機的 IPv4/IPv6 位址，例如 10.1.2.3。"""
    try:
        ipaddress.ip_address(ip.strip())
    except ValueError:
        return f"❌ '{ip}' 不是有效的 IP 位址格式。"

    try:
        result = aria_client.query_vm_by_ip(ip)
    except requests.RequestException as e:
        return f"❌ 查詢 Aria Automation 失敗：{_http_error_text(e)}"
    except Exception as e:
        return f"❌ 查詢 IP {ip} 時發生錯誤：{type(e).__name__}: {e}"

    if not result.get("found"):
        return f"查無資料：Aria Automation 中找不到 IP 為 {ip} 的虛擬機 (可能不是由 Aria 管理，或 IP 有誤)。"
    return _dump(result)


@tool
def list_project_deployments(project_name: str) -> str:
    """查詢指定專案名稱 (或專案 ID) 下目前所有的 Deployments 清單，包含名稱、運行狀態、建立者、建立時間與其中的機器 (名稱/IP/規格/電源狀態)。
    參數 project_name: Aria 專案名稱，例如 hqit-alberttest；支援部分名稱模糊比對。"""
    try:
        deployments = aria_client.list_deployments_by_project(project_name)
    except ValueError as e:
        return f"❌ {e}。可請使用者確認專案名稱。"
    except requests.RequestException as e:
        return f"❌ 查詢 Aria Automation 失敗：{_http_error_text(e)}"
    except Exception as e:
        return f"❌ 查詢專案 {project_name} 的 Deployments 時發生錯誤：{type(e).__name__}: {e}"

    if not deployments:
        return f"專案 '{project_name}' 目前沒有任何 Deployment。"

    status_count: dict[str, int] = {}
    for d in deployments:
        status_count[d.get("status") or "UNKNOWN"] = status_count.get(d.get("status") or "UNKNOWN", 0) + 1

    return _dump({
        "project_name": deployments[0].get("project_name"),
        "project_id": deployments[0].get("project_id"),
        "total": len(deployments),
        "status_summary": status_count,
        "deployments": [
            {k: v for k, v in d.items() if k not in ("project_id", "project_name")}
            for d in deployments
        ],
    })
