# API 参考

## networking-bgpvpn API

### 创建 BGPVPN

#### Endpoint
```
POST /v2.0/bgpvpn/bgpvpns
```

#### 请求体

**L2 类型（Symmetric IRB - 推荐）**:
```json
{
  "bgpvpn": {
    "name": "my-bgpvpn-l2",
    "type": "l2",
    "vni": 10100,
    "route_targets": ["65000:10100"],
    "import_targets": [],
    "export_targets": [],
    "route_distinguishers": ["192.0.2.1:10100"],
    "local_pref": 100
  }
}
```

**L3 类型（Pure Routing）**:
```json
{
  "bgpvpn": {
    "name": "my-bgpvpn-l3",
    "type": "l3",
    "vni": 20000,
    "route_targets": ["65000:20000"],
    "route_distinguishers": ["192.0.2.1:20000"]
  }
}
```

#### 参数说明

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | 否 | BGPVPN 名称 |
| `type` | string | 是 | **`l2`** 或 **`l3`** |
| `vni` | integer | 是 | VNI 号 (1-16777215) |
| `route_targets` | array | 是 | Route Target 列表（格式: `ASN:NN`）|
| `import_targets` | array | 否 | 仅 Import 的 RT（可选）|
| `export_targets` | array | 否 | 仅 Export 的 RT（可选）|
| `route_distinguishers` | array | 否 | Route Distinguisher 列表 |
| `local_pref` | integer | 否 | BGP Local Preference (0-4294967295) |

**类型差异**:

| 特性 | type=l2 | type=l3 |
|------|---------|---------|
| **VNI 用途** | L2VNI + L3VNI (同一个) | 仅 L3VNI |
| **EVPN 路由** | Type-2/3/5 | 仅 Type-5 |
| **Internal Port** | ✅ 创建 | ❌ 不创建 |
| **MAC 学习** | ✅ 支持 | ❌ 不支持 |
| **适用场景** | 大多数生产环境 | 纯路由设备 |

#### 响应
```json
{
  "bgpvpn": {
    "id": "4e8e5957-649f-477b-9e5b-f1f75b21c03c",
    "name": "my-bgpvpn-l2",
    "type": "l2",
    "vni": 10100,
    "route_targets": ["65000:10100"],
    "route_distinguishers": ["192.0.2.1:10100"],
    "local_pref": 100,
    "tenant_id": "45977fa2dbd7482098dd68d0d8970117"
  }
}
```

---

### Network Association

将 BGPVPN 关联到 Neutron Network，网络中所有端口自动获得 EVPN 能力。

#### Endpoint
```
POST /v2.0/bgpvpn/bgpvpns/{bgpvpn_id}/network_associations
```

#### 请求体
```json
{
  "network_association": {
    "network_id": "af374017-c9ae-4a1d-b799-ab73111476e2"
  }
}
```

#### 响应
```json
{
  "network_association": {
    "id": "291f73e3-def5-46a6-8e37-a30b96f6726a",
    "network_id": "af374017-c9ae-4a1d-b799-ab73111476e2"
  }
}
```

#### OVN 写入

**写入位置**: `OVN NB Logical_Switch.external_ids`

**L2 类型示例**:
```
Logical_Switch(uuid=af374017...).external_ids:
  neutron_bgpvpn:vni = "10100"
  neutron_bgpvpn:as = "65000"
  neutron_bgpvpn:type = "l2"          ← 关键
  neutron_bgpvpn:rt = "[\"65000:10100\"]"
  neutron_bgpvpn:rd = "[\"192.0.2.1:10100\"]"
  neutron_bgpvpn:local_pref = "100"
```

**L3 类型示例**:
```
Logical_Switch(uuid=...).external_ids:
  neutron_bgpvpn:vni = "20000"
  neutron_bgpvpn:as = "65000"
  neutron_bgpvpn:type = "l3"          ← 关键
  neutron_bgpvpn:rt = "[\"65000:20000\"]"
  neutron_bgpvpn:rd = "[\"192.0.2.1:20000\"]"
```

