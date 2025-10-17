# 配置指南

## 配置文件位置
```
/etc/neutron/bgp_agent.ini
```

## 完整配置示例

### 生产环境配置（推荐）
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

# 启动清理（生产建议关闭）
clear_vrf_routes_on_startup = False

# VRF 复用（生产建议启用）
delete_vrf_on_disconnect = False

# ============================================================================
# EVPN VTEP 配置（二选一）
# ============================================================================
# 选项 1: 直接指定 VTEP IP（推荐）
evpn_local_ip = 192.0.2.1

# 选项 2: 从网卡获取 IP
# evpn_nic = eth1

# VXLAN 端口（IANA 标准）
evpn_udp_dstport = 4789

# ============================================================================
# EVPN 桥接配置
# ============================================================================
evpn_bridge = br-evpn
evpn_bridge_veth = veth-to-ovs
evpn_ovs_veth = veth-to-evpn

# OVS 桥接（根据部署选择）
ovs_bridge = br-int          # Tenant 网络使用 br-int
# ovs_bridge = br-ex         # Provider 网络使用 br-ex

# ============================================================================
# EVPN 优化（根据规模调整）
# ============================================================================
# 静态 FDB 表（小规模推荐开启，大规模推荐关闭）
evpn_static_fdb = True

# 静态邻居表（小规模推荐开启，大规模推荐关闭）
evpn_static_neighbors = True

# ============================================================================
# 同步间隔（根据规模调整）
# ============================================================================
# 完整同步间隔（秒）
reconcile_interval = 300        # 小规模: 120-300, 大规模: 600-900

# FRR 配置同步间隔（秒）
frr_reconcile_interval = 15     # 建议: 15-30

# ============================================================================
# OVS 连接
# ============================================================================
ovsdb_connection = unix:/var/run/openvswitch/db.sock
ovsdb_connection_timeout = 180

# ============================================================================
# Tenant Networks（可选）
# ============================================================================
expose_tenant_networks = False
expose_ipv6_gua_tenant_networks = False

# ============================================================================
# 日志配置
# ============================================================================
debug = False
log_file = /var/log/neutron/ovn-bgp-agent.log
log_dir = /var/log/neutron

[agent]
root_helper = sudo neutron-rootwrap /etc/neutron/rootwrap.conf

[ovn]
# ============================================================================
# OVN SB 连接
# ============================================================================
ovn_sb_connection = tcp:192.0.2.10:6642

# 高可用连接（多个 OVN SB）
# ovn_sb_connection = tcp:192.0.2.10:6642,tcp:192.0.2.11:6642,tcp:192.0.2.12:6642

# SSL 连接（生产推荐）
# ovn_sb_connection = ssl:ovn-sb-1.example.com:6642,ssl:ovn-sb-2.example.com:6642
# ovn_sb_private_key = /etc/pki/tls/private/ovn_bgp_agent.key
# ovn_sb_certificate = /etc/pki/tls/certs/ovn_bgp_agent.crt
# ovn_sb_ca_cert = /etc/pki/tls/certs/ca-bundle.crt
```

---

## 配置参数详解

### 核心驱动参数

#### driver
- **类型**: String
- **必需**: 是
- **默认**: None
- **可选值**: `ovn_evpn_driver`
- **说明**: 驱动名称，必须设置为 `ovn_evpn_driver`
- **示例**: `driver = ovn_evpn_driver`

#### exposing_method
- **类型**: String
- **必需**: 是
- **默认**: `vrf`
- **可选值**: `vrf`, `dynamic`
- **说明**:
    - `vrf`: 使用 VRF 暴露路由（EVPN 标准方式）
    - `dynamic`: 混合模式（不推荐用于 EVPN）
- **推荐**: `vrf`

---

### BGP 核心参数

#### bgp_AS
- **类型**: String (数字)
- **必需**: 是
- **默认**: `64999`
- **范围**: 1-4294967295
- **说明**: BGP 自治系统号
    - 私有 AS: 64512-65534 (16-bit), 4200000000-4294967294 (32-bit)
    - 公有 AS: 需向 IANA 申请
- **示例**: `bgp_AS = 65000`
- **注意**: 必须与 FRR 配置中的 AS 号一致

#### bgp_router_id
- **类型**: IP Address
- **必需**: 否（推荐配置）
- **默认**: None（使用 VTEP IP）
- **说明**: BGP Router ID，建议与 `evpn_local_ip` 相同
- **示例**: `bgp_router_id = 192.0.2.1`

---

### EVPN VTEP 参数

#### evpn_local_ip
- **类型**: IP Address (IPv4)
- **必需**: 否（但推荐）
- **默认**: None
- **说明**: VXLAN 隧道本地端点 IP (VTEP IP)
- **优先级**: 最高
- **要求**:
    - 必须在 underlay 网络可达
    - 建议使用 Loopback IP
    - 每个节点唯一
- **示例**: `evpn_local_ip = 192.0.2.1`

#### evpn_nic
- **类型**: String (接口名)
- **必需**: 否
- **默认**: None
- **说明**: 从该网卡获取 VTEP IP
- **优先级**: 次于 `evpn_local_ip`
- **示例**: `evpn_nic = eth1`
- **用途**: 动态环境（IP 可能变化）

**VTEP IP 选择逻辑**:
```
1. evpn_local_ip (显式配置) → 直接使用
   ↓ 未配置
