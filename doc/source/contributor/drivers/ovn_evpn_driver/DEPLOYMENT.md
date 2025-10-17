# 部署指南

## 系统要求

### 硬件要求
- CPU: 2+ 核心
- 内存: 4GB+ (大规模: 8GB+)
- 网络: 10Gbps+ (生产环境)

### 软件要求
- OS: Ubuntu 20.04+ / RHEL 8+ / Debian 11+
- Kernel: 4.18+ (VRF/VXLAN 支持)
- Python: 3.8+
- FRR: 7.5+
- OVN: 20.06+

## 安装步骤

### 1. 安装依赖

**Ubuntu/Debian:**
```bash
apt update
apt install -y \
  python3-pip \
  frr \
  frr-pythontools \
  openvswitch-switch \
  bridge-utils \
  iproute2
```

**RHEL/CentOS:**
```bash
yum install -y \
  python3-pip \
  frr \
  openvswitch \
  bridge-utils \
  iproute
```

### 2. 安装 ovn-bgp-agent
```bash
pip3 install ovn-bgp-agent
```

### 3. 部署驱动文件
```bash
# 创建目录
mkdir -p /usr/local/lib/python3.8/site-packages/ovn_bgp_agent/drivers/openstack/drivers/
mkdir -p /usr/local/lib/python3.8/site-packages/ovn_bgp_agent/drivers/openstack/watchers/

# 复制文件
cp ovn_evpn_driver.py /usr/local/lib/python3.8/site-packages/ovn_bgp_agent/drivers/openstack/drivers/
cp evpn_watcher.py /usr/local/lib/python3.8/site-packages/ovn_bgp_agent/drivers/openstack/watchers/
cp frr.py /usr/local/lib/python3.8/site-packages/ovn_bgp_agent/drivers/openstack/utils/
```

### 4. 配置
```bash
mkdir -p /etc/neutron
cat > /etc/neutron/bgp_agent.ini << 'EOF'
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
bgp_AS = 65000
bgp_router_id = <NODE_IP>
evpn_local_ip = <NODE_IP>
evpn_bridge = br-evpn
ovs_bridge = br-ex
l2vni_offset = 10000

[ovn]
ovn_sb_connection = tcp:<OVN_SB_IP>:6642
EOF
```

### 5. 配置 FRR

编辑 `/etc/frr/daemons`:
```bash
bgpd=yes
zebra=yes
```

启动 FRR:
```bash
systemctl enable frr
systemctl start frr
```

配置 BGP peer:
```bash
vtysh << 'EOF'
conf t
router bgp 65000
 bgp router-id <NODE_IP>
 neighbor <SPINE_IP> remote-as 65000
 address-family l2vpn evpn
  neighbor <SPINE_IP> activate
  advertise-all-vni
 exit-address-family
end
write
EOF
```

### 6. 启动服务
```bash
systemctl daemon-reload
systemctl enable ovn-bgp-agent
systemctl start ovn-bgp-agent
```

## 网络拓扑

### Spine-Leaf 架构
```
           ┌─────────┐  ┌─────────┐
           │ Spine 1 │  │ Spine 2 │
           └────┬────┘  └────┬────┘
                │            │
       ┌────────┴────────────┴────────┐
       │                               │
  ┌────┴────┐                    ┌────┴────┐
  │ Leaf 1  │                    │ Leaf 2  │
  │(Compute)│                    │(Compute)│
  └────┬────┘                    └────┬────┘
       │                               │
  ┌────┴────┐                    ┌────┴────┐
  │  VMs    │                    │  VMs    │
  └─────────┘                    └─────────┘
```

**配置要点:**
- Spine: iBGP Route Reflector
- Leaf: EVPN PE 节点
- VXLAN over Underlay (IP fabric)

### IP 规划

| 组件 | CIDR | 用途 |
|------|------|------|
| Underlay | 192.0.2.0/24 | Spine-Leaf 互联 |
| Loopback | 10.255.255.0/24 | VTEP IP |
| Overlay | 10.0.0.0/8 | Tenant 网络 |

## 多节点部署

### Compute 节点配置

**compute-1.example.com:**
```ini
[DEFAULT]
bgp_router_id = 10.255.255.1
evpn_local_ip = 10.255.255.1

[ovn]
ovn_sb_connection = tcp:controller:6642
```

**compute-2.example.com:**
```ini
[DEFAULT]
bgp_router_id = 10.255.255.2
evpn_local_ip = 10.255.255.2

[ovn]
ovn_sb_connection = tcp:controller:6642
```

### Ansible Playbook
```yaml
- hosts: compute
  tasks:
    - name: Install packages
      apt:
        name: [frr, ovn-bgp-agent]
        state: present

    - name: Configure bgp_agent
      template:
        src: bgp_agent.ini.j2
        dest: /etc/neutron/bgp_agent.ini

    - name: Configure FRR
      template:
        src: frr.conf.j2
        dest: /etc/frr/frr.conf
      notify: restart frr

    - name: Start services
      systemd:
        name: "{{ item }}"
        state: started
        enabled: yes
      loop:
        - frr
        - ovn-bgp-agent
```

## 升级

