# 部署指南

## 系统要求

### 硬件要求

| 组件 | 最低要求 | 推荐配置 | 大规模部署 |
|------|---------|---------|-----------|
| **CPU** | 2 核心 | 4 核心 | 8+ 核心 |
| **内存** | 4GB | 8GB | 16GB+ |
| **网络** | 1Gbps | 10Gbps | 25Gbps+ |
| **存储** | 20GB | 50GB | 100GB+ |

**规模参考**:
- 小规模: < 100 VM, 2-4 核心, 4-8GB 内存
- 中等规模: 100-1000 VM, 4-8 核心, 8-16GB 内存
- 大规模: > 1000 VM, 8+ 核心, 16GB+ 内存

### 软件要求

| 软件 | 最低版本 | 推荐版本 | 说明 |
|------|---------|---------|------|
| **Linux Kernel** | 3.8+ | 4.18+ | VLAN filtering, VRF |
| **Python** | 3.6+ | 3.8+ | ovn-bgp-agent |
| **FRR** | 7.5+ | 8.0+ | EVPN 支持 |
| **OVN** | 20.06+ | 21.06+ | OVN SB API |
| **OVS** | 2.13+ | 2.15+ | - |
| **networking-bgpvpn** | Victoria+ | Yoga+ | Neutron API |

**操作系统**:
- Ubuntu 20.04 LTS / 22.04 LTS
- RHEL 8.x / CentOS Stream 8
- Debian 11+

### 网络要求

#### Underlay 网络
- **BGP 邻居**: 已配置并建立 BGP peering
- **IP 可达性**: 所有 VTEP 之间可达（Ping 通）
- **MTU**: 推荐 9000 (Jumbo Frame)
- **带宽**: 根据规模配置（10G+ 推荐）

#### 防火墙规则
```bash
# BGP (TCP 179)
iptables -A INPUT -p tcp --dport 179 -j ACCEPT

# VXLAN (UDP 4789)
iptables -A INPUT -p udp --dport 4789 -j ACCEPT

# OVN SB (TCP 6642)
iptables -A INPUT -p tcp --dport 6642 -s <controller_ip> -j ACCEPT
```

---

## 安装步骤

### 1. 准备系统

#### Ubuntu/Debian
```bash
# 更新系统
apt update && apt upgrade -y

# 安装依赖
apt install -y \
  python3-pip \
  python3-dev \
  frr \
  frr-pythontools \
  openvswitch-switch \
  bridge-utils \
  iproute2 \
  net-tools \
  vlan \
  tcpdump

# 启用 IP 转发
cat >> /etc/sysctl.conf << EOF
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF
sysctl -p
```

#### RHEL/CentOS
```bash
# 安装 EPEL
yum install -y epel-release

# 安装依赖
yum install -y \
  python3-pip \
  python3-devel \
  frr \
  openvswitch \
  bridge-utils \
  iproute \
  net-tools \
  tcpdump

# 启用 IP 转发
echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf
echo "net.ipv6.conf.all.forwarding = 1" >> /etc/sysctl.conf
sysctl -p
```

### 2. 验证前置条件

运行测试脚本（假设已从仓库获取）:
```bash
bash test_evpn_setup.sh
```

**期望输出**:
```
==========================================
EVPN Driver Setup Validation
==========================================

1. Checking kernel version (need 3.8+ for VLAN filtering)...
✓ Kernel 5.4.0 supports VLAN filtering

2. Checking required tools...
✓ ip found
✓ bridge found
✓ ovs-vsctl found
✓ vtysh found

3. Checking FRR...
✓ FRR is running
✓ BGP configured (AS 65000)

...

All critical checks passed!
System is ready for EVPN driver.
```

### 3. 安装 ovn-bgp-agent

#### 从 PyPI 安装（推荐）
```bash
pip3 install ovn-bgp-agent
```

#### 从源码安装（开发）
```bash
git clone https://opendev.org/openstack/ovn-bgp-agent
cd ovn-bgp-agent
pip3 install -e .
```

