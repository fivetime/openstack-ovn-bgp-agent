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

"""FRR (Free Range Routing) configuration utilities.

This module provides high-level functions for configuring FRR BGP and EVPN.
It wraps the low-level privileged vtysh operations with business logic.
"""

import os
import tempfile

from jinja2 import Template
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils

from ovn_bgp_agent import constants
import ovn_bgp_agent.privileged.vtysh

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

DEFAULT_REDISTRIBUTE = {'connected'}

# ============================================================================
# Jinja2 Templates for FRR Configuration
# ============================================================================

CONFIGURE_ND_TEMPLATE = '''
interface {{ intf }}
{% if is_dhcpv6 %}
 ipv6 nd managed-config-flag
{% endif %}
{% for server in dns_servers %}
 ipv6 nd rdnss {{ server }}
{% endfor %}
 ipv6 nd prefix {{ prefix }}
 no ipv6 nd suppress-ra
exit
'''

ADD_VRF_TEMPLATE = '''
vrf {{ vrf_name }}
  vni {{ vni }}
exit-vrf

router bgp {{ bgp_as }} vrf {{ vrf_name }}
  address-family ipv4 unicast
{% for redist in redistribute %}
    redistribute {{ redist }}
{% endfor %}
  exit-address-family
  address-family ipv6 unicast
{% for redist in redistribute %}
    redistribute {{ redist }}
{% endfor %}
  exit-address-family
  address-family l2vpn evpn
    advertise ipv4 unicast
    advertise ipv6 unicast
{% if route_distinguishers|length > 0 %}
    rd {{ route_distinguishers[0] }}
{% else %}
    rd {{ local_ip }}:{{ vni }}
{% endif %}
{% for route_target in route_targets %}
    route-target import {{ route_target }}
    route-target export {{ route_target }}
{% endfor %}
{% for route_target in export_targets %}
    route-target export {{ route_target }}
{% endfor %}
{% for route_target in import_targets %}
    route-target import {{ route_target }}
{% endfor %}
{% if local_pref %}
    default-originate
{% endif %}
  exit-address-family

{% if local_pref %}
route-map LOCAL_PREF_{{ vrf_name }} permit 10
 set local-preference {{ local_pref }}
exit

router bgp {{ bgp_as }} vrf {{ vrf_name }}
 address-family ipv4 unicast
  neighbor route-map LOCAL_PREF_{{ vrf_name }} in
 exit-address-family
 address-family ipv6 unicast
  neighbor route-map LOCAL_PREF_{{ vrf_name }} in
 exit-address-family
{% endif %}

'''

DEL_VRF_TEMPLATE = '''
no vrf {{ vrf_name }}
no router bgp {{ bgp_as }} vrf {{ vrf_name }}
{% if local_pref %}
no route-map LOCAL_PREF_{{ vrf_name }}
{% endif %}

'''

LEAK_VRF_TEMPLATE = '''
router bgp {{ bgp_as }}
  address-family ipv4 unicast
    import vrf {{ vrf_name }}
  exit-address-family

  address-family ipv6 unicast
    import vrf {{ vrf_name }}
  exit-address-family

router bgp {{ bgp_as }} vrf {{ vrf_name }}
  bgp router-id {{ bgp_router_id }}
  address-family ipv4 unicast
{% for redist in redistribute %}
    redistribute {{ redist }}
{% endfor %}
  exit-address-family

  address-family ipv6 unicast
{% for redist in redistribute %}
    redistribute {{ redist }}
{% endfor %}
  exit-address-family

'''


# ============================================================================
# Public API Functions
# ============================================================================

def run_vtysh_command(command):
    """Execute a single vtysh command.

    This is a thin wrapper around the privileged vtysh command execution.
    Use this for read-only or simple single-line commands.

    :param command: Single vtysh command (e.g., 'show ip bgp summary json')
    :return: Command output as string

    Example:
        output = run_vtysh_command('show running-config')
    """
    return ovn_bgp_agent.privileged.vtysh.run_vtysh_command(command)


