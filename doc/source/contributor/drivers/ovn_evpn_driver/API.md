# API 参考

## networking-bgpvpn API

### 创建 BGPVPN
```bash
POST /v2.0/bgpvpn/bgpvpns
```

**请求体:**
```json
{
  "bgpvpn": {
    "name": "my-bgpvpn",
    "type": "l3",
    "vni": 10100,
    "route_targets": ["65000:10100"],
    "import_targets": [],
    "export_targets": [],
    "route_distinguishers": ["192.0.2.1:10100"],
    "local_pref": 100
  }
}
```

**参数说明:**

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| name | string | 否 | BGPVPN 名称 |
| type | string | 是 | `l2` 或 `l3` |
| vni | integer | 是 | VNI 号 (1-16777215) |
| route_targets | array | 是 | RT 列表 (格式: ASN:NN) |
| import_targets | array | 否 | Import RT |
| export_targets | array | 否 | Export RT |
| route_distinguishers | array | 否 | RD 列表 |
| local_pref | integer | 否 | BGP Local Preference |

**响应:**
```json
{
  "bgpvpn": {
    "id": "bgpvpn-uuid",
    "name": "my-bgpvpn",
    "type": "l3",
    "vni": 10100,
    "route_targets": ["65000:10100"],
    "route_distinguishers": ["192.0.2.1:10100"],
    "local_pref": 100
  }
}
```

### Network Association
```bash
POST /v2.0/bgpvpn/bgpvpns/{bgpvpn_id}/network_associations
```

**请求体:**
```json
{
  "network_association": {
    "network_id": "network-uuid"
  }
}
```

**OVN 写入:**
```
Logical_Switch.external_ids:
  neutron_bgpvpn:vni = "10100"
  neutron_bgpvpn:as = "65000"
  neutron_bgpvpn:type = "l3"
  neutron_bgpvpn:rt = "[\"65000:10100\"]"
  neutron_bgpvpn:rd = "[\"192.0.2.1:10100\"]"
  neutron_bgpvpn:local_pref = "100"
```

### Router Association
```bash
POST /v2.0/bgpvpn/bgpvpns/{bgpvpn_id}/router_associations
```

**请求体:**
```json
{
  "router_association": {
    "router_id": "router-uuid"
  }
}
```

**OVN 写入:**
```
Logical_Router_Port.external_ids:
  (同 Network Association)
```

### Port Association
```bash
POST /v2.0/bgpvpn/bgpvpns/{bgpvpn_id}/port_associations
```

**请求体:**
```json
{
  "port_association": {
    "port_id": "port-uuid",
    "advertise_fixed_ips": true,
    "routes": [
      {
        "destination": "10.0.0.0/24",
        "nexthop": "192.168.1.10"
      }
    ]
  }
}
```

**OVN 写入:**
```
Port_Binding.external_ids:
  neutron_bgpvpn:vni = "10100"
  neutron_bgpvpn:as = "65000"
  neutron_bgpvpn:advertise_fixed_ips = "true"
  neutron_bgpvpn:routes = "[{\"destination\":\"10.0.0.0/24\",\"nexthop\":\"192.168.1.10\"}]"
```

## OVN External IDs 规范

### 键名常量
```python
OVN_EVPN_VNI_EXT_ID_KEY = 'neutron_bgpvpn:vni'
OVN_EVPN_AS_EXT_ID_KEY = 'neutron_bgpvpn:as'
OVN_EVPN_TYPE_EXT_ID_KEY = 'neutron_bgpvpn:type'
OVN_EVPN_RT_EXT_ID_KEY = 'neutron_bgpvpn:rt'
OVN_EVPN_RD_EXT_ID_KEY = 'neutron_bgpvpn:rd'
OVN_EVPN_IRT_EXT_ID_KEY = 'neutron_bgpvpn:it'
OVN_EVPN_ERT_EXT_ID_KEY = 'neutron_bgpvpn:et'
OVN_EVPN_LOCAL_PREF_EXT_ID_KEY = 'neutron_bgpvpn:local_pref'
```

### 数据格式

