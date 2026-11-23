from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_process: subprocess.Popen[str] | None = None
_public_base_url = ""
_provider = ""
_lock = threading.Lock()


def _mobile_url(token: str, provider: str) -> str:
    suffix = (
        "?serveo-skip-browser-warning=true"
        if provider == "Serveo"
        else ""
    )
    return f"{_public_base_url}/m/{token}{suffix}"


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if not process or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _stop_tunnel() -> None:
    global _process, _public_base_url, _provider
    process = _process
    _process = None
    _public_base_url = ""
    _provider = ""
    _stop_process(process)


atexit.register(_stop_tunnel)


def _ssh_executable() -> str:
    candidates = [
        shutil.which("ssh"),
        str(
            Path(os.environ.get("WINDIR", "C:/Windows"))
            / "System32/OpenSSH/ssh.exe"
        ),
        "/usr/bin/ssh",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "未找到系统 OpenSSH。请确认 Windows 可选功能中的"
        "“OpenSSH 客户端”已安装。"
    )


def _known_hosts_option() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def _provider_commands(
    base_dir: Path,
    port: int,
) -> list[tuple[str, list[str], re.Pattern[str], dict[str, str]]]:
    providers: list[
        tuple[str, list[str], re.Pattern[str], dict[str, str]]
    ] = []
    cloudflared = base_dir / "cloudflared.exe"
    if cloudflared.is_file():
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["TUNNEL_TRANSPORT_PROTOCOL"] = "http2"
        providers.append(
            (
                "Cloudflare",
                [
                    str(cloudflared),
                    "tunnel",
                    "--url",
                    f"http://127.0.0.1:{port}",
                    "--no-autoupdate",
                    "--protocol",
                    "http2",
                ],
                re.compile(
                    r"https://(?!api\.|www\.)"
                    r"[a-z0-9-]+\.trycloudflare\.com",
                    re.IGNORECASE,
                ),
                env,
            )
        )

    return providers


def _skip_dns_name(packet: bytes, offset: int) -> int:
    while offset < len(packet):
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        offset += length
    raise ValueError("DNS name is truncated")


