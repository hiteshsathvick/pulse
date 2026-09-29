import asyncio
import io

from minio import Minio

from pulse.core.config import get_settings

_client: Minio | None = None


def get_client() -> Minio:
    """Minio's client is sync (no async SDK) -- construction itself is cheap
    and non-blocking, so this stays a plain function like the other repos'
    get_client(); actual I/O calls go through asyncio.to_thread at the
    call site instead."""
    global _client
    if _client is None:
        settings = get_settings()
        endpoint = settings.s3_endpoint_url.removeprefix("https://").removeprefix("http://")
        _client = Minio(
            endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_endpoint_url.startswith("https://"),
            region=settings.s3_region,
        )
    return _client


async def check_connection() -> bool:
    """Proves the object store is reachable with a single bounded LIST call.
    Deliberately not `bucket_exists` (a HeadBucket request): confirmed live
    (Phase 28) that a bucket-scoped B2 Application Key -- the credential this
    app is actually meant to run with, not a master key -- returns
    AccessDenied for it even when fully functional for every operation this
    app actually performs (list/put/get/remove). `next(..., None)` pulls
    exactly one page of the generator MinIO's SDK returns, so this costs one
    request regardless of how many objects the bucket holds."""
    settings = get_settings()
    client = get_client()

    def _check() -> bool:
        next(client.list_objects(settings.s3_bucket, recursive=True), None)
        return True

    return await asyncio.to_thread(_check)


async def ensure_bucket() -> None:
    """Idempotent, like alembic/env.py's _ensure_app_role_exists -- called on
    worker startup so a fresh MinIO instance doesn't need manual setup."""
    settings = get_settings()
    client = get_client()

    def _ensure() -> None:
        if not client.bucket_exists(settings.s3_bucket):
            client.make_bucket(settings.s3_bucket)

    await asyncio.to_thread(_ensure)


async def put_object(key: str, data: bytes, content_type: str = "application/json") -> None:
    """Archives one object. Run in a thread so a slow object-storage write
    doesn't stall the worker's Redis Streams consume loop."""
    settings = get_settings()
    client = get_client()

    await asyncio.to_thread(
        client.put_object,
        settings.s3_bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


async def list_keys(prefix: str) -> list[str]:
    """Every object key under `prefix`, recursively. Listing is paginated by the
    SDK; it is drained fully here because callers (subject erasure) must see
    every object, not a first page."""
    settings = get_settings()
    client = get_client()

    def _list() -> list[str]:
        return [
            obj.object_name
            for obj in client.list_objects(settings.s3_bucket, prefix=prefix, recursive=True)
            if obj.object_name
        ]

    return await asyncio.to_thread(_list)


async def list_prefixes(prefix: str) -> list[str]:
    """The immediate "directories" under `prefix` (non-recursive), each ending in
    "/". Lets a caller walk only the branches it cares about."""
    settings = get_settings()
    client = get_client()

    def _list() -> list[str]:
        return [
            obj.object_name
            for obj in client.list_objects(settings.s3_bucket, prefix=prefix, recursive=False)
            if obj.object_name and obj.is_dir
        ]

    return await asyncio.to_thread(_list)


async def get_bytes(key: str) -> bytes:
    settings = get_settings()
    client = get_client()

    def _get() -> bytes:
        response = client.get_object(settings.s3_bucket, key)
        try:
            return bytes(response.read())
        finally:
            response.close()
            response.release_conn()

    return await asyncio.to_thread(_get)


async def remove_object(key: str) -> None:
    settings = get_settings()
    await asyncio.to_thread(get_client().remove_object, settings.s3_bucket, key)


def close() -> None:
    global _client
    _client = None
