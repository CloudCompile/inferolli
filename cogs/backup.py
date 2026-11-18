"""Nightly brain backup to S3 (InferencePort bucket storage).

After dreaming, snapshot data/ to the S3 bucket: dream, then archive.
Keys: backups/<date-hour>/data.tar.gz and latest/data.tar.gz.
Restores happen at sandbox boot (start.sh). Objects older than 30 days
are pruned. Usable manually via /backupnow.
"""

import asyncio
import datetime
import io
import logging
import os
import tarfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("cogs.backup")

DATA_DIR = Path(__file__).parent.parent / "data"
RETENTION_DAYS = 30


def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def _bucket() -> str:
    return os.environ["S3_BUCKET"]


def _configured() -> bool:
    return all(os.environ.get(k) for k in
               ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"))


def _make_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(DATA_DIR, arcname="data",
                filter=lambda ti: None if "/xdg/" in ti.name else ti)  # opencode's sqlite: server-managed
    return buf.getvalue()


def run_backup() -> str:
    """Tar data/ and push to S3. Returns a summary line."""
    if not _configured():
        return "backup not configured (need S3_ENDPOINT/BUCKET/credentials)"

    blob = _make_tarball()
    s3 = _s3()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    key = f"backups/{stamp}/data.tar.gz"
    s3.put_object(Bucket=_bucket(), Key=key, Body=blob)
    s3.put_object(Bucket=_bucket(), Key="latest/data.tar.gz", Body=blob)

    # also archive the env secrets (the reincarnation needs them to bootstrap)
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        s3.put_object(Bucket=_bucket(), Key="secrets/.env", Body=env_file.read_bytes())

    # prune > 30 days
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=RETENTION_DAYS)
    old = [o["Key"] for o in s3.list_objects_v2(Bucket=_bucket(), Prefix="backups/").get("Contents", [])
           if o["LastModified"] < cutoff]
    for k in old:
        s3.delete_object(Bucket=_bucket(), Key=k)

    return f"brain archived to s3 ({len(blob)//1024} KB)" + (f", pruned {len(old)} old" if old else "")


def restore_latest() -> str:
    """Restore data/ + .env from the latest backup (used at sandbox boot)."""
    if not _configured():
        return "restore skipped: not configured"
    s3 = _s3()
    try:
        obj = s3.get_object(Bucket=_bucket(), Key="latest/data.tar.gz")
        blob = obj["Body"].read()
    except Exception as e:
        msg = f"no backup restored ({str(e)[:80]})"
        log.info(msg)
        return msg

    buf = io.BytesIO(blob)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        tar.extractall(DATA_DIR.parent)
    n = sum(1 for _ in DATA_DIR.rglob("*.json"))

    # secrets: restore .env if it's missing (reincarnation boot)
    env_file = Path(__file__).parent.parent / ".env"
    try:
        env_blob = s3.get_object(Bucket=_bucket(), Key="secrets/.env")["Body"].read()
        if not env_file.exists():
            env_file.write_bytes(env_blob)
            n += 1
    except Exception:
        pass
    return f"restored brain from s3 ({n} files)"


class BackupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="backupnow", description="Snapshot the bot's brain to S3 (admin)")
    async def backupnow(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            summary = await asyncio.get_event_loop().run_in_executor(None, run_backup)
        except Exception as e:
            summary = f"backup error: {e}"
        await interaction.followup.send(f"🧠 {summary}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BackupCog(bot))
