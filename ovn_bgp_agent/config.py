# Copyright 2021 Red Hat, Inc.
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

import shlex

from oslo_config import cfg
from oslo_log import log as logging
from oslo_privsep import priv_context

from ovn_bgp_agent import constants

LOG = logging.getLogger(__name__)

agent_opts = [
    cfg.IntOpt('reconcile_interval',
               help='Time (seconds) between re-sync actions.',
               default=300),
    cfg.IntOpt('frr_reconcile_interval',
               help='Time (seconds) between re-sync actions to ensure frr '
                    'configuration is correct, in case frr is restart.',
               default=15),
    cfg.BoolOpt('expose_tenant_networks',
                help='Expose VM IPs on tenant networks. '
                     'If this flag is enabled, it takes precedence over '
                     'expose_ipv6_gua_tenant_networks flag and all tenant '
                     'network IPs will be exposed.',
                default=False),
    cfg.StrOpt('advertisement_method_tenant_networks',
               help='The NB driver is capable of advertising the tenant '
                    'networks either per host or per subnet. '
                    'So either per /32 or /128 or per subnet like /24. '
                    'Choose "host" as value for this option to advertise per '
                    'host or choose "subnet" to announce per subnet prefix.',
               default=constants.ADVERTISEMENT_METHOD_HOST,
               choices=[constants.ADVERTISEMENT_METHOD_HOST,
                        constants.ADVERTISEMENT_METHOD_SUBNET]),
    cfg.BoolOpt('require_snat_disabled_for_tenant_networks',
                help='Require SNAT on the router port to be disabled before '
                     'exposing the tenant networks. Otherwise the exposed '
                     'tenant networks will be reachable from the outside, but'
                     'the connections set up from within the tenant vm will '
                     'always be SNAT-ed by the router, thus be the router ip. '
                     'When SNAT is disabled, OVN will do pure routing without '
                     'SNAT',
                default=False),
    cfg.BoolOpt('expose_ipv6_gua_tenant_networks',
                help='Expose only VM IPv6 IPs on tenant networks if they are '
                     'GUA. The expose_tenant_networks parameter takes '
                     'precedence over this one. So if it is set, all the '
                     'tenant network IPs will be exposed and not only the '
                     'IPv6 GUA IPs.',
                default=False),
    cfg.StrOpt('driver',
               help='Driver to be used',
               choices=('ovn_bgp_driver', 'ovn_evpn_driver',
                        'ovn_stretched_l2_bgp_driver', 'nb_ovn_bgp_driver'),
               default='ovn_bgp_driver'),
    cfg.StrOpt('ovsdb_connection',
               default='unix:/usr/local/var/run/openvswitch/db.sock',
               regex=r'^(tcp|ssl|unix):.+',
               help='The connection string for the native OVSDB backend.\n'
                    'Use tcp:IP:PORT for TCP connection.\n'
                    'Use unix:FILE for unix domain socket connection.'),
    cfg.IntOpt('ovsdb_connection_timeout',
               default=180,
               help='Timeout in seconds for the OVSDB connection transaction'),
    cfg.StrOpt('bgp_AS',
               default='64999',
               help='AS number to be used by the Agent when running in BGP '
                    'mode and configuring the VRF route leaking.'),
    cfg.StrOpt('bgp_router_id',
               default=None,
               help='Router ID to be used by the Agent when running in BGP '
                    'mode and configuring the VRF route leaking.'),

    # =========================================================================
    # EVPN Configuration Options
    # =========================================================================
    cfg.IPOpt('evpn_local_ip',
              default=None,
              help='IP address of local EVPN VXLAN (tunnel) endpoint. '
                   'This is the VTEP (VXLAN Tunnel Endpoint) IP address. '
                   'If not specified, will try to use IP from evpn_nic. '
                   'If evpn_nic is also not set, will use loopback IP.'),
    cfg.StrOpt('evpn_nic',
               default=None,
               help='Network interface to get VTEP IP address from. '
                    'Only used if evpn_local_ip is not explicitly set. '
                    'If neither evpn_local_ip nor evpn_nic is set, '
                    'the loopback device IP will be used.'),
    cfg.PortOpt('evpn_udp_dstport',
                default=4789,
                help='UDP destination port for VXLAN encapsulation. '
                     'Default is 4789 (IANA assigned port for VXLAN).'),
    cfg.StrOpt('evpn_bridge',
               default='br-evpn',
               help='Linux bridge name for attaching EVPN VNI devices. '
                    'All L2VNI and L3VNI VXLAN devices will be connected '
                    'to this bridge. This bridge is separate from OVS. '
                    'VLAN filtering will be enabled on this bridge for '
                    'multi-tenant isolation.'),
    cfg.StrOpt('evpn_bridge_veth',
               default='veth-to-ovs',
               help='Veth interface name on EVPN bridge side. '
                    'This forms one end of the veth pair connecting '
                    'the EVPN bridge to OVS integration bridge.'),
    cfg.StrOpt('evpn_ovs_veth',
               default='veth-to-evpn',
               help='Veth interface name on OVS bridge side. '
                    'This forms the other end of the veth pair connecting '
                    'the OVS integration bridge to EVPN bridge.'),
    cfg.IntOpt('l2vni_offset',
               default=None,
               help='Offset for automatic L2VNI calculation from VLAN ID. '
                    'Formula: L2VNI = VLAN_ID + l2vni_offset. '
                    'Example: If VLAN is 100 and offset is 10000, L2VNI=10100. '
                    'Can be overridden by explicit VNI in OVN external_ids. '
                    'Set to None to disable automatic calculation. '
                    'Note: For Symmetric IRB (type=l2), the same VNI is used '
                    'for both L2 and L3, so this is rarely needed.'),
    cfg.BoolOpt('evpn_static_fdb',
                default=True,
                help='Pre-populate bridge FDB (Forwarding Database) with MAC '
                     'addresses from OVN Port_Binding. This optimization '
                     'reduces L2 flooding and helps trigger EVPN Type-2 '
                     'MACIP route advertisement immediately. '
                     'Only applies to type=l2 (Symmetric IRB mode).'),
    cfg.BoolOpt('evpn_static_neighbors',
                default=True,
                help='Pre-populate kernel neighbor table (ARP/NDP cache) with '
                     'IP-to-MAC mappings from OVN Port_Binding. This reduces '
                     'ARP/NDP queries and triggers EVPN Type-2 route '
                     'advertisement for known hosts immediately.'),
    cfg.StrOpt('ovs_bridge',
               default='br-int',
               help='OVS bridge name to connect EVPN infrastructure to. '
                    'For tenant networks, this should be br-int. '
                    'For provider networks, this could be br-ex.'),
    cfg.IntOpt('network_device_mtu',
               default=1500,
               min=68,
               max=9000,
               help='Default MTU for EVPN network devices (VXLAN, IRB, etc.). '
                    'This is used when MTU cannot be determined from OVN. '
                    'For VXLAN tunnels, should typically be 50 bytes less '
                    'than physical network MTU to account for VXLAN overhead. '
                    'Example: If physical MTU is 1500, set this to 1450.'),
    cfg.IntOpt('evpn_vlan_range_min',
               default=100,
               min=2,
               max=4094,
               help='Minimum VLAN ID for EVPN bridge VLAN allocation. '
                    'VLANs below this value are reserved for other uses. '
                    'Default is 100 to avoid conflicts with common VLANs '
                    '(VLAN 1 is default VLAN, VLAN 2-99 often reserved).'),
    cfg.IntOpt('evpn_vlan_range_max',
               default=4094,
               min=2,
               max=4094,
               help='Maximum VLAN ID for EVPN bridge VLAN allocation. '
                    'Default is 4094 (maximum 802.1Q VLAN ID). '
                    'Must be greater than evpn_vlan_range_min.'),

    # =========================================================================
    # BGP/VRF Configuration Options
    # =========================================================================
    cfg.BoolOpt('clear_vrf_routes_on_startup',
                help='If enabled, all routes are removed from the VRF table '
                     '(specified by bgp_vrf_table_id option) at agent startup. '
                     'Useful for cleaning up stale routes after restart.',
                default=False),
    cfg.BoolOpt('delete_vrf_on_disconnect',
                help='If enabled, agent will completely delete VRF device '
                     'from both kernel and FRR configuration when it is '
                     'no longer needed. If disabled, VRF device will be kept '
                     'even when redundant (only FRR config is removed). '
                     'For EVPN driver, recommend setting to False to allow '
                     'VRF reuse across network reconfigurations.',
                default=False),  # Changed default for EVPN
    cfg.StrOpt('bgp_nic',
               default='bgp-nic',
               help='The name of the interface used within the VRF '
                    '(bgp_vrf option) to expose the IPs and/or Networks.'),
    cfg.StrOpt('bgp_vrf',
               default='bgp-vrf',
               help='The name of the VRF to be used to expose the IPs '
                    'and/or Networks through BGP.'),
    cfg.IntOpt('bgp_vrf_table_id',
               default=10,
               help='The Routing Table ID that the VRF (bgp_vrf option) '
                    'should use. If it does not exist, this table will be '
                    'created.'),
    cfg.ListOpt('address_scopes',
                default=None,
                help='Allows to filter on the address scope. Only networks '
                     'with the same address scope on the provider and '
                     'internal interface are announced.'),
    cfg.StrOpt('exposing_method',
               default='vrf',
               choices=('underlay', 'l2vni', 'vrf', 'dynamic', 'ovn'),
               help='The exposing mechanism to be used. '
                    'For EVPN driver, use "vrf" or "dynamic". '
                    '"vrf": Expose routes in VRFs with EVPN Type-5. '
                    '"dynamic": Mix of methods based on port annotations.'),
]

