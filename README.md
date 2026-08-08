# 关系识别插件

## 功能
- 用户设置关系（`设置关系 恋人`）
- 管理员查看所有关系（`查看所有关系`）
- 自动注入关系到 LLM 上下文，模型可感知

## 安装
放入 `plugins/astrbot_plugin_user_tag/` 重启。

## 配置
在 AstrBot 管理面板（插件配置页）中，为 `admin_qq` 配置项填写管理员 QQ 号列表，支持配置多个管理员（例如 `3881756548, 123456789`）。只有配置为管理员的用户可以使用 `查看所有关系` 指令。

配置项说明：
- `admin_qq`：管理员 QQ 列表，默认 `[]`。

## 注意
- 关系数据保存在 `data/plugin_data/astrbot_plugin_user_tag/user_tag.json`。