#### 触发流程
```
1. networking-bgpvpn 写入 Logical_Switch.external_ids
   ↓
2. OVN Northd 同步到 Port_Binding.external_ids (patch ports)
   ↓
3. ovn-bgp-agent 监听到 SubnetRouterAttachedEvent
   ↓
4. 调用 driver.expose_subnet(row)
   ↓
5. 创建 VXLAN/VRF/IRB 设备
   L2 类型: 额外创建 Internal Port
   L3 类型: 仅创建路由设备
```

---

### Router Association

将 BGPVPN 关联到 Neutron Router，路由器连接的所有子网自动获得 EVPN 能力。

#### Endpoint
```
POST /v2.0/bgpvpn/bgpvpns/{bgpvpn_id}/router_associations
```

#### 请求体
```json
{
  "router_association": {
    "router_id": "a3552f6e-c89e-4b2e-b905-5c5c4e6b5e5e"
  }
}
```

#### 响应
```json
{
  "router_association": {
    "id": "cfc2ebc8-65c6-471f-816e-e85d4e70cc8c",
    "router_id": "a3552f6e-c89e-4b2e-b905-5c5c4e6b5e5e"
  }
}
```

#### OVN 写入

**写入位置**: `OVN NB Logical_Router_Port.external_ids`
```
Logical_Router_Port(uuid=...).external_ids:
  neutron_bgpvpn:vni = "10100"
  neutron_bgpvpn:as = "65000"
  neutron_bgpvpn:type = "l2"
  neutron_bgpvpn:rt = "[\"65000:10100\"]"
  ...
```

**与 Network Association 差异**:
- Network Association: 写入 `Logical_Switch`
- Router Association: 写入 `Logical_Router_Port`
- 最终都会同步到 `Port_Binding.external_ids`

---

### Port Association

端口级别的 EVPN 配置，支持：
- 单个 VM/端口独立 EVPN 配置
- 自定义路由（静态路由注入）
- 覆盖网络级别配置

#### Endpoint
```
POST /v2.0/bgpvpn/bgpvpns/{bgpvpn_id}/port_associations
```

#### 请求体
```json
{
  "port_association": {
    "port_id": "8c3a2a47-5e5c-4e2c-b8d4-eb72f3b0e4c3",
    "advertise_fixed_ips": true,
    "routes": [
      {
        "type": "prefix",
        "destination": "10.20.0.0/24",
        "nexthop": "192.168.1.10"
      },
      {
        "type": "prefix",
        "destination": "10.30.0.0/16",
        "nexthop": "192.168.1.20"
      }
    ]
  }
}
```

#### 参数说明

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `port_id` | string (UUID) | 是 | Neutron Port UUID |
| `advertise_fixed_ips` | boolean | 否 | 是否广播端口 Fixed IPs（默认 true）|
| `routes` | array | 否 | 自定义路由列表 |
| `routes[].type` | string | 是 | 路由类型（目前仅支持 `prefix`）|
| `routes[].destination` | string | 是 | 目标网段 (CIDR) |
| `routes[].nexthop` | string | 是 | 下一跳 IP |

#### 响应
```json
{
  "port_association": {
    "id": "f2b7e1c8-9a3d-4e6f-8c2b-7d5e9f3a8b1c",
    "port_id": "8c3a2a47-5e5c-4e2c-b8d4-eb72f3b0e4c3",
    "advertise_fixed_ips": true,
    "routes": [
      {
        "type": "prefix",
        "destination": "10.20.0.0/24",
        "nexthop": "192.168.1.10"
      }
    ]
  }
}
```

#### OVN 写入

**写入位置**: `OVN SB Port_Binding.external_ids` (VM port)
```
Port_Binding(logical_port="vm-port-123").external_ids:
  neutron_bgpvpn:vni = "10100"
  neutron_bgpvpn:as = "65000"
  neutron_bgpvpn:type = "l2"
  neutron_bgpvpn:advertise_fixed_ips = "true"
  neutron_bgpvpn:routes = "[{\"type\":\"prefix\",\"destination\":\"10.20.0.0/24\",\"nexthop\":\"192.168.1.10\"}]"
```

