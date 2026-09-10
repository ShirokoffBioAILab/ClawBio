"""Allow-listed boto3 S3 client, scoped to one run's own inputs and outputs.

This skill holds S3 credentials because a HealthOmics run is not finished when
it stops: its inputs have to get to S3 and its outputs live there afterwards,
and there is no HealthOmics API that can read them — the service exposes zero
output-listing operations. Verifying what a run produced therefore requires S3
or it is not possible at all.

What is deliberately NOT here: this is not a general S3 tool. It moves a run's
inputs in and a run's outputs out. It creates no buckets, sets no bucket or
object policies, and deletes nothing; those are barred by name below and no
flag unlocks them.

ONE HONEST DIFFERENCE FROM ``omics_client.py``. That module's allowlist gates
botocore *operation* names, and can do so exactly because every call it makes
is one API operation. Genomic files are large enough to need multipart, so
transfers here go through boto3's managed helpers (``upload_file`` /
``download_file``), and a single managed transfer fans out into many underlying
operations — CreateMultipartUpload, UploadPart, CompleteMultipartUpload. This
allowlist therefore gates at *method* level. Describing it as operation-level
control would be a lie; the guarantee it actually provides is that no method
outside the set below is reachable through this client.
"""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
from typing import Any, Protocol

# The only S3 methods this skill may call, all scoped to a run's own I/O.
ALLOWED_S3_METHODS = frozenset(
    {
        "head_object",      # inspect an explicitly supplied run input
        "list_objects_v2",  # enumerate a run's output prefix
        "upload_file",      # managed multipart upload of a run input
        "download_file",    # managed multipart download of a run output
    }
)

# Named rather than pattern-matched, so a reader sees *why* each is barred.
PERMANENTLY_EXCLUDED: dict[str, str] = {
    "delete_object": "destroys data this skill cannot restore",
    "delete_objects": "destroys data this skill cannot restore",
    "delete_bucket": "destroys data this skill cannot restore",
    "create_bucket": "storage administration, a different job",
    "put_bucket_policy": "changes a permission — beyond one run's blast radius",
    "put_bucket_acl": "changes a permission — beyond one run's blast radius",
    "put_object_acl": "changes a permission — beyond one run's blast radius",
    "put_bucket_versioning": "mutates shared bucket configuration",
    "put_bucket_lifecycle_configuration": "mutates shared bucket configuration",
}


class S3CallError(RuntimeError):
    """An S3 call failed or was refused."""


class S3MethodNotAllowed(S3CallError):
    """A method outside ALLOWED_S3_METHODS was requested and refused."""


class S3Client(Protocol):
    """The narrow surface this skill's own functions depend on.

    A Protocol so every offline test substitutes a recording fake and never
    constructs a real client or reads a credential.
    """

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any: ...


class S3Operations:
    """Allow-listed wrapper over a boto3 S3 client.

    The allowlist is checked BEFORE dispatch, so a refused method never reaches
    AWS even when the underlying client is live.
    """

    def __init__(self, *, _boto: Any) -> None:
        self._boto = _boto

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method not in ALLOWED_S3_METHODS:
            reason = PERMANENTLY_EXCLUDED.get(method)
            if reason:
                raise S3MethodNotAllowed(f"{method} is refused: {reason}.")
            raise S3MethodNotAllowed(
                f"{method} is not in this skill's S3 allowlist. healthomics-bridge "
                f"may call only: {', '.join(sorted(ALLOWED_S3_METHODS))}."
            )
        return getattr(self._boto, method)(*args, **kwargs)


