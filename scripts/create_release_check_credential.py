#!/usr/bin/env python3
"""Create an isolated organization-scoped credential for local release checks."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from control_plane import ControlPlaneStore


async def create_credential(database_url: str, designator: str) -> str:
    store = ControlPlaneStore(database_url)
    try:
        await store.init()
        organization = await store.create_organization(
            legal_name="Release Check Search and Rescue",
            designator=designator,
            admin_name="Release Check Administrator",
            admin_email="release-check@example.invalid",
            postal_address="Automated local test",
            actor_id="release-check",
            simulation=True,
        )
        owner = await store.activate_owner(
            designator,
            "release-check@example.invalid",
            "release-check-password",
        )
        campaign = await store.create_enrollment_campaign(
            organization_id=organization.id,
            label="Automated release check",
            created_by_user_id=owner.id,
            expires_in_hours=1,
            max_redemptions=1,
        )
        credential = await store.issue_device_credential(
            campaign_id=campaign.id,
            organization_id=organization.id,
            device_name="Release Check",
            platform="android",
        )
        return credential.token
    finally:
        await store.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database_url")
    parser.add_argument("designator")
    args = parser.parse_args()
    print(asyncio.run(create_credential(args.database_url, args.designator)))


if __name__ == "__main__":
    main()