2. evpn_nic 的第一个 IPv4 地址 → 使用
   ↓ 未配置
3. Loopback 的第一个非 127.x.x.x IPv4 → 使用
   ↓ 失败
4. 抛出异常: ConfOptionRequired
```

#### evpn_udp_dstport
- **类型**: Integer (端口号)
- **必需**: 否
- **默认**: `4789`
- **范围**: 1-65535
- **说明**: VXLAN UDP 目标端口
- **标准**: IANA 分配 4789
- **注意**:
    - 必须与网络设备（ToR/Spine）一致
    - 老版本可能使用 4300 或 8472

---

### 桥接配置

#### evpn_bridge
- **类型**: String (接口名)
- **必需**: 否
- **默认**: `br-evpn`
- **说明**: EVPN 主桥名称
- **作用**:
    - 所有 VXLAN 设备连接到此桥
    - 启用 VLAN filtering 实现多租户隔离
- **示例**: `evpn_bridge = br-evpn`

#### evpn_bridge_veth
- **类型**: String (接口名)
- **必需**: 否
- **默认**: `veth-to-ovs`
- **说明**: EVPN 桥侧的 veth 接口名
- **作用**: 连接 `br-evpn` 和 OVS 桥

#### evpn_ovs_veth
- **类型**: String (接口名)
- **必需**: 否
- **默认**: `veth-to-evpn`
- **说明**: OVS 桥侧的 veth 接口名
- **作用**: OVS 侧的连接端点

**Veth 对拓扑**:
```
br-evpn ←──[evpn_bridge_veth]──[evpn_ovs_veth]──→ OVS bridge
(Linux)                                              (br-int/br-ex)
```

#### ovs_bridge
- **类型**: String (OVS 桥名)
- **必需**: 否
- **默认**: `br-ex`
- **说明**: OVS 桥名称
- **常用值**:
    - `br-int`: Tenant 网络（overlay）
    - `br-ex`: Provider 网络（underlay）
- **选择依据**:
    - 如果 VM 在 OVN overlay: 使用 `br-int`
    - 如果 VM 直连 provider: 使用 `br-ex`

---

### 优化参数

#### evpn_static_fdb
- **类型**: Boolean
- **必需**: 否
- **默认**: `True`
- **说明**: 预填充 FDB (Forwarding Database)
- **效果**:
    - ✅ 减少 L2 flooding
    - ✅ 加速 EVPN Type-2 广播
    - ✅ 首包无需 flooding
    - ⚠️ 占用内存（每条目 ~200 字节）
- **推荐**:
    - 小规模（< 1000 VM）: `True`
    - 大规模（> 1000 VM）: `False`

**实现原理**:
```bash
# 对每个 VM MAC，添加静态 FDB
bridge fdb add 52:54:00:aa:bb:cc \
  dev veth-to-ovs \
  vlan 1000 \
  master static
