# 配置指南

## 配置文件位置
```
/etc/neutron/bgp_agent.ini
```

## 完整配置示例
```ini
[DEFAULT]
# ============================================================================
# Driver 配置
# ============================================================================
driver = ovn_evpn_driver
exposing_method = vrf

# ============================================================================
# BGP 配置
# ============================================================================
bgp_AS = 65000
bgp_router_id = 192.0.2.1

# VRF 配置
bgp_vrf = bgp-vrf
bgp_vrf_table_id = 10
bgp_nic = bgp-nic

# 启动时清理
clear_vrf_routes_on_startup = False
delete_vrf_on_disconnect = False

# ============================================================================
# EVPN VTEP 配置
# ============================================================================
# 选项 1: 直接指定 IP
evpn_local_ip = 192.0.2.1

# 选项 2: 从网卡获取
# evpn_nic = eth1

# VXLAN 端口
evpn_udp_dstport = 4789

# ============================================================================
# EVPN 桥接配置
# ============================================================================
evpn_bridge = br-evpn
evpn_bridge_veth = veth-to-ovs
evpn_ovs_veth = veth-to-evpn
ovs_bridge = br-ex

# ============================================================================
# VNI 配置
# ============================================================================
# L2VNI 自动计算 (L2VNI = VLAN_ID + offset)
l2vni_offset = 10000

# ============================================================================
# EVPN 优化
# ============================================================================
# 静态 FDB 表 (减少 flooding)
evpn_static_fdb = True

# 静态邻居表 (减少 ARP/NDP)
evpn_static_neighbors = True

# ============================================================================
# 同步间隔
# ============================================================================
reconcile_interval = 300
frr_reconcile_interval = 15

# ============================================================================
# OVS 连接
# ============================================================================
ovsdb_connection = unix:/usr/local/var/run/openvswitch/db.sock
ovsdb_connection_timeout = 180

# ============================================================================
# Tenant Networks (可选)
# ============================================================================
expose_tenant_networks = False
expose_ipv6_gua_tenant_networks = False

[agent]
root_helper = sudo

[ovn]
# OVN SB 连接
ovn_sb_connection = tcp:192.0.2.10:6642

# SSL 配置 (可选)
# ovn_sb_private_key = /etc/pki/tls/private/ovn_bgp_agent.key
# ovn_sb_certificate = /etc/pki/tls/certs/ovn_bgp_agent.crt
# ovn_sb_ca_cert = /etc/ipa/ca.crt
```

## 配置参数详解

### BGP 核心参数

#### bgp_AS
- **类型**: String
- **默认**: `64999`
- **说明**: BGP AS 号码
- **示例**: `65000`

#### bgp_router_id
- **类型**: String
- **默认**: None
- **说明**: BGP Router ID (通常使用 VTEP IP)
- **示例**: `192.0.2.1`

### EVPN VTEP 参数

#### evpn_local_ip
- **类型**: IP Address
- **默认**: None
- **说明**: VXLAN 隧道本地端点 IP (VTEP IP)
- **优先级**: 最高
- **示例**: `192.0.2.1`

#### evpn_nic
- **类型**: String
- **默认**: None
- **说明**: 从该网卡获取 VTEP IP
- **优先级**: 次于 evpn_local_ip
- **示例**: `eth1`

**VTEP IP 选择逻辑**:
```
1. evpn_local_ip (配置)
   ↓ 未配置
2. evpn_nic 的 IP
   ↓ 未配置  
3. loopback 的第一个非 127.0.0.0/8 IP
   ↓ 都失败
4. 抛出异常
```

#### evpn_udp_dstport
- **类型**: Port (1-65535)
- **默认**: `4789`
- **说明**: VXLAN UDP 目标端口
- **标准**: IANA 分配端口 4789

### 桥接配置

#### evpn_bridge
- **类型**: String
- **默认**: `br-evpn`
- **说明**: EVPN 主桥名称，所有 VXLAN 设备连接到此桥

#### evpn_bridge_veth / evpn_ovs_veth
- **类型**: String
- **默认**: `veth-to-ovs` / `veth-to-evpn`
- **说明**: 连接 EVPN 桥和 OVS 桥的 veth 对

#### ovs_bridge
- **类型**: String
- **默认**: `br-ex`
- **说明**: OVS 桥名称
- **常用值**: `br-ex` (provider) 或 `br-int` (integration)

### VNI 配置

