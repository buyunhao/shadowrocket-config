# shadowrocket-config

一份用于 Shadowrocket 的个人分流配置，主要面向中国大陆网络环境下的日常使用场景，重点关注国内外流量分流、DNS 稳定性、Apple 服务可用性，以及 iMessage / FaceTime / iCloud 等服务的连接体验。

## 配置目标

- 国内常用网站和服务尽量直连，降低延迟。
- 国外服务按规则走代理，提高可访问性。
- Apple 相关服务保持稳定，减少 iMessage、FaceTime、iCloud、App Store 等场景异常。
- 局域网、保留地址、组播地址等不进入代理隧道，避免影响本地网络访问。
- DNS 配置优先稳定，避免 DoH 初始化失败或解析路径异常导致节点误判为超时。

## 适用场景

该配置适合以下使用方式：

- Shadowrocket on iOS / iPadOS
- Shadowrocket for macOS
- 中国大陆网络环境下的代理分流
- 需要兼顾国内服务、国外服务和 Apple 生态服务的日常网络环境

## 使用方法

### 导入配置

1. 打开 Shadowrocket。
2. 进入配置管理页面。
3. 添加远程配置或导入本仓库中的 `.conf` 文件。
4. 保存后启用该配置。
5. 确认节点、策略组和规则均已正确加载。

### 移动端去广告配置

基于移动端基础配置叠加广告域名/IP 拒绝规则，订阅地址：

```text
https://raw.githubusercontent.com/buyunhao/shadowrocket-config/main/shadowrocket_gpt_maintain-mobile-adblock.conf
```

广告规则集地址：

```text
https://raw.githubusercontent.com/buyunhao/shadowrocket-config/main/rules/adblock.list
```

`rules/adblock.list` 由 `scripts/sync_johnshall_adblock.py` 从
`Johnshall/Shadowrocket-ADBlock-Rules-Forever` 的 `sr_ad_only.conf` 规范化生成，
只保留 `Reject` 规则并由父配置统一应用 `REJECT` 策略。上游内容采用 CC BY-SA 4.0 许可。

该配置不使用 HTTPS 解密、脚本或 URL Rewrite。域名/IP 拦截无法保证去除所有广告，
也可能出现误杀；遇到异常时应先结合 Shadowrocket 日志确认命中的规则。

### 更新配置

如果使用远程配置 URL 导入，可以在 Shadowrocket 中手动刷新配置。

如果是本地导入，建议每次修改后重新导入或覆盖原配置。

## 连通性测试 URL

可用于 Shadowrocket 节点连通性测试的 URL：

```text
https://www.gstatic.com/generate_204
```

也可以使用 Apple 的测试地址：

```text
https://www.apple.com/library/test/success.html
```

如果 Shadowrocket 显示所有节点测试超时，但实际浏览器仍可访问外网，不一定代表节点不可用。可能原因包括：

- 测试 URL 被当前网络、DNS 或规则特殊处理。
- 节点服务商对测试地址连接质量较差。
- Shadowrocket 的测速路径和真实应用访问路径不同。
- DNS 解析结果、策略组命中、TUN 行为与预期不一致。

建议同时结合浏览器访问、App Store、iMessage、iCloud、终端 `curl` 等真实场景判断节点是否可用。

## 配置重点

### General

`skip-proxy` 和 `tun-excluded-routes` 应尽量只排除局域网、本机、保留地址和组播地址。不要把大量公网中国 IP 段放入 TUN 排除路由，否则可能绕过 Shadowrocket 的规则系统，导致分流不可控。

常见排除范围包括：

```text
10.0.0.0/8
100.64.0.0/10
127.0.0.0/8
169.254.0.0/16
172.16.0.0/12
192.168.0.0/16
224.0.0.0/4
255.255.255.255/32
```

### DNS

DNS 配置建议优先考虑稳定性，而不是单纯追求加密 DNS 或复杂规则。

对于国内直连域名，可以优先使用当前网络或路由器 DNS；对于代理域名，则交由规则和最终策略决定。这样可以降低 DoH 初始化失败、DNS 解析超时、节点全部显示超时等问题出现的概率。

### Apple 服务

Apple 服务建议谨慎分流。以下域名通常与 iMessage、FaceTime、iCloud、App Store、Apple Push Notification service 等服务相关：

```text
apple.com
icloud.com
mzstatic.com
cdn-apple.com
push.apple.com
courier.push.apple.com
ess.apple.com
```

在中国大陆网络环境下，Apple CDN、App Store 资源、系统更新等通常直连更稳定；部分认证、推送或海外服务则需要结合实际网络环境测试后决定直连或代理。

## 常见问题

### 所有节点测速都超时，但实际可以访问外网？

这通常是测速 URL、DNS、策略组或 Shadowrocket 测试机制导致的误判。建议更换测试 URL，并用真实访问场景验证。

### iMessage 发送慢或不稳定怎么办？

可以优先检查以下项目：

1. Apple Push 相关域名是否被错误代理或错误直连。
2. DNS 是否稳定。
3. TUN 排除路由是否过宽。
4. 当前节点是否对 Apple 服务连接质量较差。
5. macOS 信息 App、iCloud、Apple ID 登录状态是否正常。

### 国内网站访问变慢怎么办？

检查国内域名、国内 IP、CDN 域名是否正确直连。如果 TUN 排除路由配置过宽，也可能导致规则系统失效，出现看似直连但行为不可控的问题。

## 维护建议

- 修改配置前先备份旧版本。
- 每次只调整一类规则，便于定位问题。
- Apple 服务、DNS、TUN 路由建议分开测试。
- 重要变更写清楚 commit message，方便回滚。
- 节点测速结果仅作参考，应以真实应用体验为准。

## 免责声明

本仓库配置仅用于个人网络环境优化与学习研究。不同地区、运营商、节点服务商、系统版本和 Shadowrocket 版本下表现可能不同，请根据实际环境自行调整。
