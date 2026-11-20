"""Drill: can this sandbox send and receive UDP at all?

Discord voice media is UDP. The voice *websocket* (TCP 443) can complete the
handshake while every RTP packet is dropped, which looks exactly like "the bot
is in the channel and says it's playing, but nobody hears anything".
"""
import socket
import struct


def udp_dns(host, port=53, timeout=4.0):
    # minimal DNS query: example.com A
    query = (b"\xab\xcd" + b"\x01\x00" + b"\x00\x01" + b"\x00\x00"
             + b"\x00\x00" + b"\x00\x00"
             + b"\x07example\x03com\x00" + b"\x00\x01" + b"\x00\x01")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(query, (host, port))
        data, addr = sock.recvfrom(512)
        return True, f"{len(data)} bytes from {addr[0]}:{addr[1]}"
    except socket.timeout:
        return False, "timeout (no reply)"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


def udp_echo(host, port=123, timeout=4.0):
    """NTP-style probe: any UDP reply proves egress+ingress."""
    ntp = b"\x1b" + b"\0" * 47
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(ntp, (host, port))
        data, addr = sock.recvfrom(512)
        return True, f"{len(data)} bytes from {addr[0]}"
    except socket.timeout:
        return False, "timeout (no reply)"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


if __name__ == "__main__":
    for label, fn, host in (
        ("dns 1.1.1.1", udp_dns, "1.1.1.1"),
        ("dns 8.8.8.8", udp_dns, "8.8.8.8"),
        ("ntp time.google.com", udp_echo, "216.239.35.0"),
    ):
        ok, detail = fn(host)
        print(f"UDP {label}: {'OK' if ok else 'BLOCKED'} — {detail}")
