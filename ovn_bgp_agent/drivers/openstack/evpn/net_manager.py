# Copyright 2025 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""网络基础设施管理"""

import ipaddress

from oslo_config import cfg
from oslo_log import log as logging

from ovn_bgp_agent import constants
from ovn_bgp_agent.drivers.openstack.utils import frr
from ovn_bgp_agent.utils import linux_net

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class NetManager:
    """管理 EVPN 网络基础设施（VXLAN/VRF/IRB）"""

    def __init__(self, ovn_helper):
        self.ovn_helper = ovn_helper
        self.vrfs = {}  # vrf_name → vrf_info

    def ensure_infrastructure(self, network_info, local_ip):
        """创建网络基础设施

        :param network_info: 网络信息字典
        :param local_ip: VTEP IP
        :return: True if success
        """
        network_id = network_info['id']
        vlan_id = network_info['vlan_id']
        vni = network_info['vni']
        evpn_type = network_info['type']

        LOG.info("Ensuring infrastructure for network %s", network_id[:8])
        LOG.info("  Bridge VLAN: %s, VNI: %s, Type: %s", vlan_id, vni, evpn_type)

        created_resources = []

        try:
            bridge_name = CONF.evpn_bridge

            # 1. Create VRF
            vrf_name = f'{constants.OVN_EVPN_VRF_PREFIX}{vni}'
            table_id = vni + 1000000

            if vrf_name not in self.vrfs:
                linux_net.ensure_vrf(vrf_name, table_id)
                linux_net.set_device_status(vrf_name, 'up')
                created_resources.append(('vrf', vrf_name))

                self.vrfs[vrf_name] = {
                    'table_id': table_id,
                    'vni': vni,
                    'networks': [],
                }

            self.vrfs[vrf_name]['networks'].append(network_id)

            # 2. Create VXLAN
            vxlan_name = self._ensure_vxlan(
                vni, vlan_id, bridge_name, local_ip, network_info['mtu'])
            created_resources.append(('vxlan', vxlan_name))

            # 3. Create IRB
            irb_device = self._ensure_irb(
                vlan_id, bridge_name, vrf_name, network_info)
            created_resources.append(('irb', irb_device))

            # 4. L2 type: Create Internal Port
            if evpn_type == 'l2':
                LOG.info("L2 type: Creating Internal Port")
                int_port = self._ensure_internal_port(
                    vni, network_info['ovn_vlan_id'], vlan_id,
                    bridge_name, network_id, network_info['mtu'])
                created_resources.append(('internal_port', int_port))
            else:
                LOG.info("L3 type: Skipping Internal Port")

            # 5. Configure FRR
            self._configure_frr(network_info, vrf_name, local_ip)

            return True

        except Exception as e:
            LOG.exception("Failed to ensure infrastructure: %s", e)
            self._rollback_resources(created_resources)
            return False

    def cleanup_infrastructure(self, network_info):
        """清理网络基础设施"""
        network_id = network_info['id']
        vlan_id = network_info['vlan_id']
        vni = network_info['vni']

        LOG.info("Cleaning up infrastructure for network %s", network_id[:8])

        try:
            bridge_name = CONF.evpn_bridge

            # Delete IRB
            irb_device = f'{bridge_name}.{vlan_id}'
            LOG.debug("Deleting IRB device: %s", irb_device)
            linux_net.delete_device(irb_device)

            # Delete VXLAN
            vxlan_name = f'{constants.OVN_EVPN_VXLAN_PREFIX}{vni}'
            LOG.debug("Deleting VXLAN device: %s", vxlan_name)
            linux_net.delete_device(vxlan_name)

            # Delete Internal Port (L2 type)
            if network_info.get('l2vni'):
                int_port_name = f'evpn-{vni}'[:15]
                self._cleanup_internal_port(int_port_name)

            # Check VRF
            vrf_name = f'{constants.OVN_EVPN_VRF_PREFIX}{vni}'
            if vrf_name in self.vrfs:
                self.vrfs[vrf_name]['networks'].remove(network_id)

                if not self.vrfs[vrf_name]['networks']:
                    LOG.info("Deleting VRF %s (no more networks)", vrf_name)
                    evpn_info = {'vrf_name': vrf_name, 'vni': vni}
                    frr.vrf_reconfigure(evpn_info, 'del-vrf')

                    if CONF.delete_vrf_on_disconnect:
                        linux_net.delete_device(vrf_name)

                    del self.vrfs[vrf_name]

        except Exception as e:
            LOG.warning("Failed to cleanup infrastructure: %s", e)

    def _ensure_vxlan(self, vni, vlan_id, bridge_name, local_ip, mtu):
        """创建 VXLAN 设备"""
        vxlan_name = f'{constants.OVN_EVPN_VXLAN_PREFIX}{vni}'

        LOG.info("Creating VXLAN %s (VNI %s, Bridge VLAN %s)",
                 vxlan_name, vni, vlan_id)

        linux_net.ensure_vxlan(
            vxlan_name, vni, local_ip,
            dstport=CONF.evpn_udp_dstport
        )

        linux_net.set_link_attribute(vxlan_name, mtu=mtu)
        linux_net.set_master_for_device(vxlan_name, bridge_name)
        linux_net.set_bridge_port_learning(vxlan_name, False)
        linux_net.set_bridge_port_neigh_suppress(vxlan_name, True)
        linux_net.set_device_status(vxlan_name, 'up')

        linux_net.ensure_bridge_vlan(
            vxlan_name, vlan_id,
            tagged=True, pvid=False, untagged=False
        )

        veth_port = CONF.evpn_bridge_veth
        linux_net.ensure_bridge_vlan(
            veth_port, vlan_id,
            tagged=True, pvid=False, untagged=False
        )

        return vxlan_name

    def _ensure_irb(self, vlan_id, bridge_name, vrf_name, network_info):
        """创建 IRB 设备"""
        LOG.info("Creating IRB for Bridge VLAN %s in VRF %s", vlan_id, vrf_name)

        linux_net.ensure_vlan_device_for_network(bridge_name, vlan_id)
        irb_device = f'{bridge_name}.{vlan_id}'

        linux_net.set_link_attribute(irb_device, mtu=network_info['mtu'])
        linux_net.set_master_for_device(irb_device, vrf_name)
        linux_net.set_device_status(irb_device, 'up')

        linux_net.enable_proxy_arp(irb_device)
        linux_net.enable_proxy_ndp(irb_device)

        # Add gateway IPs
        gateway_ips = self.ovn_helper.extract_gateway_ips(network_info['id'])
        for gw_ip in gateway_ips:
            LOG.info("Adding gateway IP %s to %s", gw_ip, irb_device)
            try:
                linux_net.add_ip_address(gw_ip, irb_device)
            except Exception as e:
                if "File exists" not in str(e):
                    LOG.warning("Failed to add IP %s: %s", gw_ip, e)

        return irb_device

    def _ensure_internal_port(self, vni, ovn_vlan_id, bridge_vlan_id,
                              bridge_name, network_id, mtu):
        """创建 OVN Internal Port"""
        from ovn_bgp_agent.privileged import ovs_vsctl

        int_port_name = f'evpn-{vni}'[:15]
        ovs_bridge = 'br-int'

        LOG.info("Creating Internal Port %s (OVN VLAN %s → Bridge VLAN %s)",
                 int_port_name, ovn_vlan_id, bridge_vlan_id)

        # 获取实际的 OVN VLAN tag
        actual_ovn_vlan = self.ovn_helper.get_ovn_vlan_tag(network_id)

        ports = ovs_vsctl.ovs_cmd('list-ports', [ovs_bridge])
        if int_port_name not in ports:
            ovs_vsctl.ovs_cmd('add-port', [
                ovs_bridge, int_port_name,
                '--', 'set', 'interface', int_port_name, 'type=internal'
            ])

        ovs_vsctl.ovs_cmd('set', [
            'port', int_port_name, f'tag={actual_ovn_vlan}'
        ])

        linux_net.set_device_status(int_port_name, 'up')
        linux_net.set_link_attribute(int_port_name, mtu=mtu)
        linux_net.set_master_for_device(int_port_name, bridge_name)

        linux_net.ensure_bridge_vlan(
            int_port_name, bridge_vlan_id,
            tagged=False, pvid=True, untagged=True
        )

        linux_net.set_bridge_port_learning(int_port_name, True)

        LOG.info("Internal Port %s created successfully", int_port_name)
        return int_port_name

    def _cleanup_internal_port(self, port_name):
        """清理 Internal Port"""
        from ovn_bgp_agent.privileged import ovs_vsctl

        LOG.info("Cleaning up internal port: %s", port_name)

        try:
            linux_net.set_master_for_device(port_name, None)
        except Exception:
            pass

        try:
            ovs_bridge = 'br-int'
            ports = ovs_vsctl.ovs_cmd('list-ports', [ovs_bridge])
            if port_name in ports:
                ovs_vsctl.ovs_cmd('del-port', [ovs_bridge, port_name])
        except Exception as e:
            LOG.debug("Failed to remove from OVS: %s", e)

        try:
            linux_net.delete_device(port_name)
        except Exception:
            pass

    def _configure_frr(self, network_info, vrf_name, local_ip):
        """配置 FRR EVPN"""
        vni = network_info['vni']

        evpn_info = {
            'vrf_name': vrf_name,
            'vni': vni,
            'bgp_as': network_info['bgp_as'],
            'route_targets': network_info.get('route_targets', []),
            'route_distinguishers': network_info.get('route_distinguishers', []),
            'import_targets': network_info.get('import_targets', []),
            'export_targets': network_info.get('export_targets', []),
            'local_ip': local_ip,
        }

        if network_info.get('local_pref'):
            evpn_info['local_pref'] = network_info['local_pref']

        frr.vrf_reconfigure(evpn_info, 'add-vrf')
        LOG.info("FRR configured for VRF %s (VNI %s)", vrf_name, vni)

    def _rollback_resources(self, resources):
        """回滚创建的资源"""
        LOG.warning("Rolling back %d resources", len(resources))
        for resource_type, resource_name in reversed(resources):
            try:
                if resource_type == 'internal_port':
                    self._cleanup_internal_port(resource_name)
                elif resource_type in ('vxlan', 'irb', 'vrf'):
                    linux_net.delete_device(resource_name)
            except Exception as e:
                LOG.error("Failed to rollback %s %s: %s",
                          resource_type, resource_name, e)