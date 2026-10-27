"""Paramiko SSH 封裝 (以私鑰登入遠端主機並執行指令)"""
import ipaddress
import socket
from pathlib import Path
from typing import Optional

import config

try:
    import paramiko
except ImportError:  # pragma: no cover - 於執行期提示安裝
    paramiko = None


class SSHClient:
    """
    無狀態 SSH 執行器：每次 exec_command 皆建立新連線、執行完畢即關閉。
    所有連線/驗證/逾時錯誤皆被捕捉並轉為 (exit_code<0, "", 錯誤訊息)，不會向上拋出。
    """

    # 自訂錯誤碼 (遠端指令本身的 exit code 一律 >= 0)
    ERR_CONFIG = -1       # 參數或環境設定錯誤 (金鑰不存在、套件未安裝、IP 格式錯)
    ERR_AUTH = -2         # 金鑰無效 / 驗證失敗
    ERR_UNREACHABLE = -3  # 主機不可達 / 連線被拒
    ERR_TIMEOUT = -4      # 連線或指令逾時
    ERR_SSH = -5          # 其他 SSH 協定錯誤

    def __init__(
        self,
        key_path: Optional[Path | str] = None,
        username: Optional[str] = None,
        port: Optional[int] = None,
        timeout: Optional[int] = None,
        passphrase: Optional[str] = None,
        strict_host_key: Optional[bool] = None,
    ):
        self.key_path = Path(key_path) if key_path else config.SSH_PRIVATE_KEY_PATH
        self.username = username or config.DEFAULT_SSH_USER
        self.port = port or config.SSH_PORT
        self.timeout = timeout or config.SSH_TIMEOUT
        self.passphrase = passphrase if passphrase is not None else config.SSH_KEY_PASSPHRASE
        self.strict_host_key = config.SSH_STRICT_HOST_KEY if strict_host_key is None else strict_host_key

    def exec_command(self, ip: str, cmd: str, username: Optional[str] = None) -> tuple[int, str, str]:
        """
        以 SSH Key 連入 ip 並執行 cmd。

        Returns:
            (exit_code, stdout, stderr)
            exit_code >= 0 為遠端指令回傳值；< 0 為本地端連線錯誤 (見 ERR_* 常數)，錯誤說明放在 stderr。
        """
        if paramiko is None:
            return self.ERR_CONFIG, "", "尚未安裝 paramiko 套件，請執行 pip install paramiko"

        try:
            ipaddress.ip_address(ip.strip())
        except ValueError:
            return self.ERR_CONFIG, "", f"無效的 IP 位址格式: {ip}"
        host = ip.strip()

        if not self.key_path.exists():
            return self.ERR_CONFIG, "", f"找不到 SSH 私鑰檔案: {self.key_path} (請確認 .env 的 SSH_PRIVATE_KEY_PATH)"

        user = username or self.username
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
            if self.strict_host_key:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            client.connect(
                hostname=host,
                port=self.port,
                username=user,
                key_filename=str(self.key_path),
                passphrase=self.passphrase,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                look_for_keys=False,
                allow_agent=False,
            )

            _, stdout, stderr = client.exec_command(cmd, timeout=self.timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            return exit_code, out, err

        except paramiko.AuthenticationException as e:
            return self.ERR_AUTH, "", f"SSH 驗證失敗 ({user}@{host})：金鑰無效或未授權 - {e}"
        except paramiko.SSHException as e:
            # 金鑰格式錯誤/密碼錯誤也會落在這裡 (PasswordRequiredException 為其子類別)
            if isinstance(e, paramiko.PasswordRequiredException):
                return self.ERR_AUTH, "", f"SSH 私鑰需要密碼 (請設定 SSH_KEY_PASSPHRASE)：{e}"
            return self.ERR_SSH, "", f"SSH 協定錯誤 ({host})：{e}"
        except (socket.timeout, TimeoutError) as e:
            return self.ERR_TIMEOUT, "", f"SSH 連線或指令執行逾時 ({self.timeout}s)：{host} - {e}"
        except (paramiko.ssh_exception.NoValidConnectionsError, ConnectionError, OSError) as e:
            return self.ERR_UNREACHABLE, "", f"主機不可達或連線被拒：{host}:{self.port} - {e}"
        except Exception as e:  # 最後防線，避免 Agent 工具崩潰
            return self.ERR_SSH, "", f"SSH 執行發生未預期錯誤 ({host})：{type(e).__name__}: {e}"
        finally:
            client.close()


# 全域預設實例
ssh_client = SSHClient()