```

#### evpn_static_neighbors
- **类型**: Boolean
- **必需**: 否
- **默认**: `True`
- **说明**: 预填充邻居表（ARP/NDP）
- **效果**:
    - ✅ 避免 ARP/NDP 查询
    - ✅ 立即触发 Type-2 广播
    - ✅ 加速首包转发
    - ⚠️ 占用内核 ARP 表
- **推荐**:
    - 小规模: `True`
    - 大规模: `False`（让 EVPN 动态学习）

**实现原理**:
```bash
# 对每个 VM IP，添加静态邻居
ip neigh add 10.0.1.10 \
  lladdr 52:54:00:aa:bb:cc \
  dev br-evpn.100 \
  nud permanent
```

---

### 同步参数

#### reconcile_interval
- **类型**: Integer (秒)
- **必需**: 否
- **默认**: `300`
- **范围**: 60-3600
- **说明**: 完整状态同步间隔
- **作用**:
    - 周期性检查 OVN 状态
    - 确保设备和配置一致
    - 清理孤儿资源
- **推荐**:
    - 小规模（< 100 VM）: `120`
    - 中等规模（100-1000 VM）: `300`
    - 大规模（> 1000 VM）: `600`
- **权衡**:
    - 短间隔: 快速恢复，高 CPU 开销
    - 长间隔: 低开销，恢复慢

#### frr_reconcile_interval
- **类型**: Integer (秒)
- **必需**: 否
- **默认**: `15`
- **范围**: 5-60
- **说明**: FRR 配置同步间隔
- **作用**: FRR 重启后快速恢复 EVPN 配置
- **推荐**: `15`（FRR 稳定后可适当延长到 30）

---

### VRF 管理参数

#### clear_vrf_routes_on_startup
- **类型**: Boolean
- **必需**: 否
- **默认**: `False`
- **说明**: 启动时清空 VRF 路由表
- **使用场景**:
    - Agent 异常崩溃后清理脏数据
    - 调试环境清理旧配置
- **生产推荐**: `False`（避免误清理）

#### delete_vrf_on_disconnect
- **类型**: Boolean
- **必需**: 否
- **默认**: `True`
- **说明**: 不再使用时删除 VRF 设备
- **EVPN 推荐**: `False`
- **原因**:
    - 允许 VRF 复用
    - 避免频繁创建/删除
    - 提升性能

---

## 场景化配置

### 场景 1: 小规模部署 (< 100 VM)
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
bgp_AS = 65000
evpn_local_ip = 192.0.2.1

# 启用所有优化
evpn_static_fdb = True
evpn_static_neighbors = True

# 快速同步
reconcile_interval = 120
frr_reconcile_interval = 15

# VRF 复用
delete_vrf_on_disconnect = False

[ovn]
ovn_sb_connection = tcp:controller:6642
```

---

### 场景 2: 中等规模 (100-1000 VM)
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
bgp_AS = 65000
evpn_local_ip = 192.0.2.1

# 部分优化
evpn_static_fdb = True
evpn_static_neighbors = False

# 标准同步
reconcile_interval = 300
frr_reconcile_interval = 20

delete_vrf_on_disconnect = False

[ovn]
ovn_sb_connection = tcp:ovn-sb-1:6642,tcp:ovn-sb-2:6642
```

---

### 场景 3: 大规模部署 (> 1000 VM)
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
bgp_AS = 65000
evpn_local_ip = 192.0.2.1

# 禁用静态表（减少内存）
evpn_static_fdb = False
evpn_static_neighbors = False

# 延长同步间隔
reconcile_interval = 600
frr_reconcile_interval = 30

delete_vrf_on_disconnect = False

[ovn]
# SSL 连接（生产）
ovn_sb_connection = ssl:ovn-sb-1.example.com:6642,ssl:ovn-sb-2.example.com:6642,ssl:ovn-sb-3.example.com:6642
ovn_sb_private_key = /etc/pki/tls/private/ovn_bgp_agent.key
ovn_sb_certificate = /etc/pki/tls/certs/ovn_bgp_agent.crt
ovn_sb_ca_cert = /etc/pki/tls/certs/ca-bundle.crt
```

---

### 场景 4: 开发/测试环境
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
bgp_AS = 64999
evpn_nic = eth0          # 动态获取 IP

