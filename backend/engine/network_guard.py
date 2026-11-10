"""The fixture-only engine and its workers share the external-network guard."""

import ipaddress
import socket


def fixture_network_guard(event, arguments):
    if event == "socket.getaddrinfo":
        host = arguments[0]
    elif event == "socket.connect":
        connection, address = arguments[:2]
        if connection.family == getattr(socket, "AF_UNIX", None):
            return
        host = address[0]
    else:
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise PermissionError("External networking is disabled in the P02 fixture spike")
