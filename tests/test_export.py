"""导出接口端到端测试：单层重打包、元数据保真、自检与失败原因、重新导入。"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.oci import load_image

# --------------------------------------------------------------------------- #
# 测试用 OCI 镜像构造器
# --------------------------------------------------------------------------- #


@dataclass
class Ent:
    """层 tar 内的一个成员。"""

    name: str
    kind: str  # dir | file | symlink | hardlink | fifo
    data: bytes = b""
    link: str = ""
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    uname: str = "root"
    gname: str = "root"
    mtime: int = 1700000000


def _layer_tar(entries: list[Ent]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for ent in entries:
            info = tarfile.TarInfo(name=ent.name)
            info.mode = ent.mode
            info.uid = ent.uid
            info.gid = ent.gid
            info.uname = ent.uname
            info.gname = ent.gname
            info.mtime = ent.mtime
            if ent.kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif ent.kind == "file":
                info.type = tarfile.REGTYPE
                info.size = len(ent.data)
                tar.addfile(info, io.BytesIO(ent.data))
            elif ent.kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = ent.link
                tar.addfile(info)
            elif ent.kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = ent.link
                tar.addfile(info)
            elif ent.kind == "fifo":
                info.type = tarfile.FIFOTYPE
                tar.addfile(info)
            else:  # pragma: no cover
                raise AssertionError(ent.kind)
    return buf.getvalue()


def _gzip(data: bytes) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(data)
    return out.getvalue()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


STARTUP_CONFIG = {
    "Env": ["PATH=/usr/bin", "APP_MODE=prod"],
    "Cmd": ["/app/run.sh", "--serve"],
    "Entrypoint": ["/bin/sh", "-c"],
    "WorkingDir": "/app",
    "User": "1000:1000",
    "Labels": {"maintainer": "release@example.com"},
}


def build_image(
    tmp_path: Path,
    layers: list[list[Ent]],
    *,
    name: str = "export-test",
    config_doc: dict | None = None,
    diff_ids_override: list[str] | None = None,
) -> Path:
    """构造单平台 OCI Image Layout tar（裸 tar 外层），返回归档路径。"""
    blobs: dict[str, bytes] = {}

    def add_blob(data: bytes) -> str:
        digest = _digest(data)
        blobs[digest.split(":", 1)[1]] = data
        return digest

    layer_descriptors = []
    diff_ids = []
    for idx, entries in enumerate(layers):
        tar_data = _layer_tar(entries)
        diff_ids.append(_digest(tar_data))
        gz_data = _gzip(tar_data)
        digest = add_blob(gz_data)
        layer_descriptors.append(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": digest,
                "size": len(gz_data),
            }
        )

    config = {
        "created": "2026-11-20T00:00:00Z",
        "architecture": "arm64",
        "os": "linux",
        "variant": "v8",
        "config": dict(STARTUP_CONFIG),
        "rootfs": {"type": "layers", "diff_ids": diff_ids_override or diff_ids},
        "history": [
            {"created": "2026-11-20T00:00:00Z", "comment": f"layer {i}"}
            for i in range(len(layers))
        ],
    }
    if config_doc:
        config.update(config_doc)
    config_bytes = json.dumps(config, indent=2).encode()
    config_digest = add_blob(config_bytes)

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": layer_descriptors,
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    manifest_digest = add_blob(manifest_bytes)

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_digest,
                "size": len(manifest_bytes),
                "platform": {"architecture": "arm64", "os": "linux", "variant": "v8"},
                "annotations": {"org.opencontainers.image.ref.name": name},
            }
        ],
    }

    archive = tmp_path / f"{name}.tar"
    with tarfile.open(archive, mode="w") as out:
        def _add(arcname: str, data: bytes) -> None:
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 1700000000
            out.addfile(info, io.BytesIO(data))

        _add("oci-layout", json.dumps({"imageLayoutVersion": "1.0.0"}).encode())
        _add("index.json", json.dumps(index, indent=2).encode())
        for hex_digest, data in sorted(blobs.items()):
            _add(f"blobs/sha256/{hex_digest}", data)
    return archive


BIG_CONTENT = bytes((i * 31 + 7) % 256 for i in range(3 * 1024 * 1024))  # 3 MiB


def rich_layers() -> list[list[Ent]]:
    """覆盖：遮蔽后同名重建、opaque 后重建、目录/文件元数据、符号链接、
    硬链接与大文件。"""
    layer0 = [
        Ent("app/", "dir", mode=0o755),
        Ent("app/run.sh", "file", data=b"#!/bin/sh\necho v1\n", mode=0o755),
        Ent("app/data/", "dir", mode=0o750, uid=3, gname="svc"),
        Ent("app/data/old.txt", "file", data=b"old-lower\n"),
        Ent("srv/", "dir", mode=0o700, uid=3, gid=4, uname="svc", gname="svc"),
        Ent("srv/keep/", "dir"),
        Ent("srv/keep/note.txt", "file", data=b"keepme\n"),
        Ent("etc/", "dir"),
        Ent("etc/hosts", "file", data=b"127.0.0.1 localhost\n", mode=0o644),
        Ent("etc/hosts.link", "hardlink", link="etc/hosts"),
        Ent("bin/", "dir"),
        Ent("bin/tool", "file", data=b"binary\n", mode=0o755),
        Ent("bin/sh", "symlink", link="/bin/tool", mode=0o777),
        Ent("opt/", "dir"),
        Ent("opt/big.bin", "file", data=BIG_CONTENT, mode=0o640, uid=5, gid=6),
    ]
    layer1 = [
        Ent("app/.wh.run.sh", "file"),  # whiteout 遮蔽下层 run.sh
        Ent("app/data/.wh..wh..opq", "file"),  # opaque 遮蔽 /app/data 全部下层子项
        Ent("app/data/fresh.txt", "file", data=b"fresh\n"),
    ]
    layer2 = [
        # 遮蔽后同名重建：内容、权限、属主全部换新
        Ent("app/run.sh", "file", data=b"#!/bin/sh\necho v3-rebuilt\n",
            mode=0o750, uid=10, gid=11, uname="builder"),
        Ent("app/data/old.txt", "file", data=b"rebuilt-after-opaque\n", mode=0o600),
    ]
    return [layer0, layer1, layer2]


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def rich_archive(tmp_path: Path) -> Path:
    return build_image(tmp_path, rich_layers())


def _post_export(client: TestClient, archive: Path):
    with open(archive, "rb") as handle:
        return client.post(
            "/export",
            files={"archive": (archive.name, handle, "application/x-tar")},
        )


def _extract_exported(resp_content: bytes, dest: Path) -> Path:
    """把下载的导出包解到全新空目录，返回布局根。"""
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(resp_content), mode="r:") as tar:
        tar.extractall(dest)
    return dest


def _read_json_blob(layout_root: Path, digest: str) -> dict:
    path = layout_root / "blobs" / "sha256" / digest.split(":", 1)[1]
    return json.loads(path.read_text())


def _layer_members(layout_root: Path) -> dict[str, tarfile.TarInfo]:
    """读取导出包单层的全部成员（解 gzip），按归一化成员名索引（目录去尾斜杠）。"""
    index = json.loads((layout_root / "index.json").read_text())
    manifest = _read_json_blob(layout_root, index["manifests"][0]["digest"])
    layer_digest = manifest["layers"][0]["digest"]
    blob = layout_root / "blobs" / "sha256" / layer_digest.split(":", 1)[1]
    members: dict[str, tarfile.TarInfo] = {}
    with tarfile.open(blob, mode="r:gz") as tar:
        for member in tar:
            key = member.name.rstrip("/") if member.isdir() else member.name
            members[key] = member
    return members


def _layer_file_bytes(layout_root: Path, member_name: str) -> bytes:
    index = json.loads((layout_root / "index.json").read_text())
    manifest = _read_json_blob(layout_root, index["manifests"][0]["digest"])
    layer_digest = manifest["layers"][0]["digest"]
    blob = layout_root / "blobs" / "sha256" / layer_digest.split(":", 1)[1]
    with tarfile.open(blob, mode="r:gz") as tar:
        src = tar.extractfile(member_name)
        assert src is not None
        return src.read()


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #


def test_export_success_view_and_rebuild(client, rich_archive, tmp_path):
    resp = _post_export(client, rich_archive)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-tar")
    disposition = resp.headers["content-disposition"]
    assert "export-test-single-layer.tar" in disposition

    layout = _extract_exported(resp.content, tmp_path / "exported")
    # 导出包自身通过完整 OCI 校验（manifest/config/blob 摘要）
    image = load_image(layout)
    assert len(image.layers) == 1

    members = _layer_members(layout)
    names = set(members)
    # whiteout / opaque 标记本身不得出现在产物里
    assert not any(".wh." in name for name in names)
    # 遮蔽后同名重建：存在且是重建后的版本
    assert "app/run.sh" in names
    run_sh = members["app/run.sh"]
    assert run_sh.mode == 0o750
    assert run_sh.uid == 10
    assert run_sh.gid == 11
    assert run_sh.uname == "builder"
    assert _layer_file_bytes(layout, "app/run.sh") == b"#!/bin/sh\necho v3-rebuilt\n"
    # opaque 后同名重建
    assert _layer_file_bytes(layout, "app/data/old.txt") == b"rebuilt-after-opaque\n"
    assert members["app/data/old.txt"].mode == 0o600
    # opaque 同层新增可见
    assert _layer_file_bytes(layout, "app/data/fresh.txt") == b"fresh\n"
    # 未遮蔽内容保留
    assert _layer_file_bytes(layout, "srv/keep/note.txt") == b"keepme\n"


def test_export_preserves_metadata(client, rich_archive, tmp_path):
    resp = _post_export(client, rich_archive)
    assert resp.status_code == 200, resp.text
    layout = _extract_exported(resp.content, tmp_path / "exported")
    members = _layer_members(layout)

    # 目录权限与属主
    srv = members["srv"]
    assert srv.isdir()
    assert srv.mode == 0o700
    assert (srv.uid, srv.gid) == (3, 4)
    assert (srv.uname, srv.gname) == ("svc", "svc")
    # 普通文件权限与属主
    big = members["opt/big.bin"]
    assert big.mode == 0o640
    assert (big.uid, big.gid) == (5, 6)
    # 符号链接目标
    sh = members["bin/sh"]
    assert sh.issym()
    assert sh.linkname == "/bin/tool"


def test_export_large_file_content_integrity(client, rich_archive, tmp_path):
    resp = _post_export(client, rich_archive)
    assert resp.status_code == 200, resp.text
    layout = _extract_exported(resp.content, tmp_path / "exported")
    data = _layer_file_bytes(layout, "opt/big.bin")
    assert len(data) == len(BIG_CONTENT)
    assert hashlib.sha256(data).hexdigest() == hashlib.sha256(BIG_CONTENT).hexdigest()


def test_export_hardlink_content_available(client, rich_archive, tmp_path):
    resp = _post_export(client, rich_archive)
    assert resp.status_code == 200, resp.text
    layout = _extract_exported(resp.content, tmp_path / "exported")
    members = _layer_members(layout)
    # 两个路径都必须能读到同一份内容（产物内可以是硬链接或重复普通文件）
    assert _layer_file_bytes(layout, "etc/hosts") == b"127.0.0.1 localhost\n"
    assert _layer_file_bytes(layout, "etc/hosts.link") == b"127.0.0.1 localhost\n"
    link = members["etc/hosts.link"]
    if link.islnk():
        assert link.linkname == "etc/hosts"


def test_export_config_platform_and_digests(client, rich_archive, tmp_path):
    resp = _post_export(client, rich_archive)
    assert resp.status_code == 200, resp.text
    layout = _extract_exported(resp.content, tmp_path / "exported")

    index = json.loads((layout / "index.json").read_text())
    assert len(index["manifests"]) == 1
    descriptor = index["manifests"][0]
    assert descriptor["platform"] == {
        "architecture": "arm64",
        "os": "linux",
        "variant": "v8",
    }

    manifest = _read_json_blob(layout, descriptor["digest"])
    assert len(manifest["layers"]) == 1
    config = _read_json_blob(layout, manifest["config"]["digest"])
    # 启动配置原样保留
    assert config["config"] == STARTUP_CONFIG
    # 平台字段保留
    assert config["os"] == "linux"
    assert config["architecture"] == "arm64"
    assert config["variant"] == "v8"
    # rootfs 重建为单层，diff_id 与层 blob 解压摘要一致
    assert len(config["rootfs"]["diff_ids"]) == 1
    layer_blob = (
        layout / "blobs" / "sha256" / manifest["layers"][0]["digest"].split(":", 1)[1]
    )
    raw = gzip.decompress(layer_blob.read_bytes())
    assert config["rootfs"]["diff_ids"][0] == "sha256:" + hashlib.sha256(raw).hexdigest()
    # manifest 中层摘要与 blob 实际摘要一致
    assert manifest["layers"][0]["digest"] == "sha256:" + hashlib.sha256(
        layer_blob.read_bytes()
    ).hexdigest()


def test_exported_package_reimport_roundtrip(client, rich_archive, tmp_path):
    """导出包重新导入：再走 /diagnose 与 /export，结果一致。"""
    resp = _post_export(client, rich_archive)
    assert resp.status_code == 200, resp.text
    exported = tmp_path / "exported.tar"
    exported.write_bytes(resp.content)

    # 重新导入诊断接口：合并视图与原镜像一致
    with open(exported, "rb") as handle:
        diag = client.post(
            "/diagnose",
            files={"archive": ("exported.tar", handle, "application/x-tar")},
            data={"target_path": "/app/run.sh"},
        )
    assert diag.status_code == 200, diag.text
    body = diag.json()
    assert body["status"] == "found"
    assert body["image"]["layer_count"] == 1
    assert body["source"]["layer_index"] == 0

    # 被遮蔽路径在导出包里彻底不存在（不是墓碑，是普通缺失）
    with open(exported, "rb") as handle:
        diag2 = client.post(
            "/diagnose",
            files={"archive": ("exported.tar", handle, "application/x-tar")},
            data={"target_path": "/app/data/nonexistent.txt"},
        )
    assert diag2.json()["status"] == "not_found"

    # 导出包再次导出（幂等）：仍成功且文件清单不变
    resp2 = _post_export(client, exported)
    assert resp2.status_code == 200, resp2.text
    layout1 = _extract_exported(resp.content, tmp_path / "first")
    layout2 = _extract_exported(resp2.content, tmp_path / "second")
    assert set(_layer_members(layout1)) == set(_layer_members(layout2))


# --------------------------------------------------------------------------- #
# 失败路径：必须给出原因，且不提供下载
# --------------------------------------------------------------------------- #


def test_export_corrupt_layer_blob_rejected(client, rich_archive, tmp_path):
    """篡改层 blob 一个字节：摘要校验必须拒绝并说明原因。"""
    extract = tmp_path / "layout"
    extract.mkdir()
    with tarfile.open(rich_archive, "r:") as tar:
        tar.extractall(extract)
    index = json.loads((extract / "index.json").read_text())
    manifest = _read_json_blob(extract, index["manifests"][0]["digest"])
    layer_hex = manifest["layers"][0]["digest"].split(":", 1)[1]
    blob = extract / "blobs" / "sha256" / layer_hex
    data = bytearray(blob.read_bytes())
    data[-1] ^= 0xFF
    blob.write_bytes(bytes(data))

    corrupted = tmp_path / "corrupted.tar"
    with tarfile.open(corrupted, "w") as out:
        for path in sorted(extract.rglob("*")):
            if path.is_file():
                out.add(path, arcname=str(path.relative_to(extract)))
    resp = _post_export(client, corrupted)
    assert resp.status_code == 400
    assert "摘要" in resp.json()["detail"]


def test_export_diff_id_mismatch_rejected(client, tmp_path):
    """config 声明的 diff_id 与层实际内容不符：解压校验拒绝。"""
    archive = build_image(
        tmp_path,
        [[Ent("a.txt", "file", data=b"hello\n")]],
        name="diffid-bad",
        diff_ids_override=["sha256:" + "0" * 64],
    )
    resp = _post_export(client, archive)
    assert resp.status_code == 400
    assert "diff_id" in resp.json()["detail"]


def test_export_unsupported_member_rejected(client, tmp_path):
    archive = build_image(
        tmp_path,
        [[Ent("dev/", "dir"), Ent("dev/null0", "fifo")]],
        name="fifo-bad",
    )
    resp = _post_export(client, archive)
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


def test_export_empty_upload_rejected(client):
    resp = client.post(
        "/export",
        files={"archive": ("empty.tar", io.BytesIO(b""), "application/x-tar")},
    )
    assert resp.status_code == 400
    assert "空" in resp.json()["detail"]


def test_export_junk_archive_rejected(client):
    resp = client.post(
        "/export",
        files={"archive": ("junk.tar", io.BytesIO(b"not a tar" * 20), "application/x-tar")},
    )
    assert resp.status_code == 400
    assert "归档解析失败" in resp.json()["detail"]


def test_export_non_oci_tar_rejected(client, tmp_path):
    """合法 tar 但不是 OCI 布局：返回 400 与原因，而不是 500。"""
    plain = tmp_path / "plain.tar"
    with tarfile.open(plain, "w") as tar:
        data = b"hello"
        info = tarfile.TarInfo(name="readme.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    resp = _post_export(client, plain)
    assert resp.status_code == 400
    assert "oci-layout" in resp.json()["detail"]


def test_export_validation_failure_returns_500(client, rich_archive, monkeypatch):
    """自检失败时返回 500 与原因，绝不提供下载。"""
    import app.export as export_mod

    def boom(*args, **kwargs):
        raise export_mod.ExportError("校验失败：模拟的视图不一致")

    monkeypatch.setattr(export_mod, "validate_package", boom)
    # main 模块在导入时绑定了 export_image，但其内部调用的 validate_package
    # 是模块级查找，替换 export_mod 上的属性即可生效
    resp = _post_export(client, rich_archive)
    assert resp.status_code == 500
    assert "校验失败" in resp.json()["detail"]


def test_export_pack_failure_returns_500(client, rich_archive, monkeypatch):
    import app.export as export_mod

    def boom(*args, **kwargs):
        raise export_mod.ExportError("打包失败：模拟的磁盘错误")

    monkeypatch.setattr(export_mod, "pack_merged_layer", boom)
    resp = _post_export(client, rich_archive)
    assert resp.status_code == 500
    assert "打包失败" in resp.json()["detail"]


def test_export_docker_media_types_preserved(client, tmp_path):
    """docker 系 mediaType 的镜像：导出包沿用 docker 系清单/层 mediaType。"""
    archive = build_image(
        tmp_path, [[Ent("a.txt", "file", data=b"hi\n")]], name="docker-mt"
    )
    # 把布局改写成 docker 系 mediaType（保持摘要一致：重写 blob 与引用）
    extract = tmp_path / "layout"
    extract.mkdir()
    with tarfile.open(archive, "r:") as tar:
        tar.extractall(extract)
    index = json.loads((extract / "index.json").read_text())
    manifest_path = (
        extract / "blobs" / "sha256" / index["manifests"][0]["digest"].split(":", 1)[1]
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["mediaType"] = "application/vnd.docker.distribution.manifest.v2+json"
    manifest["config"]["mediaType"] = "application/vnd.docker.container.image.v1+json"
    for layer in manifest["layers"]:
        layer["mediaType"] = "application/vnd.docker.image.rootfs.diff.tar.gzip"
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    manifest_path.unlink()
    new_digest = hashlib.sha256(manifest_bytes).hexdigest()
    (extract / "blobs" / "sha256" / new_digest).write_bytes(manifest_bytes)
    index["manifests"][0]["mediaType"] = (
        "application/vnd.docker.distribution.manifest.v2+json"
    )
    index["manifests"][0]["digest"] = f"sha256:{new_digest}"
    index["manifests"][0]["size"] = len(manifest_bytes)
    (extract / "index.json").write_text(json.dumps(index, indent=2))

    docker_archive = tmp_path / "docker-mt.tar"
    with tarfile.open(docker_archive, "w") as out:
        for path in sorted(extract.rglob("*")):
            if path.is_file():
                out.add(path, arcname=str(path.relative_to(extract)))

    resp = _post_export(client, docker_archive)
    assert resp.status_code == 200, resp.text
    layout = _extract_exported(resp.content, tmp_path / "exported")
    out_index = json.loads((layout / "index.json").read_text())
    out_manifest = _read_json_blob(layout, out_index["manifests"][0]["digest"])
    assert out_manifest["mediaType"] == (
        "application/vnd.docker.distribution.manifest.v2+json"
    )
    assert out_manifest["layers"][0]["mediaType"] == (
        "application/vnd.docker.image.rootfs.diff.tar.gzip"
    )
    assert out_manifest["config"]["mediaType"] == (
        "application/vnd.docker.container.image.v1+json"
    )