root_helper_opts = [
    cfg.StrOpt('root_helper', default='sudo',
               help=("Root helper application. "
                     "List of command and arguments to prefix privsep-helper "
                     "with, in order to run helper as root. Use 'sudo' to "
                     "skip the filtering and just run the command directly.")),
]

ovn_opts = [
    cfg.StrOpt('ovn_sb_private_key',
               default='/etc/pki/tls/private/ovn_bgp_agent.key',
               deprecated_group='DEFAULT',
               help='The PEM file with private key for SSL connection to '
                    'OVN-SB-DB'),
    cfg.StrOpt('ovn_sb_certificate',
               default='/etc/pki/tls/certs/ovn_bgp_agent.crt',
               deprecated_group='DEFAULT',
               help='The PEM file with certificate that certifies the '
                    'private key specified in ovn_sb_private_key'),
    cfg.StrOpt('ovn_sb_ca_cert',
               default='/etc/ipa/ca.crt',
               deprecated_group='DEFAULT',
               help='The PEM file with CA certificate that OVN should use to '
                    'verify certificates presented to it by SSL peers'),
    cfg.StrOpt('ovn_sb_connection',
               deprecated_group='DEFAULT',
               regex=r'^(tcp|ssl|unix):.+',
               help='The connection string for the OVN_Southbound OVSDB.\n'
                    'Use tcp:IP:PORT for TCP connection.\n'
                    'Use unix:FILE for unix domain socket connection.'),
    cfg.StrOpt('ovn_nb_private_key',
               default='/etc/pki/tls/private/ovn_bgp_agent.key',
               deprecated_group='DEFAULT',
               help='The PEM file with private key for SSL connection to '
                    'OVN-NB-DB'),
    cfg.StrOpt('ovn_nb_certificate',
               default='/etc/pki/tls/certs/ovn_bgp_agent.crt',
               deprecated_group='DEFAULT',
               help='The PEM file with certificate that certifies the '
                    'private key specified in ovn_nb_private_key'),
    cfg.StrOpt('ovn_nb_ca_cert',
               default='/etc/ipa/ca.crt',
               deprecated_group='DEFAULT',
               help='The PEM file with CA certificate that OVN should use to '
                    'verify certificates presented to it by SSL peers'),
    cfg.StrOpt('ovn_nb_connection',
               deprecated_group='DEFAULT',
               regex=r'^(tcp|ssl|unix):.+',
               help='The connection string for the OVN_Northbound OVSDB.\n'
                    'Use tcp:IP:PORT for TCP connection.\n'
                    'Use unix:FILE for unix domain socket connection.'),
]

