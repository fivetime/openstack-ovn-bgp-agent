# Copyright 2025 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""VLAN 管理器 - 负责 Bridge VLAN 分配和回收"""

from oslo_log import log as logging

from ovn_bgp_agent import exceptions as agent_exc

LOG = logging.getLogger(__name__)

VLAN_MIN = 100
VLAN_MAX = 4094


class VlanManager:
    """管理 Bridge VLAN 的分配和释放

    设计原则:
    - VLAN 映射到 network_id（而非 VNI）
    - 优先使用 VNI 作为 VLAN（如果在范围内且未占用）
    - 冲突时使用 Hash 算法查找空闲 VLAN
    - 不持久化（重启时从 OVN 重建）
    """

    def __init__(self):
        self.network_to_vlan = {}
        self.vlan_to_network = {}
        self.free_pool = set(range(VLAN_MIN, VLAN_MAX + 1))
        self.stats = {
            'allocations': 0,
            'releases': 0,
            'conflicts': 0,
        }

    def allocate(self, network_id, vni):
        """为 network 分配 VLAN

        :param network_id: Network UUID
        :param vni: VNI 号（优先使用）
        :return: 分配的 VLAN ID
        """
        if network_id in self.network_to_vlan:
            return self.network_to_vlan[network_id]

        if VLAN_MIN <= vni <= VLAN_MAX and vni in self.free_pool:
            vlan = vni
            LOG.debug("Allocating VLAN %s for network %s (direct VNI mapping)",
                      vlan, network_id[:8])
        else:
            vlan = self._find_free_vlan(vni)
            if vni < VLAN_MIN or vni > VLAN_MAX:
                LOG.debug("VNI %s out of VLAN range, using hash", vni)
            else:
                LOG.debug("VLAN %s occupied, using hash for VNI %s", vni, vni)
                self.stats['conflicts'] += 1

        self.network_to_vlan[network_id] = vlan
        self.vlan_to_network[vlan] = network_id
        self.free_pool.discard(vlan)
        self.stats['allocations'] += 1

        LOG.info("Allocated VLAN %s for network %s (VNI %s)",
                 vlan, network_id[:8], vni)
        return vlan

    def release(self, network_id):
        """释放 network 的 VLAN"""
        if network_id not in self.network_to_vlan:
            LOG.debug("Network %s has no VLAN allocation", network_id[:8])
            return

        vlan = self.network_to_vlan.pop(network_id)
        del self.vlan_to_network[vlan]
        self.free_pool.add(vlan)
        self.stats['releases'] += 1

        LOG.info("Released VLAN %s from network %s", vlan, network_id[:8])

    def get_vlan(self, network_id):
        """获取 network 的 VLAN"""
        return self.network_to_vlan.get(network_id)

    def _find_free_vlan(self, vni):
        """通过 Hash 查找空闲 VLAN"""
        if not self.free_pool:
            raise agent_exc.VlanIdExhausted()

        vlan_range = VLAN_MAX - VLAN_MIN + 1
        for offset in range(vlan_range):
            candidate = ((vni + offset) % vlan_range) + VLAN_MIN
            if candidate in self.free_pool:
                return candidate

        raise agent_exc.VlanIdExhausted()

    def get_stats(self):
        """获取统计信息"""
        return {
            'total_allocated': len(self.network_to_vlan),
            'free_vlans': len(self.free_pool),
            'allocations': self.stats['allocations'],
            'releases': self.stats['releases'],
            'conflicts': self.stats['conflicts'],
        }

    def cleanup_stale(self, active_networks):
        """清理孤儿 VLAN"""
        stale = set(self.network_to_vlan.keys()) - active_networks
        if stale:
            LOG.warning("Cleaning %d stale VLAN allocations", len(stale))
            for network_id in stale:
                self.release(network_id)