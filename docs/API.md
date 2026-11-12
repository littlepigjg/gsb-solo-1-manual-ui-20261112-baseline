# REST API

地址：`http://127.0.0.1:8765/api`。同源 HTML 页面与 API 由同一个 Python 服务提供。

除图片、ZIP、元数据备份外，成功响应为 `{"ok": true, "data": ...}`；错误响应为 `{"ok": false, "error": {"code": "...", "message": "...", "details": {}}}`。`POST/PATCH` JSON 请求需要 `Content-Type: application/json`，上传图片使用 multipart 的 `file` 字段。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 健康检查、版本、功能数量 |
| GET | `/config` | 文件大小、尺寸、批量数量限制 |
| GET | `/catalog` | 全部功能分组、86 种操作与参数定义 |
| GET | `/stats` | 素材、结果、导出、批次统计 |
| GET | `/diagnostics` | 依赖版本、JSON 与文件完整性检查 |
| GET | `/assets` | 素材查询，支持 q/kind/favorite/limit/offset |
| POST | `/assets` | multipart 图片上传 |
| GET | `/assets/{id}` | 素材详细信息 |
| PATCH | `/assets/{id}` | 修改 name、favorite、tags |
| DELETE | `/assets/{id}` | 删除没有下游副本且未被活动任务引用的素材 |
| GET | `/assets/{id}/file` | 读取 PNG 编辑图 |
| GET | `/assets/{id}/thumbnail` | 读取缩略图 |
| GET | `/assets/{id}/history` | 从原图到当前结果的版本链 |
| GET | `/assets/{id}/analysis` | 图像直方图、色谱及亮度分析 |
| POST | `/compare` | 采样误差、PSNR 等比较指标 |
| POST | `/process` | 执行流程并保存新副本 |
| POST | `/preview` | 执行流程并返回临时 PNG，不写入素材库 |
| POST | `/samples` | 幂等生成三个示例素材，body 为 `{}` |
| GET | `/presets` | 列出内置与自定义预设 |
| POST | `/presets` | 创建预设 |
| PATCH | `/presets/{id}` | 修改自定义预设 |
| DELETE | `/presets/{id}` | 删除自定义预设 |
| GET | `/jobs` | 最近 100 条批量任务 |
| POST | `/jobs` | 提交后台批处理，返回 202 |
| GET | `/jobs/{id}` | 查询状态和进度 |
| POST | `/jobs/{id}/cancel` | 申请取消，body 为 `{}` |
| POST | `/exports` | 单图实际格式编码 |
| POST | `/exports/archive` | 多图编码并生成 ZIP |
| POST | `/project-packages/export` | 导出可迁移项目包（自动包含所选素材的全部上游历史） |
| POST | `/project-packages/import` | 校验并整包导入项目包，任一张图片或记录损坏都不会写入素材库 |
| GET | `/exports/{id}/download` | 下载导出产物（普通编码、批量 ZIP 或项目包 ZIP） |
| GET | `/events` | 最近 100 条操作记录 |
| GET | `/backup` | 下载完整 JSON 元数据快照 |

## 处理请求

```json
{
  "asset_id": "上传响应中的32位id",
  "name": "我的胶片作品",
  "steps": [
    {"operation": "resize_long_edge", "params": {"length": 1600, "upscale": false}},
    {"operation": "filter_sepia", "params": {"strength": 0.8}},
    {"operation": "watermark_text", "params": {"text": "我的作品", "opacity": 0.6}}
  ]
}
```

`name` 可省略。未指定的参数采用 `/catalog` 中的默认值；未知参数、字符串形式数字、布尔值充当数字、非有限浮点数及越界数值都会被拒绝。相互依赖参数（如色阶黑场必须小于白场）在算法执行前继续验证。

成功返回的新素材包含 `id`、`parent_id`、`root_id`、`steps`、`url`、`thumbnail_url`、尺寸、字节数和处理耗时。原图文件不变。

## 批量请求

```json
{
  "title": "电商图统一处理",
  "asset_ids": ["图片1的id", "图片2的id"],
  "steps": [
    {"operation": "resize_pad", "params": {"width": 1000, "height": 1000, "background": "#ffffff"}},
    {"operation": "enhance_sharpness", "params": {"factor": 1.2}}
  ]
}
```

任务状态：`queued → running → completed / partial / failed`。取消使用 `cancelling → cancelled`。服务重启后，未完成任务变为 `interrupted`。批内每张图片独立保存，失败不会阻止后续图片处理。

`results` 保存成功素材的 id；`errors` 保存失败图片 id 和原因；`progress` 为 0–100；`done/total` 表示完成尝试的图片数量。取消时保留已完成结果。

## 导出请求

```json
{
  "asset_id": "结果图片id",
  "options": {
    "format": "WEBP",
    "quality": 88,
    "lossless": false,
    "background": "#ffffff",
    "dpi": 96
  }
}
```

支持 `PNG/JPEG/WEBP/BMP/TIFF/GIF`。响应的 `url` 可直接下载。ZIP 接口将 `asset_id` 改为 `asset_ids` 数组，压缩包中包含安全命名的图片和 `manifest.json`。

## 项目包迁移

项目包用于把素材带到另一台电脑并继续原来的处理历史。导出请求只需要勾选的素材标识：

```json
{ "asset_ids": ["结果图片id"] }
```

服务会自动把该结果的全部上游版本（直到导入原图）加入包内。项目包是 ZIP，结构固定为 `manifest.json` 与 `assets/<素材id>.png`。清单记录每个素材的名称、类型、尺寸、`sha256`、`parent_id`、`root_id`、完整 `steps`、标签、收藏状态和处理元数据。

导入使用 multipart 的 `file` 字段上传项目包。服务在写入任何文件或记录前完成以下校验：ZIP 路径安全且无重复条目；清单格式与版本受支持；每个素材记录字段有效、处理步骤可识别、历史关系闭合；每张 PNG 都存在、可解码、尺寸与记录一致，且字节数和 `sha256` 完全匹配。缩略图由校验通过的图片重新生成。

任一校验失败都会返回 `400` 和 `invalid_project_package`，错误消息说明具体素材或文件，整个包被拒绝，素材库和媒体目录保持导入前状态。校验全部通过后，所有图片先进入暂存目录，再一次性移动并以单次 JSON 原子写入提交，避免导入一半。

导入不会因为重名或标识碰撞覆盖已有素材：名称原样保留并允许重复；包内标识若与目标库的记录或媒体文件冲突，会为该素材分配新标识，并同步重映射 `parent_id` 和 `root_id`。响应包含 `imported`、`asset_ids`、`root_ids`、`selection_ids`、`primary_id`、`id_conflicts`、`name_conflicts` 以及新素材的完整记录。

## 状态码

- 200：读取、更新、删除或临时预览成功。
- 201：素材、处理结果、预设、导出文件创建成功。
- 202：批量任务已提交。
- 400：参数、格式、图片或尺寸无效。
- 403：跨站写入请求被拒绝。
- 404：记录或接口不存在。
- 409：资源正在使用、存在下游副本、队列已满或处理中取消。
- 410：JSON 记录存在但关联文件丢失。
- 413：上传体积超过限制。
- 415：请求不是合法 JSON Content-Type。
- 500：未预料的服务错误；响应含 request_id，可对照日志。