local_ovn_cluster_opts = [
    cfg.StrOpt('ovn_nb_connection',
               default='unix:/var/run/ovn/ovnnb_db.sock',
               regex=r'^(tcp|ssl|unix):.+',
               help='The connection string for the OVN_Northbound OVSDB.\n'
                    'Use tcp:IP:PORT for TCP connection.\n'
                    'Use unix:FILE for unix domain socket connection.'),
    cfg.ListOpt('external_nics',
                default=[],
                help='List of NICS that the local OVN cluster needs to be '
                     'connected to for the external connectivity.'),
    cfg.ListOpt('peer_ips',
                default=[],
                help='List of peer IPs used for redirecting the outgoing '
                     'traffic (ECMP supported).'),
    cfg.ListOpt('provider_networks_pool_prefixes',
                default=['192.168.0.0/16'],
                help='List of prefixes for provider networks'),
    cfg.StrOpt('bgp_chassis_id',
               default='bgp',
               help='The chassis_id used for the ovn-controller instance '
                    'related to the node-local OVN instance. Used as a '
                    'suffix for getting instance-specific options '
                    'from OVSDB. This option has effect only when the OVN '
                    'NB driver is used.'),
]

CONF = cfg.CONF
EXTRA_LOG_LEVEL_DEFAULTS = [
    'oslo.privsep.daemon=INFO'
]

logging.register_options(CONF)


def register_opts():
    CONF.register_opts(agent_opts)
    CONF.register_opts(root_helper_opts, "agent")
    CONF.register_opts(ovn_opts, "ovn")
    CONF.register_opts(local_ovn_cluster_opts, "local_ovn_cluster")


def init(args, **kwargs):
    CONF(args=args, project='bgp-agent', **kwargs)


def setup_logging():
    logging.set_defaults(default_log_levels=logging.get_default_log_levels() +
                                            EXTRA_LOG_LEVEL_DEFAULTS)
    logging.setup(CONF, 'bgp-agent')
    LOG.info("Logging enabled!")


def get_root_helper(conf):
    return conf.agent.root_helper


def setup_privsep():
    priv_context.init(root_helper=shlex.split(get_root_helper(cfg.CONF)))


def list_opts():
    return [
        ("DEFAULT", agent_opts),
        ("agent", root_helper_opts),
        ("ovn", ovn_opts),
        ("local_ovn_cluster", local_ovn_cluster_opts),
    ]