def _dns_query_a(host: str, server: str) -> list[str]:
    query_id = secrets.randbelow(65535) + 1
    labels = host.rstrip(".").split(".")
    qname = b"".join(
        bytes([len(label.encode("ascii"))]) + label.encode("ascii")
        for label in labels
    ) + b"\x00"
    packet = (
        struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
        + qname
        + struct.pack("!HH", 1, 1)
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.sendto(packet, (server, 53))
        response, _ = sock.recvfrom(4096)
    if len(response) < 12:
        return []
    response_id, _, qdcount, ancount, _, _ = struct.unpack(
        "!HHHHHH", response[:12]
    )
    if response_id != query_id:
        return []
    offset = 12
    for _ in range(qdcount):
        offset = _skip_dns_name(response, offset) + 4
    addresses: list[str] = []
    for _ in range(ancount):
        offset = _skip_dns_name(response, offset)
        if offset + 10 > len(response):
            break
        record_type, record_class, _, length = struct.unpack(
            "!HHIH", response[offset : offset + 10]
        )
        offset += 10
        data = response[offset : offset + length]
        offset += length
        if record_type == 1 and record_class == 1 and length == 4:
            addresses.append(socket.inet_ntoa(data))
    return addresses


def _resolve_public_host(host: str) -> list[str]:
    addresses: list[str] = []
    for server in ("8.8.8.8", "1.1.1.1"):
        try:
            addresses.extend(_dns_query_a(host, server))
        except Exception:
            continue
    try:
        addresses.extend(
            item[4][0]
            for item in socket.getaddrinfo(
                host, 443, socket.AF_INET, socket.SOCK_STREAM
            )
        )
    except OSError:
        pass
    return list(dict.fromkeys(address for address in addresses if address))


def _verify_public_url(url: str, timeout: float = 10.0) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme != "https" or not host:
        return False
    addresses = _resolve_public_host(host)
    if not addresses:
        return False
    request = (
        "GET /api/health HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "User-Agent: ASharePulse/1.2\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    context = ssl.create_default_context()
    for address in addresses[:4]:
        raw_socket: socket.socket | None = None
        secure_socket: ssl.SSLSocket | None = None
        try:
            raw_socket = socket.create_connection(
                (address, 443), timeout=timeout
            )
            secure_socket = context.wrap_socket(
                raw_socket, server_hostname=host
            )
            secure_socket.settimeout(timeout)
            secure_socket.sendall(request)
            chunks: list[bytes] = []
            total = 0
            while total < 16384:
                chunk = secure_socket.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            response = b"".join(chunks)
            header, _, body = response.partition(b"\r\n\r\n")
            status_line = header.split(b"\r\n", 1)[0]
            if b" 200 " not in status_line + b" ":
                continue
            payload = json.loads(body.decode("utf-8"))
            if payload.get("ok") is True:
                return True
        except Exception:
            continue
        finally:
            if secure_socket is not None:
                try:
                    secure_socket.close()
                except OSError:
                    pass
            elif raw_socket is not None:
                try:
                    raw_socket.close()
                except OSError:
                    pass
    return False


def _launch_provider(
    *,
    provider: str,
    command: list[str],
    url_pattern: re.Pattern[str],
    env: dict[str, str],
    log_path: Path,
    timeout: float,
) -> tuple[subprocess.Popen[str], str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    found = threading.Event()
    discovered: dict[str, str] = {}

    def read_logs() -> None:
        if not process.stdout:
            found.set()
            return
        with log_path.open("a", encoding="utf-8") as log:
            log.write(
                f"\n[{provider}] starting at {time.strftime('%F %T')}\n"
            )
            for line in process.stdout:
                log.write(line)
                log.flush()
                if "url" not in discovered:
                    match = url_pattern.search(line)
                    if match:
                        discovered["url"] = match.group(0).rstrip(".,;)")
                        found.set()
        found.set()

    threading.Thread(
        target=read_logs,
        name=f"public-tunnel-log-{provider}",
        daemon=True,
    ).start()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if found.wait(0.25):
            break
        if process.poll() is not None:
            break
    url = discovered.get("url", "")
    if not url:
        detail = ""
        try:
            detail = log_path.read_text(
                encoding="utf-8", errors="replace"
            )[-1600:]
        except OSError:
            pass
        _stop_process(process)
        raise RuntimeError(
            f"{provider} 公网隧道启动失败，请检查网络。"
            + ("\n" + detail if detail else "")
        )
    return process, url


def _save_state(base_dir: Path, result: dict[str, Any]) -> None:
    path = base_dir / "data" / "mobile_public_url.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **result,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def start_public_tunnel(
    base_dir: Path,
    *,
    port: int,
    token: str,
    timeout: int = 45,
) -> dict[str, Any]:
    global _process, _public_base_url, _provider
    with _lock:
        if (
            _process
            and _process.poll() is None
            and _public_base_url
            and _verify_public_url(_public_base_url, timeout=6)
        ):
            result = {
                "ok": True,
                "url": _mobile_url(token, _provider),
                "public": True,
                "provider": _provider,
                "note": "任意手机均可打开；电脑和手机服务需保持运行。",
            }
            _save_state(base_dir, result)
            return result

        _stop_tunnel()
        log_path = base_dir / "data" / "public_tunnel.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for provider, command, pattern, env in _provider_commands(
            base_dir, port
        ):
            process: subprocess.Popen[str] | None = None
            try:
                process, url = _launch_provider(
                    provider=provider,
                    command=command,
                    url_pattern=pattern,
                    env=env,
                    log_path=log_path,
                    timeout=timeout,
                )
                if provider == "Cloudflare":
                    time.sleep(8)
                verified = False
                for _ in range(6):
                    if _verify_public_url(url, timeout=5):
                        verified = True
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(2)
                if not verified:
                    _stop_process(process)
                    raise RuntimeError(
                        f"{provider} 已建立连接，但公网端到端检查失败。"
                    )
                _process = process
                _public_base_url = url
                _provider = provider
                result = {
                    "ok": True,
                    "url": _mobile_url(token, provider),
                    "public": True,
                    "provider": provider,
                    "note": (
                        "任意手机均可打开，无需同一 Wi-Fi。"
                        "电脑休眠或关机后链接失效；"
                        "后台手机服务会自动恢复连接。"
                    ),
                }
                _save_state(base_dir, result)
                return result
            except Exception as exc:
                _stop_process(process)
                errors.append(str(exc))

        raise RuntimeError(
            "公网隧道无法连接。请检查网络。\n" + "\n".join(errors)
        )
