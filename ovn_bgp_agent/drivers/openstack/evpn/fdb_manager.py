# Copyright 2025 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""FDB 和邻居表管理"""

import collections

from oslo_config import cfg
from oslo_log import log as logging

from ovn_bgp_agent.utils import linux_net

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class FdbManager:
    """管理 FDB 和邻居表条目"""

    def __init__(self):
        self.bridge_fdb_entries = collections.defaultdict(list)
        self.static_neighbors = collections.defaultdict(list)

    def ensure_fdb_entry(self, mac_address, vlan_id, bridge_device, bridge_port):
        """添加静态 FDB 条目

        :param mac_address: MAC 地址
        :param vlan_id: VLAN ID
        :param bridge_device: Bridge 名称
        :param bridge_port: Bridge 端口名称
        """
        if not CONF.evpn_static_fdb:
            return

        key = (mac_address, vlan_id)
        if key not in self.bridge_fdb_entries[bridge_device]:
            try:
                linux_net.add_bridge_fdb(
                    mac_address,
                    bridge_port,
                    vlan=vlan_id,
                    master=True,
                    static=True
                )
                self.bridge_fdb_entries[bridge_device].append(key)
                LOG.debug("Added FDB: %s VLAN %s", mac_address, vlan_id)
            except Exception as e:
                if "File exists" not in str(e):
                    LOG.warning("Failed to add FDB %s: %s", mac_address, e)

    def ensure_neighbor_entry(self, ip_address, mac_address, irb_device):
        """添加静态邻居条目

        :param ip_address: IP 地址
        :param mac_address: MAC 地址
        :param irb_device: IRB 设备名称
        """
        if not CONF.evpn_static_neighbors:
            return

        key = (ip_address, mac_address)
        if key not in self.static_neighbors[irb_device]:
            try:
                linux_net.add_ip_nei(ip_address, mac_address, irb_device)
                self.static_neighbors[irb_device].append(key)
                LOG.debug("Added neighbor: %s -> %s on %s",
                          ip_address, mac_address, irb_device)
            except Exception as e:
                if "File exists" not in str(e):
                    LOG.warning("Failed to add neighbor %s: %s", ip_address, e)

    def batch_add_fdb(self, entries, bridge_name, bridge_port):
        """批量添加 FDB 条目

        :param entries: [{'mac': '...', 'vlan': 123}, ...]
        :param bridge_name: Bridge 名称
        :param bridge_port: Bridge 端口名称
        """
        if not CONF.evpn_static_fdb:
            return

        added = 0
        for entry in entries:
            key = (entry['mac'], entry['vlan'])
            if key not in self.bridge_fdb_entries[bridge_name]:
                try:
                    linux_net.add_bridge_fdb(
                        entry['mac'],
                        bridge_port,
                        vlan=entry['vlan'],
                        master=True,
                        static=True
                    )
                    self.bridge_fdb_entries[bridge_name].append(key)
                    added += 1
                except Exception as e:
                    if "File exists" not in str(e):
                        LOG.warning("Failed to add FDB %s: %s", entry['mac'], e)

        if added:
            LOG.debug("Batch added %d FDB entries", added)

    def batch_add_neighbors(self, entries):
        """批量添加邻居条目

        :param entries: [{'ip': '...', 'mac': '...', 'device': '...'}, ...]
        """
        if not CONF.evpn_static_neighbors:
            return

        added = 0
        for entry in entries:
            device = entry['device']
            key = (entry['ip'], entry['mac'])

            if key not in self.static_neighbors[device]:
                try:
                    linux_net.add_ip_nei(entry['ip'], entry['mac'], device)
                    self.static_neighbors[device].append(key)
                    added += 1
                except Exception as e:
                    if "File exists" not in str(e):
                        LOG.warning("Failed to add neighbor %s: %s", entry['ip'], e)

        if added:
            LOG.debug("Batch added %d neighbor entries", added)

    def cleanup_device(self, device_name):
        """清理设备的所有条目"""
        if device_name in self.bridge_fdb_entries:
            del self.bridge_fdb_entries[device_name]
        if device_name in self.static_neighbors:
            del self.static_neighbors[device_name]

    def get_stats(self):
        """获取统计信息"""
        return {
            'fdb_entries_total': sum(len(v) for v in self.bridge_fdb_entries.values()),
            'neighbor_entries_total': sum(len(v) for v in self.static_neighbors.values()),
        }