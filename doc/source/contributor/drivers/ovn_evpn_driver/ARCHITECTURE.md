# EVPN Driver 架构设计

## 设计理念

### Symmetric IRB 单 VNI 架构

本驱动采用 **对称 IRB (Integrated Routing and Bridging)** 架构，核心设计：

- **单 VNI 原则**: 同一个 VNI 同时承载 L2 和 L3 流量
- **自动协商**: 同子网走 Type-2，跨子网走 Type-5
- **简化配置**: 裸金属/ToR 只需加入一个 VNI

### 为什么单 VNI？

| 对比项 | 双 VNI 方案 | 单 VNI (本方案) |
|-------|-------------|----------------|
| **裸金属配置** | 需配置 L2VNI + L3VNI | 仅配置一个 VNI |
| **路由复杂度** | 需手动区分 L2/L3 | 自动协商 |
| **EVPN 标准** | 非标准 | 标准对称 IRB |
| **资源消耗** | 双倍 VXLAN 设备 | 单一 VXLAN |

### L2 vs L3 类型
```
type=l2 (Symmetric IRB):
  同一 VNI → L2VNI + L3VNI
  完整支持: Type-2/3/5
  适用场景: 大多数生产环境

type=l3 (Pure Routing):
  仅 L3VNI
  仅支持: Type-5
  适用场景: 纯路由，MAC 表受限设备
```

---

## 核心组件

### 1. 事件监听层 (evpn_watcher.py)

监听 OVN SB `Port_Binding` 表变化：
```python
class SubnetRouterAttachedEvent:
    """Network/Router Association 创建时触发"""
    match_fn: Port_Binding.external_ids 包含 EVPN 配置
    触发: expose_subnet()

class SubnetRouterDetachedEvent:
    """Network/Router Association 删除时触发"""
    触发: withdraw_subnet()

class PortAssociationCreatedEvent:
    """Port Association 创建时触发"""
    match_fn: VM Port 的 external_ids 包含 EVPN 配置
    触发: expose_port_association()

class PortAssociationDeletedEvent:
    """Port Association 删除时触发"""
    触发: withdraw_port_association()
```

**触发条件**:
```python
# 必须同时存在这两个 key
port.external_ids['neutron_bgpvpn:vni']  # VNI 号
port.external_ids['neutron_bgpvpn:as']   # BGP AS
```

---

### 2. 数据平面层 (Linux Networking)

#### L2 类型数据路径（Symmetric IRB）
```
┌─────────────────────────────────────────────────────────────┐
│ VM (10.0.1.10)                                              │
│   │                                                          │
│   ▼                                                          │
│ OVN br-int ───────────────────► Geneve Tunnel              │
│   │ (tag=100, OVN 内部 VLAN)       │                        │
│   │                                 │                        │
│   ▼                              跨节点 VM                   │
│ evpn-10100 (Internal Port)                                  │
│   │ OVS port, type=internal                                 │
│   │ tag=100 (只接收 VLAN 100)                               │
│   │                                                          │
│   ▼                                                          │
│ Linux Network Stack                                         │
│   │                                                          │
│   ▼                                                          │
│ br-evpn (VLAN-aware bridge)                                │
│   │ VLAN filtering = 1                                      │
│   │ VLAN 1000 (unique, mapped from VNI)                    │
│   │                                                          │
│   ├──► vxlan-10100 ──────────► EVPN Network               │
│   │    (VNI 10100)              Type-2: MAC 学习            │
│   │                             Type-3: BUM 转发            │
│   │                                                          │
│   └──► br-evpn.100 (IRB) ────► vrf-10100                  │
│        10.0.1.1/24              Type-5: Prefix 路由         │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**关键设备**:

| 设备 | 类型 | 作用 | L2 类型 | L3 类型 |
|------|------|------|---------|---------|
| `evpn-<VNI>` | OVS Internal Port | 注入 VM 流量 | ✅ 创建 | ❌ 不创建 |
| `vxlan-<VNI>` | VXLAN | 封装/解封装 | ✅ | ✅ |
| `br-evpn` | Linux Bridge | VLAN 隔离 | ✅ | ✅ |
| `br-evpn.<VLAN>` | VLAN/IRB | L3 网关 | ✅ | ✅ |
| `vrf-<VNI>` | VRF | 路由隔离 | ✅ | ✅ |

#### L3 类型数据路径（Pure Routing）
```
┌─────────────────────────────────────────────────────────────┐
│ VM (10.0.1.10)                                              │
│   │                                                          │
│   ▼                                                          │
│ OVN Router ──► Gateway ──► br-evpn.100 (IRB)               │
│                              │                               │
│                              ▼                               │
│                         vrf-10100                           │
│                              │                               │
│                              ▼                               │
│                         vxlan-10100 ────► EVPN Network      │
│                         (仅 Type-5)                          │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**对比**:
- **无 Internal Port**: 不从 OVN 拉取流量
- **无 MAC 学习**: 不生成 Type-2/3
- **仅路由**: FRR 仅广播 IRB 的 Prefix (Type-5)

