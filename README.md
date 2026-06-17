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

### 用户黑名单（仅管理员）

| 命令 | 说明 |
|------|------|
| `/jm black add <QQ号>` | 拉黑用户，禁止其使用 |
| `/jm black remove <QQ号>` | 移除拉黑 |
| `/jm black list` | 查看黑名单 |

## 特性

- 支持群聊和私聊
- 静默下载，仅推送最终结果一条消息
- 已下载漫画自动复用缓存，无需重复下载
- 兼容任意自定义唤醒前缀（`/` `#` `>>` `!` 等）
- 热安装依赖，`pip install` 后无需重启
- 群聊黑名单 + 用户黑名单双重控制
