"""
Lambda: delete stale EBS snapshots.

A snapshot is stale when either:
  1. its source volume no longer exists, or
  2. its source volume exists but is not attached to any instance.

Safety rails:
  - DRY_RUN (default "true") only logs what would be deleted.
  - Snapshots used by one of your AMIs are skipped (AWS refuses to delete them anyway).
  - Snapshots tagged Keep=true are skipped.
  - Snapshots younger than MIN_AGE_DAYS are skipped.

Environment variables:
  DRY_RUN       "true" or "false"   (default "true")
  MIN_AGE_DAYS  integer             (default "1")
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")


def get_volume_attachment_map():
    """Return {volume_id: True if attached to an instance, else False}."""
    volumes = {}
    paginator = ec2.get_paginator("describe_volumes")
    for page in paginator.paginate():
        for volume in page["Volumes"]:
            volumes[volume["VolumeId"]] = len(volume["Attachments"]) > 0
    return volumes


def get_ami_snapshot_ids():
    """Return the set of snapshot IDs that back AMIs you own."""
    paginator = ec2.get_paginator("describe_images")
    pages = paginator.paginate(Owners=["self"])
    return set(pages.search("Images[].BlockDeviceMappings[].Ebs.SnapshotId"))


def has_keep_tag(snapshot):
    for tag in snapshot.get("Tags", []):
        if tag["Key"].lower() == "keep" and tag["Value"].lower() == "true":
            return True
    return False


def find_stale_snapshots(min_age_days):
    """Step 1 and 2: fetch all snapshots, then filter out the stale ones."""
    volumes = get_volume_attachment_map()
    ami_snapshots = get_ami_snapshot_ids()
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)

    stale = []
    paginator = ec2.get_paginator("describe_snapshots")
    for page in paginator.paginate(OwnerIds=["self"]):
        for snapshot in page["Snapshots"]:
            snapshot_id = snapshot["SnapshotId"]
            volume_id = snapshot.get("VolumeId")

            if snapshot["State"] != "completed":
                continue
            if snapshot_id in ami_snapshots:
                logger.info("SKIP %s: used by an AMI", snapshot_id)
                continue
            if has_keep_tag(snapshot):
                logger.info("SKIP %s: tagged Keep=true", snapshot_id)
                continue
            if snapshot["StartTime"] > cutoff:
                logger.info("SKIP %s: younger than %s day(s)", snapshot_id, min_age_days)
                continue

            if volume_id not in volumes:
                stale.append((snapshot_id, f"volume {volume_id} no longer exists"))
            elif not volumes[volume_id]:
                stale.append((snapshot_id, f"volume {volume_id} is not attached to any instance"))

    return stale


def lambda_handler(event, context):
    dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
    min_age_days = int(os.environ.get("MIN_AGE_DAYS", "1"))

    stale = find_stale_snapshots(min_age_days)

    # Step 3: delete the stale snapshots.
    deleted, failed = [], []
    for snapshot_id, reason in stale:
        if dry_run:
            logger.info("DRY RUN would delete %s: %s", snapshot_id, reason)
            continue
        try:
            ec2.delete_snapshot(SnapshotId=snapshot_id)
            logger.info("DELETED %s: %s", snapshot_id, reason)
            deleted.append(snapshot_id)
        except ClientError as e:
            # Keep going so one bad snapshot does not stop the whole cleanup.
            logger.error("FAILED %s: %s", snapshot_id, e)
            failed.append(snapshot_id)

    return {
        "dry_run": dry_run,
        "stale_found": [s for s, _ in stale],
        "deleted": deleted,
        "failed": failed,
    }
