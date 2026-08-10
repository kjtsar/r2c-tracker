import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import main


class CoordinationTenantSchemaMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_existing_coordination_rows_migrate_to_legacy_organization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "coordination.db"
            test_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
            original_engine = main.engine
            main.engine = test_engine
            try:
                async with test_engine.begin() as connection:
                    await connection.execute(text("""
                        CREATE TABLE r2c_zone_state (
                            id INTEGER PRIMARY KEY,
                            map_id TEXT NOT NULL,
                            zone_id TEXT NOT NULL,
                            guid TEXT NOT NULL,
                            last_seen_ms INTEGER DEFAULT 0
                        )
                    """))
                    await connection.execute(text("""
                        CREATE TABLE r2c_drone_owner_state (
                            id INTEGER PRIMARY KEY,
                            map_id TEXT NOT NULL,
                            remote_id TEXT NOT NULL,
                            lease_expire_ms INTEGER DEFAULT 0
                        )
                    """))
                    await connection.execute(text("""
                        CREATE TABLE r2c_drone_confirmation_state (
                            id INTEGER PRIMARY KEY,
                            map_id TEXT NOT NULL,
                            remote_id TEXT NOT NULL,
                            confirmed_at_ms INTEGER DEFAULT 0
                        )
                    """))
                    await connection.execute(text("""
                        CREATE TABLE r2c_recent_sighting (
                            id INTEGER PRIMARY KEY,
                            map_id TEXT NOT NULL,
                            remote_id TEXT NOT NULL,
                            received_ms INTEGER DEFAULT 0
                        )
                    """))
                    await connection.execute(text("""
                        INSERT INTO r2c_zone_state
                            (map_id, zone_id, guid, last_seen_ms)
                        VALUES ('MAP1', 'zone-a', 'zone-a', 1)
                    """))

                await main.migrate_r2c_coordination_schema()

                async with test_engine.connect() as connection:
                    organization_id = await connection.scalar(text(
                        "SELECT organization_id FROM r2c_zone_state WHERE id = 1"
                    ))
                    self.assertEqual(
                        main.LEGACY_COORDINATION_ORGANIZATION_ID,
                        organization_id,
                    )
                    for table_name in (
                        "r2c_zone_state",
                        "r2c_drone_owner_state",
                        "r2c_drone_confirmation_state",
                        "r2c_recent_sighting",
                    ):
                        columns = await connection.execute(
                            text(f"PRAGMA table_info({table_name})")
                        )
                        organization_column = next(
                            row for row in columns.fetchall() if row[1] == "organization_id"
                        )
                        self.assertEqual(1, organization_column[3])
            finally:
                main.engine = original_engine
                await test_engine.dispose()

    async def test_owner_upsert_and_delete_are_organization_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "coordination.db"
            test_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
            async with test_engine.begin() as connection:
                await connection.run_sync(main.Base.metadata.create_all)
            original_session_factory = main.AsyncSessionLocal
            main.AsyncSessionLocal = async_sessionmaker(
                bind=test_engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            try:
                hub = main.R2CCoordinationHub()
                owner = {
                    "owner_guid": "zone-a",
                    "owner_zone_id": "zone-a",
                    "drone_ts": 1,
                    "distance_m": 10.0,
                    "lease_seq": 1,
                    "lease_expire_ms": 999999,
                }
                await hub._upsert_owner_state("org-a", "SHARED-MAP", "SHARED-RID", owner)
                await hub._upsert_owner_state("org-b", "SHARED-MAP", "SHARED-RID", owner)
                await hub._delete_owner_state("org-a", "SHARED-MAP", "SHARED-RID")

                async with main.AsyncSessionLocal() as session:
                    rows = (
                        await session.execute(select(main.R2CDroneOwnerState))
                    ).scalars().all()
                self.assertEqual(1, len(rows))
                self.assertEqual("org-b", rows[0].organization_id)
            finally:
                main.AsyncSessionLocal = original_session_factory
                await test_engine.dispose()


if __name__ == "__main__":
    unittest.main()
