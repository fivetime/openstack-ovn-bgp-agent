# Copyright 2025 Red Hat, Inc.
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

"""OVN EVPN Helper - EVPN-specific OVN query utilities

This module provides EVPN-specific query and parsing utilities that build
on top of the generic OVN utilities in ovn.py. It handles EVPN-specific
logic such as:
- VLAN tag resolution with caching and retry for EVPN L2 mode
- Gateway IP extraction for IRB interface configuration
- EVPN route target parsing (RT/RD/Import/Export)
- Port information extraction for EVPN FDB/neighbor management
"""

import ipaddress
import json
import threading
import time

from oslo_config import cfg
from oslo_log import log as logging

from ovn_bgp_agent import constants
from ovn_bgp_agent import exceptions as agent_exc

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class OvnEvpnHelper:
    """OVN EVPN query and parsing helper

    This class provides EVPN-specific utilities that complement the generic
    OVN operations in ovn.py. It adds:
    - Caching and retry logic for EVPN L2 mode operations
    - EVPN-specific data extraction and parsing
    - Gateway IP resolution for IRB interfaces
    """

    def __init__(self, sb_idl, ovs_idl):
        """Initialize OVN EVPN helper

        :param sb_idl: OVN Southbound IDL instance (from ovn.py)
        :param ovs_idl: OVS IDL instance (from ovs.py)
        """
        self.sb_idl = sb_idl
        self.ovs_idl = ovs_idl

        # VLAN tag cache for EVPN L2 mode
        # In L2 mode, we need to frequently query OVN VLAN tags for
        # Internal Port creation, so caching improves performance
        self._ovn_vlan_cache = {}
        self._ovn_vlan_cache_lock = threading.Lock()

    # ========================================================================
    # VLAN Tag Resolution (with caching and retry for EVPN L2 mode)
    # ========================================================================

    def get_ovn_vlan_tag(self, network_id):
        """Get OVN internal VLAN tag with caching and retry

        This method is specifically designed for EVPN L2 mode where we need
        to create OVN Internal Ports with the correct VLAN tag. It adds:
        - Caching to avoid repeated queries
        - Retry logic for eventual consistency
        - Multiple query strategies (localnet + patch port)

        Note: This wraps ovn.py's get_network_name_and_tag() with EVPN-specific
        enhancements.

        :param network_id: Network UUID (Datapath UUID)
        :return: OVN VLAN tag (integer)
        :raises: PortNotFound if VLAN tag cannot be determined
        """
        # Check cache first
        with self._ovn_vlan_cache_lock:
            if network_id in self._ovn_vlan_cache:
                LOG.debug("Cache hit for network %s VLAN tag", network_id[:8])
                return self._ovn_vlan_cache[network_id]

        # Retry logic for eventual consistency
        # In EVPN L2 mode, the OVN VLAN tag might not be immediately available
        # after network creation, so we retry multiple times
        max_attempts = 10
        for attempt in range(max_attempts):
            vlan = self._query_ovn_vlan_tag(network_id)
            if vlan is not None:
                # Cache the result
                with self._ovn_vlan_cache_lock:
                    self._ovn_vlan_cache[network_id] = vlan
                LOG.info("Got OVN VLAN %s for network %s (attempt %d/%d)",
                         vlan, network_id[:8], attempt + 1, max_attempts)
                return vlan

            if attempt < max_attempts - 1:
                LOG.debug("OVN VLAN tag not ready for %s, retry %d/%d",
                          network_id[:8], attempt + 1, max_attempts)
                time.sleep(1)

        # After all retries failed
        raise agent_exc.PortNotFound(
            port=f"OVN localnet/patch port for network {network_id[:8]}")

    def _query_ovn_vlan_tag(self, network_id):
        """Query OVN VLAN tag without caching

        This method tries two strategies:
        1. Use ovn.py's generic get_network_name_and_tag() for provider networks
        2. Query OVS patch port tag directly for EVPN L2 networks

        Strategy 2 is EVPN-specific and not in ovn.py because it's only needed
        for EVPN L2 mode Internal Port creation.

        :param network_id: Network UUID
        :return: VLAN tag (integer) or None if not found
        """
        # Strategy 1: Use ovn.py's generic method for provider networks
        # This works for networks with localnet ports
        try:
            LOG.debug("Trying Strategy 1: get_network_name_and_tag() for %s",
                      network_id[:8])
            network_name, vlan_tag = self.sb_idl.get_network_name_and_tag(
                network_id,
                self.ovs_idl.get_ovn_bridge_mappings().keys()
            )
            if vlan_tag:
                LOG.debug("Strategy 1 succeeded: VLAN tag %s", vlan_tag[0])
                return vlan_tag[0]
        except Exception as e:
            LOG.debug("Strategy 1 (localnet port query) failed: %s", e)

        # Strategy 2: EVPN L2 specific - query OVS patch port tag
        # This is needed because in EVPN L2 mode, we connect OVN to Linux bridge
        # via an Internal Port, and we need to know the OVN VLAN tag to set on
        # the OVS port correctly.
        #
        # This logic is EVPN-specific and shouldn't be in ovn.py because:
        # - It requires importing ovs_vsctl (privilege escalation)
        # - It's only needed for EVPN L2 mode Internal Port creation
        # - Generic drivers don't need this level of detail
        try:
            LOG.debug("Trying Strategy 2: OVS patch port tag query for %s",
                      network_id[:8])
            return self._query_ovs_patch_port_tag(network_id)
        except Exception as e:
            LOG.debug("Strategy 2 (patch port tag query) failed: %s", e)

        return None

    def _query_ovs_patch_port_tag(self, network_id):
        """Query VLAN tag from OVS patch port (EVPN L2 specific)

        This method is EVPN-specific because:
        - It requires privileged ovs-vsctl operations
        - It's only needed for EVPN L2 mode
        - Generic OVN drivers don't need direct OVS queries

        :param network_id: Network UUID
        :return: VLAN tag or None
        """
        from ovn_bgp_agent.privileged import ovs_vsctl

        # Find patch port in OVN SB that connects to this network
        all_ports = self.sb_idl.db_list_rows('Port_Binding').execute()
        for port in all_ports:
            try:
                # Filter: must be a patch port on the target network
                if not hasattr(port, 'datapath') or not port.datapath:
                    continue
                if str(port.datapath.uuid) != network_id:
                    continue
                if port.type != 'patch':
                    continue

                # Found a patch port, now query its OVS tag
                logical_port = port.logical_port

                # Try common OVS patch port naming patterns
                ovs_port_candidates = [
                    f'patch-{logical_port}-to-br-int',
                    f'patch-{logical_port}-to-{CONF.ovs_bridge}',
                    logical_port,
                ]

                for ovs_port in ovs_port_candidates:
                    try:
                        tag_output = ovs_vsctl.ovs_cmd('get', [
                            'Port', ovs_port, 'tag'
                        ])
                        tag_str = tag_output.strip()

                        # Parse tag from OVS output
                        # OVS returns tags in various formats: "100", "[]", "set(100)"
                        if tag_str and tag_str not in ('[]', '', 'set()'):
                            tag_str = tag_str.replace('set(', '').replace(')', '')
                            vlan_tag = int(tag_str)
                            LOG.debug("Found VLAN tag %s on OVS port %s",
                                      vlan_tag, ovs_port)
                            return vlan_tag
                    except Exception as e:
                        LOG.debug("Failed to get tag from %s: %s", ovs_port, e)
                        continue
            except Exception as e:
                LOG.debug("Failed to process port: %s", e)
                continue

        return None

    def clear_vlan_cache(self, network_id=None):
        """Clear VLAN tag cache

        :param network_id: Specific network to clear, or None for all
        """
        with self._ovn_vlan_cache_lock:
            if network_id:
                self._ovn_vlan_cache.pop(network_id, None)
                LOG.debug("Cleared VLAN cache for network %s", network_id[:8])
            else:
                self._ovn_vlan_cache.clear()
                LOG.debug("Cleared entire VLAN cache")

    # ========================================================================
    # Gateway IP Extraction (for IRB interface configuration)
    # ========================================================================

    def extract_gateway_ips(self, network_id):
        """Extract gateway IPs from network for IRB interface configuration

        In EVPN, we create IRB (Integrated Routing and Bridging) interfaces
        that need to have the subnet gateway IPs configured. This method
        extracts those IPs from OVN patch ports.

        This is EVPN-specific because generic drivers don't need to extract
        gateway IPs for interface configuration.

        :param network_id: Network UUID
        :return: List of gateway IPs as strings (e.g., ['10.0.0.1/24'])
        """
        gateway_ips = []

        try:
            # Query all ports on this network's datapath
            all_ports = self.sb_idl.db_list_rows('Port_Binding').execute()

            for port in all_ports:
                try:
                    # Filter: must be on target network and be a patch port
                    if not hasattr(port, 'datapath') or not port.datapath:
                        continue
                    if str(port.datapath.uuid) != network_id:
                        continue
                    if port.type != 'patch':
                        continue

                    # Extract IPs from port MAC field
                    # OVN stores gateway IPs in patch port's MAC field
                    # Format: "mac_addr ip1/prefix ip2/prefix ..."
                    port_info = self.extract_port_info(port)
                    if port_info and port_info.get('ips'):
                        for gw_ip in port_info['ips']:
                            try:
                                # Validate and normalize IP
                                ip_obj = ipaddress.ip_interface(gw_ip)
                                normalized_ip = str(ip_obj)
                                gateway_ips.append(normalized_ip)
                                LOG.debug("Extracted gateway IP %s from port %s",
                                          normalized_ip, port.logical_port)
                            except (ValueError, ipaddress.AddressValueError) as e:
                                LOG.warning("Invalid gateway IP %s on port %s: %s",
                                            gw_ip, port.logical_port, e)
                except Exception as e:
                    LOG.debug("Failed to process port for gateway IPs: %s", e)
                    continue

        except Exception as e:
            LOG.warning("Failed to extract gateway IPs for network %s: %s",
                        network_id[:8], e)

        if gateway_ips:
            LOG.info("Extracted %d gateway IPs for network %s: %s",
                     len(gateway_ips), network_id[:8], gateway_ips)
        else:
            LOG.debug("No gateway IPs found for network %s", network_id[:8])

        return gateway_ips

    # ========================================================================
    # Port Information Extraction (for EVPN FDB/neighbor management)
    # ========================================================================

    def extract_port_info(self, port_binding):
        """Extract MAC and IPs from Port_Binding for EVPN operations

        This extracts port information needed for:
        - FDB (Forwarding Database) entry creation
        - Neighbor table population
        - EVPN Type-2 route advertisement

        :param port_binding: Port_Binding row from OVN SB
        :return: {'mac': 'aa:bb:cc:dd:ee:ff', 'ips': ['10.0.0.2', ...]} or None
        """
        # Validate port has MAC information
        if not port_binding.mac or port_binding.mac == ['unknown']:
            LOG.debug("Port %s has no MAC information",
                      getattr(port_binding, 'logical_port', 'unknown'))
            return None

        try:
            # OVN stores MAC and IPs in a space-separated string
            # Format: "mac_addr ip1 ip2 ..."
            mac_ips = port_binding.mac[0].strip().split()
            if len(mac_ips) < 1:
                return None

            result = {
                'mac': mac_ips[0],
                'ips': mac_ips[1:] if len(mac_ips) > 1 else [],
            }

            LOG.debug("Extracted port info from %s: MAC=%s, IPs=%s",
                      getattr(port_binding, 'logical_port', 'unknown'),
                      result['mac'], result['ips'])

            return result

        except (IndexError, AttributeError) as e:
            LOG.warning("Failed to parse port info from %s: %s",
                        getattr(port_binding, 'logical_port', 'unknown'), e)
            return None

    # ========================================================================
    # EVPN Route Target Parsing
    # ========================================================================

    def parse_route_targets(self, ext_ids):
        """Parse Route Targets from OVN external_ids

        Route Targets (RT) control EVPN route import/export. This method
        parses RT from the external_ids field, supporting both:
        - JSON array format: '["65000:100", "65000:200"]'
        - Single string format: '65000:100'

        :param ext_ids: external_ids dict from OVN Port_Binding
        :return: List of route targets
        """
        rt_str = ext_ids.get(constants.OVN_EVPN_ROUTE_TARGETS_EXT_ID_KEY)
        if not rt_str:
            LOG.debug("No route targets found in external_ids")
            return []

        try:
            # Try parsing as JSON array
            route_targets = json.loads(rt_str)
            LOG.debug("Parsed route targets (JSON): %s", route_targets)
            return route_targets
        except json.JSONDecodeError:
            # Fall back to single string
            LOG.debug("Parsed route target (string): %s", rt_str)
            return [rt_str]

    def parse_route_distinguishers(self, ext_ids):
        """Parse Route Distinguishers from OVN external_ids

        Route Distinguishers (RD) ensure unique EVPN routes across VPNs.

        :param ext_ids: external_ids dict from OVN Port_Binding
        :return: List of route distinguishers
        """
        rd_str = ext_ids.get(constants.OVN_EVPN_ROUTE_DISTINGUISHERS_EXT_ID_KEY)
        if not rd_str:
            LOG.debug("No route distinguishers found in external_ids")
            return []

        try:
            route_distinguishers = json.loads(rd_str)
            LOG.debug("Parsed route distinguishers (JSON): %s",
                      route_distinguishers)
            return route_distinguishers
        except json.JSONDecodeError:
            LOG.debug("Parsed route distinguisher (string): %s", rd_str)
            return [rd_str]

    def parse_import_targets(self, ext_ids):
        """Parse Import Targets from OVN external_ids

        Import Targets specify which EVPN routes to import into this VRF.

        :param ext_ids: external_ids dict from OVN Port_Binding
        :return: List of import targets
        """
        it_str = ext_ids.get(constants.OVN_EVPN_IMPORT_TARGETS_EXT_ID_KEY)
        if not it_str:
            LOG.debug("No import targets found in external_ids")
            return []

        try:
            import_targets = json.loads(it_str)
            LOG.debug("Parsed import targets (JSON): %s", import_targets)
            return import_targets
        except json.JSONDecodeError:
            LOG.debug("Parsed import target (string): %s", it_str)
            return [it_str]

    def parse_export_targets(self, ext_ids):
        """Parse Export Targets from OVN external_ids

        Export Targets specify which routes to export from this VRF.

        :param ext_ids: external_ids dict from OVN Port_Binding
        :return: List of export targets
        """
        et_str = ext_ids.get(constants.OVN_EVPN_EXPORT_TARGETS_EXT_ID_KEY)
        if not et_str:
            LOG.debug("No export targets found in external_ids")
            return []

        try:
            export_targets = json.loads(et_str)
            LOG.debug("Parsed export targets (JSON): %s", export_targets)
            return export_targets
        except json.JSONDecodeError:
            LOG.debug("Parsed export target (string): %s", et_str)
            return [et_str]

    # ========================================================================
    # Network MTU Resolution
    # ========================================================================

    def get_network_mtu(self, datapath):
        """Get network MTU from OVN datapath or fallback to config

        MTU is needed for:
        - VXLAN device configuration
        - IRB interface configuration
        - Internal Port configuration

        :param datapath: OVN Datapath_Binding object
        :return: MTU value (integer)
        """
        # Try to get MTU from OVN datapath external_ids
        try:
            if hasattr(datapath, 'external_ids'):
                mtu_str = datapath.external_ids.get('neutron:mtu')
                if mtu_str:
                    mtu = int(mtu_str)
                    LOG.debug("Got MTU %s from datapath external_ids", mtu)
                    return mtu
        except (AttributeError, ValueError, KeyError) as e:
            LOG.debug("Failed to get MTU from datapath: %s", e)

        # Fall back to config
        if hasattr(CONF, 'network_device_mtu') and CONF.network_device_mtu:
            LOG.debug("Using MTU %s from config", CONF.network_device_mtu)
            return CONF.network_device_mtu

        # Final fallback to standard Ethernet MTU
        LOG.debug("Using default MTU 1500")
        return 1500

    # ========================================================================
    # Utility Methods
    # ========================================================================

    def get_evpn_info(self, port_binding):
        """Get EVPN VNI and BGP AS from port (wrapper for ovn.py method)

        This is a convenience wrapper that delegates to ovn.py's get_evpn_info()
        method. We keep it here so OvnEvpnHelper provides all EVPN-related
        queries in one place.

        :param port_binding: Port_Binding object
        :return: {'vni': 12345, 'bgp_as': 65000} or {}
        """
        return self.sb_idl.get_evpn_info(port_binding)