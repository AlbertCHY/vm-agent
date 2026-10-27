"""VM 診斷工具 (透過 SSH Key 登入檢查磁碟空間)"""
import re
import shlex
from typing import Optional

from langchain_core.tools import tool

from clients.ssh_client import ssh_client

# 使用率達此門檻 (%) 即標記警示
DISK_WARN_PCT = 75
DISK_CRIT_PCT = 90

# 排除無意義的虛擬檔案系統
_PSEUDO_FS = ("tmpfs", "devtmpfs", "overlay", "squashfs", "udev", "efivarfs")


def _parse_df(output: str) -> list[dict[str, str]]:
    """解析 `df -hP` (POSIX 格式，每筆固定一行) 輸出"""
    rows: list[dict[str, str]] = []
    for line in output.strip().splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        fs, size, used, avail, pct, mount = parts
        rows.append({"filesystem": fs, "size": size, "used": used, "avail": avail, "use_pct": pct, "mount": mount})
    return rows


@tool
def check_vm_disk_usage(ip: str, partition: Optional[str] = None) -> str:
    """透過 SSH Key 連入目標 Linux 主機，執行 df -h 並回傳各磁區的總容量、已用、可用容量與使用百分比，並標示使用率過高的磁區。
    參數 ip: 目標主機 IP。
    參數 partition: (選填) 只檢查特定掛載點或路徑，例如 / 或 /data；不填則列出全部磁區。"""
    cmd = "df -hP"
    if partition:
        cmd += f" -- {shlex.quote(partition.strip())}"

    code, out, err = ssh_client.exec_command(ip, cmd)
    if code < 0:
        return f"❌ 無法連線至 {ip}：{err}"
    if code != 0 and not out.strip():
        return f"❌ 在 {ip} 執行 `{cmd}` 失敗 (exit={code})：{err.strip() or '無錯誤輸出'}"

    rows = _parse_df(out)
    if not partition:
        rows = [r for r in rows if not r["filesystem"].startswith(_PSEUDO_FS)]
    if not rows:
        return f"⚠️ {ip} 的 df 輸出無法解析，原始輸出：\n{out.strip()}"

    lines = [f"🖥️ 主機 {ip} 磁碟使用狀況 (`{cmd}`)：", "",
             "| 掛載點 | 檔案系統 | 總容量 | 已用 | 可用 | 使用率 | 狀態 |",
             "|---|---|---|---|---|---|---|"]
    alerts: list[str] = []
    for r in rows:
        m = re.match(r"(\d+)%", r["use_pct"])
        pct = int(m.group(1)) if m else -1
        if pct >= DISK_CRIT_PCT:
            status = "🔴 危險"
            alerts.append(f"{r['mount']} 使用率 {r['use_pct']} (剩 {r['avail']})")
        elif pct >= DISK_WARN_PCT:
            status = "🟡 偏高"
            alerts.append(f"{r['mount']} 使用率 {r['use_pct']} (剩 {r['avail']})")
        else:
            status = "🟢 正常"
        lines.append(f"| {r['mount']} | {r['filesystem']} | {r['size']} | {r['used']} | {r['avail']} | {r['use_pct']} | {status} |")

    lines.append("")
    if alerts:
        lines.append(f"⚠️ 使用率 ≥ {DISK_WARN_PCT}% 的磁區：" + "；".join(alerts))
    else:
        lines.append(f"✅ 所有磁區使用率皆低於 {DISK_WARN_PCT}%。")
    if err.strip():
        lines.append(f"(stderr: {err.strip()[:300]})")
    return "\n".join(lines)