### 4. 部署 EVPN Driver
```bash
# 克隆驱动代码
git clone <evpn-driver-repo>
cd ovn-evpn-driver

# 找到 Python site-packages 路径
SITE_PACKAGES=$(python3 -c "import site; print(site.getsitepackages()[0])")

# 创建目录
mkdir -p $SITE_PACKAGES/ovn_bgp_agent/drivers/openstack/drivers/
mkdir -p $SITE_PACKAGES/ovn_bgp_agent/drivers/openstack/watchers/
mkdir -p $SITE_PACKAGES/ovn_bgp_agent/drivers/openstack/utils/

# 复制文件
cp ovn_evpn_driver.py $SITE_PACKAGES/ovn_bgp_agent/drivers/openstack/drivers/
cp evpn_watcher.py $SITE_PACKAGES/ovn_bgp_agent/drivers/openstack/watchers/

# 更新 frr.py（如果有增强）
cp frr.py $SITE_PACKAGES/ovn_bgp_agent/drivers/openstack/utils/

# 更新 constants.py（如果有新常量）
cp constants.py $SITE_PACKAGES/ovn_bgp_agent/

echo "Driver deployed successfully"
```

**验证部署**:
```bash
python3 -c "from ovn_bgp_agent.drivers.openstack.drivers import ovn_evpn_driver; print('Driver loaded')"
```

### 5. 配置

#### 创建配置文件
```bash
mkdir -p /etc/neutron
cat > /etc/neutron/bgp_agent.ini << EOF
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
bgp_AS = 65000
bgp_router_id = $(hostname -I | awk '{print $1}')
evpn_local_ip = $(hostname -I | awk '{print $1}')
evpn_bridge = br-evpn
ovs_bridge = br-int

evpn_static_fdb = True
evpn_static_neighbors = True
delete_vrf_on_disconnect = False

reconcile_interval = 300
frr_reconcile_interval = 15

debug = False
log_file = /var/log/neutron/ovn-bgp-agent.log

[agent]
root_helper = sudo neutron-rootwrap /etc/neutron/rootwrap.conf

[ovn]
ovn_sb_connection = tcp:<OVN_SB_IP>:6642
EOF
```

**自定义配置**:
```bash
# 编辑配置
vi /etc/neutron/bgp_agent.ini

# 修改以下参数:
# - bgp_AS: 你的 BGP AS 号
# - evpn_local_ip: 本节点 VTEP IP
# - ovn_sb_connection: OVN SB 连接地址
```

### 6. 配置 FRR

#### 启用守护进程
```bash
cat > /etc/frr/daemons << EOF
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
EOF
```

#### 配置 BGP EVPN
```bash
# 获取本地 IP
LOCAL_IP=$(hostname -I | awk '{print $1}')
SPINE_IP="<YOUR_SPINE_IP>"
BGP_AS="65000"

# 生成配置
cat > /etc/frr/frr.conf << EOF
frr version 8.0
frr defaults traditional
hostname $(hostname)
log syslog informational
service integrated-vtysh-config
!
router bgp $BGP_AS
 bgp router-id $LOCAL_IP
 bgp log-neighbor-changes
 no bgp default ipv4-unicast
 !
 neighbor spine peer-group
 neighbor spine remote-as $BGP_AS
 neighbor $SPINE_IP peer-group spine
 !
 address-family l2vpn evpn
  neighbor spine activate
  advertise-all-vni
 exit-address-family
!
line vty
!
end
EOF
```

#### 启动 FRR
```bash
systemctl enable frr
systemctl start frr

# 验证
vtysh -c "show bgp summary"
```

**期望输出**:
```
BGP router identifier <LOCAL_IP>, local AS number 65000
...
Neighbor        V         AS MsgRcvd MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd
<SPINE_IP>      4      65000       5       5        0    0    0 00:00:15            0
```

### 7. 创建 systemd 服务
```bash
cat > /etc/systemd/system/ovn-bgp-agent.service << EOF
[Unit]
Description=OVN BGP Agent with EVPN Driver
After=network.target openvswitch-switch.service frr.service
Wants=openvswitch-switch.service frr.service

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/ovn-bgp-agent --config-file /etc/neutron/bgp_agent.ini
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 重载 systemd
systemctl daemon-reload
```

