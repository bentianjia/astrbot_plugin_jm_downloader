# JMComic 漫画下载器 for AstrBot

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) API 的 AstrBot 插件，支持在 QQ 群聊和私聊中下载禁漫天堂漫画并转为 PDF 上传。

## 安装依赖

在 AstrBot 的 Python 环境中安装：

```bash
pip install jmcomic Pillow
```

> ⚠️ 注意：必须在 **AstrBot 所使用的 Python 环境** 中安装，不是系统 Python。
> 如果不确定，可以在 AstrBot WebUI → 插件管理 → 打开终端，执行 `pip install jmcomic Pillow`。

## 命令

### 下载

| 命令 | 权限 | 说明 |
|------|------|------|
| `/jm <编号>` | 所有人 | 下载漫画 → 转PDF → 上传文件 → @提示。已下载的直接发送缓存 |

### 管理（仅管理员）

| 命令 | 说明 |
|------|------|
| `/jm list` | 列出已下载的漫画（所有人可用） |
| `/jm delete <编号>` | 删除指定漫画 |
| `/jm delete all` | 删除全部（需二次确认） |

### 群聊控制（仅管理员）

| 命令 | 说明 |
|------|------|
| `/jm group off <群号>` | 对该群禁用 JM 下载 |
| `/jm group on <群号>` | 对该群恢复使用 |

### 漫画编号黑名单（仅管理员）

| 命令 | 说明 |
|------|------|
| `/jm black_jm add <编号>` | 拉黑漫画，禁止任何人下载 |
| `/jm black_jm remove <编号>` | 移除漫画拉黑 |
| `/jm black_jm list` | 查看已拉黑漫画 |

### 用户黑名单（仅管理员）

| 命令 | 说明 |
|------|------|
| `/jm black add <QQ号>` | 拉黑用户，禁止其使用 |
| `/jm black remove <QQ号>` | 移除拉黑 |
| `/jm black list` | 查看黑名单 |

### 标签黑名单（仅管理员，支持多作用域）
_作用域支持：`global`(全局)、`group <群号>`、`user <QQ号>`_

| 命令 | 说明 |
|------|------|
| `/jm black_tag add <作用域> <标签>` | 拉黑包含该标签的漫画，并在下载前自动拦截 |
| `/jm black_tag remove <作用域> <标签>` | 移除指定作用域的标签拉黑 |
| `/jm black_tag list <作用域>` | 查看该作用域已拉黑的标签 |
| `/jm black_tag remove_all <作用域> confirm` | 一键清空该作用域的所有黑名单限制 |

## 特性

- 支持群聊和私聊
- 静默下载，仅推送最终结果一条消息
- **5分钟 ZIP 缓存化**：后台打包 ZIP，5 分钟无人下载自动删除占空间的原始 PDF，再次请求极速解压，极致节省硬盘空间！
- **标签自动拦截**：获取信息时自动比对黑名单，拦截不受欢迎的题材。
- 兼容任意自定义唤醒前缀（`/` `#` `>>` `!` 等）
- 自动尝试通过国内镜像源安装依赖 `jmcomic` 和 `Pillow`
- 群聊黑名单 + 用户黑名单 + 标签黑名单等多重风控