def apply_vtysh_config(config_string):
    """Apply multi-line FRR configuration.

    This function handles the complexity of applying multi-line vtysh
    configuration by automatically creating a temporary file and executing
    'vtysh -f <file>'.

    :param config_string: Multi-line FRR configuration string

    Example:
        config = '''
        router bgp 65000
          neighbor 1.1.1.1 remote-as 65001
          exit
        '''
        apply_vtysh_config(config)
    """
    try:
        # Create temporary config file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.conf',
                                         delete=False) as f:
            f.write(config_string)
            config_file = f.name
    except (IOError, OSError) as e:
        LOG.error('Failed to create FRR config file: %s', e)
        raise

    try:
        # Apply configuration via vtysh
        ovn_bgp_agent.privileged.vtysh.run_vtysh_config(config_file)
        LOG.debug('Successfully applied FRR configuration')
    except Exception as e:
        LOG.error('Failed to apply FRR configuration: %s', e)
        raise
    finally:
        # Cleanup temp file
        try:
            os.unlink(config_file)
        except OSError as e:
            LOG.debug('Failed to delete temp config file %s: %s',
                      config_file, e)


def set_default_redistribute(redist_opts):
    """Set default redistribute options for BGP.

    :param redist_opts: Set or list of redistribute options
                       (e.g., {'connected', 'static'})
    """
    if not isinstance(redist_opts, set):
        redist_opts = set(redist_opts)

    if redist_opts == DEFAULT_REDISTRIBUTE:
        return

    LOG.info('Setting default redistribute options: %s', redist_opts)
    DEFAULT_REDISTRIBUTE.clear()
    DEFAULT_REDISTRIBUTE.update(redist_opts)


def get_router_id():
    """Get BGP router ID from FRR.

    :return: Router ID as string, or None if not found
    """
    try:
        output = run_vtysh_command('show ip bgp summary json')
        bgp_summary = jsonutils.loads(output)
        router_id = bgp_summary.get('ipv4Unicast', {}).get('routerId')
        return router_id
    except Exception as e:
        LOG.warning('Failed to get BGP router ID: %s', e)
        return None


def get_asn():
    """Get BGP AS number from configuration.

    Tries config first, then queries running FRR configuration.

    :return: AS number as string, or None if not found
    """
    # Try config first
    if hasattr(CONF, 'bgp_AS') and CONF.bgp_AS:
        return str(CONF.bgp_AS)

    # Query FRR
    try:
        output = run_vtysh_command('show running-config')
        for line in output.splitlines():
            line = line.strip()
            if line.startswith('router bgp '):
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]
    except Exception as e:
        LOG.debug('Failed to get ASN from FRR: %s', e)

    return None


# ============================================================================
# EVPN Configuration Functions
# ============================================================================

def ensure_evpn_base_config():
    """Ensure base EVPN configuration in FRR.

    This configures:
    - advertise-all-vni (enables Type-2/3 EVPN routes)
    - EVPN address family activation

    This should be called once during agent initialization.
    """
    asn = get_asn()
    if not asn:
        LOG.warning('Cannot configure base EVPN without BGP AS number')
        return

    LOG.info('Configuring base EVPN settings for AS %s', asn)

    config = f'''
router bgp {asn}
 address-family l2vpn evpn
  advertise-all-vni
 exit-address-family
exit
'''

    apply_vtysh_config(config)
    LOG.info('Base EVPN configuration applied')


