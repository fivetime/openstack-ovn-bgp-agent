# Copyright 2025 Red Hat, Inc.
# Copyright 2024 Tore Anderson (evpn_agent design concepts)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""OVN EVPN Driver - Complete L2VNI/L3VNI/IRB Implementation

This driver works with networking-bgpvpn to provide full EVPN support:
- Reads EVPN configuration from OVN external_ids (written by networking-bgpvpn)
- Configures data plane: VXLAN devices, VRFs, IRB devices
- Integrates with FRR for BGP EVPN signaling
- Manages static FDB and neighbor entries for optimization

Architecture:
  [networking-bgpvpn] → [OVN NB external_ids] → [ovn-bgp-agent/this driver]
      → [VXLAN/VRF/IRB] + [FRR EVPN]
"""

import collections
import ipaddress
import json
import threading

from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_log import log as logging

from ovn_bgp_agent import constants
from ovn_bgp_agent.drivers import driver_api
from ovn_bgp_agent.drivers.openstack.utils import driver_utils
from ovn_bgp_agent.drivers.openstack.utils import frr
from ovn_bgp_agent.drivers.openstack.utils import ovn
from ovn_bgp_agent.drivers.openstack.utils import ovs
from ovn_bgp_agent.drivers.openstack.watchers import evpn_watcher
from ovn_bgp_agent import exceptions as agent_exc
from ovn_bgp_agent.utils import linux_net


CONF = cfg.CONF
LOG = logging.getLogger(__name__)

# OVN tables to monitor
OVN_TABLES = [
    "Port_Binding",
    "Chassis",
    "Datapath_Binding",
    "Load_Balancer",
    "Chassis_Private",
]


class OVNEVPNDriver(driver_api.AgentDriverBase):
    """OVN EVPN Driver with full L2VNI/L3VNI/symmetric IRB support.

    Key Features:
    - L2VNI: VXLAN-based Layer 2 extension
    - L3VNI: Symmetric IRB with VRF per tenant
    - Static FDB/Neighbor entries for EVPN Type-2 routes
    - FRR integration for BGP EVPN (Type-2 MACIP, Type-5 Prefix)

    EVPN Configuration Source:
    - networking-bgpvpn writes to OVN NB Logical_Switch.external_ids
    - OVN syncs to SB Port_Binding.external_ids (patch ports)
    - This driver reads from Port_Binding and configures data plane
    """

    def __init__(self):
        super().__init__()

        # Tracked EVPN networks: {network_id: network_info}
        self.evpn_networks = {}

        # Tracked VRFs: {vrf_name: vrf_info}
        self.evpn_vrfs = {}

        # Tracked ports: {port_id: port_info}
        self.evpn_ports = {}

        # FDB entries: {bridge: [(mac, vlan), ...]}
        self.bridge_fdb_entries = collections.defaultdict(list)

        # Static neighbors: {irb_device: [(ip, mac), ...]}
        self.static_neighbors = collections.defaultdict(list)

        # OVN connection handles
        self._sb_idl = None
        self._post_fork_event = threading.Event()

    @property
    def sb_idl(self):
        """Lazy-initialized SB IDL"""
        if not self._sb_idl:
            self._post_fork_event.wait()
        return self._sb_idl

    @sb_idl.setter
    def sb_idl(self, val):
        self._sb_idl = val

    def start(self):
        """Initialize EVPN driver and set up infrastructure."""
        LOG.info("=" * 80)
        LOG.info("Starting OVN EVPN Driver")
        LOG.info("=" * 80)

        # Initialize OVS connection
        self.ovs_idl = ovs.OvsIdl()
        self.ovs_idl.start(CONF.ovsdb_connection)
        self.chassis = self.ovs_idl.get_own_chassis_id()
        self.ovn_remote = self.ovs_idl.get_ovn_remote()
        LOG.info("Loaded chassis: %s", self.chassis)
        LOG.info("OVN remote: %s", self.ovn_remote)

        # Validate configuration
        if CONF.exposing_method not in [
            constants.EXPOSE_METHOD_VRF,
            constants.EXPOSE_METHOD_DYNAMIC
        ]:
            LOG.error("EVPN driver requires exposing_method=vrf or dynamic")
            raise agent_exc.UnsupportedWiringConfig(
                method=CONF.exposing_method)

        LOG.info("EVPN exposing method: %s", CONF.exposing_method)

        # Set up EVPN prerequisites
        try:
            self._ensure_evpn_prerequisites()
        except Exception as e:
            LOG.exception("Failed to set up EVPN prerequisites: %s", e)
            raise

        # Clear VRF routes on startup if configured
        if CONF.clear_vrf_routes_on_startup:
            LOG.info("Clearing VRF routes on startup")
            linux_net.delete_routes_from_table(CONF.bgp_vrf_table_id)

        # Start OVN SB IDL with event watchers
        LOG.info("Starting OVN SB IDL with EVPN watchers")
        self._post_fork_event.clear()
        events = self._get_events()
        self.sb_idl = ovn.OvnSbIdl(
            self.ovn_remote,
            chassis=self.chassis,
            tables=OVN_TABLES,
            events=events
        ).start()
        self._post_fork_event.set()

        LOG.info("OVN EVPN Driver started successfully")
        LOG.info("=" * 80)

    def _ensure_evpn_prerequisites(self):
        """Ensure EVPN prerequisites are configured.

        - Main EVPN bridge (br-evpn)
        - Veth pair to OVS (veth-to-ovs <-> veth-to-evpn)
        - Base FRR EVPN configuration
        """
        LOG.info("Setting up EVPN prerequisites")

        # Get configuration
        bridge_name = CONF.evpn_bridge
        evpn_veth = CONF.evpn_bridge_veth
        ovs_veth = CONF.evpn_ovs_veth
        ovs_bridge = CONF.ovs_bridge

        # Create main EVPN bridge
        LOG.info("Creating EVPN bridge: %s", bridge_name)
        linux_net.ensure_bridge(bridge_name)

        # Set bridge properties
        linux_net.set_device_state(bridge_name, 'up')

        # Create veth pair if not exists
        if not driver_utils.get_interface(evpn_veth):
            LOG.info("Creating veth pair: %s <-> %s", evpn_veth, ovs_veth)
            driver_utils.add_veth(evpn_veth, ovs_veth)

        # Attach evpn_veth to EVPN bridge
        linux_net.set_master_for_device(evpn_veth, bridge_name)
        linux_net.set_device_state(evpn_veth, 'up')

        # Attach ovs_veth to OVS bridge
        linux_net.set_device_state(ovs_veth, 'up')
        self._ensure_ovs_veth_port(ovs_veth, ovs_bridge)

        # Configure base FRR EVPN
        LOG.info("Configuring base FRR EVPN settings")
        frr.ensure_evpn_base_config()

        LOG.info("EVPN prerequisites ready")

    def _ensure_ovs_veth_port(self, veth_name, bridge_name):
        """Ensure veth is added to OVS bridge."""
        try:
            # Check if port already exists
            from ovn_bgp_agent.privileged import ovs_vsctl

            ports = ovs_vsctl.ovs_cmd(
                'list-ports', [bridge_name])

            if veth_name not in ports:
                LOG.info("Adding %s to OVS bridge %s", veth_name, bridge_name)
                ovs_vsctl.ovs_cmd('add-port', [bridge_name, veth_name])
        except Exception as e:
            LOG.warning("Failed to add OVS port %s: %s", veth_name, e)

    def _get_events(self):
        """Get event watchers for EVPN driver."""
        LOG.debug("Registering EVPN event watchers")

        events = {
            # EVPN-specific events (from evpn_watcher.py)
            evpn_watcher.SubnetRouterAttachedEvent(self),
            evpn_watcher.SubnetRouterDetachedEvent(self),
            evpn_watcher.PortBindingChassisCreatedEvent(self),
            evpn_watcher.PortBindingChassisDeletedEvent(self),
            evpn_watcher.LocalnetCreateDeleteEvent(self),
            evpn_watcher.PortAssociationCreatedEvent(self),
            evpn_watcher.PortAssociationDeletedEvent(self),
        }

        # Add tenant network events if enabled
        if CONF.expose_tenant_networks:
            LOG.info("Enabling tenant network exposure")
            events.update({
                evpn_watcher.TenantPortCreatedEvent(self),
                evpn_watcher.TenantPortDeletedEvent(self),
            })

        return events

    # =========================================================================
    # Sync operations
    # =========================================================================

    @lockutils.synchronized('evpn')
    def sync(self):
        """Synchronize EVPN state with OVN database.

        Called periodically to ensure all EVPN resources match OVN state.
        """
        LOG.info("=" * 80)
        LOG.info("Starting EVPN sync")
        LOG.info("=" * 80)

        # Reset tracking structures
        self.evpn_networks = {}
        self.evpn_ports = {}
        self.bridge_fdb_entries = collections.defaultdict(list)
        self.static_neighbors = collections.defaultdict(list)

        # Get all Port_Bindings with EVPN configuration
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

        # Process each network
        for network_id, ports in networks_with_ports.items():
            self._sync_network(network_id, ports)

        # Clean up orphaned resources
        self._cleanup_orphaned_resources()

        LOG.info("EVPN sync completed")
        LOG.info("  Active networks: %d", len(self.evpn_networks))
        LOG.info("  Active VRFs: %d", len(self.evpn_vrfs))
        LOG.info("  Tracked ports: %d", len(self.evpn_ports))
        LOG.info("=" * 80)

    def _get_evpn_ports(self):
        """Get all Port_Bindings with EVPN external_ids."""
        evpn_ports = []

        try:
            all_ports = self.sb_idl.db_list_rows('Port_Binding').execute()

            for port in all_ports:
                # Check if port has EVPN configuration
                if (port.external_ids.get(constants.OVN_EVPN_VNI_EXT_ID_KEY) and
                        port.external_ids.get(constants.OVN_EVPN_AS_EXT_ID_KEY)):
                    evpn_ports.append(port)

        except Exception as e:
            LOG.error("Failed to get EVPN ports: %s", e)

        return evpn_ports

    def _sync_network(self, network_id, ports):
        """Sync a single network and its ports.

        :param network_id: OVN datapath UUID
        :param ports: List of Port_Binding rows with EVPN config
        """
        LOG.debug("Syncing network %s with %d ports", network_id, len(ports))

        # Extract EVPN configuration from first port (all should be same)
        network_info = self._build_network_info(network_id, ports[0])
        if not network_info:
            LOG.warning("Failed to build network info for %s", network_id)
            return

        # Ensure network infrastructure (VNI, VRF, IRB)
        if not self._ensure_network_infrastructure(network_info):
            LOG.warning("Failed to ensure infrastructure for %s", network_id)
            return

        # Store network info
        self.evpn_networks[network_id] = network_info

        # Process all ports on this network
        for port in ports:
            self._sync_port(port, network_info)

    def _build_network_info(self, network_id, sample_port):
        """Build network info dict from Port_Binding external_ids.

        :param network_id: Network datapath UUID
        :param sample_port: Sample Port_Binding with EVPN config
        :return: Dict with network info or None
        """
        try:
            ext_ids = sample_port.external_ids

            # Get VLAN ID from network
            datapath = sample_port.datapath
            network_name, vlan_tag = self.sb_idl.get_network_name_and_tag(
                str(datapath.uuid),
                self.ovs_idl.get_ovn_bridge_mappings().keys())

            if not vlan_tag:
                LOG.debug("Network %s has no VLAN tag", network_id)
                return None

            vlan_id = vlan_tag[0]

            # Extract EVPN configuration from external_ids
            vni = int(ext_ids.get(constants.OVN_EVPN_VNI_EXT_ID_KEY))
            evpn_type = ext_ids.get(
                constants.OVN_EVPN_TYPE_EXT_ID_KEY, 'l3')
            bgp_as = ext_ids.get(constants.OVN_EVPN_AS_EXT_ID_KEY)

            # Parse route targets (JSON array)
            route_targets = self._parse_route_targets(ext_ids)

            # Parse route distinguishers (JSON array)
            route_distinguishers = self._parse_route_distinguishers(ext_ids)

            # Parse import/export targets
            import_targets = self._parse_import_targets(ext_ids)
            export_targets = self._parse_export_targets(ext_ids)

            # Parse local preference
            local_pref = ext_ids.get(constants.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY)

            # Get MTU from datapath or config
            mtu = self._get_network_mtu(datapath)

            # Determine L2VNI and L3VNI based on type
            if evpn_type == 'l2':
                l2vni = vni
                l3vni = None
            elif evpn_type == 'l3':
                l2vni = None
                l3vni = vni
            else:
                # Default: use as L3VNI
                l2vni = None
                l3vni = vni

            # Check if l2vni_offset is configured
            if l2vni is None and CONF.l2vni_offset:
                l2vni = vlan_id + int(CONF.l2vni_offset)
                LOG.debug("Calculated L2VNI=%d from VLAN %d + offset %d",
                          l2vni, vlan_id, CONF.l2vni_offset)

            return {
                'id': network_id,
                'vlan_id': vlan_id,
                'l2vni': l2vni,
                'l3vni': l3vni,
                'type': evpn_type,
                'bgp_as': bgp_as,
                'route_targets': route_targets,
                'route_distinguishers': route_distinguishers,
                'import_targets': import_targets,
                'export_targets': export_targets,
                'local_pref': local_pref,
                'mtu': mtu,
            }

        except Exception as e:
            LOG.exception("Failed to build network info: %s", e)
            return None

    def _sync_port(self, port, network_info):
        """Sync single port - add FDB and neighbor entries.

        :param port: Port_Binding row
        :param network_info: Network information dict
        """
        if not port.mac or port.mac == ['unknown']:
            LOG.debug("Port %s has no MAC, skipping", port.logical_port)
            return

        # Parse MAC and IPs
        try:
            mac_ips = port.mac[0].strip().split()
            if len(mac_ips) < 1:
                return

            mac_address = mac_ips[0]
            ip_addresses = mac_ips[1:] if len(mac_ips) > 1 else []
        except (IndexError, AttributeError) as e:
            LOG.debug("Failed to parse port MAC/IPs: %s", e)
            return

        vlan_id = network_info['vlan_id']
        bridge_name = CONF.evpn_bridge

        # Add static FDB entry for L2VNI
        if network_info.get('l2vni'):
            self._ensure_fdb_entry(mac_address, vlan_id, bridge_name)

        # Add static neighbor entries for L3VNI
        if network_info.get('l3vni') is not None and ip_addresses:
            irb_device = f'{bridge_name}.{vlan_id}'
            for ip in ip_addresses:
                self._ensure_neighbor_entry(ip, mac_address, irb_device)

        # Track port
        self.evpn_ports[port.logical_port] = {
            'mac': mac_address,
            'ips': ip_addresses,
            'network_id': network_info['id'],
            'vlan_id': vlan_id,
        }

    # =========================================================================
    # Network infrastructure management
    # =========================================================================

    def _ensure_network_infrastructure(self, network_info):
        """Ensure network EVPN infrastructure is configured.

        Creates:
        - L2VNI VXLAN device (if l2vni configured)
        - VRF (if l3vni configured)
        - L3VNI VXLAN device (if l3vni > 0)
        - IRB device (if l3vni configured)

        :param network_info: Network information dict
        :return: True if successful
        """
        network_id = network_info['id']
        vlan_id = network_info['vlan_id']
        l2vni = network_info['l2vni']
        l3vni = network_info['l3vni']

        LOG.info("Ensuring infrastructure for network %s", network_id)
        LOG.info("  VLAN: %s, L2VNI: %s, L3VNI: %s, Type: %s",
                 vlan_id, l2vni, l3vni, network_info['type'])

        try:
            bridge_name = CONF.evpn_bridge
            local_ip = self._get_local_vtep_ip()

            # Create L2VNI if configured
            if l2vni:
                self._ensure_l2vni(l2vni, vlan_id, bridge_name, local_ip,
                                   network_info['mtu'])

            # Create VRF and IRB if L3VNI configured
            if l3vni is not None:
                self._ensure_l3vni_infrastructure(
                    l3vni, vlan_id, bridge_name, local_ip,
                    network_info)

            return True

        except Exception as e:
            LOG.exception("Failed to ensure infrastructure for %s: %s",
                          network_id, e)
            return False

    def _ensure_l2vni(self, l2vni, vlan_id, bridge_name, local_ip, mtu):
        """Create L2VNI VXLAN device.

        :param l2vni: L2VNI number
        :param vlan_id: VLAN ID
        :param bridge_name: Bridge name
        :param local_ip: Local VTEP IP
        :param mtu: MTU size
        """
        vxlan_name = f'{constants.OVN_EVPN_VXLAN_PREFIX}{l2vni}'

        LOG.info("Creating L2VNI %s (device: %s)", l2vni, vxlan_name)

        # Create VXLAN device
        linux_net.ensure_vxlan(
            vxlan_name,
            l2vni,
            local_ip,
            dstport=CONF.evpn_udp_dstport
        )

        # Set MTU
        driver_utils.set_device_mtu(vxlan_name, mtu)

        # Attach to bridge
        linux_net.set_master_for_device(vxlan_name, bridge_name)

        # Disable learning (EVPN controls FDB)
        linux_net.set_bridge_port_learning(vxlan_name, False)
        linux_net.set_bridge_port_neigh_suppress(vxlan_name, True)

        # Bring up
        linux_net.set_device_state(vxlan_name, 'up')

        # Add VLAN to bridge and VXLAN port
        linux_net.ensure_bridge_vlan(bridge_name, vlan_id,
                                     tagged=True, pvid=False, untagged=False)

        # Make VLAN untagged on VXLAN port
        veth_port = CONF.evpn_bridge_veth
        linux_net.ensure_bridge_vlan(veth_port, vlan_id,
                                     tagged=False, pvid=True, untagged=True)

        LOG.debug("L2VNI %s created successfully", l2vni)

    def _ensure_l3vni_infrastructure(self, l3vni, vlan_id, bridge_name,
                                     local_ip, network_info):
        """Create L3VNI, VRF, and IRB infrastructure.

        :param l3vni: L3VNI number (0 for underlay leak, >0 for EVPN)
        :param vlan_id: VLAN ID
        :param bridge_name: Bridge name
        :param local_ip: Local VTEP IP
        :param network_info: Network info dict
        """
        vrf_name = f'{constants.OVN_EVPN_VRF_PREFIX}{l3vni}'
        table_id = l3vni + 1000000  # Large offset

        LOG.info("Creating L3VNI infrastructure: VRF=%s, L3VNI=%s",
                 vrf_name, l3vni)

        # Create or reuse VRF
        if vrf_name not in self.evpn_vrfs:
            linux_net.ensure_vrf(vrf_name, table_id)
            linux_net.set_device_state(vrf_name, 'up')

            # Configure FRR for this VRF
            leak_to_underlay = (l3vni == 0)

            # Build FRR EVPN info with all parameters
            evpn_info = {
                'vrf_name': vrf_name,
                'vni': l3vni if l3vni > 0 else 0,
                'bgp_as': network_info['bgp_as'],
                'route_targets': network_info.get('route_targets', []),
                'route_distinguishers': network_info.get('route_distinguishers', []),
                'import_targets': network_info.get('import_targets', []),
                'export_targets': network_info.get('export_targets', []),
                'local_ip': local_ip,
            }

            # Add local preference if specified
            if network_info.get('local_pref'):
                evpn_info['local_pref'] = network_info['local_pref']

            # Configure FRR VRF
            frr.vrf_reconfigure(evpn_info, 'add-vrf')

            # Configure route leaking if needed
            if leak_to_underlay:
                frr.vrf_leak(vrf_name, CONF.bgp_AS)

            self.evpn_vrfs[vrf_name] = {
                'table_id': table_id,
                'l3vni': l3vni,
                'networks': [],
            }

        self.evpn_vrfs[vrf_name]['networks'].append(network_info['id'])

        # Create L3VNI VXLAN device if l3vni > 0
        if l3vni > 0:
            l3vni_name = f'{constants.OVN_EVPN_VXLAN_PREFIX}l3-{l3vni}'
            irb_bridge = f'{constants.OVN_EVPN_BRIDGE_PREFIX}{l3vni}'

            # Create IRB bridge for L3VNI
            linux_net.ensure_bridge(irb_bridge)
            linux_net.set_master_for_device(irb_bridge, vrf_name)
            linux_net.set_device_state(irb_bridge, 'up')

            # Create L3VNI VXLAN
            linux_net.ensure_vxlan(
                l3vni_name,
                l3vni,
                local_ip,
                dstport=CONF.evpn_udp_dstport
            )

            driver_utils.set_device_mtu(l3vni_name, network_info['mtu'])
            linux_net.set_master_for_device(l3vni_name, irb_bridge)
            linux_net.set_bridge_port_learning(l3vni_name, False)
            linux_net.set_bridge_port_neigh_suppress(l3vni_name, True)
            linux_net.set_device_state(l3vni_name, 'up')

        # Create IRB (SVI) for this network
        self._ensure_irb_device(vlan_id, bridge_name, vrf_name, network_info)

    def _ensure_irb_device(self, vlan_id, bridge_name, vrf_name, network_info):
        """Create IRB device for network.

        :param vlan_id: VLAN ID
        :param bridge_name: Bridge name
        :param vrf_name: VRF name
        :param network_info: Network info dict
        """
        LOG.info("Creating IRB for VLAN %s in VRF %s", vlan_id, vrf_name)

        # Create VLAN device on bridge
        linux_net.ensure_vlan_device_for_network(bridge_name, vlan_id)
        irb_device = f'{bridge_name}.{vlan_id}'

        # Attach to VRF
        linux_net.set_master_for_device(irb_device, vrf_name)
        linux_net.set_device_state(irb_device, 'up')

        # Enable proxy ARP/NDP for anycast gateway
        linux_net.enable_proxy_arp(irb_device)
        linux_net.enable_proxy_ndp(irb_device)

        # Add gateway IPs (extracted from Port_Binding.mac)
        self._add_gateway_ips_to_irb(irb_device, network_info)

        LOG.debug("IRB %s created in VRF %s", irb_device, vrf_name)

    def _add_gateway_ips_to_irb(self, irb_device, network_info):
        """Add gateway IPs to IRB device.

        Gateway IPs are extracted from Port_Binding.mac field of router ports.
        Format: "MAC IP1 IP2 ..."

        :param irb_device: IRB device name
        :param network_info: Network info dict
        """
        # TODO: Query OVN for router interface ports and extract gateway IPs
        # For now, this is a placeholder
        LOG.debug("Gateway IP configuration for %s (will be extracted from router ports)",
                  irb_device)

    # =========================================================================
    # FDB and Neighbor management
    # =========================================================================

    def _ensure_fdb_entry(self, mac_address, vlan_id, bridge_device):
        """Ensure static FDB entry exists on bridge.

        :param mac_address: MAC address
        :param vlan_id: VLAN ID
        :param bridge_device: Bridge device name
        """
        if not CONF.evpn_static_fdb:
            return

        bridge_port = CONF.evpn_bridge_veth

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
                LOG.debug("Added FDB: %s VLAN %s on %s",
                          mac_address, vlan_id, bridge_port)
            except Exception as e:
                # Ignore if already exists
                if "File exists" not in str(e):
                    LOG.warning("Failed to add FDB %s: %s", mac_address, e)

    def _ensure_neighbor_entry(self, ip_address, mac_address, irb_device):
        """Ensure static neighbor entry exists.

        :param ip_address: IP address
        :param mac_address: MAC address
        :param irb_device: IRB device name
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
                # Ignore if already exists
                if "File exists" not in str(e):
                    LOG.warning("Failed to add neighbor %s: %s", ip_address, e)

    # =========================================================================
    # Cleanup operations
    # =========================================================================

    def _cleanup_orphaned_resources(self):
        """Clean up EVPN resources that are no longer needed."""
        LOG.debug("Cleaning up orphaned EVPN resources")

        try:
            all_links = linux_net.get_interfaces()

            # Find orphaned VXLANs
            for link in all_links:
                if link.startswith(constants.OVN_EVPN_VXLAN_PREFIX):
                    # Extract VNI from device name
                    vni_str = link.replace(constants.OVN_EVPN_VXLAN_PREFIX, '')
                    vni_str = vni_str.replace('l3-', '')  # Handle L3VNI prefix

                    try:
                        vni = int(vni_str)
                        if not self._is_vni_in_use(vni):
                            LOG.warning("Deleting orphaned VXLAN device: %s", link)
                            linux_net.delete_device(link)
                    except ValueError:
                        LOG.debug("Could not parse VNI from device: %s", link)

                # Find orphaned VRFs
                elif link.startswith(constants.OVN_EVPN_VRF_PREFIX):
                    if link not in self.evpn_vrfs:
                        LOG.warning("Deleting orphaned VRF: %s", link)
                        # Delete VRF from FRR first
                        l3vni_str = link.replace(constants.OVN_EVPN_VRF_PREFIX, '')
                        try:
                            l3vni = int(l3vni_str)
                            evpn_info = {'vrf_name': link, 'vni': l3vni}
                            frr.vrf_reconfigure(evpn_info, 'del-vrf')
                        except (ValueError, Exception) as e:
                            LOG.debug("Failed to delete VRF from FRR: %s", e)

                        # Delete VRF device
                        if CONF.delete_vrf_on_disconnect:
                            linux_net.delete_device(link)

        except Exception as e:
            LOG.warning("Failed to clean up orphaned resources: %s", e)

    def _is_vni_in_use(self, vni):
        """Check if VNI is currently in use.

        :param vni: VNI to check
        :return: True if in use
        """
        for net_info in self.evpn_networks.values():
            if net_info.get('l2vni') == vni or net_info.get('l3vni') == vni:
                return True
        return False

    def _get_local_vtep_ip(self):
        """Get local VTEP IP address.

        Priority:
        1. CONF.evpn_local_ip
        2. IP from CONF.evpn_nic
        3. First global IPv4 on loopback

        :return: IP address string
        """
        # Check config first
        if CONF.evpn_local_ip:
            LOG.debug("Using configured VTEP IP: %s", CONF.evpn_local_ip)
            return str(CONF.evpn_local_ip)

        # Check evpn_nic
        if CONF.evpn_nic:
            try:
                ip_addrs = linux_net.get_ip_addresses(label=CONF.evpn_nic)
                if ip_addrs:
                    # Get first IPv4 address
                    for addr in ip_addrs:
                        addr_dict = dict(addr['attrs'])
                        if addr['family'] == constants.AF_INET:
                            vtep_ip = addr_dict['IFA_ADDRESS']
                            LOG.debug("Using VTEP IP from %s: %s",
                                      CONF.evpn_nic, vtep_ip)
                            return vtep_ip
            except Exception as e:
                LOG.warning("Failed to get IP from %s: %s", CONF.evpn_nic, e)

        # Fallback to loopback
        try:
            ip_addrs = linux_net.get_ip_addresses(label='lo')
            for addr in ip_addrs:
                addr_dict = dict(addr['attrs'])
                if addr['family'] == constants.AF_INET:
                    vtep_ip = addr_dict['IFA_ADDRESS']
                    if not vtep_ip.startswith('127.'):
                        LOG.debug("Using loopback VTEP IP: %s", vtep_ip)
                        return vtep_ip
        except Exception as e:
            LOG.warning("Failed to get loopback IP: %s", e)

        raise agent_exc.ConfOptionRequired(option='evpn_local_ip or evpn_nic')

    # =========================================================================
    # Event handler methods (called by evpn_watcher)
    # =========================================================================

    @lockutils.synchronized('evpn')
    def expose_subnet(self, row):
        """Handle subnet attachment to router with EVPN config.

        Called by SubnetRouterAttachedEvent when a patch port gets
        EVPN external_ids added.

        :param row: Port_Binding row (type=patch)
        """
        LOG.info("Exposing EVPN subnet for port %s", row.logical_port)

        try:
            # Extract EVPN configuration from Port_Binding external_ids
            ext_ids = row.external_ids
            vni = int(ext_ids.get(constants.OVN_EVPN_VNI_EXT_ID_KEY))
            bgp_as = ext_ids.get(constants.OVN_EVPN_AS_EXT_ID_KEY)
            evpn_type = ext_ids.get(
                constants.OVN_EVPN_TYPE_EXT_ID_KEY, 'l3')

            # Get network information
            datapath = row.datapath
            network_id = str(datapath.uuid)

            # Get VLAN ID
            network_name, vlan_tag = self.sb_idl.get_network_name_and_tag(
                network_id,
                self.ovs_idl.get_ovn_bridge_mappings().keys())

            if not vlan_tag:
                LOG.warning("No VLAN tag for network %s", network_id)
                return

            vlan_id = vlan_tag[0]

            # Parse EVPN parameters
            route_targets = self._parse_route_targets(ext_ids)
            route_distinguishers = self._parse_route_distinguishers(ext_ids)
            import_targets = self._parse_import_targets(ext_ids)
            export_targets = self._parse_export_targets(ext_ids)
            local_pref = ext_ids.get(constants.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY)

            # Get MTU
            mtu = self._get_network_mtu(datapath)

            # Build network info
            network_info = {
                'id': network_id,
                'vlan_id': vlan_id,
                'l2vni': vni if evpn_type == 'l2' else None,
                'l3vni': vni if evpn_type == 'l3' else None,
                'type': evpn_type,
                'bgp_as': bgp_as,
                'route_targets': route_targets,
                'route_distinguishers': route_distinguishers,
                'import_targets': import_targets,
                'export_targets': export_targets,
                'local_pref': local_pref,
                'mtu': mtu,
            }

            # Auto-calculate L2VNI if configured
            if network_info['l2vni'] is None and CONF.l2vni_offset:
                network_info['l2vni'] = vlan_id + int(CONF.l2vni_offset)
                LOG.debug("Auto-calculated L2VNI: %d", network_info['l2vni'])

            LOG.info("Network config: VLAN=%s, L2VNI=%s, L3VNI=%s, Type=%s",
                     vlan_id, network_info['l2vni'],
                     network_info['l3vni'], evpn_type)

            # Configure EVPN infrastructure
            if self._ensure_network_infrastructure(network_info):
                self.evpn_networks[network_id] = network_info

                # Extract and add gateway IPs if this is a router port
                if evpn_type == 'l3':
                    self._process_router_port_gateway_ips(row, network_info)

        except Exception as e:
            LOG.exception("Failed to expose EVPN subnet: %s", e)

    @lockutils.synchronized('evpn')
    def withdraw_subnet(self, row):
        """Handle subnet detachment from router (EVPN config removed).

        Called by SubnetRouterDetachedEvent when EVPN external_ids
        are removed from a patch port.

        :param row: Port_Binding row (type=patch)
        """
        LOG.info("Withdrawing EVPN subnet for port %s", row.logical_port)

        try:
            datapath = row.datapath
            network_id = str(datapath.uuid)

            if network_id in self.evpn_networks:
                network_info = self.evpn_networks[network_id]

                LOG.info("Cleaning up network %s (VLAN %s)",
                         network_id, network_info['vlan_id'])

                # Remove network infrastructure
                self._cleanup_network_infrastructure(network_info)

                # Remove from tracking
                del self.evpn_networks[network_id]
            else:
                LOG.debug("Network %s not in tracked networks", network_id)

        except Exception as e:
            LOG.exception("Failed to withdraw EVPN subnet: %s", e)

    @lockutils.synchronized('evpn')
    def expose_ip(self, row, cr_lrp=False):
        """Handle port binding to local chassis.

        Called when:
        - VM port is bound to this chassis (TenantPortCreatedEvent)
        - Gateway port is bound to this chassis (PortBindingChassisCreatedEvent)

        :param row: Port_Binding row
        :param cr_lrp: True if chassisredirect port (gateway)
        """
        LOG.debug("expose_ip called for %s (cr_lrp=%s)",
                  row.logical_port, cr_lrp)

        try:
            # For gateway ports, just log and return
            if cr_lrp:
                LOG.debug("Gateway port bound: %s", row.logical_port)
                return

            # Check if port is on EVPN network
            datapath = row.datapath
            network_id = str(datapath.uuid)

            if network_id not in self.evpn_networks:
                LOG.debug("Port %s not on EVPN network", row.logical_port)
                return

            network_info = self.evpn_networks[network_id]

            # Parse MAC and IPs
            if not row.mac or row.mac == ['unknown']:
                LOG.debug("Port %s has no MAC", row.logical_port)
                return

            mac_ips = row.mac[0].strip().split()
            if len(mac_ips) < 1:
                return

            mac_address = mac_ips[0]
            ip_addresses = mac_ips[1:] if len(mac_ips) > 1 else []

            LOG.info("Adding FDB/neighbor for %s: MAC=%s, IPs=%s",
                     row.logical_port, mac_address, ip_addresses)

            # Add static FDB entry
            if network_info.get('l2vni'):
                vlan_id = network_info['vlan_id']
                bridge_name = CONF.evpn_bridge
                self._ensure_fdb_entry(mac_address, vlan_id, bridge_name)

            # Add static neighbor entries
            if network_info.get('l3vni') is not None and ip_addresses:
                vlan_id = network_info['vlan_id']
                bridge_name = CONF.evpn_bridge
                irb_device = f'{bridge_name}.{vlan_id}'

                for ip in ip_addresses:
                    self._ensure_neighbor_entry(ip, mac_address, irb_device)

            # Track port
            self.evpn_ports[row.logical_port] = {
                'mac': mac_address,
                'ips': ip_addresses,
                'network_id': network_id,
            }

        except Exception as e:
            LOG.exception("Failed to expose IP: %s", e)

    @lockutils.synchronized('evpn')
    def withdraw_ip(self, row, cr_lrp=False):
        """Handle port unbinding from local chassis.

        Called when:
        - VM port is unbound from this chassis
        - Gateway port is unbound from this chassis

        :param row: Port_Binding row
        :param cr_lrp: True if chassisredirect port
        """
        LOG.debug("withdraw_ip called for %s (cr_lrp=%s)",
                  row.logical_port, cr_lrp)

        try:
            # Remove from tracking
            if row.logical_port in self.evpn_ports:
                port_info = self.evpn_ports[row.logical_port]
                LOG.info("Removing port %s: MAC=%s, IPs=%s",
                         row.logical_port, port_info['mac'], port_info['ips'])
                del self.evpn_ports[row.logical_port]

            # Note: FDB and neighbor cleanup happens in sync()
            # to avoid race conditions

        except Exception as e:
            LOG.exception("Failed to withdraw IP: %s", e)

    @lockutils.synchronized('evpn')
    def expose_remote_ip(self, ips, row):
        """Handle tenant port on remote chassis.

        For EVPN, we rely on BGP EVPN Type-2 routes rather than
        explicit remote IP exposure. This is a no-op.

        :param ips: List of IP addresses
        :param row: Port_Binding row
        """
        LOG.debug("expose_remote_ip called for %s (no-op in EVPN mode)",
                  row.logical_port)

    @lockutils.synchronized('evpn')
    def withdraw_remote_ip(self, ips, row, chassis=None):
        """Handle tenant port removal on remote chassis.

        No-op for EVPN driver.

        :param ips: List of IP addresses
        :param row: Port_Binding row
        :param chassis: Chassis name
        """
        LOG.debug("withdraw_remote_ip called for %s (no-op in EVPN mode)",
                  row.logical_port)

    @lockutils.synchronized('evpn')
    def expose_port_association(self, row):
        """Handle port-specific EVPN configuration (Port Association).

        Port Association allows per-port EVPN config with custom routes.
        This is triggered when networking-bgpvpn creates a port association
        and writes EVPN external_ids to the Port_Binding.

        :param row: Port_Binding row (VM port with EVPN external_ids)
        """
        LOG.info("Exposing port association for port %s", row.logical_port)

        try:
            # Get EVPN config from port external_ids
            ext_ids = row.external_ids
            vni = int(ext_ids.get(constants.OVN_EVPN_VNI_EXT_ID_KEY))
            evpn_type = ext_ids.get(constants.OVN_EVPN_TYPE_EXT_ID_KEY, 'l3')
            bgp_as = ext_ids.get(constants.OVN_EVPN_AS_EXT_ID_KEY)

            # Get network info
            network_id = str(row.datapath.uuid)
            network_name, vlan_tag = self.sb_idl.get_network_name_and_tag(
                network_id,
                self.ovs_idl.get_ovn_bridge_mappings().keys())

            if not vlan_tag:
                LOG.warning("No VLAN tag for network %s", network_id)
                return

            vlan_id = vlan_tag[0]

            # Parse EVPN parameters
            route_targets = self._parse_route_targets(ext_ids)
            route_distinguishers = self._parse_route_distinguishers(ext_ids)
            import_targets = self._parse_import_targets(ext_ids)
            export_targets = self._parse_export_targets(ext_ids)
            local_pref = ext_ids.get(constants.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY)

            # Get MTU
            mtu = self._get_network_mtu(row.datapath)

            # Build network info (similar to expose_subnet)
            network_info = {
                'id': network_id,
                'vlan_id': vlan_id,
                'l2vni': vni if evpn_type == 'l2' else None,
                'l3vni': vni if evpn_type == 'l3' else None,
                'type': evpn_type,
                'bgp_as': bgp_as,
                'route_targets': route_targets,
                'route_distinguishers': route_distinguishers,
                'import_targets': import_targets,
                'export_targets': export_targets,
                'local_pref': local_pref,
                'mtu': mtu,
            }

            # Auto-calculate L2VNI if needed
            if network_info['l2vni'] is None and CONF.l2vni_offset:
                network_info['l2vni'] = vlan_id + int(CONF.l2vni_offset)

            # Ensure infrastructure exists
            if not self._ensure_network_infrastructure(network_info):
                LOG.error("Failed to ensure infrastructure for port %s",
                          row.logical_port)
                return

            # Store network info
            self.evpn_networks[network_id] = network_info

            # Parse port MAC/IPs
            if not row.mac or row.mac == ['unknown']:
                LOG.warning("Port %s has no MAC", row.logical_port)
                return

            mac_ips = row.mac[0].strip().split()
            if len(mac_ips) < 1:
                return

            mac_address = mac_ips[0]
            ip_addresses = mac_ips[1:] if len(mac_ips) > 1 else []

            # Add FDB entry for L2
            if network_info.get('l2vni'):
                self._ensure_fdb_entry(mac_address, vlan_id, CONF.evpn_bridge)

            # Add neighbor entries for L3
            if network_info.get('l3vni') is not None and ip_addresses:
                irb_device = f'{CONF.evpn_bridge}.{vlan_id}'
                for ip in ip_addresses:
                    self._ensure_neighbor_entry(ip, mac_address, irb_device)

            # Process custom routes from port association
            routes_str = ext_ids.get('neutron_bgpvpn:routes')
            if routes_str:
                self._add_port_custom_routes(routes_str, network_info,
                                             ip_addresses)

            # Track port
            self.evpn_ports[row.logical_port] = {
                'mac': mac_address,
                'ips': ip_addresses,
                'network_id': network_id,
                'vlan_id': vlan_id,
            }

            LOG.info("Port association exposed for %s", row.logical_port)

        except Exception as e:
            LOG.exception("Failed to expose port association: %s", e)

    @lockutils.synchronized('evpn')
    def withdraw_port_association(self, row):
        """Withdraw port association.

        :param row: Port_Binding row
        """
        LOG.info("Withdrawing port association for port %s", row.logical_port)

        try:
            # Simply remove from tracking
            # Infrastructure cleanup happens in sync()
            if row.logical_port in self.evpn_ports:
                del self.evpn_ports[row.logical_port]

            LOG.info("Port association withdrawn for %s", row.logical_port)

        except Exception as e:
            LOG.exception("Failed to withdraw port association: %s", e)

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _process_router_port_gateway_ips(self, port_binding, network_info):
        """Extract gateway IPs from router port and add to IRB.

        Router interface ports (patch ports) have gateway IPs in their
        mac field: "MAC IP1 IP2 ..."

        :param port_binding: Port_Binding row
        :param network_info: Network info dict
        """
        try:
            if not port_binding.mac or port_binding.mac == ['unknown']:
                return

            mac_ips = port_binding.mac[0].strip().split()
            if len(mac_ips) < 2:
                return

            gateway_ips = mac_ips[1:]  # Skip MAC address
            vlan_id = network_info['vlan_id']
            bridge_name = CONF.evpn_bridge
            irb_device = f'{bridge_name}.{vlan_id}'

            LOG.info("Adding gateway IPs to %s: %s", irb_device, gateway_ips)

            for gw_ip in gateway_ips:
                # Need to determine correct prefix length
                # For now, use common defaults
                try:
                    ip_obj = ipaddress.ip_address(gw_ip)

                    # TODO: Query OVN for actual subnet CIDR
                    # For now use common defaults
                    if ip_obj.version == 4:
                        gw_cidr = f"{gw_ip}/24"
                    else:
                        gw_cidr = f"{gw_ip}/64"

                    driver_utils.add_ips_to_dev(irb_device, [gw_cidr])
                    LOG.info("Added gateway IP %s to %s", gw_cidr, irb_device)

                except Exception as e:
                    LOG.warning("Failed to add gateway IP %s: %s", gw_ip, e)

        except Exception as e:
            LOG.warning("Failed to process gateway IPs: %s", e)

    def _cleanup_network_infrastructure(self, network_info):
        """Remove network infrastructure.

        :param network_info: Network info dict
        """
        network_id = network_info['id']
        vlan_id = network_info['vlan_id']
        l2vni = network_info['l2vni']
        l3vni = network_info['l3vni']

        LOG.info("Cleaning up infrastructure for network %s", network_id)

        try:
            bridge_name = CONF.evpn_bridge

            # Delete IRB device
            if l3vni is not None:
                irb_device = f'{bridge_name}.{vlan_id}'
                LOG.debug("Deleting IRB device: %s", irb_device)
                linux_net.delete_device(irb_device)

                # Clean up static neighbors
                if irb_device in self.static_neighbors:
                    del self.static_neighbors[irb_device]

            # Delete L2VNI
            if l2vni:
                vxlan_name = f'{constants.OVN_EVPN_VXLAN_PREFIX}{l2vni}'
                LOG.debug("Deleting L2VNI device: %s", vxlan_name)
                linux_net.delete_device(vxlan_name)

            # Check if VRF is still in use
            if l3vni is not None:
                vrf_name = f'{constants.OVN_EVPN_VRF_PREFIX}{l3vni}'

                if vrf_name in self.evpn_vrfs:
                    # Remove this network from VRF's network list
                    self.evpn_vrfs[vrf_name]['networks'].remove(network_id)

                    # If VRF has no more networks, delete it
                    if not self.evpn_vrfs[vrf_name]['networks']:
                        LOG.info("Deleting VRF %s (no more networks)", vrf_name)

                        # Delete from FRR
                        evpn_info = {'vrf_name': vrf_name, 'vni': l3vni}
                        frr.vrf_reconfigure(evpn_info, 'del-vrf')

                        # Delete L3VNI VXLAN if exists
                        if l3vni > 0:
                            l3vni_name = f'{constants.OVN_EVPN_VXLAN_PREFIX}l3-{l3vni}'
                            linux_net.delete_device(l3vni_name)

                            irb_bridge = f'{constants.OVN_EVPN_BRIDGE_PREFIX}{l3vni}'
                            linux_net.delete_device(irb_bridge)

                        # Delete VRF device
                        if CONF.delete_vrf_on_disconnect:
                            linux_net.delete_device(vrf_name)

                        del self.evpn_vrfs[vrf_name]

            # Clean up FDB entries
            if bridge_name in self.bridge_fdb_entries:
                # Remove entries for this VLAN
                self.bridge_fdb_entries[bridge_name] = [
                    entry for entry in self.bridge_fdb_entries[bridge_name]
                    if entry[1] != vlan_id
                ]

        except Exception as e:
            LOG.warning("Failed to cleanup network infrastructure: %s", e)

    def _parse_route_targets(self, ext_ids):
        """Parse route targets from external_ids.

        :param ext_ids: external_ids dict
        :return: List of route targets
        """
        route_targets = []
        rt_str = ext_ids.get(constants.OVN_EVPN_RT_EXT_ID_KEY)
        if rt_str:
            try:
                route_targets = json.loads(rt_str)
            except json.JSONDecodeError:
                route_targets = [rt_str]
        return route_targets

    def _parse_route_distinguishers(self, ext_ids):
        """Parse route distinguishers from external_ids.

        :param ext_ids: external_ids dict
        :return: List of route distinguishers
        """
        rds = []
        rd_str = ext_ids.get(constants.OVN_EVPN_RD_EXT_ID_KEY)
        if rd_str:
            try:
                rds = json.loads(rd_str)
            except json.JSONDecodeError:
                rds = [rd_str]
        return rds

    def _parse_import_targets(self, ext_ids):
        """Parse import targets from external_ids.

        :param ext_ids: external_ids dict
        :return: List of import targets
        """
        import_targets = []
        it_str = ext_ids.get(constants.OVN_EVPN_IRT_EXT_ID_KEY)
        if it_str:
            try:
                import_targets = json.loads(it_str)
            except json.JSONDecodeError:
                import_targets = [it_str]
        return import_targets

    def _parse_export_targets(self, ext_ids):
        """Parse export targets from external_ids.

        :param ext_ids: external_ids dict
        :return: List of export targets
        """
        export_targets = []
        et_str = ext_ids.get(constants.OVN_EVPN_ERT_EXT_ID_KEY)
        if et_str:
            try:
                export_targets = json.loads(et_str)
            except json.JSONDecodeError:
                export_targets = [et_str]
        return export_targets

    def _add_port_custom_routes(self, routes_str, network_info, port_ips):
        """Add custom routes from port association.

        Port associations can specify custom routes via bgpvpn-routes-control.

        :param routes_str: JSON string with routes
        :param network_info: Network info dict
        :param port_ips: Port IP addresses
        """
        try:
            routes = json.loads(routes_str)

            if not network_info.get('l3vni'):
                LOG.warning("Cannot add custom routes without L3VNI")
                return

            vrf_name = f'{constants.OVN_EVPN_VRF_PREFIX}{network_info["l3vni"]}'

            if vrf_name not in self.evpn_vrfs:
                LOG.warning("VRF %s not found for custom routes", vrf_name)
                return

            table_id = self.evpn_vrfs[vrf_name]['table_id']

            for route in routes:
                dst = route.get('destination')
                nexthop = route.get('nexthop')

                if dst and nexthop:
                    LOG.info("Adding custom route: %s via %s (table %s)",
                             dst, nexthop, table_id)

                    # Add route to VRF table
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
                        LOG.warning("Failed to add custom route %s: %s", dst, e)

        except (json.JSONDecodeError, KeyError) as e:
            LOG.warning("Failed to parse custom routes: %s", e)

    def _get_network_mtu(self, datapath):
        """Get MTU for network from OVN datapath.

        :param datapath: OVN Datapath_Binding
        :return: MTU value (int)
        """
        # Try to get MTU from datapath external_ids
        try:
            if hasattr(datapath, 'external_ids'):
                mtu_str = datapath.external_ids.get('neutron:mtu')
                if mtu_str:
                    return int(mtu_str)
        except (AttributeError, ValueError, KeyError) as e:
            LOG.debug("Could not get MTU from datapath: %s", e)

        # Fallback to configuration or default
        if hasattr(CONF, 'network_device_mtu') and CONF.network_device_mtu:
            return CONF.network_device_mtu

        # Default MTU
        return 1500

    # =========================================================================
    # FRR sync
    # =========================================================================

    @lockutils.synchronized('evpn')
    def frr_sync(self):
        """Ensure FRR EVPN configuration is synchronized.

        Called periodically to ensure FRR config matches tracked state.
        """
        LOG.debug("Syncing FRR EVPN configuration")

        try:
            # Ensure base EVPN config
            frr.ensure_evpn_base_config()

            # Ensure all VRF configurations
            for vrf_name, vrf_info in self.evpn_vrfs.items():
                l3vni = vrf_info.get('l3vni')

                # Get network info for this VRF to access full EVPN params
                network_ids = vrf_info.get('networks', [])
                if network_ids:
                    # Use first network's info (all should have same VRF config)
                    network_info = self.evpn_networks.get(network_ids[0])
                    if network_info:
                        # Build full EVPN info for FRR
                        evpn_info = {
                            'vrf_name': vrf_name,
                            'vni': l3vni if l3vni and l3vni > 0 else 0,
                            'bgp_as': network_info.get('bgp_as', CONF.bgp_AS),
                            'route_targets': network_info.get('route_targets', []),
                            'route_distinguishers': network_info.get('route_distinguishers', []),
                            'import_targets': network_info.get('import_targets', []),
                            'export_targets': network_info.get('export_targets', []),
                            'local_ip': self._get_local_vtep_ip(),
                        }

                        # Add local preference if specified
                        if network_info.get('local_pref'):
                            evpn_info['local_pref'] = network_info['local_pref']

                        # Reconfigure VRF in FRR
                        frr.vrf_reconfigure(evpn_info, 'add-vrf')

                        # Configure route leaking if needed
                        if l3vni == 0:
                            frr.vrf_leak(vrf_name, CONF.bgp_AS)

        except Exception as e:
            LOG.exception("FRR sync failed: %s", e)