---

### 3. 控制平面层 (FRR BGP EVPN)

#### FRR 配置结构
```
┌─────────────────────────────────────────────────────────────┐
│ Base EVPN Config (Global)                                   │
│                                                              │
│ router bgp 65000                                            │
│  address-family l2vpn evpn                                  │
│   advertise-all-vni         ← 自动发现所有 VNI             │
│  exit-address-family                                        │
│                                                              │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ VRF Config (Per Tenant/VNI)                                │
│                                                              │
│ vrf vrf-10100                                               │
│  vni 10100                  ← 关联 VNI                      │
│ exit-vrf                                                    │
│                                                              │
│ router bgp 65000 vrf vrf-10100                              │
│  address-family ipv4 unicast                                │
│   redistribute connected    ← 从 IRB 学习路由               │
│   redistribute kernel                                       │
│  exit-address-family                                        │
│                                                              │
│  address-family ipv6 unicast                                │
│   redistribute connected                                    │
│   redistribute kernel                                       │
│  exit-address-family                                        │
│                                                              │
│  address-family l2vpn evpn                                  │
│   advertise ipv4 unicast    ← Type-5 广播                   │
│   advertise ipv6 unicast                                    │
│   rd 192.0.2.1:10100        ← Route Distinguisher          │
│   route-target import 65000:10100                           │
│   route-target export 65000:10100                           │
│  exit-address-family                                        │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

#### EVPN 路由生成机制

**Type-2 (MAC/IP) - 仅 L2 类型**:
```
触发条件: 内核 neighbor 表有条目
来源: 
  1. ARP 学习 (br-evpn 学习 MAC → 内核邻居表)
  2. 静态注入 (evpn_static_neighbors=True)

示例:
  ip neigh add 10.0.1.10 lladdr 52:54:00:aa:bb:cc dev br-evpn.100
  ↓
  FRR zebra 发现
  ↓
  BGP 生成: [2]:[0]:[48]:[52:54:00:aa:bb:cc]:[32]:[10.0.1.10]
```

**Type-3 (IMET) - 仅 L2 类型**:
```
触发条件: VXLAN 设备创建
自动生成: FRR 发现 vxlan-10100
示例:
  FRR 自动生成: [3]:[0]:[32]:[192.0.2.1]
  (告知其他 VTEP: 我有 VNI 10100)
```

**Type-5 (IP Prefix) - 所有类型**:
```
触发条件: VRF 路由表有路由
来源:
  1. IRB 配置 IP (connected route)
  2. 内核路由 (kernel route)
  3. 自定义静态路由

示例:
  ip addr add 10.0.1.1/24 dev br-evpn.100
  ↓
  VRF 路由表: 10.0.1.0/24 dev br-evpn.100 proto kernel
  ↓
  FRR zebra 发现
  ↓
  BGP 生成: [5]:[0]:[24]:[10.0.1.0]
```

---

## 配置流程

### Network Association 完整流程
```
┌────────────────────────────────────────────────────────────┐
│ 1. OpenStack API                                           │
│    POST /v2.0/bgpvpn/bgpvpns/{id}/network_associations     │
│    Body: {"network_association": {"network_id": "..."}}   │
└──────────────────────┬─────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────┐
│ 2. networking-bgpvpn Driver                                │
│    写入 OVN NB:                                            │
│    Logical_Switch.external_ids:                            │
│      neutron_bgpvpn:vni = "10100"                          │
│      neutron_bgpvpn:as = "65000"                           │
│      neutron_bgpvpn:type = "l2"                            │
│      neutron_bgpvpn:rt = "[\"65000:10100\"]"              │
│      neutron_bgpvpn:rd = "[\"192.0.2.1:10100\"]"          │
│      neutron_bgpvpn:local_pref = "100"                     │
└──────────────────────┬─────────────────────────────────────┘
                       │ OVN Northd 同步
                       ▼