def vrf_reconfigure(evpn_info, action):
    """Configure or delete VRF for EVPN.

    :param evpn_info: Dictionary with VRF configuration:
        - vrf_name: VRF name (required)
        - vni: VNI number (required)
        - bgp_as: BGP AS number (required)
        - route_targets: List of route targets (optional)
        - route_distinguishers: List of RDs (optional)
        - import_targets: List of import-only RTs (optional)
        - export_targets: List of export-only RTs (optional)
        - local_ip: Local VTEP IP (optional)
        - local_pref: BGP local preference (optional)
    :param action: 'add-vrf' or 'del-vrf'

    Example:
        evpn_info = {
            'vrf_name': 'vrf-10100',
            'vni': 10100,
            'bgp_as': '65000',
            'route_targets': ['65000:10100'],
            'local_ip': '192.168.1.1',
        }
        vrf_reconfigure(evpn_info, 'add-vrf')
    """
    LOG.info('FRR VRF reconfiguration: action=%s, evpn_info=%s',
             action, evpn_info)

    vrf_templates = {
        'add-vrf': ADD_VRF_TEMPLATE,
        'del-vrf': DEL_VRF_TEMPLATE,
    }

    if action not in vrf_templates:
        LOG.error('Unknown FRR reconfiguration action: %s', action)
        return

    # Set defaults
    opts = {
        'route_targets': [],
        'route_distinguishers': [],
        'export_targets': [],
        'import_targets': [],
        'local_ip': CONF.evpn_local_ip or '0.0.0.0',
        'redistribute': DEFAULT_REDISTRIBUTE,
        'bgp_as': CONF.bgp_AS,
        'vrf_name': '',
        'vni': 0,
        'local_pref': None,
    }
    opts.update(evpn_info)

    # Auto-generate VRF name if not provided
    if not opts['vrf_name']:
        opts['vrf_name'] = f"{constants.OVN_EVPN_VRF_PREFIX}{opts['vni']}"

    # Render template
    vrf_template = Template(vrf_templates[action])
    vrf_config = vrf_template.render(**opts)

    # Apply configuration
    apply_vtysh_config(vrf_config)


def vrf_leak(vrf, bgp_as, bgp_router_id=None, template=LEAK_VRF_TEMPLATE):
    """Configure VRF route leaking.

    This allows routes from a VRF to be imported into the global BGP table.

    :param vrf: VRF name to leak routes from
    :param bgp_as: BGP AS number
    :param bgp_router_id: BGP router ID (auto-detected if not provided)
    :param template: Custom Jinja2 template (optional)
    """
    LOG.info('Configuring VRF leak for VRF %s on BGP AS %s', vrf, bgp_as)

    if not bgp_router_id:
        bgp_router_id = get_router_id()
        if not bgp_router_id:
            LOG.error('Cannot configure route leaking: router-id unknown')
            return

    vrf_template = Template(template)
    vrf_config = vrf_template.render(
        vrf_name=vrf,
        bgp_as=bgp_as,
        redistribute=DEFAULT_REDISTRIBUTE,
        bgp_router_id=bgp_router_id
    )

    apply_vtysh_config(vrf_config)


def nd_reconfigure(interface, prefix, opts):
    """Configure IPv6 Neighbor Discovery on interface.

    :param interface: Interface name (e.g., 'br-ex')
    :param prefix: IPv6 prefix (e.g., '2001:db8::/64')
    :param opts: Dictionary with ND options:
        - dhcpv6_stateless: Enable stateless DHCPv6 (bool)
        - dns_server: DNS servers as string "[2001:db8::1,2001:db8::2]"

    Example:
        nd_reconfigure('br-ex', '2001:db8::/64', {
            'dhcpv6_stateless': True,
            'dns_server': '[2001:4860:4860::8888]'
        })
    """
    LOG.info('Configuring IPv6 ND on interface %s with prefix %s',
             interface, prefix)

    nd_template = Template(CONFIGURE_ND_TEMPLATE)

    # Configure autoconfig
    if (not opts.get('dhcpv6_stateless', False) or
            opts.get('dhcpv6_stateless', '') not in ('true', True)):
        prefix += ' no-autoconfig'

    # Parse DNS servers
    dns_servers = []
    if opts.get('dns_server'):
        dns_str = opts['dns_server'].strip('[]')
        dns_servers = [s.strip() for s in dns_str.split(',')]

    # Managed config flag (DHCPv6)
    is_dhcpv6 = True

    # Render configuration
    nd_config = nd_template.render(
        intf=interface,
        prefix=prefix,
        dns_servers=dns_servers,
        is_dhcpv6=is_dhcpv6,
    )

    apply_vtysh_config(nd_config)