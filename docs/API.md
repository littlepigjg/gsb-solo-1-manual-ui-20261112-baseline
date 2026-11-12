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
| GET | `/exports/{id}/download` | 下载导出产物 |
| GET | `/palettes` | 列出色板库中的调色板 |
| POST | `/palettes` | 手动创建调色板 |
| POST | `/palettes/import` | multipart 导入 GIMP 调色板（.gpl） |
| POST | `/palettes/extract` | 从图片提取主要颜色生成调色板 |
| GET | `/palettes/{id}` | 调色板详情 |
| PATCH | `/palettes/{id}` | 改名、替换或重排颜色 |
| DELETE | `/palettes/{id}` | 删除调色板 |
| POST | `/palettes/{id}/apply` | 把调色板套用到图片，生成新副本 |
| POST | `/palettes/{id}/export` | 导出 gpl/css/json/txt，返回下载记录 |
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

## 配色方案

调色板记录：`{name, columns, colors: [{hex: "#rrggbb", name}], source, source_asset_id}`，`source` 为 `gpl`（导入）、`image`（图片提取）或 `manual`（手动创建）。每个调色板 1–256 种颜色，色板库最多 100 个。

**导入**：`POST /palettes/import` 使用 multipart 的 `file` 字段，文件需为 UTF-8 的 GIMP 调色板（首行 `GIMP Palette`，支持 `Name:`、`Columns:`、`#` 注释与 `R G B[名称]` 颜色行）。缺少文件头、无法识别的行、非整数通道、超出 0–255 的颜色值、空调色板或超过 256 色都会返回 400 并指出具体行号；任何错误都会在写入前发现，不会留下半份调色板。

**提取**：`POST /palettes/extract`，body 为 `{"asset_id": "...", "count": 8, "name": "可选"}`，`count` 取 2–32。算法为 5 位直方图上的确定性中位切分，只统计不透明度 ≥ 50% 的像素；同一图片每次提取的颜色与排序完全一致。

**修改**：`PATCH /palettes/{id}` 接受 `name`、`colors`、`columns`。颜色次序即数组次序，重排、改名、增删颜色都通过整体替换 `colors` 完成；全部颜色校验通过后才会原子写入，任一颜色非法则原调色板保持不变。

**套用**：`POST /palettes/{id}/apply`，body 为 `{"asset_id": "..."}`。每个像素替换为 sRGB 欧氏距离最近的调色板颜色，距离相同时取调色板中排列靠前的颜色——同一输入永远得到同一结果。透明通道逐像素保留，结果保存为新的处理副本，原图不变。

**导出**：`POST /palettes/{id}/export`，body 为 `{"format": "gpl|css|json|txt"}`。生成实际文件并登记到导出记录，响应的 `url` 经 `/exports/{id}/download` 下载；.gpl 导出可被 GIMP / Inkscape / Krita 直接读取，也可重新导入本工作室。

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
