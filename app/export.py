"""把合并后的镜像文件视图重打成可独立导入的单层 OCI Image Layout。

流程（全部流式/分块，不把大文件读进内存）：

1. ``decompress_layers``：把各层 blob 解压成裸 tar 落盘（1 MiB 分块），
   同时校验解压后摘要与 config 中的 diff_id 一致；
2. ``pack_merged_layer``：按合并视图确定性遍历，把可见条目写成单个
   gzip 层 tar——普通文件按 1 MiB 分块回读源层内容并顺带计算内容摘要，
   相同内容的重复条目（硬链接）以硬链接成员去重；
3. ``build_config / build_manifest / build_index``：保留平台与启动配置，
   重算层、配置、清单三级摘要引用；
4. ``validate_package``：把产出的布局当作外部输入重新加载（校验全部 blob
   摘要）、重新合并，并与原合并视图逐路径比对类型/权限/属主/链接目标/
   文件内容摘要，任何不一致都拒绝提供下载；
5. ``build_package_tar``：校验通过后才把布局打成外层 tar 供下载。
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .archive import sha256_file
from .layers import ContentRef, Node, NodeKind, merge_layers
from .oci import ImageModel, LayerBlob, load_image

CHUNK_SIZE = 1024 * 1024  # 1 MiB，所有大文件读写的分块粒度

_GZIP_MAGIC = b"\x1f\x8b"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

MEDIA_LAYER_GZIP_OCI = "application/vnd.oci.image.layer.v1.tar+gzip"
MEDIA_LAYER_GZIP_DOCKER = "application/vnd.docker.image.rootfs.diff.tar.gzip"
MEDIA_CONFIG_OCI = "application/vnd.oci.image.config.v1+json"
MEDIA_MANIFEST_OCI = "application/vnd.oci.image.manifest.v1+json"


class ExportError(RuntimeError):
    """打包或自检失败（对应 HTTP 500，detail 中给出具体原因）。"""


@dataclass(frozen=True)
class PackedFile:
    path: str  # 层内相对路径（正斜杠）
    sha256: str  # 内容摘要（打包写入时顺带计算）
    size: int


@dataclass(frozen=True)
class PackStats:
    diff_id: str  # 单层解压态摘要（写入 config.rootfs.diff_ids）
    digest: str  # 单层 gzip blob 摘要（写入 manifest.layers）
    size: int  # gzip blob 字节数
    file_count: int
    dir_count: int
    symlink_count: int
    files: dict[str, PackedFile] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportResult:
    package_path: Path  # 外层 tar 的磁盘路径（由调用方负责清理）
    filename: str  # 下载文件名
    manifest_digest: str
    layer_diff_id: str
    layer_digest: str
    file_count: int
    dir_count: int
    symlink_count: int
    package_size: int


# --------------------------------------------------------------------------- #
# 1. 层解压（分块 + diff_id 校验）
# --------------------------------------------------------------------------- #


def _sniff_compression(path: Path) -> str:
    with open(path, "rb") as handle:
        head = handle.read(4)
    if head[:2] == _GZIP_MAGIC:
        return "gzip"
    if head[:4] == _ZSTD_MAGIC:
        return "zstd"
    return "tar"


def _copy_chunked(src: BinaryIO, dst: BinaryIO, hasher: "hashlib._Hash") -> None:
    while True:
        chunk = src.read(CHUNK_SIZE)
        if not chunk:
            return
        hasher.update(chunk)
        dst.write(chunk)


def decompress_layers(image: ImageModel, work_dir: Path) -> tuple[LayerBlob, ...]:
    """把每个层 blob 解压为裸 tar 落盘，返回指向裸 tar 的层描述。

    解压流顺带计算 sha256 并与 config 声明的 diff_id 比对，
    不一致说明归档被篡改或 config 与层不配套，直接拒绝。
    """
    layer_dir = work_dir / "layers"
    layer_dir.mkdir(parents=True, exist_ok=True)

    decompressed: list[LayerBlob] = []
    for layer in image.layers:
        kind = _sniff_compression(layer.path)
        if kind == "zstd":
            raise ValueError(
                f"第 {layer.index} 层使用 zstd 压缩，本服务暂不支持，"
                "请改用 gzip 或裸 tar 层重新导出"
            )
        dest = layer_dir / f"layer-{layer.index}.tar"
        hasher = hashlib.sha256()
        try:
            if kind == "gzip":
                with gzip.open(layer.path, "rb") as src, open(dest, "wb") as dst:
                    _copy_chunked(src, dst, hasher)
            else:
                with open(layer.path, "rb") as src, open(dest, "wb") as dst:
                    _copy_chunked(src, dst, hasher)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise ValueError(f"第 {layer.index} 层解压失败: {exc}") from exc

        actual_diff_id = "sha256:" + hasher.hexdigest()
        if actual_diff_id != layer.diff_id:
            raise ValueError(
                f"第 {layer.index} 层解压后摘要 {actual_diff_id} 与 config 声明的 "
                f"diff_id {layer.diff_id} 不一致，归档可能已被篡改"
            )
        decompressed.append(
            LayerBlob(
                digest=layer.digest,
                diff_id=layer.diff_id,
                media_type=layer.media_type,
                path=dest,
                index=layer.index,
            )
        )
    return tuple(decompressed)


# --------------------------------------------------------------------------- #
# 2. 合并视图 → 单层 gzip tar
# --------------------------------------------------------------------------- #


class _HashingWriter:
    """包在输出流外：写出的每个字节都计入 sha256（用于 diff_id）。"""

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self._hasher = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self._hasher.update(data)
        return self._raw.write(data)

    @property
    def hexdigest(self) -> str:
        return self._hasher.hexdigest()


class _HashingReader:
    """包在输入流外：被 tarfile 分块读走的每个字节都计入 sha256。"""

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self._hasher = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        if data:
            self._hasher.update(data)
        return data

    @property
    def hexdigest(self) -> str:
        return self._hasher.hexdigest()


def _iter_merged_view(root: Node) -> Iterator[tuple[tuple[str, ...], Node]]:
    """前序遍历合并视图：目录在其子项之前，同级按名称排序，跳过墓碑。"""
    stack: list[tuple[tuple[str, ...], Node]] = [((), root)]
    while stack:
        parts, node = stack.pop()
        if node.kind == NodeKind.TOMBSTONE:
            continue
        if parts:  # 根节点本身不产出 tar 成员
            yield parts, node
        if node.kind == NodeKind.DIRECTORY:
            for name in sorted(node.children, reverse=True):
                child = node.children[name]
                if child.kind is not NodeKind.TOMBSTONE:
                    stack.append((parts + (name,), child))


def _fill_meta(info: tarfile.TarInfo, node: Node) -> None:
    info.mode = node.mode & 0o7777
    info.uid = node.uid
    info.gid = node.gid
    info.uname = node.uname
    info.gname = node.gname
    info.mtime = node.mtime


def _read_content(ref: ContentRef) -> BinaryIO:
    handle = open(ref.tar_path, "rb")
    handle.seek(ref.offset)
    return handle


def pack_merged_layer(root: Node, out_gz_path: Path) -> PackStats:
    """把合并视图写成单个 gzip 层 tar，返回 diff_id / blob 摘要 / 内容清单。

    * 普通文件内容从源层 tar 按偏移流式回读，全程分块，不进内存；
    * 内容位置完全相同的条目（硬链接展开来的）第二次起写成硬链接成员，
      避免同一份内容在层里重复占用空间；
    * whiteout / opaque / 墓碑条目不会出现在产物里——产物就是容器可见视图。
    """
    files: dict[str, PackedFile] = {}
    dir_count = 0
    symlink_count = 0
    seen_content: dict[ContentRef, str] = {}  # 内容位置 -> 首次写出的成员名

    raw = open(out_gz_path, "wb")
    try:
        gz = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
        hashing = _HashingWriter(gz)
        tar = tarfile.open(fileobj=hashing, mode="w|", format=tarfile.PAX_FORMAT)
        try:
            for parts, node in _iter_merged_view(root):
                arcname = "/".join(parts)
                if node.kind is NodeKind.DIRECTORY:
                    info = tarfile.TarInfo(name=arcname + "/")
                    info.type = tarfile.DIRTYPE
                    _fill_meta(info, node)
                    tar.addfile(info)
                    dir_count += 1
                elif node.kind is NodeKind.SYMLINK:
                    info = tarfile.TarInfo(name=arcname)
                    info.type = tarfile.SYMTYPE
                    info.linkname = node.link_target or ""
                    _fill_meta(info, node)
                    tar.addfile(info)
                    symlink_count += 1
                elif node.kind is NodeKind.FILE:
                    ref = node.content
                    if ref is None:
                        raise ExportError(f"内部错误：文件节点缺少内容位置: /{arcname}")
                    first = seen_content.get(ref)
                    if first is not None:
                        # 同一份内容已写过：以硬链接成员引用，节省层体积
                        info = tarfile.TarInfo(name=arcname)
                        info.type = tarfile.LNKTYPE
                        info.linkname = first
                        _fill_meta(info, node)
                        tar.addfile(info)
                        prior = files[first]
                        files[arcname] = PackedFile(
                            path=arcname, sha256=prior.sha256, size=prior.size
                        )
                        continue
                    info = tarfile.TarInfo(name=arcname)
                    info.type = tarfile.REGTYPE
                    info.size = ref.size
                    _fill_meta(info, node)
                    with _read_content(ref) as src:
                        reader = _HashingReader(src)
                        # tarfile 内部按块把 reader 的内容拷进层流
                        tar.addfile(info, reader)
                        files[arcname] = PackedFile(
                            path=arcname, sha256=reader.hexdigest, size=ref.size
                        )
                    seen_content[ref] = arcname
                else:  # TOMBSTONE 已在遍历时跳过，其余类型不应出现
                    raise ExportError(f"内部错误：合并视图含不可导出节点 {node.kind}: /{arcname}")
        finally:
            tar.close()
        diff_id = "sha256:" + hashing.hexdigest
        gz.close()
    finally:
        raw.close()

    digest = "sha256:" + sha256_file(out_gz_path)
    return PackStats(
        diff_id=diff_id,
        digest=digest,
        size=out_gz_path.stat().st_size,
        file_count=len(files),
        dir_count=dir_count,
        symlink_count=symlink_count,
        files=files,
    )


# --------------------------------------------------------------------------- #
# 3. 重建 config / manifest / index（保留平台与启动配置，重算摘要引用）
# --------------------------------------------------------------------------- #


def _json_bytes(doc: Any) -> bytes:
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


def build_config_bytes(image: ImageModel, diff_id: str) -> bytes:
    """在原 config 基础上替换 rootfs 为单层；启动配置（config 段）、
    os/architecture/variant 等平台字段原样保留。"""
    cfg = copy.deepcopy(image.config_doc)
    cfg["rootfs"] = {"type": "layers", "diff_ids": [diff_id]}
    created = cfg.get("created", "1970-01-01T00:00:00Z")
    cfg["history"] = [
        {
            "created": created,
            "comment": "merged single-layer repack by oci-path-diag export",
        }
    ]
    return _json_bytes(cfg)


def build_manifest_bytes(
    image: ImageModel,
    config_digest: str,
    config_size: int,
    layer_digest: str,
    layer_size: int,
) -> bytes:
    manifest = copy.deepcopy(image.manifest_doc)
    manifest["schemaVersion"] = 2
    manifest_media_type = manifest.get("mediaType", MEDIA_MANIFEST_OCI)

    original_config = image.manifest_doc.get("config") or {}
    manifest["config"] = {
        "mediaType": original_config.get("mediaType", MEDIA_CONFIG_OCI),
        "digest": config_digest,
        "size": config_size,
    }

    if str(manifest_media_type).startswith("application/vnd.docker."):
        layer_media_type = MEDIA_LAYER_GZIP_DOCKER
    else:
        layer_media_type = MEDIA_LAYER_GZIP_OCI
    manifest["layers"] = [
        {"mediaType": layer_media_type, "digest": layer_digest, "size": layer_size}
    ]
    return _json_bytes(manifest)


def build_index_bytes(image: ImageModel, manifest_digest: str, manifest_size: int) -> bytes:
    index = copy.deepcopy(image.index_doc)
    index["schemaVersion"] = 2
    descriptor = copy.deepcopy(image.manifest_descriptor)
    descriptor["mediaType"] = descriptor.get(
        "mediaType", image.manifest_doc.get("mediaType", MEDIA_MANIFEST_OCI)
    )
    descriptor["digest"] = manifest_digest
    descriptor["size"] = manifest_size
    if "platform" not in descriptor:
        platform: dict[str, Any] = {}
        for key in ("os", "architecture", "variant", "os.version"):
            value = image.config_doc.get(key)
            if value:
                platform[key] = value
        if platform:
            descriptor["platform"] = platform
    index["manifests"] = [descriptor]
    return _json_bytes(index)


def write_layout(
    work_dir: Path,
    layer_gz_path: Path,
    layer_digest: str,
    config_bytes: bytes,
    config_digest: str,
    manifest_bytes: bytes,
    manifest_digest: str,
    index_bytes: bytes,
) -> Path:
    """把单层布局写到 work_dir/export-layout/，返回布局根目录。"""
    layout_root = work_dir / "export-layout"
    blob_dir = layout_root / "blobs" / "sha256"
    blob_dir.mkdir(parents=True)

    (layout_root / "oci-layout").write_text(
        json.dumps({"imageLayoutVersion": "1.0.0"}, indent=2), encoding="utf-8"
    )
    (layout_root / "index.json").write_bytes(index_bytes)

    # 层 blob 已在磁盘上，直接移动到位（同一文件系统，零拷贝）
    shutil.move(str(layer_gz_path), blob_dir / layer_digest.split(":", 1)[1])
    (blob_dir / config_digest.split(":", 1)[1]).write_bytes(config_bytes)
    (blob_dir / manifest_digest.split(":", 1)[1]).write_bytes(manifest_bytes)
    return layout_root


# --------------------------------------------------------------------------- #
# 4. 产物自检：重新加载 + 重新合并 + 逐路径比对
# --------------------------------------------------------------------------- #


def _hash_node_content(node: Node) -> str:
    """分块计算节点内容的 sha256：从偏移起恰好读 size 字节。"""
    assert node.content is not None
    ref = node.content
    hasher = hashlib.sha256()
    remaining = ref.size
    with _read_content(ref) as src:
        while remaining > 0:
            chunk = src.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise ExportError(f"校验失败：层 tar 中内容长度不足 {ref.size} 字节")
            hasher.update(chunk)
            remaining -= len(chunk)
    return hasher.hexdigest()


def _compare_views(original: Node, repacked: Node, files: dict[str, PackedFile]) -> None:
    """逐路径比对原合并视图与重打包后重新合并的视图。"""
    original_map = {"/".join(parts): node for parts, node in _iter_merged_view(original)}
    repacked_map = {"/".join(parts): node for parts, node in _iter_merged_view(repacked)}

    missing = sorted(set(original_map) - set(repacked_map))
    extra = sorted(set(repacked_map) - set(original_map))
    if missing or extra:
        raise ExportError(
            "校验失败：重打包后文件清单不一致"
            + (f"，丢失 {missing[:3]} 等 {len(missing)} 项" if missing else "")
            + (f"，多出 {extra[:3]} 等 {len(extra)} 项" if extra else "")
        )

    for path, expected in original_map.items():
        actual = repacked_map[path]
        if actual.kind is not expected.kind:
            raise ExportError(
                f"校验失败：/{path} 类型不一致（期望 {expected.kind.value}，"
                f"实际 {actual.kind.value}）"
            )
        for label, want, got in (
            ("权限", expected.mode, actual.mode),
            ("属主 uid", expected.uid, actual.uid),
            ("属主 gid", expected.gid, actual.gid),
        ):
            if want != got:
                raise ExportError(
                    f"校验失败：/{path} 的{label}不一致（期望 {want}，实际 {got}）"
                )
        if expected.kind is NodeKind.SYMLINK:
            if (expected.link_target or "") != (actual.link_target or ""):
                raise ExportError(
                    f"校验失败：符号链接 /{path} 目标不一致（期望 "
                    f"{expected.link_target!r}，实际 {actual.link_target!r}）"
                )
        elif expected.kind is NodeKind.FILE:
            record = files.get(path)
            if record is None:
                raise ExportError(f"校验失败：/{path} 缺少打包时的内容摘要记录")
            actual_hash = _hash_node_content(actual)
            if actual_hash != record.sha256 or actual.size != record.size:
                raise ExportError(
                    f"校验失败：文件 /{path} 内容摘要与打包结果不一致"
                )


def validate_package(
    original_root: Node,
    image: ImageModel,
    layout_root: Path,
    stats: PackStats,
    work_dir: Path,
) -> None:
    """把导出布局当作外部输入完整自检，任何一步不符都抛 ExportError。"""
    try:
        reloaded = load_image(layout_root)  # 重新校验 manifest/config/blob 全部摘要
    except Exception as exc:
        raise ExportError(f"校验失败：导出布局无法重新加载: {exc}") from exc

    if len(reloaded.layers) != 1:
        raise ExportError(f"校验失败：导出包应为单层，实际 {len(reloaded.layers)} 层")
    layer = reloaded.layers[0]
    if layer.digest != stats.digest or layer.diff_id != stats.diff_id:
        raise ExportError("校验失败：导出包层摘要与打包结果不一致")

    # 平台与启动配置必须与原镜像一致
    new_config = reloaded.config_doc
    if new_config.get("config") != image.config_doc.get("config"):
        raise ExportError("校验失败：导出包启动配置（config 段）与原镜像不一致")
    for key in ("os", "architecture", "variant"):
        if new_config.get(key) != image.config_doc.get(key):
            raise ExportError(f"校验失败：导出包平台字段 {key} 与原镜像不一致")

    # 重新解压、重新合并，与原视图逐路径比对；
    # 解压到独立子目录，避免覆盖原层 tar（原合并视图的内容引用仍指向那里）
    try:
        repacked_layers = decompress_layers(reloaded, work_dir / "repack-check")
        repacked_root = merge_layers(repacked_layers, capture_content=True)
    except Exception as exc:
        raise ExportError(f"校验失败：导出包单层重新合并失败: {exc}") from exc
    _compare_views(original_root, repacked_root, stats.files)


# --------------------------------------------------------------------------- #
# 5. 外层 tar 打包 + 编排
# --------------------------------------------------------------------------- #


def build_package_tar(layout_root: Path, out_path: Path) -> None:
    """把布局目录打成外层 tar（成员固定元数据，内容分块拷贝）。"""
    members = ["oci-layout", "index.json"]
    blob_dir = layout_root / "blobs" / "sha256"
    members += [
        f"blobs/sha256/{p.name}" for p in sorted(blob_dir.iterdir()) if p.is_file()
    ]
    with tarfile.open(out_path, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for arcname in members:
            path = layout_root / arcname
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.REGTYPE
            info.size = path.stat().st_size
            info.mode = 0o644
            info.uid = info.gid = 0
            info.mtime = 0
            with open(path, "rb") as src:
                tar.addfile(info, src)


def _download_filename(image: ImageModel, manifest_digest: str) -> str:
    ref = ""
    for source in (image.manifest_descriptor, image.index_doc):
        annotations = source.get("annotations") or {}
        candidate = annotations.get("org.opencontainers.image.ref.name")
        if isinstance(candidate, str) and candidate.strip():
            ref = candidate.strip()
            break
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in ref).strip("-.")
    if not safe:
        safe = f"oci-image-{manifest_digest.split(':', 1)[1][:12]}"
    return f"{safe}-single-layer.tar"


def export_image(image: ImageModel, work_dir: Path) -> ExportResult:
    """编排整个导出流程；成功返回结果，失败抛 ValueError（输入问题）或
    ExportError（打包/自检问题）。"""
    try:
        plain_layers = decompress_layers(image, work_dir)
        merged_root = merge_layers(plain_layers, capture_content=True)

        layer_gz = work_dir / "layer.tar.gz"
        stats = pack_merged_layer(merged_root, layer_gz)

        config_bytes = build_config_bytes(image, stats.diff_id)
        config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        manifest_bytes = build_manifest_bytes(
            image, config_digest, len(config_bytes), stats.digest, stats.size
        )
        manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        index_bytes = build_index_bytes(image, manifest_digest, len(manifest_bytes))

        layout_root = write_layout(
            work_dir,
            layer_gz,
            stats.digest,
            config_bytes,
            config_digest,
            manifest_bytes,
            manifest_digest,
            index_bytes,
        )

        # 先自检，通过后才产出下载包
        validate_package(merged_root, image, layout_root, stats, work_dir)

        package_path = work_dir / "package.tar"
        build_package_tar(layout_root, package_path)
    except (ValueError, ExportError):
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ExportError(f"打包失败: {exc}") from exc

    return ExportResult(
        package_path=package_path,
        filename=_download_filename(image, manifest_digest),
        manifest_digest=manifest_digest,
        layer_diff_id=stats.diff_id,
        layer_digest=stats.digest,
        file_count=stats.file_count,
        dir_count=stats.dir_count,
        symlink_count=stats.symlink_count,
        package_size=package_path.stat().st_size,
    )
