# OVN EVPN Driver for ovn-bgp-agent

完整的 EVPN 驱动实现，为 OVN 提供 BGP EVPN (RFC 7432) 支持，采用对称 IRB 架构。

## 核心设计

### Symmetric IRB 架构
- **单 VNI 设计**: 同一个 VNI 同时支持 L2 和 L3 (对称 IRB)
- **L2 类型**: 完整支持（Type-2/3 MAC 学习 + Type-5 路由）
- **L3 类型**: 纯路由（仅 Type-5，无 MAC 学习）

### 为什么单 VNI？
- ✅ **裸金属配置简单**: 只需加入一个 VNI
- ✅ **自动 L2+L3 协商**: 同子网走 Type-2，跨子网走 Type-5
- ✅ **符合 EVPN 标准**: 对称 IRB 是标准做法
- ✅ **资源利用高效**: 避免双 VNI 管理开销

## 特性

### EVPN 路由类型
- **Type-2 (MAC/IP)**: L2 类型网络的 MAC 学习和 ARP 抑制
- **Type-3 (IMET)**: BUM 流量处理（广播/未知单播/组播）
- **Type-5 (IP Prefix)**: 跨子网路由（所有类型）

### 三种关联模式
| 模式 | 粒度 | EVPN 能力 | 适用场景 |
|------|------|-----------|---------|
| **Network Association** | 网络级别 | L2+L3 或 L3 | 整网 EVPN |
| **Router Association** | 路由器级别 | L2+L3 或 L3 | 多网络共享 EVPN |
| **Port Association** | 端口级别 | 精细控制+自定义路由 | 特定 VM 定制 |

### 关键能力
- **Linux Bridge VLAN Filtering**: 多租户二层隔离（无需物理交换机）
- **FRR 集成**: 完整的 BGP EVPN 信令
- **性能优化**: 静态 FDB 和邻居表预填充
- **自动同步**: 周期性状态同步和 FRR 配置恢复

## 架构

### 数据流向
```
┌─────────────────────────────────────────────────────────┐
│ OpenStack Neutron + networking-bgpvpn                   │
│   └─ API: 创建 BGPVPN + Association                    │
└──────────────────────┬──────────────────────────────────┘
                       │ 写入 external_ids
                       ▼
┌─────────────────────────────────────────────────────────┐
│ OVN Northbound DB                                        │
│   └─ Logical_Switch.external_ids                        │
│      └─ neutron_bgpvpn:vni, type, rt, rd...            │
└──────────────────────┬──────────────────────────────────┘
                       │ OVN Northd 同步
                       ▼
┌─────────────────────────────────────────────────────────┐
│ OVN Southbound DB                                        │
│   └─ Port_Binding.external_ids                          │
└──────────────────────┬──────────────────────────────────┘
                       │ 事件监听
                       ▼
┌─────────────────────────────────────────────────────────┐
│ ovn-bgp-agent (本 EVPN Driver)                          │
│   ├─ 创建 VXLAN/VRF/IRB 设备                           │
│   ├─ 配置 Linux Bridge VLAN Filtering                  │
│   ├─ L2 类型: 创建 OVN Internal Port                   │
│   └─ 配置 FRR BGP EVPN                                  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ Linux Data Plane + FRR Control Plane                    │
│   ├─ vxlan-<VNI>: 单 VXLAN 设备（L2+L3）              │
│   ├─ vrf-<VNI>: VRF 设备                               │
│   ├─ br-evpn.<VLAN>: IRB 设备                          │
│   ├─ evpn-<VNI>: Internal Port (L2 类型)               │
│   └─ FRR: Type-2/3/5 路由广播                          │
└─────────────────────────────────────────────────────────┘
```