# 快速反馈
evpn_static_fdb = True
evpn_static_neighbors = True
reconcile_interval = 60
frr_reconcile_interval = 10

# 调试日志
debug = True

[ovn]
ovn_sb_connection = unix:/var/run/ovn/ovnsb_db.sock
```

---

## FRR 配置

### 启用守护进程

编辑 `/etc/frr/daemons`:
```bash
bgpd=yes
zebra=yes
staticd=no
ospfd=no
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=no
fabricd=no
vrrpd=no
```

### 基础 EVPN 配置

编辑 `/etc/frr/frr.conf`:
```
frr version 8.0
frr defaults traditional
hostname compute-node-1
log syslog informational
service integrated-vtysh-config
!
router bgp 65000
 bgp router-id 192.0.2.1
 bgp log-neighbor-changes
 no bgp default ipv4-unicast
 !
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
end
```

**关键配置解释**:
- `bgp router-id`: 必须唯一（通常用 VTEP IP）
- `no bgp default ipv4-unicast`: 禁用默认 IPv4（EVPN 不需要）
- `advertise-all-vni`: 自动广播所有 VNI（关键）

---

## 验证配置

### 配置语法检查
```bash
# 检查 Agent 配置
ovn-bgp-agent --config-file /etc/neutron/bgp_agent.ini --dry-run

# 检查 FRR 配置
vtysh -c "show running-config" | less
```

### 启动验证
```bash
# 启动服务
systemctl start ovn-bgp-agent

# 查看日志
journalctl -u ovn-bgp-agent -n 50

# 检查关键信息
journalctl -u ovn-bgp-agent | grep -E "(VTEP IP|Starting|EVPN)"
```

**期望输出**:
```
Starting OVN EVPN Driver (Symmetric IRB)
VTEP IP: 192.0.2.1
Chassis: abc123...
EVPN prerequisites ready
```

### 运行时检查
```bash
# 检查 EVPN 基础设施
ip link show br-evpn
ip -d link show br-evpn | grep vlan_filtering  # 应输出 1

# 检查 VTEP 连通性
ping <remote_vtep_ip>

# 检查 BGP 会话
vtysh -c "show bgp summary"
```

---

## 配置模板生成

### 自动生成配置
```bash
# 生成示例配置
cat > /etc/neutron/bgp_agent.ini << EOF
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
bgp_AS = $(read -p "Enter BGP AS: " as; echo $as)
evpn_local_ip = $(ip -4 addr show dev eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}')

[ovn]
ovn_sb_connection = tcp:$(read -p "Enter OVN SB IP: " ip; echo $ip):6642
EOF
```

---

## 常见配置错误

### 错误 1: VTEP IP 未配置
```
ERROR: ConfOptionRequired: evpn_local_ip or evpn_nic
```
**解决**: 添加 `evpn_local_ip` 或 `evpn_nic`

### 错误 2: FRR 未启用 bgpd
```
ERROR: vtysh: can't connect to bgpd
```
**解决**: 编辑 `/etc/frr/daemons`，设置 `bgpd=yes`

### 错误 3: OVS 连接失败
```
ERROR: Could not connect to OVS
```
**解决**: 检查 `ovsdb_connection` 路径和权限

### 错误 4: BGP AS 不匹配
```
WARNING: BGP AS mismatch (config: 65000, FRR: 64999)
```
**解决**: 确保 `bgp_AS` 与 FRR 配置一致

---

## 配置最佳实践

1. **生产环境**:
    - 使用 SSL 连接 OVN SB
    - 配置多个 OVN SB 节点（高可用）
    - 禁用静态表（大规模）
    - 延长同步间隔

2. **安全**:
    - 限制 vtysh 权限
    - 使用 SSL 证书
    - 防火墙规则（BGP 179, VXLAN 4789）

3. **性能**:
    - 根据规模调整 `reconcile_interval`
    - 大规模禁用静态优化
    - 考虑 Jumbo Frame (MTU 9000)

4. **可维护性**:
    - 记录所有自定义配置
    - 使用配置管理工具（Ansible/Puppet）
    - 定期备份配置文件