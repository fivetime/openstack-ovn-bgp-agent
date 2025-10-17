# EVPN Driver 架构设计

## 概述

OVN EVPN Driver 实现了完整的 BGP EVPN (RFC 7432) 数据平面和控制平面。

## 核心组件

### 1. 事件监听 (evpn_watcher.py)

监听 OVN SB Port_Binding 表变化：
```python
SubnetRouterAttachedEvent    # Router/Network Association
SubnetRouterDetachedEvent    # 移除关联
PortAssociationCreatedEvent  # Port Association
PortAssociationDeletedEvent  # 移除 Port Association
```

**触发条件**:
- Port_Binding.external_ids 包含 `neutron_bgpvpn:vni` 和 `neutron_bgpvpn:as`

### 2. 数据平面 (ovn_evpn_driver.py)

#### L2VNI 数据路径
```
VM (OVN) → br-int → veth-to-evpn → br-evpn
         → vxlan-10100 (L2VNI) → EVPN Network
```

创建组件：
- VXLAN 设备 (`vxlan-<VNI>`)
- Bridge VLAN 配置
- 静态 FDB 条目

#### L3VNI 数据路径
```
VM → br-int → veth-to-evpn → br-evpn → br-evpn.100 (IRB)
   → vrf-10100 → vxlan-l3-10100 (L3VNI) → EVPN Network
```

创建组件：
- VRF 设备 (`vrf-<L3VNI>`)
- IRB/SVI 设备 (`br-evpn.<VLAN>`)
- L3VNI VXLAN 设备
- 静态邻居条目

### 3. 控制平面 (FRR)

#### BGP EVPN 配置结构
```
router bgp 65000
  address-family l2vpn evpn
    advertise-all-vni
  exit-address-family

router bgp 65000 vrf vrf-10100
  address-family ipv4 unicast
    redistribute connected
    redistribute kernel
  exit-address-family
  
  address-family ipv6 unicast
    redistribute connected
    redistribute kernel
  exit-address-family
  
  address-family l2vpn evpn
    advertise ipv4 unicast
    advertise ipv6 unicast
    rd 192.0.2.1:10100
    route-target import 65000:10100
    route-target export 65000:10100
  exit-address-family

vrf vrf-10100
  vni 10100
exit-vrf
```

## 配置流程

### Network/Router Association
```
┌──────────────────────────────────────────────────────────┐
│ 1. networking-bgpvpn API                                 │
│    POST /v2.0/bgpvpn/bgpvpns/{id}/network_associations   │
└───────────────────────┬──────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────┐
│ 2. networking-bgpvpn Driver                              │
│    写入 OVN NB Logical_Switch.external_ids:              │
│    - neutron_bgpvpn:vni = 10100                          │
│    - neutron_bgpvpn:as = 65000                           │
│    - neutron_bgpvpn:type = l3                            │
│    - neutron_bgpvpn:rt = ["65000:10100"]                 │
└───────────────────────┬──────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────┐
│ 3. OVN Northd                                            │
│    同步到 SB Port_Binding.external_ids (patch ports)     │
└───────────────────────┬──────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────┐
│ 4. ovn-bgp-agent (本驱动)                                │
│    SubnetRouterAttachedEvent 触发                        │
│    → expose_subnet()                                     │
│      ├─ 创建 VXLAN 设备                                  │
│      ├─ 创建 VRF                                         │
│      ├─ 创建 IRB                                         │
│      └─ 配置 FRR                                         │
└──────────────────────────────────────────────────────────┘
```

### Port Association
```
┌──────────────────────────────────────────────────────────┐
│ 1. networking-bgpvpn API                                 │
│    POST /v2.0/bgpvpn/bgpvpns/{id}/port_associations      │
└───────────────────────┬──────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────┐
│ 2. networking-bgpvpn Driver                              │
│    写入 OVN SB Port_Binding.external_ids (VM port):      │
│    - neutron_bgpvpn:vni = 10100                          │
│    - neutron_bgpvpn:routes = [...]  # 自定义路由         │
└───────────────────────┬──────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────┐
│ 3. ovn-bgp-agent (本驱动)                                │
│    PortAssociationCreatedEvent 触发                      │
│    → expose_port_association()                           │
│      ├─ 复用/创建网络基础设施                            │
│      ├─ 添加端口 FDB/邻居条目                            │
│      └─ 配置自定义路由                                   │
└──────────────────────────────────────────────────────────┘
```