### 网络拓扑（L2 类型 - Symmetric IRB）
```
┌──────────────────────────────────────────────────────────┐
│ Compute Node                                             │
│                                                           │
│ VM-A (10.0.1.10) ──► OVN br-int ──► evpn-10100          │
│                      (Geneve)        (Internal Port)     │
│                                            │              │
│                                            ▼              │
│                      ┌──────────────────────────┐        │
│                      │   br-evpn (VLAN-aware)  │        │
│                      │   - VLAN 100            │        │
│                      └──┬────────────────┬─────┘        │
│                         │                │               │
│                         ▼                ▼               │
│                   vxlan-10100      br-evpn.100           │
│                   (VNI 10100)      (IRB, VRF)            │
│                         │                │               │
│                         │                └─► L3 路由     │
│                         └─► L2 MAC 学习                  │
│                                                           │
└────────────────────────┬──────────────────────────────────┘
                         │ VXLAN (VNI 10100)
                         ▼
┌─────────────────────────────────────────────────────────┐
│ EVPN Network (ToR/Spine)                                │
│   同子网流量 → Type-2 (MAC 查找)                         │
│   跨子网流量 → Type-5 (路由查找)                         │
└─────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 前置要求

**系统要求:**
- Linux Kernel 3.8+ (VLAN filtering)
- Python 3.8+
- FRR 7.5+
- OVN 20.06+

**安装依赖:**
```bash
# Ubuntu/Debian
apt install -y frr frr-pythontools openvswitch-switch bridge-utils iproute2

# RHEL/CentOS
yum install -y frr openvswitch bridge-utils iproute
```

### 2. 安装 ovn-bgp-agent
```bash
pip3 install ovn-bgp-agent
```

### 3. 部署驱动
```bash
# 获取驱动代码
git clone <repository>
cd ovn-evpn-driver

# 复制文件到正确位置
./deploy.sh
```

### 4. 配置

编辑 `/etc/neutron/bgp_agent.ini`:
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf

# BGP 配置
bgp_AS = 65000
bgp_router_id = 192.0.2.1

# VTEP 配置（二选一）
evpn_local_ip = 192.0.2.1      # 直接指定
# evpn_nic = eth1              # 或从网卡获取

# EVPN 桥接
evpn_bridge = br-evpn
evpn_bridge_veth = veth-to-ovs
evpn_ovs_veth = veth-to-evpn
ovs_bridge = br-int             # tenant: br-int, provider: br-ex

# 优化选项
evpn_static_fdb = True          # 静态 FDB（减少泛洪）
evpn_static_neighbors = True    # 静态邻居（减少 ARP）
delete_vrf_on_disconnect = False # VRF 复用

[ovn]
ovn_sb_connection = tcp:192.0.2.10:6642
```

### 5. 配置 FRR

编辑 `/etc/frr/daemons`:
```bash
bgpd=yes
zebra=yes
```

配置 BGP Peer（`/etc/frr/frr.conf`）:
```
router bgp 65000
 bgp router-id 192.0.2.1
 neighbor 192.0.2.254 remote-as 65000
 !
 address-family l2vpn evpn
  neighbor 192.0.2.254 activate
  advertise-all-vni
 exit-address-family
!
```

启动服务:
```bash
systemctl enable frr
systemctl start frr
systemctl enable ovn-bgp-agent
systemctl start ovn-bgp-agent
```

## 使用示例

### L2 类型（Symmetric IRB - 推荐）

**同时支持同子网 L2 和跨子网 L3**:
```bash
# 1. 创建网络
openstack network create tenant-net1
openstack subnet create --network tenant-net1 \
  --subnet-range 10.0.1.0/24 tenant-subnet1

# 2. 创建 L2 类型 BGPVPN（自动支持 L2+L3）
openstack bgpvpn create \
  --type l2 \
  --vni 10100 \
  --route-target 65000:10100 \
  --name bgpvpn-symmetric \
  my-bgpvpn

# 3. 关联网络
openstack bgpvpn network association create \
  my-bgpvpn \
  --network tenant-net1
```

**效果**:
- ✅ 同子网（10.0.1.x ↔ 10.0.1.y）：L2 直达，EVPN Type-2
- ✅ 跨子网（10.0.1.x ↔ 10.0.2.y）：L3 路由，EVPN Type-5
- ✅ 裸金属只需加入 VNI 10100

### L3 类型（Pure Routing）

**仅路由，无 MAC 学习**:
```bash
# 创建 L3 类型 BGPVPN（纯路由）
openstack bgpvpn create \
  --type l3 \
  --vni 20000 \
  --route-target 65000:20000 \
  --name bgpvpn-routing-only \
  routing-bgpvpn

# 路由器关联
openstack bgpvpn router association create \
  routing-bgpvpn \
  --router tenant-router
```

**效果**:
- ✅ 仅 EVPN Type-5 路由
- ✅ 无 Internal Port，无 MAC 表
- ✅ 优化性能（路由器/防火墙场景）