#### 触发流程
```
1. networking-bgpvpn 写入 Port_Binding.external_ids (VM port)
   ↓
2. ovn-bgp-agent 监听到 PortAssociationCreatedEvent
   ↓
3. 调用 driver.expose_port_association(row)
   ↓
4. 复用或创建网络基础设施
   ↓
5. 添加 FDB/邻居条目（如果 type=l2）
   ↓
6. 解析 routes 字段，添加静态路由到 VRF 路由表
```

#### 自定义路由实现

Driver 将 `routes` 转换为 VRF 静态路由：
```python
# routes JSON:
[
  {"destination": "10.20.0.0/24", "nexthop": "192.168.1.10"}
]

# 转换为:
ip route add 10.20.0.0/24 via 192.168.1.10 vrf vrf-10100 table 1010100
```

**FRR 自动广播**:
- VRF 路由表中的路由会被 FRR `redistribute kernel` 学习
- 自动生成 EVPN Type-5 路由广播

---

## OVN External IDs 规范

### 键名常量
```python
# constants.py

# 核心 EVPN 参数
OVN_EVPN_VNI_EXT_ID_KEY = 'neutron_bgpvpn:vni'
OVN_EVPN_AS_EXT_ID_KEY = 'neutron_bgpvpn:as'
OVN_EVPN_TYPE_EXT_ID_KEY = 'neutron_bgpvpn:type'

# Route Target / Distinguisher
OVN_EVPN_RT_EXT_ID_KEY = 'neutron_bgpvpn:rt'
OVN_EVPN_RD_EXT_ID_KEY = 'neutron_bgpvpn:rd'
OVN_EVPN_IRT_EXT_ID_KEY = 'neutron_bgpvpn:it'  # Import-only RT
OVN_EVPN_ERT_EXT_ID_KEY = 'neutron_bgpvpn:et'  # Export-only RT

# BGP 配置
OVN_EVPN_LOCAL_PREF_EXT_ID_KEY = 'neutron_bgpvpn:local_pref'

# Port Association 特有
OVN_EVPN_ADVERTISE_FIXED_IPS_KEY = 'neutron_bgpvpn:advertise_fixed_ips'
OVN_EVPN_ROUTES_KEY = 'neutron_bgpvpn:routes'
```

### 数据格式

| 键 | 格式 | 示例 | 说明 |
|---|------|------|------|
| `vni` | string (数字) | `"10100"` | VNI 号 |
| `as` | string (数字) | `"65000"` | BGP AS |
| `type` | string | `"l2"` 或 `"l3"` | **核心区分** |
| `rt` | JSON array | `"[\"65000:10100\"]"` | Route Target 列表 |
| `rd` | JSON array | `"[\"192.0.2.1:10100\"]"` | RD 列表 |
| `it` | JSON array | `"[\"65000:20000\"]"` | Import RT |
| `et` | JSON array | `"[\"65000:30000\"]"` | Export RT |
| `local_pref` | string (数字) | `"100"` | BGP Local Pref |
| `advertise_fixed_ips` | string (bool) | `"true"` | 广播固定 IP |
| `routes` | JSON array | `"[{...}]"` | 自定义路由 |

**JSON 编码规则**:
- 所有数组必须 JSON 序列化
- 字符串中的双引号必须转义: `\"`
- 示例: `"[\"65000:10100\",\"65000:10200\"]"`

---

## Driver 内部 API

### OVNEVPNDriver 类
```python
class OVNEVPNDriver(driver_api.AgentDriverBase):
    """OVN EVPN Driver with Symmetric IRB support."""
```

#### 公共方法

##### start()
```python
def start(self):
    """Initialize driver and setup infrastructure."""
```
- 初始化 OVS/OVN 连接
- 创建 `br-evpn` 并启用 VLAN filtering
- 配置 FRR base EVPN
- 启动 OVN SB 事件监听

