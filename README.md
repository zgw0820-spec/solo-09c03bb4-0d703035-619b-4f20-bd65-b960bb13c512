# OCI 镜像容器路径诊断 API

容器启动报「找不到配置文件」时，发布工程师需要判断问题出在哪个**镜像层**（whiteout /
opaque 遮蔽）还是哪个**挂载点**（卷整棵替换了镜像子树）。本服务接收单平台
[OCI Image Layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
tar（层 blob 支持 gzip 或裸 tar）与容器内绝对路径，按「分层合并 → 目录挂载 → 符号链接
解析」的容器视图给出解析结果与可定位的来源。

另外，离线节点拿不到原镜像仓库时，`POST /export` 可以把镜像按层合并后的可见文件视图
**重打成单层 gzip 层**，重建 config / manifest / index 三级摘要引用，输出可独立导入的
OCI Image Layout tar 下载包（打包后先自检，通过才提供下载）。

## 语义约定

| 场景 | 行为 |
| --- | --- |
| 普通目录/文件/符号链接 | 直接读取归档中的元数据，全程在内存树中处理，不落盘执行 |
| whiteout（`.wh.名称`） | 仅遮蔽**下层**同名条目；同层在遮蔽之后新增的条目照常可见 |
| opaque（`.wh..wh..opq`） | 遮蔽所在目录的全部**下层**子项；同层新增项照常可见 |
| 目录挂载 | 用卷内容整棵替换挂载点子树，旧子树保留为遮蔽证据 |
| 符号链接 | 按挂载后的容器视图解析；相对链接以链接所在目录为基准，支持绝对/相对/`..` |
| 链接成环 | 第二次解析到同一链接绝对路径即报 `symlink_loop`，返回完整链接链 |
| 路径断裂 | 返回第一个断裂段、已解析前缀；若被遮蔽则附 whiteout/opaque/mount 来源 |

## 目录结构

```
app/
  archive.py   外层 tar 安全解包（禁绝对路径/.. 越界/危险成员类型）+ sha256
  oci.py       OCI 布局、index/manifest/config 解析与 blob 摘要校验
  layers.py    层合并引擎（whiteout/opaque/墓碑/硬链接，采集权限/属主/内容位置）
  mounts.py    目录挂载（挂载点校验、卷子树替换、嵌套挂载拒绝）
  resolver.py  容器视图路径解析（符号链接、环检测、断裂定位）
  export.py    单层重打包：层解压、合并视图写 gzip 层、重建摘要引用、产物自检
  models.py    Pydantic 请求/响应模型（驱动 Swagger）
  main.py      FastAPI 路由
scripts/build_sample_oci.py  构造三层演示镜像
tests/         54 个 pytest 用例（层语义/解析器/端到端 API/导出/安全）
```

## 用 Docker Compose 启动

```bash
docker compose up --build
# 服务监听 http://localhost:8000
# Swagger UI:  http://localhost:8000/docs
# OpenAPI:     http://localhost:8000/openapi.json
# 健康检查:     GET /health
```

容器以非 root 用户运行，上传内容写入 tmpfs（默认上限 2 GiB，导出流程需要
「上传包 + 解包布局 + 解压层 + 单层产物」的额外空间），请求结束即删。

## 在 Swagger 里试查样例

先生成样例镜像（三层，含 whiteout、opaque、相对/绝对符号链接与链接环）：

```bash
python3 scripts/build_sample_oci.py samples/sample-oci.tar.gz
```

打开 `http://localhost:8000/docs`，在 `POST /diagnose` 上：

1. `archive` 选择 `samples/sample-oci.tar.gz`
2. `target_path` 填入下表任一路径
3. 可选 `mounts` 填 JSON 字符串，例如
   `[{"mount_point":"/data","volume_files":["config.yaml","tls/cert.pem"]}]`

### 样例路径速查

| target_path | 预期结果 |
| --- | --- |
| `/etc/app/config.yaml` | found，来源层 1（覆盖了层 0） |
| `/etc/app/secret.env` | not_found，层 1 whiteout 遮蔽（附层摘要） |
| `/etc/app/legacy/old.conf` | not_found，层 2 opaque 遮蔽（附 opaque 目录） |
| `/etc/app/legacy/keep.txt` | found，层 2 opaque 同层新增，仍可见 |
| `/etc/app/current` | found，相对符号链接解析到 `/etc/app/config.yaml` |
| `/var/log/app.log` | found，绝对符号链接解析到 `/opt/app/bin/tool` |
| `/etc/app/loop/a` | symlink_loop，返回 `a -> b -> a` 完整链接链 |
| `/etc/app/missing.yaml` | not_found，普通缺失，`masking=null`，指出断裂段 |
| 挂载 `/data` 后查 `/data/readme.txt` | not_found，遮蔽原因 `mount`，挂载点 `/data` |
| 挂载 `/data` 后查 `/data/tls/cert.pem` | found，来源 `{"kind":"mount","mount_point":"/data"}` |

### curl 等价调用

```bash
curl -F "archive=@samples/sample-oci.tar.gz" \
     -F "target_path=/etc/app/secret.env" \
     http://localhost:8000/diagnose

curl -F "archive=@samples/sample-oci.tar.gz" \
     -F "target_path=/data/tls/cert.pem" \
     -F 'mounts=[{"mount_point":"/data","volume_files":["config.yaml","tls/cert.pem"]}]' \
     http://localhost:8000/diagnose
```

## 导出单层镜像包（离线导入）

`POST /export` 接收与诊断接口相同的上传表单（仅 `archive` 一个字段），把镜像按层合并
后的**容器可见文件视图**重打成单层镜像包：

| 方面 | 行为 |
| --- | --- |
| 可见文件 | 内容、权限（mode）、属主（uid/gid/uname/gname）原样保留；大文件全程 1 MiB 分块读写，不进内存 |
| 目录 | 显式目录条目保留权限与属主（多层同名目录以最高层条目为准） |
| 符号链接 | 保留原始链接目标字符串（绝对/相对/`..` 均不重写） |
| 硬链接 | 同内容条目在层内去重为硬链接成员，两个路径都能读到同一份内容 |
| whiteout / opaque | 只影响合并视图；标记本身与被遮蔽条目都不会出现在产物里 |
| 平台与启动配置 | config 的 `os`/`architecture`/`variant` 与 `config` 段（Env/Cmd/Entrypoint/WorkingDir/User/Labels 等）原样保留 |
| 摘要引用 | 层 diff_id/blob 摘要、config 摘要、manifest 摘要、index 描述符全部按新内容重算 |
| 自检 | 产物重新加载（校验全部 blob 摘要）→ 重新解压合并 → 与原视图逐路径比对类型/权限/属主/链接目标/内容摘要，全部通过才提供下载；任一步失败返回具体原因，不给文件 |

```bash
curl -OJ -F "archive=@samples/sample-oci.tar.gz" http://localhost:8000/export
# 得到 <镜像名>-single-layer.tar（单层 OCI Image Layout），可拷贝到离线节点：
#   ctr images import oci-image-<id>-single-layer.tar
#   podman load -i  oci-image-<id>-single-layer.tar   # oci-archive 格式
```

导出包可再次喂回本服务：`/diagnose` 查到与原镜像一致的可见视图（层数为 1），
`/export` 幂等。

## 响应模型摘要

```jsonc
{
  "status": "found | not_found | symlink_loop | not_directory",
  "resolved_path": "/归一化后的容器内绝对路径",
  "node_kind": "file | directory | symlink",
  "source": { "kind": "layer", "layer_index": 1, "digest": "sha256:...", "diff_id": "sha256:..." },
  // 或挂载来源：{ "kind": "mount", "mount_point": "/data" }
  "symlink_chain": [ { "link_path": "...", "target": "...", "layer_index": 2, "digest": "..." } ],
  "failure": {
    "failed_component": "断裂路径段",
    "resolved_prefix": "/断裂前已解析前缀",
    "masking": { "reason": "whiteout|opaque|mount", "layer_index": 2, "digest": "...", "opaque_directory": "..." },
    "message": "人类可读的定位说明"
  }
}
```

## 本地开发与测试（无 Docker 时）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -I -m pytest          # 54 个用例
PYTHONPATH=. .venv/bin/python -I -m uvicorn app.main:app --port 8000
```

## 安全说明

* 每个请求使用独立临时目录，结束后整体删除（下载流送完才清理）；
* 外层 tar 拒绝绝对路径、`..` 跳转、设备/fifo 等成员，符号链接目标禁止逃逸解包根；
* 所有内容寻址 blob 按 sha256 校验，摘要不符直接拒绝；
* 导出时额外校验每层解压后的 diff_id 与 config 声明一致；
* 层 tar 仅在内存中读取元数据（类型/链接名/大小），不提取、不执行任何归档内容；
  导出流程只把普通文件内容流式拷贝进新层，同样不落盘执行；
* 上传体积默认上限 512 MiB（`app/main.py` 可调）。