### Port Association（精细控制）
```bash
# 端口级别 EVPN + 自定义路由
openstack bgpvpn port association create \
  my-bgpvpn \
  --port vm-port-uuid \
  --advertise-fixed-ips \
  --routes destination=10.0.0.0/24,nexthop=192.168.1.10
```

## 验证

### 基础设施检查
```bash
# 检查 EVPN 桥（VLAN filtering 应为 on）
ip -d link show br-evpn | grep vlan_filtering

# 检查 VXLAN 设备（单 VNI）
ip -d link show type vxlan
# 输出: vxlan-10100 (VNI 10100, local 192.0.2.1)

# 检查 VRF
ip vrf show
# 输出: vrf-10100 (table 1010100)

# 检查 IRB
ip link show br-evpn.100

# L2 类型: 检查 Internal Port
ip link show evpn-10100
```

### FRR 验证
```bash
vtysh << EOF
show bgp l2vpn evpn summary
show bgp l2vpn evpn vni
show bgp l2vpn evpn route type macip    # Type-2 (L2 类型)
show bgp l2vpn evpn route type prefix   # Type-5 (所有类型)
EOF
```

### 数据平面验证
```bash
# 检查 VLAN 配置
bridge vlan show

# 检查静态 FDB（L2 类型）
bridge fdb show dev veth-to-ovs | grep static

# 检查邻居表
ip neigh show dev br-evpn.100

# 检查 VRF 路由
ip route show vrf vrf-10100
```

## 故障排查

### 问题 1: Internal Port 未创建（L2 类型）

**症状**: L2 类型网络无 Type-2 路由

**检查**:
```bash
# 应该存在
ip link show evpn-10100

# OVS 端口应存在
ovs-vsctl list-ports br-int | grep evpn
```

**原因**:
- `type=l3` 不会创建 Internal Port
- 检查日志确认类型: `journalctl -u ovn-bgp-agent | grep "type="`

### 问题 2: VLAN Filtering 未启用

**症状**: 多租户流量泄露

**检查**:
```bash
ip -d link show br-evpn | grep vlan_filtering
# 应输出: vlan_filtering 1
```

**修复**:
```bash
ip link set br-evpn type bridge vlan_filtering 1
```

### 问题 3: FRR 无 Type-2 路由（L2 类型）

**症状**: 同子网不通

**检查**:
```bash
# 检查邻居表（应有条目）
ip neigh show dev br-evpn.100

# 检查 FRR
vtysh -c "show bgp l2vpn evpn route type macip"
```

**原因**: Internal Port 未注入流量或邻居表未填充

## 性能调优

### 大规模部署（1000+ VM）
```ini
[DEFAULT]
# 禁用静态表（减少内存）
evpn_static_fdb = False
evpn_static_neighbors = False

# 延长同步间隔
reconcile_interval = 600
frr_reconcile_interval = 30

# VRF 复用
delete_vrf_on_disconnect = False
```

### 小规模高性能
```ini
[DEFAULT]
# 启用所有优化
evpn_static_fdb = True
evpn_static_neighbors = True

# 快速同步
reconcile_interval = 120
frr_reconcile_interval = 15
```

## 限制

- **OVN VLAN 范围**: 单节点最多 4096 个逻辑网络（OVN 本地 VLAN tag）
- **Kernel 版本**: 需要 3.8+ (VLAN filtering)
- **MTU**: 需考虑 VXLAN 封装（建议 Jumbo Frame）

## 文档

- [架构设计](ARCHITECTURE.md) - 详细技术架构
- [配置指南](CONFIGURATION.md) - 完整配置参数
- [部署指南](DEPLOYMENT.md) - 生产部署步骤
- [API 参考](API.md) - API 和数据结构

## 相关项目

- [networking-bgpvpn](https://github.com/openstack/networking-bgpvpn) - Neutron BGPVPN API
- [ovn-bgp-agent](https://github.com/openstack/ovn-bgp-agent) - OVN BGP Agent 框架
- [FRRouting](https://frrouting.org/) - 开源路由协议栈

## 许可证

Apache License 2.0

## 贡献

欢迎提交 Issue 和 Pull Request！

## 支持

- 邮件列表: openstack-discuss@lists.openstack.org
- IRC: #openstack-neutron @ OFTC
- Issues: GitHub Issues