| 键 | 格式 | 示例 |
|---|------|------|
| vni | string (数字) | `"10100"` |
| as | string (数字) | `"65000"` |
| type | string | `"l2"` 或 `"l3"` |
| rt | JSON array | `"[\"65000:10100\"]"` |
| rd | JSON array | `"[\"192.0.2.1:10100\"]"` |
| it | JSON array | `"[\"65000:10100\"]"` |
| et | JSON array | `"[\"65000:10100\"]"` |
| local_pref | string (数字) | `"100"` |

## Driver 内部 API

### OVNEVPNDriver 类

#### 事件处理方法
```python
def expose_subnet(self, row):
    """Network/Router Association 处理"""

def withdraw_subnet(self, row):
    """移除 Network/Router Association"""

def expose_port_association(self, row):
    """Port Association 处理"""

def withdraw_port_association(self, row):
    """移除 Port Association"""

def expose_ip(self, row, cr_lrp=False):
    """端口绑定到本地 chassis"""

def withdraw_ip(self, row, cr_lrp=False):
    """端口从本地 chassis 解绑"""
```

#### 同步方法
```python
def sync(self):
    """完整状态同步"""

def frr_sync(self):
    """FRR 配置同步"""
```

#### 辅助方法
```python
def get_interface(self, device):
    """检查接口是否存在"""

def set_device_mtu(self, device, mtu):
    """设置设备 MTU"""

def add_ips_to_dev(self, device, ips):
    """添加 IP 地址到设备"""

def _get_local_vtep_ip(self):
    """获取本地 VTEP IP"""

def _parse_route_targets(self, ext_ids):
    """解析 Route Targets"""
```

### 数据结构

#### network_info
```python
{
    'id': str,              # Network UUID
    'vlan_id': int,         # VLAN ID
    'l2vni': int,           # L2VNI (可选)
    'l3vni': int,           # L3VNI (可选)
    'type': str,            # 'l2' | 'l3'
    'bgp_as': str,          # BGP AS 号
    'route_targets': list,  # RT 列表
    'route_distinguishers': list,
    'import_targets': list,
    'export_targets': list,
    'local_pref': str,      # Local Preference
    'mtu': int,             # MTU
}
```

## CLI 工具

### ovn-bgp-agent-status
```bash
# 查看状态
ovn-bgp-agent-status

# 输出示例
EVPN Networks: 5
Active VRFs: 3
Tracked Ports: 120
VXLAN Devices: 8
```

### ovn-evpn-show
```bash
# 显示 EVPN 网络
ovn-evpn-show networks

# 显示 VRF
ovn-evpn-show vrfs

# 显示端口
ovn-evpn-show ports
```

## REST API (未来扩展)

计划实现 REST API 用于监控和管理:
```
GET    /evpn/networks        # 列出网络
GET    /evpn/networks/{id}   # 网络详情
GET    /evpn/vrfs            # 列出 VRF
POST   /evpn/sync            # 触发同步
GET    /evpn/metrics         # 获取指标
```

## 示例脚本

### Python 客户端
```python
from neutronclient.v2_0 import client

neutron = client.Client(
    username='admin',
    password='secret',
    tenant_name='admin',
    auth_url='http://controller:5000/v3'
)

# 创建 BGPVPN
bgpvpn = neutron.create_bgpvpn({
    'bgpvpn': {
        'name': 'my-bgpvpn',
        'type': 'l3',
        'vni': 10100,
        'route_targets': ['65000:10100']
    }
})

# 关联网络
neutron.create_bgpvpn_network_association(
    bgpvpn['bgpvpn']['id'],
    {'network_association': {'network_id': 'net-uuid'}}
)
```

### Bash 脚本
```bash
#!/bin/bash
# 批量创建 EVPN 网络

for i in {100..110}; do
  VNI=$((10000 + i))
  
  # 创建 BGPVPN
  BGPVPN_ID=$(openstack bgpvpn create \
    --type l3 \
    --vni $VNI \
    --route-target 65000:$VNI \
    -f value -c id \
    bgpvpn-vlan-$i)
  
  # 创建网络
  NET_ID=$(openstack network create \
    --provider-network-type vlan \
    --provider-physical-network provider \
    --provider-segment $i \
    -f value -c id \
    net-vlan-$i)
  
  # 关联
  openstack bgpvpn network association create \
    $BGPVPN_ID --network $NET_ID
done
```