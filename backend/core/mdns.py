"""mDNS: announce this PC as <name>.local (default festcast.local) so phones and TVs can open
http://festcast.local:<port>/... instead of an IP that changes between networks.

Only announced on the interface FestCast uses for links (lan_ip), never on VPN/VirtualBox adapters. If the name is
already taken by another device, or mDNS can't start, links fall back to the IP. Phones/TVs that don't resolve .local
names still have the IP link (the dashboard shows both)."""
from __future__ import annotations

import logging
import socket

log = logging.getLogger("festcast.mdns")


class Mdns:
    def __init__(self, name: str, ip: str, port: int) -> None:
        self.name, self.ip, self.port = name.strip().lower().removesuffix(".local"), ip, port
        self.host: str | None = None          # "festcast.local" once announced
        self._zc = None
        self._info = None

    async def start(self) -> str | None:
        if not self.name or self.ip.startswith("127."):
            return None
        try:
            from zeroconf import AddressResolverIPv4, IPVersion
            from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
            server = f"{self.name}.local."
            self._zc = AsyncZeroconf(interfaces=[self.ip], ip_version=IPVersion.V4Only)
            # Someone else already answering for this name? Then keep using the IP.
            probe = AddressResolverIPv4(server)
            if await probe.async_request(self._zc.zeroconf, 1500) and probe.parsed_addresses() and \
                    self.ip not in probe.parsed_addresses():
                log.warning("mDNS name %s.local is used by another device; links use the IP", self.name)
                await self.stop()
                return None
            self._info = AsyncServiceInfo("_http._tcp.local.", "FestCast._http._tcp.local.", port=self.port,
                                          addresses=[socket.inet_aton(self.ip)], server=server,
                                          properties={"path": "/"})
            await self._zc.async_register_service(self._info, allow_name_change=True)
            self.host = f"{self.name}.local"
            log.info("mDNS: %s -> %s", self.host, self.ip)
            return self.host
        except Exception as e:  # noqa: BLE001
            log.warning("mDNS not available (%s); links use the IP", e)
            await self.stop()
            return None

    async def stop(self) -> None:
        zc, self._zc = self._zc, None
        if not zc:
            return
        try:
            if self._info:
                await zc.async_unregister_service(self._info)
            await zc.async_close()
        except Exception:  # noqa: BLE001
            pass
        self.host = None
