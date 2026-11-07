#!/usr/bin/env python3
"""Открывает порт на домашнем роутере через UPnP (IGD), чтобы сайт был виден из интернета.

    .venv/bin/python scripts/open_port.py            # открыть 8000 → эта машина
    .venv/bin/python scripts/open_port.py --close    # закрыть проброс
    .venv/bin/python scripts/open_port.py --status   # что сейчас открыто

Ничего не требует от роутера, кроме включённого UPnP (на большинстве домашних
роутеров он включён по умолчанию). Никаких прав root не нужно: используется
SSDP-обнаружение и SOAP-вызовы к самому роутеру.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_MESSAGE = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
)


def local_ip() -> str:
    """Адрес машины в локальной сети (тот, что видит роутер)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


def discover() -> list[str]:
    """Ищем роутер по SSDP; возвращаем адреса описаний устройств."""
    found: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(3)
    try:
        sock.sendto(SSDP_MESSAGE.encode(), SSDP_ADDR)
        while True:
            try:
                data, addr = sock.recvfrom(65507)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            match = re.search(r"(?im)^location:\s*(.+)$", text)
            if match:
                url = match.group(1).strip()
                if url not in found:
                    found.append(url)
    finally:
        sock.close()
    return found


def _text(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def find_control_url(description_url: str, service_type: str) -> str | None:
    """Ищем controlURL для нужного сервиса в описании устройства."""
    try:
        with urllib.request.urlopen(description_url, timeout=6) as response:
            body = response.read()
    except Exception:  # noqa: BLE001
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    base = description_url
    for service in root.iter():
        if not service.tag.endswith("service"):
            continue
        stype = ""
        control = ""
        for child in service:
            if child.tag.endswith("serviceType"):
                stype = _text(child)
            elif child.tag.endswith("controlURL"):
                control = _text(child)
        if stype == service_type and control:
            return urllib.parse.urljoin(base, control)
    # суб-устройства: обходим все вложенные описания не нужно — IGD обычно в корне
    return None


def soap(control_url: str, service_type: str, action: str, args: dict[str, str]) -> str:
    """Один SOAP-вызов к роутеру."""
    inner = "".join(f"<{key}>{value}</{key}>" for key, value in args.items())
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body><u:{action} xmlns:u=\"{service_type}\">{inner}</u:{action}></s:Body>"
        "</s:Envelope>"
    )
    request = urllib.request.Request(
        control_url, data=envelope.encode(),
        headers={"Content-Type": 'text/xml; charset="utf-8"',
                 "SOAPAction": f'"{service_type}#{action}"'},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.read().decode("utf-8", "replace")


WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


def find_wan() -> tuple[str, str, str] | None:
    """Возвращает (control_url, service_type, external_ip) или None."""
    for location in discover():
        for service_type in WAN_SERVICES:
            control = find_control_url(location, service_type)
            if not control:
                continue
            try:
                reply = soap(control, service_type, "GetExternalIPAddress", {})
            except Exception:  # noqa: BLE001
                continue
            match = re.search(r"<NewExternalIPAddress>([^<]*)<", reply)
            if match:
                return control, service_type, match.group(1).strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--lease", type=int, default=0)
    args = parser.parse_args()

    wan = find_wan()
    if not wan:
        print("Роутер с поддержкой UPnP не найден.\n"
              "Варианты: включить UPnP в настройках роутера или пробросить порт вручную:\n"
              f"  внешний {args.port} → {local_ip()}:{args.port} (TCP)", file=sys.stderr)
        return 2
    control, service_type, external_ip = wan
    host = local_ip()
    print(f"роутер найден: внешний IP {external_ip}, внутренний адрес {host}")

    if args.status or args.lease == -1:
        try:
            reply = soap(control, service_type, "GetSpecificPortMappingEntry", {
                "NewRemoteHost": "", "NewExternalPort": str(args.port),
                "NewProtocol": "TCP"})
            target = re.search(r"<NewInternalClient>([^<]*)<", reply)
            print(f"порт {args.port} проброшен на {target.group(1) if target else '?'}")
        except Exception:  # noqa: BLE001
            print(f"порт {args.port} не проброшен")
        return 0

    if args.close:
        try:
            soap(control, service_type, "DeletePortMapping", {
                "NewRemoteHost": "", "NewExternalPort": str(args.port),
                "NewProtocol": "TCP"})
            print(f"проброс порта {args.port} удалён")
        except Exception as exc:  # noqa: BLE001
            print(f"не удалось снять проброс: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        soap(control, service_type, "AddPortMapping", {
            "NewRemoteHost": "",
            "NewExternalPort": str(args.port),
            "NewProtocol": "TCP",
            "NewInternalPort": str(args.port),
            "NewInternalClient": host,
            "NewEnabled": "1",
            "NewPortMappingDescription": "Graeae analysis",
            "NewLeaseDuration": str(args.lease),
        })
    except Exception as exc:  # noqa: BLE001
        print(f"роутер отказал в пробросе: {exc}", file=sys.stderr)
        return 1

    print(f"порт открыт: http://{external_ip}:{args.port}  →  {host}:{args.port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
