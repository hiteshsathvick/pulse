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


def close() -> None:
    global _client
    _client = None