┌────────────────────────────────────────────────────────────┐
│ 3. OVN SB Port_Binding                                     │
│    Patch Port (连接 LS 和 LR) 继承 external_ids           │
└──────────────────────┬─────────────────────────────────────┘
                       │ OVSDB Monitor
                       ▼
┌────────────────────────────────────────────────────────────┐
│ 4. ovn-bgp-agent Event                                     │
│    SubnetRouterAttachedEvent.match_fn() → True            │
│    SubnetRouterAttachedEvent._run()                        │
│      → driver.expose_subnet(row)                           │
└──────────────────────┬─────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────┐
│ 5. Driver 创建基础设施                                     │
│    _build_network_info():                                  │
│      解析 external_ids → network_info dict                │
│      type=l2 → l2vni=10100, l3vni=10100 (同一个)          │
│                                                            │
│    _ensure_network_infrastructure():                       │
│      ├─ 创建 VRF: vrf-10100 (table 1010100)               │
│      ├─ 创建 VXLAN: vxlan-10100 (VNI 10100)               │
│      ├─ 创建 IRB: br-evpn.100                              │
│      ├─ L2 类型: 创建 Internal Port: evpn-10100           │
│      └─ 配置 FRR VRF                                       │
└──────────────────────┬─────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────┐
│ 6. FRR 配置 (frr.py)                                       │
│    vrf_reconfigure(evpn_info, 'add-vrf')                  │
│      生成配置 → vtysh 应用                                 │
└────────────────────────────────────────────────────────────┘
```

### Port Association 流程
```
┌────────────────────────────────────────────────────────────┐
│ 1. OpenStack API                                           │
│    POST /v2.0/bgpvpn/bgpvpns/{id}/port_associations        │
│    Body: {                                                 │
│      "port_association": {                                 │
│        "port_id": "vm-port-uuid",                          │
│        "advertise_fixed_ips": true,                        │
│        "routes": [{"destination": "...", "nexthop": "..."}]│
│      }                                                      │
│    }                                                        │
└──────────────────────┬─────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────┐
│ 2. networking-bgpvpn Driver                                │
│    写入 OVN SB Port_Binding (VM Port):                    │
│      neutron_bgpvpn:vni = "10100"                          │
│      neutron_bgpvpn:advertise_fixed_ips = "true"           │
│      neutron_bgpvpn:routes = "[...]"                       │
└──────────────────────┬─────────────────────────────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────┐
│ 3. ovn-bgp-agent Event                                     │
│    PortAssociationCreatedEvent                             │
│      → driver.expose_port_association(row)                 │
│        ├─ 复用或创建网络基础设施                           │
│        ├─ 添加 FDB/邻居条目                                │
│        └─ 配置自定义路由 (VRF table)                       │
└────────────────────────────────────────────────────────────┘
```

---

## 数据结构

### network_info 字典
```python
{
    'id': str,              # Network UUID
    'vlan_id': int,         # OVN 内部 VLAN (1-4095)
    'l2vni': int or None,   # L2VNI (type=l2 时等于 vni)
    'l3vni': int or None,   # L3VNI (总是等于 vni)
    'vni': int,             # 单一 VNI (核心)
    'type': str,            # 'l2' | 'l3'
    'bgp_as': str,          # '65000'
    'route_targets': list,  # ['65000:10100']
    'route_distinguishers': list,  # ['192.0.2.1:10100']
    'import_targets': list,
    'export_targets': list,
    'local_pref': str,      # '100' (可选)
    'mtu': int,             # 1500
}
```

**关键点**:
- `type=l2`: `l2vni == l3vni == vni` (对称 IRB)
- `type=l3`: `l2vni = None`, `l3vni == vni`

### vrf_info 字典
```python
{
    'table_id': int,        # 1010100 (vni + 1000000)
    'vni': int,             # 10100
    'networks': list,       # ['net-uuid-1', 'net-uuid-2']
}
```

**VRF 复用**: 同一 VNI 的多个网络共享 VRF。

---

## 设备命名约定

| 设备类型 | 命名格式 | 示例 | 说明 |
|---------|---------|------|------|
| VXLAN | `vxlan-<VNI>` | `vxlan-10100` | 单 VXLAN，L2+L3 |
| VRF | `vrf-<VNI>` | `vrf-10100` | VRF 设备 |
| IRB/SVI | `<bridge>.<VLAN>` | `br-evpn.100` | VLAN 子接口 |
| Internal Port | `evpn-<VNI>` | `evpn-10100` | L2 类型专用 |

**命名限制**: Linux 接口名最长 15 字符。

---

## VLAN 映射机制

### 多租户隔离策略
```
租户A: OVN VLAN 100 → Bridge VLAN 1000 → VNI 10100
租户B: OVN VLAN 100 → Bridge VLAN 2000 → VNI 20100
         ↑                    ↑
    可以相同          全局唯一 (隔离)
