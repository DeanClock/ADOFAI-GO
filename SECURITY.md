# 安全策略

## 支持范围

当前维护版本：`0.17.x`。

安全修复会优先进入最新版本。旧版本不保证继续收到修复。

## 报告安全问题

请不要在公开 Issue 里粘贴 Cookie、登录态、`settings.json`、`records.json`、
`draft.json` 或 `browser_profile` 目录内容。

仓库启用 GitHub Security Advisories 后，请通过：

```text
Security -> Report a vulnerability
```

提交报告。报告里请包含：

- 受影响版本；
- 复现步骤；
- 影响范围；
- 是否涉及 Cookie/账号；
- 你建议的修复方式（可选）。

## 威胁模型

这是一个本地 Windows 桌面工具，安全边界如下：

- 工具只主动访问 B 站与 TUF 相关域名。
- 工具不发送遥测，不向第三方上传历史、草稿或 Cookie。
- 登录态保存在 `%APPDATA%\AdofaiTufBridge\browser_profile`。
- 该目录属于当前 Windows 用户的本地数据；同一 Windows 账户下的其他进程、
  恶意软件或拥有管理员权限的攻击者可以读取它。
- 工具不额外加密该目录；加密由 Chromium/Edge 的 Cookie 存储机制负责。

因此，「同一 Windows 账户被攻破」不在本工具能防御的范围内。

## 安全设计约定

- 不硬编码账号、Cookie、Token、API Key 或个人信息。
- 不把 Cookie 写进日志、历史记录或 UI 信号。
- 不关闭 TLS 校验，不使用 `verify=False`。
- 不使用 `eval` / `exec` / `shell=True` 执行外部输入。
- 不关闭浏览器沙箱。
- 不绕过 B 站验证码或风控；遇到验证码时由用户手动完成。
- 删除登录数据时，只允许删除工具数据目录内部的浏览器配置，避免误删日常浏览器配置。

## 维护者发布检查

发布源码或二进制前至少完成：

```powershell
python -m pytest -q
python -m pip_audit
python -m bandit -r adofai_publisher -ll
```

并确认：

- 仓库没有提交 `browser_profile/`、`settings.json`、`records.json`、`draft.json`、日志或 `.env`。
- 截图和示例数据不含真实账号、Cookie、BV 号或个人信息。
- 依赖版本已审查，构建环境干净。
- 发布的 exe 提供 SHA256；有条件时进行代码签名。
- Release Notes 说明安全相关变更。

## 不在安全范围内

- B 站/TUF 账号被风控、限流或封禁。
- B 站/TUF 服务条款变化。
- B 站/TUF 页面结构或私有接口变化。
- 用户把登录数据目录、Cookie 或账号信息主动发给别人。
- 第三方浏览器、操作系统或依赖库自身的漏洞（应及时升级依赖）。

## 免责声明

本项目是个人向的非官方工具，与 B 站、TUF、ADOFAI 官方无隶属关系。
使用本工具即表示你理解：自动化投稿可能受平台服务条款约束，
账号风险与数据安全最终由使用者自行承担。
