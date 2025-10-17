# OVN EVPN Driver for ovn-bgp-agent

完整的 EVPN 驱动实现，为 OVN 提供 BGP EVPN (RFC 7432) 支持。

## 特性

- **L2VNI (EVPN Type-2)** - 基于 VXLAN 的 L2 扩展
- **L3VNI (EVPN Type-5)** - 对称 IRB 路由
- **三种关联模式**:
    - Network Association - 整个网络级别 EVPN
    - Router Association - 路由器级别 EVPN
    - Port Association - 端口级别精细控制
- **FRR 集成** - 完整的 BGP EVPN 信令
- **性能优化** - 静态 FDB 和 ARP/NDP 表

## 架构
```
┌─────────────────┐
│ networking-bgpvpn│  OpenStack Neutron API
└────────┬────────┘
         │ 写入 external_ids
         ▼
┌─────────────────┐
│   OVN NB DB     │  Logical_Switch.external_ids
└────────┬────────┘
         │ 同步
         ▼
┌─────────────────┐
│   OVN SB DB     │  Port_Binding.external_ids
└────────┬────────┘
         │ 监听事件
         ▼
┌─────────────────┐
│ ovn-bgp-agent   │  本驱动
│ (EVPN Driver)   │
└────────┬────────┘
         │
         ├─► 创建 VXLAN/VRF/IRB 设备
         └─► 配置 FRR BGP EVPN
```

## 快速开始

### 1. 安装
```bash
# 安装 ovn-bgp-agent (如果尚未安装)
pip install ovn-bgp-agent

# 安装 FRR
apt install frr frr-pythontools  # Debian/Ubuntu
yum install frr                   # RHEL/CentOS
```

### 2. 配置

编辑 `/etc/neutron/bgp_agent.ini`:
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf

# BGP 配置
bgp_AS = 65000
bgp_router_id = 192.0.2.1

# EVPN VTEP 配置
evpn_local_ip = 192.0.2.1
evpn_udp_dstport = 4789

# 桥接配置
evpn_bridge = br-evpn
evpn_bridge_veth = veth-to-ovs
evpn_ovs_veth = veth-to-evpn
ovs_bridge = br-ex

# L2VNI 自动计算 (可选)
l2vni_offset = 10000

# 优化选项
evpn_static_fdb = True
evpn_static_neighbors = True

[ovn]
ovn_sb_connection = tcp:192.0.2.10:6642
```

### 3. 启动
```bash
systemctl start ovn-bgp-agent
systemctl enable ovn-bgp-agent
```

### 4. 使用示例

#### Network Association
```bash
# 创建 BGPVPN
openstack bgpvpn create \
  --type l3 \
  --vni 10100 \
  --route-target 65000:10100 \
  my-bgpvpn

# 关联到网络
openstack bgpvpn network association create \
  my-bgpvpn \
  --network my-network
```

#### Router Association
```bash
# 关联到路由器
openstack bgpvpn router association create \
  my-bgpvpn \
  --router my-router
```

#### Port Association
```bash
# 端口级别关联（精细控制）
openstack bgpvpn port association create \
  my-bgpvpn \
  --port vm-port-uuid \
  --advertise-fixed-ips \
  --routes destination=10.0.0.0/24,nexthop=192.168.1.10
```

## 验证

### 检查 EVPN 基础设施
```bash
# 查看 EVPN 桥接
ip link show br-evpn

# 查看 VXLAN 设备
ip -d link show | grep vxlan

# 查看 VRF
ip vrf show

# 查看 IRB 设备
ip link show | grep "br-evpn\."
```

### 检查 FRR 配置
```bash
# 进入 FRR vtysh
vtysh

# 查看 BGP EVPN 配置
show bgp l2vpn evpn summary
show bgp l2vpn evpn vni
show evpn vni

# 查看 EVPN 路由
show bgp l2vpn evpn route type macip
show bgp l2vpn evpn route type prefix
```

### 检查路由和邻居
```bash
# 查看 VRF 路由表
ip route show vrf vrf-10100

# 查看静态 FDB
bridge fdb show dev veth-to-ovs | grep static

# 查看静态邻居
ip neigh show dev br-evpn.100
```

## 故障排查

### 日志位置
```bash
# ovn-bgp-agent 日志
journalctl -u ovn-bgp-agent -f

# FRR 日志
tail -f /var/log/frr/frr.log
```

### 常见问题

**1. VXLAN 设备未创建**
- 检查 `evpn_local_ip` 配置
- 验证 OVN external_ids 是否正确写入
- 查看 agent 日志

**2. FRR 无 EVPN 路由**
- 确认 FRR 启用了 bgpd
- 检查 BGP peering 状态
- 验证 `advertise-all-vni` 配置

**3. 无 L3 连通性**
- 检查 IRB 设备状态
- 验证 VRF 路由表
- 确认 gateway IP 已添加

## 性能调优

### 大规模部署建议
```ini
[DEFAULT]
# 禁用不必要的优化
evpn_static_fdb = False
evpn_static_neighbors = False

# 调整同步间隔
reconcile_interval = 600
frr_reconcile_interval = 30

# VRF 复用
delete_vrf_on_disconnect = False
```

### 监控指标

关键监控点：
- VXLAN 设备数量: `ip -o link | grep vxlan | wc -l`
- VRF 数量: `ip vrf | wc -l`
- EVPN 路由数量: `vtysh -c "show bgp l2vpn evpn summary"`
- FDB 条目数: `bridge fdb show | wc -l`

## 限制

- **不支持** IPv4/IPv6 双栈 EVPN (选择其一)
- **需要** FRR 7.5+ (EVPN 支持)
- **需要** Linux Kernel 4.18+ (VRF/VXLAN)
- **网络 MTU** 需考虑 VXLAN 封装开销 (建议 +50 字节)

## 相关项目

- [networking-bgpvpn](https://github.com/openstack/networking-bgpvpn) - Neutron BGPVPN API
- [ovn-bgp-agent](https://github.com/openstack/ovn-bgp-agent) - OVN BGP Agent
- [FRRouting](https://frrouting.org/) - FRR 路由协议栈

## 许可证

Apache License 2.0

## 贡献

欢迎提交 Issue 和 Pull Request！

## 支持

- 邮件列表: openstack-discuss@lists.openstack.org
- IRC: #openstack-neutron @ OFTC