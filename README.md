# JMComic 漫画下载器 for AstrBot

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) API 的 AstrBot 插件，支持在 QQ 群内下载禁漫天堂漫画并转为 PDF 上传群文件。

## 安装依赖

在 AstrBot 的 Python 环境中安装：

```bash
pip install jmcomic Pillow
```

> ⚠️ 注意：必须在 **AstrBot 所使用的 Python 环境** 中安装，不是系统 Python。如果 AstrBot 安装在 `C:\Users\PC\.astrbot_launcher\`，请使用对应 Python 路径安装。
>
> 如果不确定，可以在 AstrBot WebUI → 插件管理 → 打开终端，执行 `pip install jmcomic Pillow`。

## 命令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/jm <编号>` | 所有人 | 下载漫画 → 转PDF → 上传群文件 → @提示 |
| `/jm list` | 所有人 | 列出已下载的漫画 |
| `/jm delete <编号>` | 管理员 | 删除指定漫画 |
| `/jm delete all` | 管理员 | 删除全部（需二次确认） |

## 特性

- 已下载的漫画自动复用缓存，直接发送无需重复下载
- 兼容 AstrBot 任意自定义唤醒前缀（`/` `#` `>>` `!` 等）
- `pip install` 后无需重启 AstrBot 即可生效
- 用于下载图片并合并为单个 PDF 上传至 QQ 群文件