#### l2vni_offset
- **类型**: Integer
- **默认**: None
- **说明**: L2VNI 自动计算偏移量
- **公式**: `L2VNI = VLAN_ID + l2vni_offset`
- **示例**:
```
  l2vni_offset = 10000
  VLAN 100 → L2VNI 10100
  VLAN 200 → L2VNI 10200
```
- **覆盖**: OVN external_ids 显式指定的 VNI 优先

### 优化参数

#### evpn_static_fdb
- **类型**: Boolean
- **默认**: `True`
- **说明**: 预填充 FDB 表
- **效果**:
    - ✅ 减少 L2 flooding
    - ✅ 加速 EVPN Type-2 路由广播
    - ⚠️ 大规模场景下占用内存

#### evpn_static_neighbors
- **类型**: Boolean
- **默认**: `True`
- **说明**: 预填充邻居表 (ARP/NDP)
- **效果**:
    - ✅ 减少 ARP/NDP 查询
    - ✅ 加速首包转发
    - ⚠️ 占用内核邻居表空间

### 同步间隔

#### reconcile_interval
- **类型**: Integer (秒)
- **默认**: `300`
- **说明**: 完整状态同步间隔
- **建议**:
    - 小规模: `300` (5 分钟)
    - 大规模: `600` (10 分钟)

#### frr_reconcile_interval
- **类型**: Integer (秒)
- **默认**: `15`
- **说明**: FRR 配置同步间隔
- **用途**: FRR 重启后快速恢复配置

### VRF 管理

#### clear_vrf_routes_on_startup
- **类型**: Boolean
- **默认**: `False`
- **说明**: 启动时清空 VRF 路由表
- **使用场景**: 清理 agent 崩溃后的脏数据

#### delete_vrf_on_disconnect
- **类型**: Boolean
- **默认**: `True`
- **说明**: 删除不再使用的 VRF 设备
- **EVPN 建议**: `False` (允许 VRF 复用)

## 环境特定配置

### 生产环境
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf

bgp_AS = 65000
bgp_router_id = <node_vtep_ip>
evpn_local_ip = <node_vtep_ip>

# 大规模优化
evpn_static_fdb = False
evpn_static_neighbors = False
reconcile_interval = 600

# VRF 复用
delete_vrf_on_disconnect = False

[ovn]
ovn_sb_connection = ssl:ovn-sb-1.example.com:6642,\
                    ssl:ovn-sb-2.example.com:6642,\
                    ssl:ovn-sb-3.example.com:6642
ovn_sb_private_key = /etc/pki/tls/private/ovn_bgp_agent.key
ovn_sb_certificate = /etc/pki/tls/certs/ovn_bgp_agent.crt
ovn_sb_ca_cert = /etc/pki/tls/certs/ca-bundle.crt
```

### 开发/测试环境
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf

bgp_AS = 64999
evpn_nic = eth0

# 启用所有优化
evpn_static_fdb = True
evpn_static_neighbors = True

# 快速同步
reconcile_interval = 60
frr_reconcile_interval = 10

[ovn]
ovn_sb_connection = unix:/var/run/ovn/ovnsb_db.sock
```

## FRR 配置

需要在 `/etc/frr/daemons` 启用：
```bash
bgpd=yes
zebra=yes
```

`/etc/frr/frr.conf` 基础配置：
```
frr version 8.0
frr defaults traditional
hostname ovn-compute-1
log syslog informational
service integrated-vtysh-config

router bgp 65000
 bgp router-id 192.0.2.1
 no bgp default ipv4-unicast
 neighbor spine peer-group
 neighbor spine remote-as 65000
 neighbor 192.0.2.254 peer-group spine
 !
 address-family l2vpn evpn
  neighbor spine activate
  advertise-all-vni
 exit-address-family
!
line vty
!
```

## 验证配置
```bash
# 检查配置语法
ovn-bgp-agent --config-file /etc/neutron/bgp_agent.ini --validate

# 启动时查看配置
systemctl start ovn-bgp-agent
journalctl -u ovn-bgp-agent | grep "Loaded"

# 检查 VTEP IP
journalctl -u ovn-bgp-agent | grep "VTEP IP"
```

## 配置文件模板

从示例生成：
```bash
# 生成示例配置
ovn-bgp-agent-config-generator > /etc/neutron/bgp_agent.ini.example

# 基于示例创建
cp /etc/neutron/bgp_agent.ini.example /etc/neutron/bgp_agent.ini
vi /etc/neutron/bgp_agent.ini
```