```

**映射函数**:
```python
def _get_unique_vlan_for_vni(vni, ovn_vlan_id):
    if vni < 4095:
        return vni  # 直接使用 VNI 作为 VLAN
    else:
        return (vni % 3994) + 101  # Hash 到 101-4094
```

**VLAN Filtering 作用**:
- `br-evpn` 使用全局唯一 VLAN 隔离租户
- 不同租户的流量在二层完全隔离

---

## 性能优化

### 静态 FDB (evpn_static_fdb=True)

**目的**: 避免 L2 flooding

**实现**:
```bash
bridge fdb add 52:54:00:aa:bb:cc \
  dev veth-to-ovs \
  vlan 1000 \
  master static
```

**效果**:
- ✅ 减少广播流量
- ✅ 加速首包转发
- ⚠️ 占用 FDB 表空间

### 静态邻居 (evpn_static_neighbors=True)

**目的**: 避免 ARP/NDP 查询

**实现**:
```bash
ip neigh add 10.0.1.10 \
  lladdr 52:54:00:aa:bb:cc \
  dev br-evpn.100 \
  nud permanent
```

**效果**:
- ✅ 立即触发 Type-2 广播
- ✅ 减少 ARP 流量
- ⚠️ 占用邻居表

---

## 同步机制

### sync() - 全量同步

**周期**: `reconcile_interval` (默认 300 秒)

**流程**:
1. 清空内存跟踪结构
2. 查询所有 EVPN Port_Bindings
3. 按 network_id 分组
4. 逐网络调用 `_ensure_network_infrastructure()`
5. 逐端口添加 FDB/邻居
6. 清理孤儿资源

### frr_sync() - FRR 配置同步

**周期**: `frr_reconcile_interval` (默认 15 秒)

**目的**: FRR 重启后快速恢复

**流程**:
1. 确保 base EVPN 配置
2. 遍历 `self.evpn_vrfs`
3. 重新应用 VRF 配置

---

## 错误处理

### 幂等性保证

所有 `ensure_*` 方法必须幂等：
```python
def _ensure_vxlan(...):
    try:
        linux_net.ensure_vxlan(...)  # 如果存在则跳过
    except Exception as e:
        if "File exists" in str(e):
            LOG.debug("VXLAN already exists")
        else:
            raise
```

### 关键错误场景

| 错误 | 处理策略 |
|------|---------|
| VTEP IP 未配置 | 抛出异常，停止启动 |
| VRF 表冲突 | 使用 `vni + 1000000` 避免冲突 |
| FRR 连接失败 | 记录错误，下次 sync 重试 |
| 设备已存在 | 忽略，继续执行 |

---

## 扩展点

### 添加新的 EVPN 参数

1. 在 `constants.py` 定义 key:
```python
OVN_EVPN_NEW_PARAM_EXT_ID_KEY = 'neutron_bgpvpn:new_param'
```

2. 在 `_build_network_info()` 解析:
```python
new_param = ext_ids.get(constants.OVN_EVPN_NEW_PARAM_EXT_ID_KEY)
```

3. 在 FRR 模板使用:
```jinja2
{% if new_param %}
  custom-config {{ new_param }}
{% endif %}
```

### 添加新的 Association 类型

1. 在 `evpn_watcher.py` 创建事件类
2. 在 driver 添加 `expose_*` 和 `withdraw_*` 方法
3. 在 `_get_events()` 注册

---

## 测试策略

### 单元测试

- Mock `linux_net` 和 `frr` 调用
- 验证事件触发逻辑
- 验证数据结构转换

### 集成测试

- 完整 OVN + Neutron + networking-bgpvpn 环境
- 创建 BGPVPN + Association
- 验证设备创建和 FRR 配置
- 验证 EVPN 路由

### 性能测试

- 1000+ VM 场景
- 100+ VNI 场景
- 测量 sync 时间和内存使用

---

## 参考

- [RFC 7432](https://datatracker.ietf.org/doc/html/rfc7432) - BGP MPLS-Based Ethernet VPN
- [RFC 8365](https://datatracker.ietf.org/doc/html/rfc8365) - A Network Virtualization Overlay Solution Using Ethernet VPN (EVPN)
- [FRR EVPN Documentation](http://docs.frrouting.org/en/latest/evpn.html)