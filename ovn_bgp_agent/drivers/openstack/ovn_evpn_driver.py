# Copyright 2025 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""OVN EVPN Driver - Symmetric IRB Implementation (重构版)

主要职责:
1. 驱动生命周期管理
2. 事件分发和协调
3. 同步状态管理
4. 指标收集
"""

import collections
import ipaddress
import json
import threading
import time

from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_log import log as logging

from ovn_bgp_agent import constants
from ovn_bgp_agent.drivers import driver_api
from ovn_bgp_agent.drivers.openstack.evpn.fdb_manager import FdbManager
from ovn_bgp_agent.drivers.openstack.evpn.net_manager import NetManager
from ovn_bgp_agent.drivers.openstack.evpn.ovn_helper import OvnEvpnHelper
from ovn_bgp_agent.drivers.openstack.evpn.vlan_manager import VlanManager
from ovn_bgp_agent.drivers.openstack.utils import frr
from ovn_bgp_agent.drivers.openstack.utils import ovn
from ovn_bgp_agent.drivers.openstack.utils import ovs
from ovn_bgp_agent.drivers.openstack.watchers import evpn_watcher
from ovn_bgp_agent import exceptions as agent_exc
from ovn_bgp_agent.utils import linux_net

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

OVN_TABLES = [
    "Port_Binding",
    "Chassis",
    "Datapath_Binding",
    "Load_Balancer",
    "Chassis_Private",
]


class OVNEVPNDriver(driver_api.AgentDriverBase):
    """OVN EVPN Driver with Symmetric IRB support (重构版)

    职责:
    - 驱动初始化和生命周期管理
    - 事件监听和分发
    - 状态同步协调
    - 指标收集和暴露
    """

    def __init__(self):
        super().__init__()

        # 核心状态
        self.evpn_networks = {}  # network_id → network_info
        self.evpn_ports = {}     # port_name → port_info

        # 管理器（职责分离）
        self.vlan_mgr = VlanManager()
        self.fdb_mgr = FdbManager()
        # network_mgr 和 ovn_helper 在 start() 中初始化
        self.network_mgr = None
        self.ovn_helper = None

        # 指标收集
        self.metrics = {
            'sync_count': 0,
            'sync_duration': 0.0,
            'sync_errors': 0,
            'last_sync_time': None,
        }

        # IDL 初始化标志
        self._sb_idl = None
        self._post_fork_event = threading.Event()

    @property
    def sb_idl(self):
        if not self._sb_idl:
            self._post_fork_event.wait()
        return self._sb_idl

    @sb_idl.setter
    def sb_idl(self, val):
        self._sb_idl = val

    def start(self):
        """Initialize driver."""
        LOG.info("=" * 80)
        LOG.info("Starting OVN EVPN Driver (Symmetric IRB) - Refactored")
        LOG.info("=" * 80)

        # Initialize OVS
        self.ovs_idl = ovs.OvsIdl()
        self.ovs_idl.start(CONF.ovsdb_connection)
        self.chassis = self.ovs_idl.get_own_chassis_id()
        self.ovn_remote = self.ovs_idl.get_ovn_remote()
        LOG.info("Chassis: %s, OVN remote: %s", self.chassis, self.ovn_remote)

        # Validate config
        if CONF.exposing_method not in [
            constants.EXPOSE_METHOD_VRF,
            constants.EXPOSE_METHOD_DYNAMIC
        ]:
            raise agent_exc.UnsupportedWiringConfig(method=CONF.exposing_method)

        # Setup infrastructure
        self._ensure_evpn_prerequisites()

        if CONF.clear_vrf_routes_on_startup:
            linux_net.delete_routes_from_table(CONF.bgp_vrf_table_id)

        # Start OVN SB IDL
        self._post_fork_event.clear()
        events = self._get_events()
        self.sb_idl = ovn.OvnSbIdl(
            self.ovn_remote,
            chassis=self.chassis,
            tables=OVN_TABLES,
            events=events
        ).start()
        self._post_fork_event.set()

        # 初始化管理器（需要 IDL）
        self.ovn_helper = OvnEvpnHelper(self.sb_idl, self.ovs_idl)
        self.network_mgr = NetManager(self.ovn_helper)

        LOG.info("OVN EVPN Driver started successfully")
        LOG.info("=" * 80)

    def _ensure_evpn_prerequisites(self):
        """Setup EVPN bridge with VLAN filtering."""
        LOG.info("Setting up EVPN prerequisites")

        bridge_name = CONF.evpn_bridge
        evpn_veth = CONF.evpn_bridge_veth
        ovs_veth = CONF.evpn_ovs_veth
        ovs_bridge = CONF.ovs_bridge

        # Create EVPN bridge
        linux_net.ensure_bridge(bridge_name)
        linux_net.set_device_state(bridge_name, 'up')

        # Enable VLAN filtering
        linux_net.set_link_attribute(
            bridge_name, type='bridge', vlan_filtering=1)
        linux_net.set_link_attribute(
            bridge_name, type='bridge', vlan_default_pvid=1)

        # Create veth pair
        if not linux_net.get_link_id(evpn_veth):
            LOG.info("Creating veth pair: %s <-> %s", evpn_veth, ovs_veth)
            linux_net.ensure_veth(evpn_veth, ovs_veth)

        linux_net.set_master_for_device(evpn_veth, bridge_name)
        linux_net.set_device_state(evpn_veth, 'up')
        linux_net.set_device_state(ovs_veth, 'up')

        # Configure as trunk
        try:
            linux_net.del_bridge_vlan(evpn_veth, 1)
        except Exception:
            pass

        # Attach to OVS
        self._ensure_ovs_veth_port(ovs_veth, ovs_bridge)

        # Configure FRR
        frr.ensure_evpn_base_config()

        LOG.info("EVPN prerequisites ready")

    def _ensure_ovs_veth_port(self, veth_name, bridge_name):
        """Add veth to OVS bridge."""
        try:
            from ovn_bgp_agent.privileged import ovs_vsctl
            ports = ovs_vsctl.ovs_cmd('list-ports', [bridge_name])
            if veth_name not in ports:
                LOG.info("Adding %s to OVS bridge %s", veth_name, bridge_name)
                ovs_vsctl.ovs_cmd('add-port', [bridge_name, veth_name])
        except Exception as e:
            LOG.warning("Failed to add OVS port %s: %s", veth_name, e)

    def _get_events(self):
        """Register event watchers."""
        events = {
            evpn_watcher.SubnetRouterAttachedEvent(self),
            evpn_watcher.SubnetRouterDetachedEvent(self),
            evpn_watcher.PortBindingChassisCreatedEvent(self),
            evpn_watcher.PortBindingChassisDeletedEvent(self),
            evpn_watcher.LocalnetCreateDeleteEvent(self),
            evpn_watcher.PortAssociationCreatedEvent(self),
            evpn_watcher.PortAssociationDeletedEvent(self),
        }
        if CONF.expose_tenant_networks:
            events.update({
                evpn_watcher.TenantPortCreatedEvent(self),
                evpn_watcher.TenantPortDeletedEvent(self),
            })
        return events

    @lockutils.synchronized('evpn')
    def sync(self):
        """Sync EVPN state with OVN."""
        LOG.info("=" * 80)
        LOG.info("Starting EVPN sync")
        LOG.info("=" * 80)

        start_time = time.time()

        try:
            old_networks = self.evpn_networks.copy()

            self.evpn_networks = {}
            self.evpn_ports = {}
            self.fdb_mgr = FdbManager()

            # Get EVPN ports
            evpn_ports = self._get_evpn_ports()
            LOG.info("Found %d ports with EVPN configuration", len(evpn_ports))

            # Group by network
            networks_with_ports = collections.defaultdict(list)
            for port in evpn_ports:
                try:
                    datapath_uuid = str(port.datapath.uuid)
                    networks_with_ports[datapath_uuid].append(port)
                except (AttributeError, IndexError) as e:
                    LOG.warning("Failed to get datapath for port %s: %s",
                                port.logical_port, e)
                    continue

            # Sync each network
            for network_id, ports in networks_with_ports.items():
                try:
                    self._sync_network(network_id, ports)
                except Exception as e:
                    LOG.exception("Failed to sync network %s: %s",
                                  network_id[:8], e)
                    self.metrics['sync_errors'] += 1

            # Cleanup orphaned resources
            self._cleanup_orphaned_resources()

            # Cleanup VLAN manager
            active_networks = set(self.evpn_networks.keys())
            self.vlan_mgr.cleanup_stale(active_networks)

            # Update metrics
            duration = time.time() - start_time
            self.metrics['sync_count'] += 1
            self.metrics['sync_duration'] = duration
            self.metrics['last_sync_time'] = time.time()

            LOG.info("EVPN sync completed successfully")
            LOG.info("  Duration: %.2f seconds", duration)
            LOG.info("  Active networks: %d", len(self.evpn_networks))
            LOG.info("  Active VRFs: %d", len(self.network_mgr.vrfs))
            LOG.info("  Tracked ports: %d", len(self.evpn_ports))
            LOG.info("  VLAN stats: %s", self.vlan_mgr.get_stats())
            LOG.info("  FDB stats: %s", self.fdb_mgr.get_stats())
            LOG.info("=" * 80)

        except Exception as e:
            LOG.exception("EVPN sync failed: %s", e)
            self.evpn_networks = old_networks
            self.metrics['sync_errors'] += 1
            raise

    def _get_evpn_ports(self):
        """Get all Port_Bindings with EVPN config."""
        evpn_ports = []
        try:
            all_ports = self.sb_idl.db_list_rows('Port_Binding').execute()
            for port in all_ports:
                if (port.external_ids.get(constants.OVN_EVPN_VNI_EXT_ID_KEY) and
                        port.external_ids.get(constants.OVN_EVPN_AS_EXT_ID_KEY)):
                    evpn_ports.append(port)
        except Exception as e:
            LOG.error("Failed to get EVPN ports: %s", e)
        return evpn_ports

    def _sync_network(self, network_id, ports):
        """Sync single network."""
        LOG.debug("Syncing network %s with %d ports", network_id[:8], len(ports))

        network_info = self._build_network_info(network_id, ports[0])
        if not network_info:
            LOG.warning("Failed to build network info for %s", network_id[:8])
            return

        local_ip = self._get_local_vtep_ip()
        if not self.network_mgr.ensure_infrastructure(network_info, local_ip):
            LOG.warning("Failed to ensure infrastructure for %s", network_id[:8])
            return

        self.evpn_networks[network_id] = network_info

        # Batch sync ports
        self._sync_ports_batch(ports, network_info)

    def _build_network_info(self, network_id, sample_port):
        """Build network info from Port_Binding."""
        try:
            ext_ids = sample_port.external_ids
            datapath = sample_port.datapath

            network_name, vlan_tag = self.sb_idl.get_network_name_and_tag(
                str(datapath.uuid),
                self.ovs_idl.get_ovn_bridge_mappings().keys())

            if not vlan_tag:
                LOG.debug("Network %s has no VLAN tag", network_id[:8])
                return None

            ovn_vlan_id = vlan_tag[0]
            vni = int(ext_ids.get(constants.OVN_EVPN_VNI_EXT_ID_KEY))
            evpn_type = ext_ids.get(constants.OVN_EVPN_TYPE_EXT_ID_KEY, 'l3')
            bgp_as = ext_ids.get(constants.OVN_EVPN_AS_EXT_ID_KEY)

            bridge_vlan_id = self.vlan_mgr.allocate(network_id, vni)

            if evpn_type == 'l2':
                l2vni = vni
                l3vni = vni
            else:
                l2vni = None
                l3vni = vni

            return {
                'id': network_id,
                'ovn_vlan_id': ovn_vlan_id,
                'vlan_id': bridge_vlan_id,
                'l2vni': l2vni,
                'l3vni': l3vni,
                'vni': vni,
                'type': evpn_type,
                'bgp_as': bgp_as,
                'route_targets': self.ovn_helper.parse_route_targets(ext_ids),
                'route_distinguishers': self.ovn_helper.parse_route_distinguishers(ext_ids),
                'import_targets': self.ovn_helper.parse_import_targets(ext_ids),
                'export_targets': self.ovn_helper.parse_export_targets(ext_ids),
                'local_pref': ext_ids.get(constants.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY),
                'mtu': self.ovn_helper.get_network_mtu(datapath),
            }

        except Exception as e:
            LOG.exception("Failed to build network info: %s", e)
            return None

    def _sync_ports_batch(self, ports, network_info):
        """批量同步端口."""
        fdb_entries = []
        neighbor_entries = []

        for port in ports:
            try:
                port_info = self.ovn_helper.extract_port_info(port)
                if not port_info:
                    continue

                if network_info.get('l2vni'):
                    fdb_entries.append({
                        'mac': port_info['mac'],
                        'vlan': network_info['vlan_id'],
                    })

                if network_info.get('l3vni') and port_info.get('ips'):
                    irb_device = f"{CONF.evpn_bridge}.{network_info['vlan_id']}"
                    for ip in port_info['ips']:
                        neighbor_entries.append({
                            'ip': ip,
                            'mac': port_info['mac'],
                            'device': irb_device,
                        })

                self.evpn_ports[port.logical_port] = {
                    'mac': port_info['mac'],
                    'ips': port_info.get('ips', []),
                    'network_id': network_info['id'],
                    'vlan_id': network_info['vlan_id'],
                }

            except Exception as e:
                LOG.warning("Failed to process port %s: %s",
                            port.logical_port, e)

        if fdb_entries:
            self.fdb_mgr.batch_add_fdb(
                fdb_entries, CONF.evpn_bridge, CONF.evpn_bridge_veth)

        if neighbor_entries:
            self.fdb_mgr.batch_add_neighbors(neighbor_entries)

    def _cleanup_orphaned_resources(self):
        """Clean up unused EVPN resources."""
        LOG.debug("Cleaning up orphaned resources")

        try:
            all_links = linux_net.get_interfaces()

            for link in all_links:
                if link.startswith(constants.OVN_EVPN_VXLAN_PREFIX):
                    vni_str = link.replace(constants.OVN_EVPN_VXLAN_PREFIX, '')
                    try:
                        vni = int(vni_str)
                        if not self._is_vni_in_use(vni):
                            LOG.warning("Deleting orphaned VXLAN: %s", link)
                            linux_net.delete_device(link)
                    except ValueError:
                        pass

                elif link.startswith(constants.OVN_EVPN_VRF_PREFIX):
                    if link not in self.network_mgr.vrfs:
                        LOG.warning("Deleting orphaned VRF: %s", link)
                        vni_str = link.replace(constants.OVN_EVPN_VRF_PREFIX, '')
                        try:
                            vni = int(vni_str)
                            evpn_info = {'vrf_name': link, 'vni': vni}
                            frr.vrf_reconfigure(evpn_info, 'del-vrf')
                        except Exception as e:
                            LOG.debug("Failed to delete VRF from FRR: %s", e)

                        if CONF.delete_vrf_on_disconnect:
                            linux_net.delete_device(link)

                elif link.startswith('evpn-'):
                    if not any(str(info['vni']) in link
                               for info in self.evpn_networks.values()
                               if info.get('l2vni')):
                        LOG.warning("Deleting orphaned internal port: %s", link)
                        self.network_mgr._cleanup_internal_port(link)

        except Exception as e:
            LOG.warning("Failed to clean up orphaned resources: %s", e)

    def _is_vni_in_use(self, vni):
        """Check if VNI is in use."""
        for net_info in self.evpn_networks.values():
            if net_info.get('vni') == vni:
                return True
        return False

    def _get_local_vtep_ip(self):
        """Get local VTEP IP address."""
        if CONF.evpn_local_ip:
            try:
                ip = ipaddress.ip_address(CONF.evpn_local_ip)
                return str(ip)
            except ValueError as e:
                LOG.error("Invalid evpn_local_ip %s: %s", CONF.evpn_local_ip, e)
                raise agent_exc.ConfOptionRequired(option='evpn_local_ip')

        if CONF.evpn_nic:
            try:
                ip_addrs = linux_net.get_ip_addresses(label=CONF.evpn_nic)
                for addr in ip_addrs:
                    addr_dict = dict(addr['attrs'])
                    if addr['family'] == constants.AF_INET:
                        vtep_ip = addr_dict['IFA_ADDRESS']
                        LOG.info("Using VTEP IP %s from NIC %s",
                                 vtep_ip, CONF.evpn_nic)
                        return vtep_ip
            except Exception as e:
                LOG.warning("Failed to get IP from %s: %s", CONF.evpn_nic, e)

        try:
            ip_addrs = linux_net.get_ip_addresses(label='lo')
            for addr in ip_addrs:
                addr_dict = dict(addr['attrs'])
                if addr['family'] == constants.AF_INET:
                    vtep_ip = addr_dict['IFA_ADDRESS']
                    if not vtep_ip.startswith('127.'):
                        LOG.info("Using VTEP IP %s from loopback", vtep_ip)
                        return vtep_ip
        except Exception as e:
            LOG.warning("Failed to get loopback IP: %s", e)

        raise agent_exc.ConfOptionRequired(option='evpn_local_ip or evpn_nic')

    # ========================================================================
    # 事件处理方法（简化版 - 委托给管理器）
    # ========================================================================

    @lockutils.synchronized('evpn')
    def expose_subnet(self, row):
        """Handle subnet attachment to router with EVPN config."""
        LOG.info("Exposing EVPN subnet for port %s", row.logical_port)

        try:
            datapath = row.datapath
            network_id = str(datapath.uuid)

            network_info = self._build_network_info(network_id, row)
            if not network_info:
                return

            LOG.info("Network config: Bridge VLAN=%s, VNI=%s, Type=%s",
                     network_info['vlan_id'], network_info['vni'], network_info['type'])

            local_ip = self._get_local_vtep_ip()
            if self.network_mgr.ensure_infrastructure(network_info, local_ip):
                self.evpn_networks[network_id] = network_info

        except Exception as e:
            LOG.exception("Failed to expose EVPN subnet: %s", e)

    @lockutils.synchronized('evpn')
    def withdraw_subnet(self, row):
        """Handle subnet detachment from router."""
        LOG.info("Withdrawing EVPN subnet for port %s", row.logical_port)

        try:
            datapath = row.datapath
            network_id = str(datapath.uuid)

            if network_id in self.evpn_networks:
                network_info = self.evpn_networks[network_id]
                self.network_mgr.cleanup_infrastructure(network_info)
                self.fdb_mgr.cleanup_device(CONF.evpn_bridge)
                del self.evpn_networks[network_id]

        except Exception as e:
            LOG.exception("Failed to withdraw EVPN subnet: %s", e)

    @lockutils.synchronized('evpn')
    def expose_ip(self, row, cr_lrp=False):
        """Handle port binding to local chassis."""
        if cr_lrp:
            return

        try:
            network_id = str(row.datapath.uuid)
            if network_id not in self.evpn_networks:
                return

            network_info = self.evpn_networks[network_id]
            port_info = self.ovn_helper.extract_port_info(row)
            if not port_info:
                return

            mac_address = port_info['mac']
            ip_addresses = port_info.get('ips', [])

            LOG.info("Adding FDB/neighbor for %s: MAC=%s, IPs=%s",
                     row.logical_port, mac_address, ip_addresses)

            # Add FDB
            if network_info.get('l2vni'):
                self.fdb_mgr.ensure_fdb_entry(
                    mac_address, network_info['vlan_id'],
                    CONF.evpn_bridge, CONF.evpn_bridge_veth)

            # Add neighbors
            if network_info.get('l3vni') and ip_addresses:
                irb_device = f"{CONF.evpn_bridge}.{network_info['vlan_id']}"
                for ip in ip_addresses:
                    self.fdb_mgr.ensure_neighbor_entry(ip, mac_address, irb_device)

            self.evpn_ports[row.logical_port] = {
                'mac': mac_address,
                'ips': ip_addresses,
                'network_id': network_id,
                'vlan_id': network_info['vlan_id'],
            }

        except Exception as e:
            LOG.exception("Failed to expose IP: %s", e)

    @lockutils.synchronized('evpn')
    def withdraw_ip(self, row, cr_lrp=False):
        """Handle port unbinding."""
        try:
            if row.logical_port in self.evpn_ports:
                del self.evpn_ports[row.logical_port]
        except Exception as e:
            LOG.exception("Failed to withdraw IP: %s", e)

    @lockutils.synchronized('evpn')
    def expose_remote_ip(self, ips, row):
        """Handle tenant port on remote chassis (no-op)."""
        pass

    @lockutils.synchronized('evpn')
    def withdraw_remote_ip(self, ips, row, chassis=None):
        """Handle remote port removal (no-op)."""
        pass

    @lockutils.synchronized('evpn')
    def expose_port_association(self, row):
        """Handle port-specific EVPN configuration."""
        LOG.info("Exposing port association for port %s", row.logical_port)

        try:
            ext_ids = row.external_ids
            network_id = str(row.datapath.uuid)

            # 复用或创建网络基础设施
            if network_id not in self.evpn_networks:
                network_info = self._build_network_info(network_id, row)
                if not network_info:
                    return

                local_ip = self._get_local_vtep_ip()
                if not self.network_mgr.ensure_infrastructure(network_info, local_ip):
                    LOG.error("Failed to ensure infrastructure for port %s",
                              row.logical_port)
                    return
                self.evpn_networks[network_id] = network_info
            else:
                network_info = self.evpn_networks[network_id]

            # Parse port MAC/IPs
            port_info = self.ovn_helper.extract_port_info(row)
            if not port_info:
                LOG.warning("Port %s has no MAC", row.logical_port)
                return

            mac_address = port_info['mac']
            ip_addresses = port_info.get('ips', [])

            # Add FDB for L2
            if network_info.get('l2vni'):
                self.fdb_mgr.ensure_fdb_entry(
                    mac_address,
                    network_info['vlan_id'],
                    CONF.evpn_bridge,
                    CONF.evpn_bridge_veth
                )

            # Add neighbors
            if network_info.get('l3vni') and ip_addresses:
                irb_device = f"{CONF.evpn_bridge}.{network_info['vlan_id']}"
                for ip in ip_addresses:
                    self.fdb_mgr.ensure_neighbor_entry(ip, mac_address, irb_device)

            # Process custom routes
            routes_str = ext_ids.get('neutron_bgpvpn:routes')
            if routes_str:
                self._add_port_custom_routes(routes_str, network_info, ip_addresses)

            # Track port
            self.evpn_ports[row.logical_port] = {
                'mac': mac_address,
                'ips': ip_addresses,
                'network_id': network_id,
                'vlan_id': network_info['vlan_id'],
            }

            LOG.info("Port association exposed for %s", row.logical_port)

        except Exception as e:
            LOG.exception("Failed to expose port association: %s", e)

    @lockutils.synchronized('evpn')
    def withdraw_port_association(self, row):
        """Withdraw port association."""
        LOG.info("Withdrawing port association for port %s", row.logical_port)

        try:
            if row.logical_port in self.evpn_ports:
                del self.evpn_ports[row.logical_port]
            LOG.info("Port association withdrawn for %s", row.logical_port)
        except Exception as e:
            LOG.exception("Failed to withdraw port association: %s", e)

    def _add_port_custom_routes(self, routes_str, network_info, port_ips):
        """Add custom routes from port association."""
        try:
            routes = json.loads(routes_str)

            if not network_info.get('l3vni'):
                LOG.warning("Cannot add custom routes without L3VNI")
                return

            vrf_name = f'{constants.OVN_EVPN_VRF_PREFIX}{network_info["vni"]}'

            if vrf_name not in self.network_mgr.vrfs:
                LOG.warning("VRF %s not found", vrf_name)
                return

            table_id = self.network_mgr.vrfs[vrf_name]['table_id']

            for route in routes:
                dst = route.get('destination')
                nexthop = route.get('nexthop')

                if dst and nexthop:
                    LOG.info("Adding custom route: %s via %s (table %s)",
                             dst, nexthop, table_id)

                    try:
                        ip_version = 4 if ':' not in dst else 6
                        linux_net.route_create({
                            'dst': dst,
                            'gateway': nexthop,
                            'table': table_id,
                            'family': constants.AF_INET if ip_version == 4
                            else constants.AF_INET6,
                        })
                    except Exception as e:
                        LOG.warning("Failed to add route %s: %s", dst, e)

        except (json.JSONDecodeError, KeyError) as e:
            LOG.warning("Failed to parse custom routes: %s", e)

    # ========================================================================
    # FRR 同步
    # ========================================================================

    @lockutils.synchronized('evpn')
    def frr_sync(self):
        """Sync FRR configuration."""
        LOG.debug("Syncing FRR EVPN configuration")

        try:
            frr.ensure_evpn_base_config()

            for vrf_name, vrf_info in self.network_mgr.vrfs.items():
                vni = vrf_info.get('vni')
                network_ids = vrf_info.get('networks', [])

                if network_ids:
                    network_info = self.evpn_networks.get(network_ids[0])
                    if network_info:
                        evpn_info = {
                            'vrf_name': vrf_name,
                            'vni': vni,
                            'bgp_as': network_info.get('bgp_as', CONF.bgp_AS),
                            'route_targets': network_info.get('route_targets', []),
                            'route_distinguishers': network_info.get('route_distinguishers', []),
                            'import_targets': network_info.get('import_targets', []),
                            'export_targets': network_info.get('export_targets', []),
                            'local_ip': self._get_local_vtep_ip(),
                        }

                        if network_info.get('local_pref'):
                            evpn_info['local_pref'] = network_info['local_pref']

                        frr.vrf_reconfigure(evpn_info, 'add-vrf')

        except Exception as e:
            LOG.exception("FRR sync failed: %s", e)

    # ========================================================================
    # 指标方法
    # ========================================================================

    def get_metrics(self):
        """Get driver metrics."""
        vlan_stats = self.vlan_mgr.get_stats()
        fdb_stats = self.fdb_mgr.get_stats()

        return {
            'evpn_networks_total': len(self.evpn_networks),
            'evpn_networks_l2': sum(1 for n in self.evpn_networks.values()
                                    if n.get('type') == 'l2'),
            'evpn_networks_l3': sum(1 for n in self.evpn_networks.values()
                                    if n.get('type') == 'l3'),
            'evpn_vrfs_total': len(self.network_mgr.vrfs),
            'evpn_ports_total': len(self.evpn_ports),
            'evpn_fdb_entries_total': fdb_stats['fdb_entries_total'],
            'evpn_neighbor_entries_total': fdb_stats['neighbor_entries_total'],
            'evpn_vlan_allocated': vlan_stats['total_allocated'],
            'evpn_vlan_free': vlan_stats['free_vlans'],
            'evpn_vlan_conflicts': vlan_stats['conflicts'],
            'sync_count': self.metrics['sync_count'],
            'sync_duration_seconds': self.metrics['sync_duration'],
            'sync_errors_total': self.metrics['sync_errors'],
            'last_sync_timestamp': self.metrics['last_sync_time'],
        }