### 8. 启动服务
```bash
# 启动 agent
systemctl enable ovn-bgp-agent
systemctl start ovn-bgp-agent

# 检查状态
systemctl status ovn-bgp-agent
```

**验证启动**:
```bash
# 查看日志
journalctl -u ovn-bgp-agent -f

# 期望看到
# Starting OVN EVPN Driver (Symmetric IRB)
# VTEP IP: <LOCAL_IP>
# EVPN prerequisites ready
```

### 9. 验证部署

#### 检查 EVPN 基础设施
```bash
# br-evpn 应存在且启用 VLAN filtering
ip link show br-evpn
ip -d link show br-evpn | grep vlan_filtering
# 输出: vlan_filtering 1

# veth 对应存在
ip link show veth-to-ovs
ip link show veth-to-evpn

# veth 应连接到 br-evpn
bridge link show | grep veth-to-ovs
```

#### 检查 FRR EVPN 配置
```bash
vtysh << EOF
show running-config
show bgp l2vpn evpn summary
EOF
```

---

## 网络拓扑

### Spine-Leaf 架构
```
                  ┌─────────────┐  ┌─────────────┐
                  │  Spine 1    │  │  Spine 2    │
                  │  (RR)       │  │  (RR)       │
                  │ BGP AS 65000│  │ BGP AS 65000│
                  └──────┬──────┘  └──────┬──────┘
                         │                 │
              ┌──────────┴────────┬────────┴──────────┐
              │                   │                   │
         ┌────┴────┐         ┌────┴────┐        ┌────┴────┐
         │ Leaf 1  │         │ Leaf 2  │        │ Leaf 3  │
         │ (Compute│         │ (Compute│        │ (Compute│
         │  Node)  │         │  Node)  │        │  Node)  │
         └────┬────┘         └────┬────┘        └────┬────┘
              │                   │                   │
         ┌────┴────┐         ┌────┴────┐        ┌────┴────┐
         │  VMs    │         │  VMs    │        │  VMs    │
         │裸金属   │         │裸金属   │        │裸金属   │
         └─────────┘         └─────────┘        └─────────┘
```

**角色说明**:
- **Spine**: BGP Route Reflector，汇聚 EVPN 路由
- **Leaf (Compute Node)**: EVPN PE，运行 ovn-bgp-agent
- **VM/裸金属**: EVPN CE，加入 VNI

### IP 规划示例

| 用途 | CIDR | 示例 |
|------|------|------|
| **Underlay (互联)** | 192.0.2.0/24 | Spine-Leaf 链路 |
| **Loopback (VTEP)** | 10.255.255.0/24 | VXLAN 端点 |
| **Overlay (租户)** | 10.0.0.0/8 | VM 网络 |
| **管理网络** | 172.16.0.0/16 | OVN/OpenStack API |

**示例配置**:
```
Spine-1:  192.0.2.254 (Underlay), 10.255.255.254 (Loopback)
Compute-1: 192.0.2.1 (Underlay), 10.255.255.1 (VTEP)
Compute-2: 192.0.2.2 (Underlay), 10.255.255.2 (VTEP)
Compute-3: 192.0.2.3 (Underlay), 10.255.255.3 (VTEP)
```

---

## 多节点部署

### 使用 Ansible

#### 1. Inventory 配置

`inventory/hosts`:
```ini
[compute]
compute-1 ansible_host=192.0.2.1 vtep_ip=10.255.255.1
compute-2 ansible_host=192.0.2.2 vtep_ip=10.255.255.2
compute-3 ansible_host=192.0.2.3 vtep_ip=10.255.255.3

[compute:vars]
bgp_as=65000
spine_ip=192.0.2.254
ovn_sb_ip=172.16.0.10
```

#### 2. Playbook