##### sync()
```python
@lockutils.synchronized('evpn')
def sync(self):
    """Full state synchronization."""
```
- 查询所有 EVPN Port_Bindings
- 按网络分组
- 确保基础设施
- 添加 FDB/邻居条目
- 清理孤儿资源

##### frr_sync()
```python
@lockutils.synchronized('evpn')
def frr_sync(self):
    """Sync FRR EVPN configuration."""
```
- 确保 base EVPN 配置
- 重新配置所有 VRF

#### 事件处理方法

##### expose_subnet()
```python
@lockutils.synchronized('evpn')
def expose_subnet(self, row):
    """Handle Network/Router Association.
    
    :param row: Port_Binding row (patch port)
    """
```
**触发**: `SubnetRouterAttachedEvent`

**流程**:
1. 解析 `external_ids` → `network_info`
2. 调用 `_ensure_network_infrastructure()`
3. 保存到 `self.evpn_networks`

##### withdraw_subnet()
```python
@lockutils.synchronized('evpn')
def withdraw_subnet(self, row):
    """Handle Network/Router Association removal.
    
    :param row: Port_Binding row
    """
```
**触发**: `SubnetRouterDetachedEvent`

**流程**:
1. 查找 `network_info`
2. 调用 `_cleanup_network_infrastructure()`
3. 从 `self.evpn_networks` 删除

##### expose_port_association()
```python
@lockutils.synchronized('evpn')
def expose_port_association(self, row):
    """Handle Port Association.
    
    :param row: Port_Binding row (VM port)
    """
```
**触发**: `PortAssociationCreatedEvent`

**流程**:
1. 解析 `external_ids`
2. 复用或创建网络基础设施
3. 添加 FDB/邻居（type=l2）
4. 解析 `routes`，添加静态路由

##### withdraw_port_association()
```python
@lockutils.synchronized('evpn')
def withdraw_port_association(self, row):
    """Handle Port Association removal.
    
    :param row: Port_Binding row
    """
```

##### expose_ip() / withdraw_ip()
```python
@lockutils.synchronized('evpn')
def expose_ip(self, row, cr_lrp=False):
    """Handle port binding to local chassis."""

@lockutils.synchronized('evpn')
def withdraw_ip(self, row, cr_lrp=False):
    """Handle port unbinding."""
```
**触发**: `PortBindingChassisCreatedEvent` / `PortBindingChassisDeletedEvent`

**作用**: 添加/删除 FDB 和邻居条目

#### 辅助方法

##### _build_network_info()
```python
def _build_network_info(self, network_id, sample_port):
    """Build network info from Port_Binding.
    
    :param network_id: Network UUID
    :param sample_port: Sample Port_Binding row
    :return: network_info dict or None
    """
```
**关键逻辑**:
```python
evpn_type = ext_ids.get('neutron_bgpvpn:type', 'l3')

if evpn_type == 'l2':
    # Symmetric IRB: 同一 VNI
    l2vni = vni
    l3vni = vni
else:
    # Pure L3
    l2vni = None
    l3vni = vni
```

##### _ensure_network_infrastructure()
```python
def _ensure_network_infrastructure(self, network_info):
    """Ensure EVPN infrastructure for a network.
    
    :param network_info: Network info dict
    :return: True if successful, False otherwise
    """
```
**流程**:
1. 创建 VRF
2. 创建 VXLAN (单个)
3. 创建 IRB
4. **L2 类型**: 创建 Internal Port
5. 配置 FRR

##### _cleanup_network_infrastructure()
```python
def _cleanup_network_infrastructure(self, network_info):
    """Remove network infrastructure."""
```
**流程**:
1. 删除 IRB
2. 删除 VXLAN
3. **L2 类型**: 删除 Internal Port
4. 检查 VRF（如无其他网络则删除）

---

## 数据结构

