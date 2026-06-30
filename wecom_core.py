# -*- coding: utf-8 -*-
"""企业微信会话内容存档核心模块。

封装企业微信官方会话存档 SDK（libWeWorkFinanceSdk）以及开放接口，
提供消息拉取、解密、查询、群资料获取等核心能力。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import requests
from ctypes import c_char_p, c_int, c_uint, c_ulonglong, c_void_p
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from dotenv import load_dotenv

# 在模块加载时读取项目根目录的 .env，便于在 CLI / 测试中直接 import 使用
_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
WECOM_API_BASE = "https://qyapi.weixin.qq.com"
ACCESS_TOKEN_REFRESH_MARGIN = 300  # 秒，提前这么多秒刷新 token
DEFAULT_HTTP_TIMEOUT = 10  # 秒
DEFAULT_FETCH_LIMIT = 100
MAX_FETCH_LIMIT = 1000
DEFAULT_SDK_TIMEOUT = 30
R2_PRESIGN_EXPIRES = 3600  # 秒
R2_IMAGE_KEY_PREFIX = "wecom/images/"

_R2_REGION = "auto"  # Cloudflare R2 使用 auto 即可

_IMAGE_MIME_BY_MAGIC: list[tuple[bytes, str, str]] = [
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
_R2_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

# 文件后缀 -> MIME（按需扩展）
_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".amr": "audio/amr",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


# ----------------------------------------------------------------------------
# R2 配置
# ----------------------------------------------------------------------------
@dataclass
class R2Config:
    """Cloudflare R2（S3 兼容）配置。"""

    endpoint: str = ""
    bucket: str = ""
    custom_domain: str = ""
    s3_id: str = ""
    s3_key: str = ""

    @classmethod
    def from_env(cls) -> R2Config:
        endpoint = os.getenv("R2_ENDPOINT", "").rstrip("/")
        bucket = os.getenv("R2_BUCKET", "")
        base_endpoint = endpoint

        if endpoint:
            parsed = urlparse(endpoint)
            path_bucket = parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else ""
            if path_bucket:
                bucket = bucket or path_bucket
                base_endpoint = f"{parsed.scheme}://{parsed.netloc}"

        return cls(
            endpoint=base_endpoint,
            bucket=bucket,
            custom_domain=os.getenv("R2_CUSTOM_DOMAIN", "").rstrip("/"),
            s3_id=os.getenv("R2_S3_ID", ""),
            s3_key=os.getenv("R2_S3_KEY", ""),
        )

    def is_configured(self) -> bool:
        return bool(self.endpoint and self.bucket and self.s3_id and self.s3_key)


def _r2_client(config: R2Config):
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        aws_access_key_id=config.s3_id,
        aws_secret_access_key=config.s3_key,
        region_name=_R2_REGION,
    )


def _detect_image_mime(data: bytes) -> tuple[str, str]:
    """根据魔术字节嗅探图片 MIME 与扩展名。"""
    for magic, mime, ext in _IMAGE_MIME_BY_MAGIC:
        if data[: len(magic)] == magic:
            return mime, ext

    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[:2] == b"BM":
        return "image/bmp", ".bmp"
    return "image/jpeg", ".jpg"


def upload_to_r2_and_presign(
    data: bytes,
    *,
    key: str,
    content_type: str,
    config: R2Config | None = None,
    expires_in: int = R2_PRESIGN_EXPIRES,
) -> str:
    """上传对象到 R2 并返回（可选用自定义域名替换的）预签名 URL。"""
    r2 = config or R2Config.from_env()
    if not r2.is_configured():
        raise RuntimeError(
            "R2 未配置，请在 .env 中设置 R2_ENDPOINT、R2_BUCKET、R2_S3_ID、R2_S3_KEY"
        )

    client = _r2_client(r2)
    client.put_object(Bucket=r2.bucket, Key=key, Body=data, ContentType=content_type)
    presigned = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": r2.bucket, "Key": key},
        ExpiresIn=expires_in,
    )

    if r2.custom_domain:
        parsed = urlparse(presigned)
        presigned = f"https://{r2.custom_domain}{parsed.path}?{parsed.query}"
    return presigned


# ----------------------------------------------------------------------------
# 主配置
# ----------------------------------------------------------------------------
@dataclass
class WeComConfig:
    """企业微信会话存档配置（从 .env / 环境变量加载）。"""

    corp_id: str = ""
    msgaudit_secret: str = ""
    external_contact_secret: str = ""
    sdk_lib_path: str = "./libWeWorkFinanceSdk_C.so"
    private_key_path: str = "./private_key.pem"
    seq_file: str = "./wecom_msg_seq.txt"
    output_jsonl: str = "./wecom_messages.jsonl"
    fetch_limit: int = DEFAULT_FETCH_LIMIT
    sdk_timeout: int = DEFAULT_SDK_TIMEOUT
    proxy: str = ""
    proxy_password: str = ""

    @classmethod
    def from_env(cls) -> WeComConfig:
        return cls(
            corp_id=os.getenv("CORP_ID", ""),
            msgaudit_secret=os.getenv("MSGAUDIT_SECRET", ""),
            external_contact_secret=os.getenv("EXTERNAL_CONTACT_SECRET", ""),
            sdk_lib_path=os.getenv("SDK_LIB_PATH", cls.sdk_lib_path),
            private_key_path=os.getenv("PRIVATE_KEY_PATH", cls.private_key_path),
            seq_file=os.getenv("SEQ_FILE", cls.seq_file),
            output_jsonl=os.getenv("OUTPUT_JSONL", cls.output_jsonl),
            fetch_limit=int(os.getenv("FETCH_LIMIT", str(DEFAULT_FETCH_LIMIT))),
            sdk_timeout=int(os.getenv("SDK_TIMEOUT", str(DEFAULT_SDK_TIMEOUT))),
            proxy=os.getenv("PROXY", ""),
            proxy_password=os.getenv("PROXY_PASSWORD", ""),
        )

    def with_overrides(self, **kwargs: Any) -> WeComConfig:
        """返回带有部分字段覆盖的副本。"""
        return replace(self, **{k: v for k, v in kwargs.items() if v is not None})

    def validate_for_pull(self) -> list[str]:
        missing: list[str] = []
        if not self.corp_id:
            missing.append("企业 ID")
        if not self.msgaudit_secret:
            missing.append("会话内容存档密钥")
        if not Path(self.private_key_path).exists():
            missing.append("解密私钥")
        if not Path(self.sdk_lib_path).exists():
            missing.append("会话存档 SDK")
        return missing

    def status(self) -> dict[str, Any]:
        return {
            "corp_id_set": bool(self.corp_id),
            "msgaudit_secret_set": bool(self.msgaudit_secret),
            "external_contact_secret_set": bool(self.external_contact_secret),
            "private_key_exists": Path(self.private_key_path).exists(),
            "sdk_lib_exists": Path(self.sdk_lib_path).exists(),
            "seq_file": self.seq_file,
            "current_seq": load_seq(self.seq_file),
            "output_jsonl": self.output_jsonl,
            "output_jsonl_exists": Path(self.output_jsonl).exists(),
            "message_count": count_jsonl_lines(self.output_jsonl),
        }

    def client_status(self) -> dict[str, Any]:
        """面向 MCP 调用方的就绪状态，不暴露内部配置细节。"""
        missing = self.validate_for_pull()
        return {
            "ready": not missing,
            "ready_for_pull": not missing,
            "ready_for_internal_groups": bool(self.corp_id and self.msgaudit_secret),
            "ready_for_external_groups": bool(
                self.corp_id and self.external_contact_secret
            ),
            "current_seq": load_seq(self.seq_file),
            "message_count": count_jsonl_lines(self.output_jsonl),
        }


# ----------------------------------------------------------------------------
# RSA 解密
# ----------------------------------------------------------------------------
def rsa_decrypt_encrypt_random_key(
    encrypt_random_key: str, private_key_path: str
) -> bytes:
    """用 RSA 私钥解密会话存档 SDK 返回的 encrypt_random_key。"""
    with open(private_key_path, "rb") as f:
        private_key = RSA.import_key(f.read())

    cipher = PKCS1_v1_5.new(private_key)
    encrypted = base64.b64decode(encrypt_random_key)
    sentinel = b"__RSA_DECRYPT_FAILED__"
    decrypted = cipher.decrypt(encrypted, sentinel)
    if decrypted == sentinel:
        raise RuntimeError(
            "RSA 解密 encrypt_random_key 失败，请检查私钥与公钥版本是否匹配。"
        )
    return decrypted


# ----------------------------------------------------------------------------
# 官方 SDK 封装（ctypes）
# ----------------------------------------------------------------------------
class WeWorkFinanceSDK:
    """企业微信官方 libWeWorkFinanceSdk_C 的轻量封装。"""

    def __init__(self, lib_path: str, corp_id: str, secret: str):
        if not Path(lib_path).exists():
            raise FileNotFoundError(f"SDK 动态库不存在: {lib_path}")

        self.lib = ctypes.cdll.LoadLibrary(lib_path)
        self._bind_functions()

        self.sdk = self.lib.NewSdk()
        if not self.sdk:
            raise RuntimeError("NewSdk 失败")

        ret = self.lib.Init(
            self.sdk,
            corp_id.encode("utf-8"),
            secret.encode("utf-8"),
        )
        if ret != 0:
            raise RuntimeError(f"SDK Init 失败，ret={ret}")

    def _bind_functions(self) -> None:
        lib = self.lib
        lib.NewSdk.restype = c_void_p

        lib.DestroySdk.argtypes = [c_void_p]
        lib.DestroySdk.restype = None

        lib.Init.argtypes = [c_void_p, c_char_p, c_char_p]
        lib.Init.restype = c_int

        lib.NewSlice.restype = c_void_p

        lib.FreeSlice.argtypes = [c_void_p]
        lib.FreeSlice.restype = None

        lib.GetContentFromSlice.argtypes = [c_void_p]
        lib.GetContentFromSlice.restype = c_char_p

        lib.GetChatData.argtypes = [
            c_void_p, c_ulonglong, c_uint, c_char_p, c_char_p, c_int, c_void_p,
        ]
        lib.GetChatData.restype = c_int

        lib.DecryptData.argtypes = [c_char_p, c_char_p, c_void_p]
        lib.DecryptData.restype = c_int

        lib.GetMediaData.argtypes = [
            c_char_p, c_char_p, c_char_p, c_char_p, c_int, c_void_p,
        ]
        lib.GetMediaData.restype = c_int

    def close(self) -> None:
        if getattr(self, "sdk", None):
            self.lib.DestroySdk(self.sdk)
            self.sdk = None

    def get_chat_data(
        self,
        seq: int,
        limit: int,
        timeout: int = DEFAULT_SDK_TIMEOUT,
        proxy: str = "",
        passwd: str = "",
    ) -> dict:
        """拉取一段加密聊天记录。"""
        chat_datas = self.lib.NewSlice()
        if not chat_datas:
            raise RuntimeError("NewSlice 失败")

        try:
            ret = self.lib.GetChatData(
                self.sdk,
                c_ulonglong(seq),
                c_uint(limit),
                proxy.encode("utf-8") if proxy else b"",
                passwd.encode("utf-8") if passwd else b"",
                c_int(timeout),
                chat_datas,
            )
            if ret != 0:
                raise RuntimeError(f"GetChatData 调用失败，ret={ret}")

            raw = self.lib.GetContentFromSlice(chat_datas)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
        finally:
            self.lib.FreeSlice(chat_datas)

    def decrypt_data(self, encrypt_key: bytes, encrypt_chat_msg: str) -> dict:
        """用 encrypt_key 解密单条密文，返回明文 JSON。"""
        msg = self.lib.NewSlice()
        if not msg:
            raise RuntimeError("NewSlice 失败")

        try:
            ret = self.lib.DecryptData(encrypt_key, encrypt_chat_msg.encode("utf-8"), msg)
            if ret != 0:
                raise RuntimeError(f"DecryptData 调用失败，ret={ret}")

            raw = self.lib.GetContentFromSlice(msg)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
        finally:
            self.lib.FreeSlice(msg)

    def get_media_data(
        self,
        sdkfileid: str,
        timeout: int = DEFAULT_SDK_TIMEOUT,
        proxy: str = "",
        passwd: str = "",
    ) -> bytes:
        """通过 sdkfileid 拉取媒体二进制。"""
        index_buf = self.lib.NewSlice()
        if not index_buf:
            raise RuntimeError("NewSlice 失败")

        try:
            ret = self.lib.GetMediaData(
                b"",
                proxy.encode("utf-8") if proxy else b"",
                passwd.encode("utf-8") if passwd else b"",
                sdkfileid.encode("utf-8"),
                c_int(timeout),
                index_buf,
            )
            if ret != 0:
                raise RuntimeError(f"GetMediaData 调用失败，ret={ret}")

            raw = self.lib.GetContentFromSlice(index_buf)
            if not raw:
                raise RuntimeError("GetMediaData 返回空数据")
            return raw
        finally:
            self.lib.FreeSlice(index_buf)


# ----------------------------------------------------------------------------
# 开放接口客户端
# ----------------------------------------------------------------------------
class WeComAPIClient:
    """企业微信开放接口的轻量 HTTP 客户端，自动管理 access_token。"""

    def __init__(self, corp_id: str, secret: str):
        self.corp_id = corp_id
        self.secret = secret
        self.session = requests.Session()
        self.access_token: str | None = None
        self.expire_at = 0

    def get_access_token(self) -> str:
        now = int(time.time())
        if self.access_token and now < self.expire_at - ACCESS_TOKEN_REFRESH_MARGIN:
            return self.access_token

        resp = self.session.get(
            f"{WECOM_API_BASE}/cgi-bin/gettoken",
            params={"corpid": self.corp_id, "corpsecret": self.secret},
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"获取 access_token 失败: {data}")

        self.access_token = data["access_token"]
        self.expire_at = now + int(data.get("expires_in", 7200))
        return self.access_token

    def post(self, path: str, payload: dict) -> dict:
        resp = self.session.post(
            f"{WECOM_API_BASE}{path}",
            params={"access_token": self.get_access_token()},
            json=payload,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        return resp.json()


# ----------------------------------------------------------------------------
# 本地消息文件辅助
# ----------------------------------------------------------------------------
def load_seq(seq_file: str) -> int:
    path = Path(seq_file)
    if not path.exists():
        return 0
    content = path.read_text(encoding="utf-8").strip()
    return int(content) if content else 0


def save_seq(seq_file: str, seq: int) -> None:
    Path(seq_file).write_text(str(seq), encoding="utf-8")


def append_jsonl(path: str, data: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False))
        f.write("\n")


def count_jsonl_lines(path: str) -> int:
    path_obj = Path(path)
    if not path_obj.exists():
        return 0
    with open(path_obj, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def extract_sdkfileids(message: dict) -> list[str]:
    """递归从消息 JSON 中收集所有 sdkfileid。"""

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "sdkfileid" and isinstance(v, str):
                    sdkfileids.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    sdkfileids: list[str] = []
    walk(message)
    return sdkfileids


def decrypt_one_chat_item(
    item: dict, private_key_path: str, sdk: WeWorkFinanceSDK
) -> dict:
    encrypt_key = rsa_decrypt_encrypt_random_key(
        encrypt_random_key=item["encrypt_random_key"],
        private_key_path=private_key_path,
    )
    plain_msg = sdk.decrypt_data(
        encrypt_key=encrypt_key,
        encrypt_chat_msg=item["encrypt_chat_msg"],
    )
    return {
        "seq": item.get("seq"),
        "msgid": item.get("msgid"),
        "publickey_ver": item.get("publickey_ver"),
        "message": plain_msg,
        "sdkfileids": extract_sdkfileids(plain_msg),
    }


# ----------------------------------------------------------------------------
# 业务：拉取消息
# ----------------------------------------------------------------------------
@dataclass
class PullResult:
    start_seq: int
    end_seq: int
    success_count: int
    fail_count: int
    messages: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def pull_messages(
    config: WeComConfig,
    *,
    limit: int | None = None,
    save_to_file: bool = True,
) -> PullResult:
    missing = config.validate_for_pull()
    if missing:
        raise RuntimeError("缺少必要配置：\n" + "\n".join(f"  - {m}" for m in missing))

    fetch_limit = min(limit or config.fetch_limit, MAX_FETCH_LIMIT)
    current_seq = load_seq(config.seq_file)

    sdk = WeWorkFinanceSDK(
        lib_path=config.sdk_lib_path,
        corp_id=config.corp_id,
        secret=config.msgaudit_secret,
    )
    try:
        resp = sdk.get_chat_data(
            seq=current_seq,
            limit=fetch_limit,
            timeout=config.sdk_timeout,
            proxy=config.proxy,
            passwd=config.proxy_password,
        )

        if not resp or resp.get("errcode") != 0:
            if resp and resp.get("errcode") != 0:
                raise RuntimeError(f"GetChatData 返回错误: {resp}")
            return PullResult(
                start_seq=current_seq,
                end_seq=current_seq,
                success_count=0,
                fail_count=0,
            )

        chatdata = resp.get("chatdata", [])
        if not chatdata:
            return PullResult(
                start_seq=current_seq,
                end_seq=current_seq,
                success_count=0,
                fail_count=0,
            )

        max_seq = current_seq
        success_count = 0
        fail_count = 0
        messages: list[dict] = []
        errors: list[dict] = []

        for item in chatdata:
            item_seq = int(item.get("seq", 0))
            max_seq = max(max_seq, item_seq)
            try:
                decrypted = decrypt_one_chat_item(
                    item=item,
                    private_key_path=config.private_key_path,
                    sdk=sdk,
                )
                success_count += 1
                messages.append(decrypted)
                if save_to_file and config.output_jsonl:
                    append_jsonl(config.output_jsonl, decrypted)
            except Exception as e:
                fail_count += 1
                errors.append(
                    {
                        "seq": item_seq,
                        "msgid": item.get("msgid"),
                        "error": str(e),
                    }
                )

        save_seq(config.seq_file, max_seq)
        return PullResult(
            start_seq=current_seq,
            end_seq=max_seq,
            success_count=success_count,
            fail_count=fail_count,
            messages=messages,
            errors=errors,
        )
    finally:
        sdk.close()


# ----------------------------------------------------------------------------
# 业务：媒体下载
# ----------------------------------------------------------------------------
def _is_image_data(data: bytes, sdkfileid: str) -> bool:
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return True
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if len(data) >= 2 and data[:2] == b"BM":
        return True

    guessed, _ = mimetypes.guess_type(sdkfileid)
    if guessed and guessed.startswith("image/"):
        return True
    return any(sdkfileid.endswith(ext) for ext in _IMAGE_EXTS)


def download_media(
    config: WeComConfig,
    sdkfileid: str,
    save_to: str | None = None,
) -> str:
    missing = config.validate_for_pull()
    if missing:
        raise RuntimeError("缺少必要配置：\n" + "\n".join(f"  - {m}" for m in missing))

    sdk = WeWorkFinanceSDK(
        lib_path=config.sdk_lib_path,
        corp_id=config.corp_id,
        secret=config.msgaudit_secret,
    )
    try:
        data = sdk.get_media_data(
            sdkfileid=sdkfileid,
            timeout=config.sdk_timeout,
            proxy=config.proxy,
            passwd=config.proxy_password,
        )

        size = len(data)
        size_str = f"{size / 1024:.1f}KB" if size >= 1024 else f"{size}B"
        lines = ["下载成功", f"sdkfileid: {sdkfileid}", f"大小: {size_str}"]

        if save_to:
            out_path = Path(save_to)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            ext = out_path.suffix.lower()
            mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
            lines.append(f"类型: {mime}")
            lines.append(f"已保存到: {out_path}")
            return "\n".join(lines)

        if _is_image_data(data, sdkfileid):
            mime, ext = _detect_image_mime(data)
            file_hash = hashlib.sha256(data).hexdigest()[:16]
            object_key = f"{R2_IMAGE_KEY_PREFIX}{file_hash}{ext}"
            url = upload_to_r2_and_presign(data, key=object_key, content_type=mime)
            lines.append(f"类型: {mime}")
            lines.append(
                f"访问链接（R2 预签名，有效期 {R2_PRESIGN_EXPIRES // 3600} 小时）: {url}"
            )
            lines.append(
                "用法说明: 将上述链接在浏览器中打开即可查看图片；"
                "链接过期后需重新调用本工具获取新链接"
            )
            return "\n".join(lines)

        ext = ".bin"
        for e in _R2_IMAGE_EXTS:
            if sdkfileid.endswith(e):
                ext = e
                break
        out_path = Path(config.output_jsonl).parent / f"media_{sdkfileid[:16]}{ext}"
        out_path.write_bytes(data)
        mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
        lines.append(f"类型: {mime}")
        lines.append(f"已保存到本地: {out_path}")
        lines.append("用法说明: 非图片文件默认保存到本地路径，可用 save_to 参数指定保存位置")
        return "\n".join(lines)
    finally:
        sdk.close()


# ----------------------------------------------------------------------------
# 业务：本地消息查询 / 群 roomid
# ----------------------------------------------------------------------------
def load_roomids_from_messages(path: str) -> set[str]:
    roomids: set[str] = set()
    path_obj = Path(path)
    if not path_obj.exists():
        return roomids

    with open(path_obj, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            roomid = row.get("message", {}).get("roomid")
            if roomid:
                roomids.add(roomid)
    return roomids


def query_messages(
    path: str,
    *,
    roomid: str | None = None,
    from_user: str | None = None,
    msgtype: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    path_obj = Path(path)
    if not path_obj.exists():
        return {"total": 0, "messages": [], "offset": offset, "limit": limit}

    matched: list[dict] = []
    with open(path_obj, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            msg = row.get("message", {})

            if roomid and msg.get("roomid") != roomid:
                continue
            if from_user and msg.get("from") != from_user:
                continue
            if msgtype and msg.get("msgtype") != msgtype:
                continue
            if keyword:
                text = json.dumps(row, ensure_ascii=False)
                if keyword not in text:
                    continue

            matched.append(row)

    total = len(matched)
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "messages": matched[offset : offset + limit],
    }


# ----------------------------------------------------------------------------
# 业务：群资料
# ----------------------------------------------------------------------------
def get_external_group_info(config: WeComConfig, roomid: str) -> dict[str, Any]:
    if not config.corp_id:
        raise RuntimeError("服务未就绪：缺少企业身份")
    if not config.external_contact_secret:
        raise RuntimeError("服务未就绪：未开通客户联系能力")

    client = WeComAPIClient(config.corp_id, config.external_contact_secret)
    return client.post(
        "/cgi-bin/externalcontact/groupchat/get",
        {"chat_id": roomid, "need_name": 1},
    )


def get_internal_group_info(config: WeComConfig, roomid: str) -> dict[str, Any]:
    if not config.corp_id:
        raise RuntimeError("服务未就绪：缺少企业身份")
    if not config.msgaudit_secret:
        raise RuntimeError("服务未就绪：未开通会话存档能力")

    client = WeComAPIClient(config.corp_id, config.msgaudit_secret)
    return client.post("/cgi-bin/msgaudit/groupchat/get", {"roomid": roomid})


def list_external_groups(config: WeComConfig, limit: int = 100) -> list[dict]:
    if not config.corp_id:
        raise RuntimeError("服务未就绪：缺少企业身份")
    if not config.external_contact_secret:
        raise RuntimeError("服务未就绪：未开通客户联系能力")

    client = WeComAPIClient(config.corp_id, config.external_contact_secret)
    groups: list[dict] = []
    cursor = ""

    while True:
        data = client.post(
            "/cgi-bin/externalcontact/groupchat/list",
            {
                "status_filter": 0,
                "owner_filter": {"userid_list": []},
                "cursor": cursor,
                "limit": min(limit, 1000),
            },
        )
        if data.get("errcode") != 0:
            raise RuntimeError(f"获取客户群列表失败: {data}")

        groups.extend(data.get("group_chat_list", []))
        cursor = data.get("next_cursor", "")
        if not cursor or len(groups) >= limit:
            break

    return groups[:limit]