### 滚动升级
```bash
# 每个节点执行
systemctl stop ovn-bgp-agent

# 更新代码
pip3 install --upgrade ovn-bgp-agent
cp <new_driver_files> /usr/local/lib/...

# 重启
systemctl start ovn-bgp-agent

# 验证
ovn-bgp-agent-status
```

### 回滚
```bash
systemctl stop ovn-bgp-agent
pip3 install ovn-bgp-agent==<old_version>
cp <old_driver_files> /usr/local/lib/...
systemctl start ovn-bgp-agent
```

## 监控

### Prometheus Exporter
```bash
# 安装 exporter
pip3 install ovn-bgp-agent-exporter

# 配置
cat > /etc/ovn-bgp-agent-exporter.yaml << EOF
listen_address: 0.0.0.0:9101
agent_socket: /var/run/ovn-bgp-agent.sock
EOF

# 启动
systemctl start ovn-bgp-agent-exporter
```

### 关键指标
```prometheus
# EVPN 网络数量
ovn_bgp_evpn_networks_total

# VRF 数量
ovn_bgp_evpn_vrfs_total

# VXLAN 设备数量
ovn_bgp_evpn_vxlan_devices_total

# FDB 条目数
ovn_bgp_evpn_fdb_entries_total

# 同步耗时
ovn_bgp_evpn_sync_duration_seconds
```

### 告警规则
```yaml
groups:
- name: ovn-bgp-evpn
  rules:
  - alert: EVPNSyncFailed
    expr: ovn_bgp_evpn_sync_errors_total > 5
    for: 5m
    annotations:
      summary: "EVPN sync failures"

  - alert: EVPNHighSyncTime
    expr: ovn_bgp_evpn_sync_duration_seconds > 60
    for: 10m
    annotations:
      summary: "EVPN sync taking too long"
```

## 故障排查

### 日志分析
```bash
# 实时查看
journalctl -u ovn-bgp-agent -f

# 错误日志
journalctl -u ovn-bgp-agent -p err

# 特定时间范围
journalctl -u ovn-bgp-agent --since "1 hour ago"
```

### 诊断命令
```bash
# 检查服务状态
systemctl status ovn-bgp-agent

# 检查 EVPN 设备
ip link | grep -E "(vxlan|vrf|br-evpn)"

# 检查 FRR EVPN
vtysh -c "show bgp l2vpn evpn summary"
vtysh -c "show evpn vni"

# 检查 OVN 连接
ovn-sbctl --db=tcp:<OVN_SB>:6642 show
```

### 常见问题

**问题 1: Agent 无法启动**
```bash
# 检查配置
ovn-bgp-agent --config-file /etc/neutron/bgp_agent.ini --dry-run

# 检查权限
sudo -u neutron ovn-bgp-agent --config-file /etc/neutron/bgp_agent.ini
```

**问题 2: FRR 无路由**
```bash
# 检查 BGP 邻居
vtysh -c "show bgp summary"

# 检查 EVPN 配置
vtysh -c "show running-config"

# 手动触发同步
systemctl restart ovn-bgp-agent
```

**问题 3: 无 L3 连通性**
```bash
# 检查 VRF 路由
ip route show vrf vrf-10100

# 检查 IRB 设备
ip link show br-evpn.100

# 检查邻居表
ip neigh show dev br-evpn.100
```

## 安全加固

### OVN SSL 连接
```ini
[ovn]
ovn_sb_connection = ssl:ovn-sb:6642
ovn_sb_private_key = /etc/pki/tls/private/client.key
ovn_sb_certificate = /etc/pki/tls/certs/client.crt
ovn_sb_ca_cert = /etc/pki/tls/certs/ca.crt
```

### FRR 认证
```
router bgp 65000
 neighbor spine password <secret>
```

### 防火墙规则
```bash
# VXLAN
iptables -A INPUT -p udp --dport 4789 -j ACCEPT

# BGP
iptables -A INPUT -p tcp --dport 179 -j ACCEPT

# OVN SB
iptables -A INPUT -p tcp --dport 6642 -s <controller_ip> -j ACCEPT
```

## 性能调优

### 大规模优化
```ini
[DEFAULT]
# 减少同步频率
reconcile_interval = 900

# 禁用静态表
evpn_static_fdb = False
evpn_static_neighbors = False

# VRF 复用
delete_vrf_on_disconnect = False
```

### 内核参数
```bash
# 增加邻居表容量
sysctl -w net.ipv4.neigh.default.gc_thresh1=2048
sysctl -w net.ipv4.neigh.default.gc_thresh2=4096
sysctl -w net.ipv4.neigh.default.gc_thresh3=8192

# 增加 netlink 缓冲
sysctl -w net.core.rmem_max=134217728
sysctl -w net.core.wmem_max=134217728
```

## 备份和恢复

### 备份配置
```bash
tar czf ovn-bgp-backup-$(date +%F).tar.gz \
  /etc/neutron/bgp_agent.ini \
  /etc/frr/frr.conf \
  /etc/frr/daemons
```

### 恢复
```bash
systemctl stop ovn-bgp-agent frr
tar xzf ovn-bgp-backup-*.tar.gz -C /
systemctl start frr ovn-bgp-agent
```