### network_info
```python
{
    'id': str,              # Network UUID
    'vlan_id': int,         # OVN 内部 VLAN (1-4095)
    'l2vni': int or None,   # L2VNI (type=l2 时 == vni)
    'l3vni': int or None,   # L3VNI (总是 == vni)
    'vni': int,             # 单一 VNI (核心字段)
    'type': str,            # 'l2' 或 'l3'
    'bgp_as': str,          # '65000'
    'route_targets': list,  # ['65000:10100']
    'route_distinguishers': list,
    'import_targets': list,
    'export_targets': list,
    'local_pref': str,      # '100' (可选)
    'mtu': int,             # 1500
}
```

**类型差异**:
```python
# type=l2 (Symmetric IRB)
{
    'vni': 10100,
    'l2vni': 10100,  # == vni
    'l3vni': 10100,  # == vni
    'type': 'l2'
}

# type=l3 (Pure Routing)
{
    'vni': 20000,
    'l2vni': None,   # 无 L2VNI
    'l3vni': 20000,  # == vni
    'type': 'l3'
}
```

### vrf_info
```python
{
    'table_id': int,        # 1010100 (vni + 1000000)
    'vni': int,             # 10100
    'networks': list,       # ['net-uuid-1', 'net-uuid-2']
}
```

**VRF 复用**: 多个网络可共享同一 VRF（相同 VNI）

---

## CLI 工具（计划）

### ovn-bgp-agent-status
```bash
# 查看 EVPN 状态
ovn-bgp-agent-status

# 输出示例
EVPN Driver Status
==================
Active Networks: 10 (L2: 7, L3: 3)
Active VRFs: 8
Tracked Ports: 150
VXLAN Devices: 10
FDB Entries: 350
Neighbor Entries: 200

Last Sync: 2025-01-15 10:30:45
Sync Duration: 23.5s
```

### ovn-evpn-show
```bash
# 显示 EVPN 网络
ovn-evpn-show networks

# 输出
NETWORK ID                            VNI    TYPE  VLAN  VRF
af374017-c9ae-4a1d-b799-ab73111476e2  10100  l2    100   vrf-10100
4e8e5957-649f-477b-9e5b-f1f75b21c03c  20000  l3    200   vrf-20000

# 显示 VRF
ovn-evpn-show vrfs

# 显示特定网络详情
ovn-evpn-show network af374017-c9ae-4a1d-b799-ab73111476e2
```

---

## 示例脚本

### Python 客户端
```python
from neutronclient.v2_0 import client

# 初始化客户端
neutron = client.Client(
    username='admin',
    password='secret',
    tenant_name='admin',
    auth_url='http://controller:5000/v3'
)

# 创建 L2 类型 BGPVPN (Symmetric IRB)
bgpvpn_l2 = neutron.create_bgpvpn({
    'bgpvpn': {
        'name': 'tenant-a-l2',
        'type': 'l2',
        'vni': 10100,
        'route_targets': ['65000:10100'],
        'route_distinguishers': ['192.0.2.1:10100']
    }
})

bgpvpn_id = bgpvpn_l2['bgpvpn']['id']
print(f"Created L2 BGPVPN: {bgpvpn_id}")

# 关联到网络
network_assoc = neutron.create_bgpvpn_network_association(
    bgpvpn_id,
    {'network_association': {'network_id': 'net-uuid'}}
)

print(f"Associated to network: {network_assoc['network_association']['id']}")

# 创建 Port Association（精细控制）
port_assoc = neutron.create_bgpvpn_port_association(
    bgpvpn_id,
    {
        'port_association': {
            'port_id': 'port-uuid',
            'advertise_fixed_ips': True,
            'routes': [
                {
                    'type': 'prefix',
                    'destination': '10.20.0.0/24',
                    'nexthop': '192.168.1.10'
                }
            ]
        }
    }
)

print(f"Port association created: {port_assoc['port_association']['id']}")
```