`deploy-evpn.yml`:
```yaml
---
- name: Deploy OVN EVPN Driver
  hosts: compute
  become: yes
  
  vars:
    driver_repo: "https://github.com/example/ovn-evpn-driver"
    site_packages: "/usr/local/lib/python3.8/site-packages"
  
  tasks:
    - name: Install system packages
      apt:
        name:
          - python3-pip
          - frr
          - openvswitch-switch
          - bridge-utils
          - iproute2
        state: present
        update_cache: yes
      when: ansible_os_family == "Debian"
    
    - name: Install ovn-bgp-agent
      pip:
        name: ovn-bgp-agent
        state: present
    
    - name: Clone driver repository
      git:
        repo: "{{ driver_repo }}"
        dest: /tmp/ovn-evpn-driver
        version: main
    
    - name: Deploy driver files
      copy:
        src: "/tmp/ovn-evpn-driver/{{ item.src }}"
        dest: "{{ site_packages }}/{{ item.dest }}"
        remote_src: yes
      loop:
        - { src: "ovn_evpn_driver.py", dest: "ovn_bgp_agent/drivers/openstack/drivers/" }
        - { src: "evpn_watcher.py", dest: "ovn_bgp_agent/drivers/openstack/watchers/" }
        - { src: "frr.py", dest: "ovn_bgp_agent/drivers/openstack/utils/" }
        - { src: "evpn/vlan_manager.py", dest: "ovn_bgp_agent/drivers/openstack/evpn/" }
        - { src: "evpn/fdb_manager.py", dest: "ovn_bgp_agent/drivers/openstack/evpn/" }
        - { src: "evpn/net_manager.py", dest: "ovn_bgp_agent/drivers/openstack/evpn/" }
        - { src: "evpn/ovn_helper.py", dest: "ovn_bgp_agent/drivers/openstack/evpn/" }
    
    - name: Configure bgp_agent.ini
      template:
        src: templates/bgp_agent.ini.j2
        dest: /etc/neutron/bgp_agent.ini
      notify: restart ovn-bgp-agent
    
    - name: Configure FRR daemons
      copy:
        dest: /etc/frr/daemons
        content: |
          bgpd=yes
          zebra=yes
      notify: restart frr
    
    - name: Configure FRR
      template:
        src: templates/frr.conf.j2
        dest: /etc/frr/frr.conf
      notify: restart frr
    
    - name: Create systemd service
      copy:
        dest: /etc/systemd/system/ovn-bgp-agent.service
        content: |
          [Unit]
          Description=OVN BGP Agent with EVPN Driver
          After=network.target frr.service
          
          [Service]
          Type=simple
          User=root
          ExecStart=/usr/local/bin/ovn-bgp-agent --config-file /etc/neutron/bgp_agent.ini
          Restart=on-failure
          
          [Install]
          WantedBy=multi-user.target
      notify: reload systemd
    
    - name: Enable and start services
      systemd:
        name: "{{ item }}"
        enabled: yes
        state: started
      loop:
        - frr
        - ovn-bgp-agent
  
  handlers:
    - name: reload systemd
      systemd:
        daemon_reload: yes
    
    - name: restart frr
      systemd:
        name: frr
        state: restarted
    
    - name: restart ovn-bgp-agent
      systemd:
        name: ovn-bgp-agent
        state: restarted
```

#### 3. 配置模板

`templates/bgp_agent.ini.j2`:
```ini
[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
bgp_AS = {{ bgp_as }}
bgp_router_id = {{ vtep_ip }}
evpn_local_ip = {{ vtep_ip }}
evpn_bridge = br-evpn
ovs_bridge = br-int

evpn_static_fdb = True
evpn_static_neighbors = True
delete_vrf_on_disconnect = False

reconcile_interval = 300
frr_reconcile_interval = 15

[agent]
root_helper = sudo

[ovn]
ovn_sb_connection = tcp:{{ ovn_sb_ip }}:6642
```

`templates/frr.conf.j2`:
```
frr version 8.0
frr defaults traditional
hostname {{ inventory_hostname }}
log syslog informational
service integrated-vtysh-config
!
router bgp {{ bgp_as }}
 bgp router-id {{ vtep_ip }}
 bgp log-neighbor-changes
 no bgp default ipv4-unicast
 !
 neighbor spine peer-group
 neighbor spine remote-as {{ bgp_as }}
 neighbor {{ spine_ip }} peer-group spine
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

#### 4. 执行部署
```bash
ansible-playbook -i inventory/hosts deploy-evpn.yml
```

---

## 升级

### 滚动升级流程
```bash
#!/bin/bash
# rolling-upgrade.sh