def build_s3_client(region: str | None = None, profile: str | None = None) -> Any:
    """Create the underlying boto3 S3 client.

    Imported lazily so the offline demo runs with no cloud dependency at all.
    Credentials are never read, stored or forwarded by this skill — boto3
    resolves them itself from the standard chain.
    """
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised by absence
        raise S3CallError(
            "Live S3 transfers need boto3. Install it with `uv pip install boto3`. "
            "The offline demo (--demo) needs no cloud dependency at all."
        ) from exc

    from botocore.config import Config

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client(
        "s3",
        region_name=region,
        config=Config(retries={"mode": "adaptive", "max_attempts": 10}),
    )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key`` into its parts.

    Refuses anything else loudly: a ``gs://`` or ``https://`` destination is a
    sign the caller believes this skill talks to something it does not.
    """
    if not isinstance(uri, str) or not uri.startswith("s3://"):
        raise ValueError(f"Not an s3:// URI: {uri!r}. This skill speaks only to S3.")
    remainder = uri[len("s3://"):]
    if not remainder:
        raise ValueError(f"No bucket in URI: {uri!r}")
    bucket, _, key = remainder.partition("/")
    if not bucket:
        raise ValueError(f"No bucket in URI: {uri!r}")
    return bucket, key


def inspect_run_paths(*, client: S3Client, params: dict[str, Any], output_uri: str,
                      file_uris: set[str] | None = None) -> list[dict[str, Any]]:
    """Caller-visible metadata only; never claim this proves role write access."""
    from preflight import _walk_strings, check
    from ecr_client import exception_code

    checks = []
    try:
        bucket, prefix = parse_s3_uri(output_uri)
        client.call("list_objects_v2", Bucket=bucket, Prefix=prefix, MaxKeys=1)
        checks.append(check("output_prefix_listing", "PASS", "Caller can list the output prefix; write access is unverified.", required=False))
    except Exception as exc:
        checks.append(check("output_prefix_listing", "UNKNOWN", f"Output inspection: {exception_code(exc)}; caller listing is not execution-role access.", required=False))
    file_uris = file_uris or set()
    for uri in sorted({v for v in _walk_strings(params) if v.startswith("s3://")} | file_uris):
        required = uri in file_uris
        try:
            if required and not uri.startswith("s3://"):
                checks.append(check("input_metadata", "UNKNOWN", f"{uri}: typed File uses unsupported storage URI."))
                continue
            bucket, key = parse_s3_uri(uri)
            if not key or key.endswith("/"):
                checks.append(check("input_metadata", "FAIL" if required else "UNKNOWN", f"{uri}: prefix input, object existence not checked.", required=required))
                continue
            client.call("head_object", Bucket=bucket, Key=key)
            checks.append(check("input_metadata", "PASS", f"{uri}: caller can read object metadata.", required=required))
        except Exception as exc:
            code = exception_code(exc)
            absent = code in {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}
            checks.append(check("input_metadata", "FAIL" if required and absent else "UNKNOWN",
                                f"{uri}: {code}; {'required File missing' if required and absent else 'access/existence unverified'}.", required=required))
    return checks


def describe_etag(etag: str) -> dict[str, Any]:
    """Say what an S3 ETag actually is, rather than implying it is a checksum.

    A single-part ETag alone does not prove MD5 (SSE-KMS/SSE-C differ). For a multipart
    upload it is the MD5 of the concatenated part MD5s, suffixed ``-<n>``,
    which hashes nothing the caller can recompute from the file. Genomic
    outputs are routinely multipart, so labelling this a checksum would put a
    guarantee in the repro bundle that does not hold.
    """
    cleaned = (etag or "").strip('"')
    if "-" in cleaned:
        _, _, count = cleaned.rpartition("-")
        try:
            parts = int(count)
        except ValueError:
            parts = 0
        return {
            "etag": cleaned,
            "is_md5": False,
            "parts": parts,
            "note": (
                "multipart ETag — the MD5 of the concatenated part MD5s, not a "
                "checksum of the object's bytes and not recomputable locally"
            ),
        }
    return {
        "etag": cleaned,
        "is_md5": False,
        "parts": 1,
        "note": "opaque ETag: encryption/upload evidence unavailable; not a verified MD5",
    }


def list_objects(*, client: S3Client, uri: str) -> list[dict[str, Any]]:
    """Every object under an ``s3://`` prefix, following continuation tokens.

    S3 truncates at 1,000 keys and returns a continuation token; a run with
    more outputs than that would otherwise be silently under-reported, which is
    exactly the failure mode this skill exists to avoid elsewhere.
    """
    bucket, prefix = parse_s3_uri(uri)
    objects: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.call("list_objects_v2", **kwargs)
        for item in response.get("Contents", []) or []:
            key = item.get("Key", "")
            # S3 has no directories; consoles and some SDKs fake them with
            # zero-byte keys ending in "/". They are not outputs. A live deep
            # verification counted one as an output that failed to download,
            # so a complete run reported itself incomplete. The trailing slash
            # is the test, not the size -- an empty output FILE is a real and
            # often meaningful result.
            if key.endswith("/") and item.get("Size", 0) == 0:
                continue
            objects.append(
                {
                    "key": key,
                    "size": item.get("Size", 0),
                    "etag": str(item.get("ETag", "")).strip('"'),
                    "last_modified": str(item.get("LastModified", "")),
                }
            )
        token = response.get("NextContinuationToken")
        if not token:
            break
    return objects


def upload_files(
    *, client: S3Client, sources: list[Path], destination: str
) -> dict[str, Any]:
    """Upload each source into the destination prefix, under its own basename.

    Every source is checked to exist BEFORE anything transfers: a half-uploaded
    input set is worse than a refused one, because the run that reads it fails
    somewhere inside AWS for a reason invisible here.
    """
    bucket, prefix = parse_s3_uri(destination)
    resolved = [Path(s).expanduser().resolve() for s in sources]
    missing = [str(p) for p in resolved if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            "Nothing was uploaded. These sources do not exist as files: "
            + ", ".join(missing)
        )

    base = prefix.rstrip("/")
    if len({p.name for p in resolved}) != len(resolved):
        raise ValueError("Duplicate source basenames would overwrite an input; nothing uploaded.")
    uploaded: list[dict[str, Any]] = []
    for source in resolved:
        key = f"{base}/{source.name}" if base else source.name
        client.call("upload_file", Filename=str(source), Bucket=bucket, Key=key)
        uploaded.append(
            {"source": str(source), "key": key, "uri": f"s3://{bucket}/{key}",
             "n_bytes": source.stat().st_size}
        )

    return {
        "bucket": bucket,
        "destination": destination,
        # Named *_files, not "uploaded": the caller adds a boolean "uploaded"
        # status alongside, and one key cannot honestly be both.
        "uploaded_files": uploaded,
        "uris": [u["uri"] for u in uploaded],
        "n_uploaded": len(uploaded),
        "n_bytes": sum(u["n_bytes"] for u in uploaded),
    }


def download_objects(
    *, client: S3Client, bucket: str, objects: list[dict[str, Any]],
    key_prefix: str, destination: Path,
) -> dict[str, Any]:
    """Download listed objects, preserving their layout under ``key_prefix``.

    A per-object failure is collected rather than raised, so one unreadable
    object does not hide the rest — and the caller can compare what landed
    against what was listed. ``write_checksums`` cannot do that comparison for
    us: it silently skips paths that do not exist.
    """
    destination = Path(destination).expanduser().resolve()
    downloaded: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for item in objects:
        key = item["key"]
        temporary = None
        try:
            if not key.startswith(key_prefix):
                raise ValueError("Object is outside the selected run prefix")
            relative = key[len(key_prefix):]
            if not relative or relative.endswith("/"):
                continue
            if any(p in ("", ".", "..") for p in relative.split("/")) or "\\" in relative:
                raise ValueError("Unsafe object path")
            target = destination / relative
            if not target.resolve().is_relative_to(destination):
                raise ValueError("Object path escapes destination")
            if any(p.is_symlink() for p in (target, *target.parents) if p.is_relative_to(destination)):
                raise ValueError("Symlink in output path")
            if target.exists():
                raise FileExistsError("Output already exists; choose a fresh destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".healthomics-", delete=False) as handle:
                temporary = Path(handle.name)
            client.call("download_file", Bucket=bucket, Key=key, Filename=str(temporary))
            # Publish complete bytes atomically without overwriting an existing file.
            os.link(temporary, target)
        except Exception as exc:
            failures.append({"key": key, "error": str(exc)})
            continue
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        downloaded.append({"key": key, "path": str(target), "etag": item.get("etag", "")})

    return {
        "destination": str(destination),
        "downloaded_files": downloaded,
        "failures": failures,
        "n_downloaded": len(downloaded),
        "n_failed": len(failures),
    }