### Bash 批量创建
```bash
#!/bin/bash
# bulk-create-evpn.sh

BGP_AS="65000"
BASE_VNI=10000

for i in {100..110}; do
  VNI=$((BASE_VNI + i))
  
  echo "Creating BGPVPN for VLAN $i (VNI $VNI)..."
  
  # 创建 L2 类型 BGPVPN
  BGPVPN_ID=$(openstack bgpvpn create \
    --type l2 \
    --vni $VNI \
    --route-target ${BGP_AS}:${VNI} \
    --route-distinguisher 192.0.2.1:${VNI} \
    --name bgpvpn-vlan-$i \
    -f value -c id)
  
  # 创建网络
  NET_ID=$(openstack network create \
    --provider-network-type vlan \
    --provider-physical-network provider \
    --provider-segment $i \
    --name net-vlan-$i \
    -f value -c id)
  
  # 创建子网
  openstack subnet create \
    --network $NET_ID \
    --subnet-range 10.0.$i.0/24 \
    --name subnet-vlan-$i
  
  # 关联
  openstack bgpvpn network association create \
    $BGPVPN_ID \
    --network $NET_ID
  
  echo "✓ VLAN $i configured with VNI $VNI"
done

echo "All VLANs configured successfully"
```

### 验证脚本
```bash
#!/bin/bash
# verify-evpn.sh

BGPVPN_ID=$1

if [ -z "$BGPVPN_ID" ]; then
  echo "Usage: $0 <bgpvpn_id>"
  exit 1
fi

echo "Verifying BGPVPN: $BGPVPN_ID"
echo "================================"

# 获取 BGPVPN 详情
BGPVPN_INFO=$(openstack bgpvpn show $BGPVPN_ID -f json)
VNI=$(echo $BGPVPN_INFO | jq -r '.vni')
TYPE=$(echo $BGPVPN_INFO | jq -r '.type')

echo "VNI: $VNI"
echo "Type: $TYPE"
echo ""

# 检查 VXLAN 设备
echo "Checking VXLAN device..."
if ip link show vxlan-$VNI &> /dev/null; then
  echo "✓ vxlan-$VNI exists"
else
  echo "✗ vxlan-$VNI NOT found"
fi

# 检查 VRF
echo ""
echo "Checking VRF..."
if ip link show vrf-$VNI &> /dev/null; then
  echo "✓ vrf-$VNI exists"
  ip route show vrf vrf-$VNI
else
  echo "✗ vrf-$VNI NOT found"
fi

# 检查 FRR EVPN
echo ""
echo "Checking FRR EVPN routes..."
vtysh -c "show bgp l2vpn evpn vni $VNI"

echo ""
echo "Verification complete"
```

---

## REST API（未来扩展）

计划实现 RESTful API 用于监控和管理：
```
# 列出 EVPN 网络
GET /v1/evpn/networks
Response: [{"id": "...", "vni": 10100, "type": "l2", ...}]

# 网络详情
GET /v1/evpn/networks/{network_id}
Response: {"id": "...", "vni": 10100, "type": "l2", "vrf": "vrf-10100", ...}

# 列出 VRF
GET /v1/evpn/vrfs
Response: [{"name": "vrf-10100", "table_id": 1010100, "networks": [...]}]

# 触发同步
POST /v1/evpn/sync
Response: {"status": "started", "task_id": "..."}

# 获取指标
GET /v1/evpn/metrics
Response: {
  "networks_total": 10,
  "vrfs_total": 8,
  "ports_total": 150,
  "sync_duration_seconds": 23.5
}
```

---

## 错误码

| 错误码 | HTTP 状态 | 说明 |
|-------|---------|------|
| `EVPN-001` | 400 | 无效的 VNI (超出范围) |
| `EVPN-002` | 400 | 无效的 Route Target 格式 |
| `EVPN-003` | 404 | BGPVPN 不存在 |
| `EVPN-004` | 409 | Network 已关联其他 BGPVPN |
| `EVPN-005` | 500 | 基础设施创建失败 |
| `EVPN-006` | 500 | FRR 配置失败 |

---

## 参考

- [networking-bgpvpn API 文档](https://docs.openstack.org/networking-bgpvpn/latest/user/api.html)
- [RFC 7432 - BGP MPLS-Based Ethernet VPN](https://datatracker.ietf.org/doc/html/rfc7432)
- [RFC 8365 - EVPN Overlay](https://datatracker.ietf.org/doc/html/rfc8365)