## 数据结构

### network_info
```python
{
    'id': 'network-uuid',
    'vlan_id': 100,
    'l2vni': 10100,         # L2 VNI
    'l3vni': 10100,         # L3 VNI
    'type': 'l3',           # l2/l3
    'bgp_as': '65000',
    'route_targets': ['65000:10100'],
    'route_distinguishers': ['192.0.2.1:10100'],
    'import_targets': [],
    'export_targets': [],
    'local_pref': 100,      # BGP Local Preference
    'mtu': 1500,
}
```

### vrf_info
```python
{
    'table_id': 1010100,
    'l3vni': 10100,
    'networks': ['network-uuid-1', 'network-uuid-2'],
}
```

## 设备命名约定

| 设备类型 | 命名格式 | 示例 |
|---------|---------|------|
| L2VNI VXLAN | `vxlan-<L2VNI>` | `vxlan-10100` |
| L3VNI VXLAN | `vxlan-l3-<L3VNI>` | `vxlan-l3-10100` |
| VRF | `vrf-<L3VNI>` | `vrf-10100` |
| IRB/SVI | `<bridge>.<vlan>` | `br-evpn.100` |
| L3VNI Bridge | `br-<L3VNI>` | `br-10100` |

## 性能优化

### 静态 FDB (evpn_static_fdb=True)

避免 L2 flooding：
```bash
bridge fdb add 52:54:00:12:34:56 \
  dev veth-to-ovs \
  vlan 100 \
  master static
```

FRR 自动生成 EVPN Type-2 MACIP 路由。

### 静态邻居 (evpn_static_neighbors=True)

避免 ARP/NDP 查询：
```bash
ip neigh add 10.0.0.10 \
  lladdr 52:54:00:12:34:56 \
  dev br-evpn.100 \
  nud permanent
```

触发 FRR 立即广播 EVPN Type-2 路由。

## 同步机制

### sync() 方法
周期性全量同步 (默认 300 秒)：

1. 清空跟踪数据结构
2. 查询所有 EVPN Port_Bindings
3. 按网络分组
4. 确保基础设施
5. 添加 FDB/邻居条目
6. 清理孤儿资源

### frr_sync() 方法
FRR 配置同步 (默认 15 秒)：

1. 确保 base EVPN 配置
2. 遍历所有 VRF
3. 重新配置 FRR VRF

## 错误处理

### 关键错误场景

1. **VTEP IP 未配置**
    - 检查 evpn_local_ip / evpn_nic
    - Fallback 到 loopback

2. **VRF 表冲突**
    - 使用 `l3vni + 1000000` 避免冲突

3. **FRR 连接失败**
    - vtysh privileged 调用失败
    - 检查 FRR 服务状态

4. **设备已存在**
    - 所有 ensure_* 方法幂等
    - 忽略 "File exists" 错误

## 扩展点

### 添加新的 Association 类型

1. 在 `evpn_watcher.py` 添加事件类
2. 在 driver 添加 `expose_*` 和 `withdraw_*` 方法
3. 在 `_get_events()` 注册事件

### 自定义 EVPN 参数

在 `constants.py` 添加新的 external_ids key：
```python
OVN_EVPN_CUSTOM_EXT_ID_KEY = 'neutron_bgpvpn:custom'
```

在 `_build_network_info()` 解析并使用。

## 测试策略

### 单元测试
- Mock OVN IDL
- Mock linux_net 调用
- 验证事件触发逻辑

### 集成测试
- 完整 OVN + Neutron 环境
- networking-bgpvpn 创建关联
- 验证设备和路由

### 性能测试
- 1000+ VM 场景
- 100+ VNI 场景
- 测量同步时间和内存使用