NODES="compute-1 compute-2 compute-3"

for node in $NODES; do
  echo "Upgrading $node..."
  
  ssh $node << 'EOF'
    # 停止服务
    systemctl stop ovn-bgp-agent
    
    # 备份配置
    cp /etc/neutron/bgp_agent.ini /etc/neutron/bgp_agent.ini.backup
    
    # 更新代码
    pip3 install --upgrade ovn-bgp-agent
    cd /tmp && git pull origin main
    
    # 部署新驱动
    SITE_PACKAGES=$(python3 -c "import site; print(site.getsitepackages()[0])")
    cp ovn_evpn_driver.py $SITE_PACKAGES/ovn_bgp_agent/drivers/openstack/drivers/
    
    # 重启服务
    systemctl start ovn-bgp-agent
    
    # 验证
    sleep 10
    systemctl status ovn-bgp-agent
EOF
  
  echo "$node upgraded. Waiting 30s before next node..."
  sleep 30
done

echo "All nodes upgraded successfully"
```

### 回滚
```bash
#!/bin/bash
# rollback.sh

NODE=$1

ssh $NODE << 'EOF'
  systemctl stop ovn-bgp-agent
  
  # 恢复配置
  cp /etc/neutron/bgp_agent.ini.backup /etc/neutron/bgp_agent.ini
  
  # 回滚版本
  pip3 install ovn-bgp-agent==<old_version>
  
  # 恢复驱动代码（从备份）
  cp /root/driver-backup/* $SITE_PACKAGES/ovn_bgp_agent/...
  
  systemctl start ovn-bgp-agent
EOF
```

---

## 监控

### Prometheus Exporter（计划功能）
```bash
# 安装 exporter
pip3 install ovn-bgp-agent-exporter

# 配置
cat > /etc/ovn-bgp-agent-exporter.yaml << EOF
listen_address: 0.0.0.0:9101
agent_socket: /var/run/ovn-bgp-agent.sock
scrape_interval: 30
EOF

# 启动
systemctl enable ovn-bgp-agent-exporter
systemctl start ovn-bgp-agent-exporter
```

### 关键指标
```bash
# 手动采集指标
curl http://localhost:9101/metrics
```

**关键指标**:
```prometheus
# EVPN 网络数量
ovn_bgp_evpn_networks_total{type="l2"} 10
ovn_bgp_evpn_networks_total{type="l3"} 5

# VRF 数量
ovn_bgp_evpn_vrfs_total 8

# VXLAN 设备数量
ovn_bgp_evpn_vxlan_devices_total 15

# FDB 条目数
ovn_bgp_evpn_fdb_entries_total 250

# 邻居条目数
ovn_bgp_evpn_neighbor_entries_total 180

# 同步耗时
ovn_bgp_evpn_sync_duration_seconds 45.2

# 同步错误计数
ovn_bgp_evpn_sync_errors_total 0
```

### 告警规则

`alerting-rules.yml`:
```yaml
groups:
- name: ovn-bgp-evpn
  interval: 30s
  rules:
    - alert: EVPNAgentDown
      expr: up{job="ovn-bgp-agent"} == 0
      for: 2m
      labels:
        severity: critical
      annotations:
        summary: "OVN BGP Agent is down on {{ $labels.instance }}"
    
    - alert: EVPNSyncErrors
      expr: rate(ovn_bgp_evpn_sync_errors_total[5m]) > 0.1
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "EVPN sync errors on {{ $labels.instance }}"
    
    - alert: EVPNHighSyncTime
      expr: ovn_bgp_evpn_sync_duration_seconds > 120
      for: 10m
      labels:
        severity: warning
      annotations:
        summary: "EVPN sync taking too long on {{ $labels.instance }}"
    
    - alert: BGPPeerDown
      expr: bgp_peer_up{job="frr"} == 0
      for: 3m
      labels:
        severity: critical
      annotations:
        summary: "BGP peer down on {{ $labels.instance }}"
```