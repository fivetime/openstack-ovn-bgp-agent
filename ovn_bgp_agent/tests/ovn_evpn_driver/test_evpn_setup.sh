#!/bin/bash
# EVPN Driver Test and Validation Script

set -e

echo "=========================================="
echo "EVPN Driver Setup Validation"
echo "=========================================="

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

function check_pass() {
    echo -e "${GREEN}✓${NC} $1"
}

function check_fail() {
    echo -e "${RED}✗${NC} $1"
}

function check_warn() {
    echo -e "${YELLOW}!${NC} $1"
}

# 1. Check kernel version
echo ""
echo "1. Checking kernel version (need 3.8+ for VLAN filtering)..."
KERNEL_VERSION=$(uname -r | cut -d. -f1,2)
if (( $(echo "$KERNEL_VERSION >= 3.8" | bc -l) )); then
    check_pass "Kernel $KERNEL_VERSION supports VLAN filtering"
else
    check_fail "Kernel $KERNEL_VERSION too old, need 3.8+"
    exit 1
fi

# 2. Check required tools
echo ""
echo "2. Checking required tools..."
for tool in ip bridge ovs-vsctl vtysh; do
    if command -v $tool &> /dev/null; then
        check_pass "$tool found"
    else
        check_fail "$tool not found"
        exit 1
    fi
done

# 3. Check FRR
echo ""
echo "3. Checking FRR..."
if systemctl is-active --quiet frr; then
    check_pass "FRR is running"

    # Check BGP
    if vtysh -c "show running-config" | grep -q "router bgp"; then
        ASN=$(vtysh -c "show running-config" | grep "router bgp" | head -1 | awk '{print $3}')
        check_pass "BGP configured (AS $ASN)"
    else
        check_warn "BGP not configured yet"
    fi
else
    check_fail "FRR is not running"
    exit 1
fi

# 4. Check OVS
echo ""
echo "4. Checking OVS..."
if ovs-vsctl show &> /dev/null; then
    check_pass "OVS is accessible"

    # Check br-int
    if ovs-vsctl br-exists br-int; then
        check_pass "br-int exists"
    else
        check_fail "br-int does not exist"
        exit 1
    fi
else
    check_fail "OVS is not accessible"
    exit 1
fi

# 5. Check bridge command supports JSON
echo ""
echo "5. Checking bridge tool capabilities..."
if bridge -j link show &> /dev/null; then
    check_pass "Bridge tool supports JSON output"
else
    check_warn "Bridge tool does not support JSON (old version)"
fi

# 6. Test VLAN filtering
echo ""
echo "6. Testing VLAN filtering capability..."
TEST_BR="test-vlan-br-$$"
ip link add $TEST_BR type bridge vlan_filtering 1 2>/dev/null
if [ $? -eq 0 ]; then
    check_pass "VLAN filtering is supported"
    ip link del $TEST_BR
else
    check_fail "VLAN filtering is NOT supported"
    exit 1
fi

# 7. Check VXLAN support
echo ""
echo "7. Testing VXLAN capability..."
TEST_VXLAN="test-vxlan-$$"
ip link add $TEST_VXLAN type vxlan id 999 local 127.0.0.1 dstport 4789 2>/dev/null
if [ $? -eq 0 ]; then
    check_pass "VXLAN is supported"
    ip link del $TEST_VXLAN
else
    check_fail "VXLAN is NOT supported"
    exit 1
fi

# 8. Check VRF support
echo ""
echo "8. Testing VRF capability..."
TEST_VRF="test-vrf-$$"
ip link add $TEST_VRF type vrf table 999 2>/dev/null
if [ $? -eq 0 ]; then
    check_pass "VRF is supported"
    ip link del $TEST_VRF
else
    check_fail "VRF is NOT supported"
    exit 1
fi

# 9. Check if br-evpn exists (if agent is running)
echo ""
echo "9. Checking EVPN infrastructure..."
if ip link show br-evpn &> /dev/null; then
    check_pass "br-evpn exists"

    # Check VLAN filtering
    if ip -d link show br-evpn | grep -q "vlan_filtering 1"; then
        check_pass "VLAN filtering enabled on br-evpn"
    else
        check_warn "VLAN filtering NOT enabled on br-evpn"
    fi

    # Check veth pair
    if ip link show veth-to-ovs &> /dev/null; then
        check_pass "veth-to-ovs exists"
    else
        check_warn "veth-to-ovs does not exist"
    fi
else
    check_warn "br-evpn does not exist (agent not started?)"
fi

# 10. Summary
echo ""
echo "=========================================="
echo "Validation Summary"
echo "=========================================="
echo ""
check_pass "All critical checks passed!"
echo ""
echo "System is ready for EVPN driver."
echo ""
echo "Next steps:"
echo "  1. Configure /etc/ovn-bgp-agent/bgp-agent.conf"
echo "  2. Set driver=ovn_evpn_driver"
echo "  3. Configure evpn_local_ip or evpn_nic"
echo "  4. Start ovn-bgp-agent service"
echo ""