import asyncio
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from control_plane import (
    AUDIT_EVENT_HOT_DAYS,
    AUDIT_EVENT_RETENTION_DAYS,
    ControlPlaneAuditEvent,
    DEFAULT_OWNER_ROLES,
    ControlPlaneError,
    ControlPlaneStore,
    DeviceCredential,
    DuplicateOrganizationError,
    InvalidOrganizationError,
    MANAGED_ACCESS_TERMS_VERSION,
    OrganizationUser,
    UsageDaily,
    hash_password,
    is_emergency_video_fallback,
    managed_video_quality_choices,
    normalize_designator,
    normalize_session_description,
    normalize_video_preflight_answer,
    require_separate_database,
    verify_password,
)


class ControlPlaneStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "control-plane.db"
        self.store = ControlPlaneStore(
            f"sqlite+aiosqlite:///{database_path}"
        )
        asyncio.run(self.store.init())
        self.now = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)

    def tearDown(self):
        asyncio.run(self.store.dispose())
        self.temp_dir.cleanup()

    def create_organization(self, **overrides):
        values = {
            "legal_name": "North County Search and Rescue",
            "designator": "NCSSAR",
            "admin_name": "Primary Administrator",
            "admin_email": "admin@ncssar.example",
            "postal_address": "100 Rescue Way\nAuburn, CA",
            "actor_id": "platform-admin",
            "simulation": True,
            "now": self.now,
        }
        values.update(overrides)
        return asyncio.run(self.store.create_organization(**values))

    def test_simulated_onboarding_creates_separate_commercial_record(self):
        organization = self.create_organization()

        self.assertEqual("NCSSAR", organization.designator)
        self.assertEqual("r2c-tracker.com/ncssar", organization.hostname)
        self.assertEqual("simulation ready", organization.provisioning_state)
        self.assertEqual("restricted", organization.records_visibility)
        self.assertEqual(730, organization.record_retention_days)
        self.assertEqual(30, organization.log_retention_days)

        organizations = asyncio.run(self.store.list_organizations())
        self.assertEqual((organization,), organizations)
        jobs = asyncio.run(self.store.list_provisioning_jobs())
        self.assertEqual(1, len(jobs))
        self.assertTrue(jobs[0].simulation)
        self.assertTrue(all(step["state"] == "simulated" for step in jobs[0].steps))
        audit_events = asyncio.run(self.store.list_audit_events())
        self.assertEqual("organization.created", audit_events[0].event_type)

    def test_external_webhook_delivery_claims_retry_and_deduplicates(self):
        values = {
            "provider": "app_store_connect",
            "event_id": "event-1",
            "event_type": "betaFeedbackCrashSubmissionCreated",
            "resource_type": "betaFeedbackCrashSubmissions",
            "resource_id": "feedback-1",
            "now": self.now,
        }

        first = asyncio.run(
            self.store.claim_external_webhook_delivery(**values)
        )
        processing_duplicate = asyncio.run(
            self.store.claim_external_webhook_delivery(**values)
        )
        asyncio.run(self.store.mark_external_webhook_delivery_failed(
            provider="app_store_connect",
            event_id="event-1",
            error="mail unavailable",
            now=self.now + timedelta(seconds=1),
        ))
        retry = asyncio.run(self.store.claim_external_webhook_delivery(
            **{**values, "now": self.now + timedelta(seconds=2)}
        ))
        asyncio.run(self.store.mark_external_webhook_delivery_sent(
            provider="app_store_connect",
            event_id="event-1",
            now=self.now + timedelta(seconds=3),
        ))
        sent_duplicate = asyncio.run(self.store.claim_external_webhook_delivery(
            **{**values, "now": self.now + timedelta(minutes=10)}
        ))

        self.assertEqual("claimed", first)
        self.assertEqual("processing", processing_duplicate)
        self.assertEqual("claimed", retry)
        self.assertEqual("sent", sent_duplicate)

    def test_audit_search_paginates_filters_and_reports_total(self):
        organization = self.create_organization()

        async def add_events():
            async with self.store.sessions() as session:
                session.add_all((
                    ControlPlaneAuditEvent(
                        organization_id=organization.id,
                        actor_type="organization_user",
                        actor_id="member-1",
                        event_type="member.updated",
                        created_at=self.now + timedelta(minutes=1),
                    ),
                    ControlPlaneAuditEvent(
                        organization_id=organization.id,
                        actor_type="organization_device",
                        actor_id="device-1",
                        event_type="video.streaming",
                        created_at=self.now + timedelta(minutes=2),
                    ),
                ))
                await session.commit()

        asyncio.run(add_events())
        administration = asyncio.run(self.store.search_audit_events(
            page_size=1,
            organization_designator="ncssar",
            categories=("administration",),
        ))

        self.assertEqual(2, administration.total)
        self.assertEqual(2, administration.total_pages)
        self.assertEqual("member.updated", administration.events[0].event_type)
        administration_page_two = asyncio.run(self.store.search_audit_events(
            page=2,
            page_size=1,
            organization_designator="NCSSAR",
            categories=("administration",),
        ))
        self.assertEqual(
            "organization.created",
            administration_page_two.events[0].event_type,
        )
        video = asyncio.run(self.store.search_audit_events(
            actor_type="organization_device",
            event_type="video.streaming",
            categories=("video",),
        ))
        self.assertEqual(1, video.total)
        self.assertEqual("NCSSAR", video.events[0].designator)

    def test_audit_retention_deletes_expired_events_but_preserves_holds(self):
        organization = self.create_organization()
        expired_at = self.now - timedelta(days=AUDIT_EVENT_RETENTION_DAYS + 1)

        async def add_old_events():
            async with self.store.sessions() as session:
                expired = ControlPlaneAuditEvent(
                    organization_id=organization.id,
                    actor_type="organization_user",
                    actor_id="expired-member",
                    event_type="member.updated",
                    created_at=expired_at,
                )
                held = ControlPlaneAuditEvent(
                    organization_id=organization.id,
                    actor_type="organization_user",
                    actor_id="held-member",
                    event_type="member.deleted",
                    created_at=expired_at,
                    retention_hold=True,
                )
                session.add_all((expired, held))
                await session.commit()
                return expired.id, held.id

        expired_id, held_id = asyncio.run(add_old_events())
        removed = asyncio.run(self.store.purge_expired_audit_events(now=self.now))
        self.assertEqual(1, removed)
        retained = asyncio.run(self.store.search_audit_events(
            start_at=expired_at - timedelta(days=1),
            end_at=self.now + timedelta(days=1),
        ))
        retained_ids = {event.id for event in retained.events}
        self.assertNotIn(expired_id, retained_ids)
        self.assertIn(held_id, retained_ids)

    def test_audit_access_and_retention_hold_changes_are_audited(self):
        organization = self.create_organization()
        created = asyncio.run(self.store.list_audit_events())[0]

        held = asyncio.run(self.store.set_audit_event_retention_hold(
            event_id=created.id,
            retention_hold=True,
            actor_id="platform-admin",
            now=self.now + timedelta(minutes=1),
        ))
        self.assertTrue(held.retention_hold)
        asyncio.run(self.store.record_audit_access(
            actor_id="platform-admin",
            details={"view": "audit_log"},
            now=self.now + timedelta(minutes=2),
        ))
        events = asyncio.run(self.store.search_audit_events(
            categories=("audit",),
        )).events
        self.assertEqual(
            ["audit.viewed", "audit.retention_hold_placed"],
            [event.event_type for event in events[:2]],
        )
        released = asyncio.run(self.store.set_audit_event_retention_hold(
            event_id=created.id,
            retention_hold=False,
            actor_id="platform-admin",
            now=self.now + timedelta(minutes=3),
        ))
        self.assertFalse(released.retention_hold)
        self.assertEqual(90, AUDIT_EVENT_HOT_DAYS)

    def test_audit_retention_schema_migrates_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy-control-plane.db"
            connection = sqlite3.connect(database_path)
            connection.execute("""
                CREATE TABLE control_plane_audit_events (
                    id VARCHAR(36) PRIMARY KEY,
                    organization_id VARCHAR(36),
                    actor_type VARCHAR(32) NOT NULL,
                    actor_id VARCHAR(160) NOT NULL,
                    event_type VARCHAR(80) NOT NULL,
                    details_json TEXT,
                    created_at DATETIME
                )
            """)
            connection.commit()
            connection.close()
            store = ControlPlaneStore(
                f"sqlite+aiosqlite:///{database_path}"
            )
            asyncio.run(store.init())
            connection = sqlite3.connect(database_path)
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(control_plane_audit_events)"
                )
            }
            indexes = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list(control_plane_audit_events)"
                )
            }
            connection.close()
            asyncio.run(store.dispose())

        self.assertIn("retention_hold", columns)
        self.assertIn("idx_control_plane_audit_events_created_at", indexes)

    def test_live_onboarding_waits_for_email_and_starts_extended_beta_on_activation(self):
        organization = self.create_organization(
            designator="LIVESAR",
            admin_email="admin@livesar.example",
            simulation=False,
        )
        self.assertIsNone(organization.trial_ends_at)
        self.assertEqual("provisioning queued", organization.provisioning_state)

        asyncio.run(
            self.store.mark_organization_invitation_sent(
                organization_id=organization.id,
                actor_id="platform-admin",
                now=self.now,
            )
        )
        pending = asyncio.run(self.store.get_organization("LIVESAR"))
        self.assertEqual("activation pending", pending.provisioning_state)
        promoted_job = asyncio.run(self.store.list_provisioning_jobs())[0]
        self.assertFalse(promoted_job.simulation)

        invitation = asyncio.run(
            self.store.get_invitation("LIVESAR", "admin@livesar.example")
        )
        asyncio.run(
            self.store.activate_owner(
                "LIVESAR",
                "admin@livesar.example",
                "generated complex password",
                now=self.now + timedelta(hours=1),
                activation_nonce=invitation.activation_nonce,
            )
        )
        activated = asyncio.run(self.store.get_organization("LIVESAR"))
        self.assertEqual("ready", activated.provisioning_state)
        self.assertEqual("extended_beta", activated.lifecycle_state)
        self.assertEqual("extended beta", activated.billing_mode)
        self.assertIsNone(activated.trial_ends_at)
        job = asyncio.run(self.store.list_provisioning_jobs())[0]
        self.assertEqual("completed", job.state)
        self.assertTrue(all(step["state"] == "completed" for step in job.steps))

    def test_session_description_normalizes_escaped_newlines_to_crlf(self):
        escaped = "v=0\\no=- 1 2 IN IP4 127.0.0.1\\ns=-\\nt=0 0\\n"

        normalized = normalize_session_description(escaped)

        self.assertEqual(
            "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n",
            normalized,
        )

    def test_preflight_answer_omits_optional_sctp_message_size(self):
        apple_answer = (
            "v=0\r\n"
            "o=- 1 2 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
            "a=sctp-port:5000\r\n"
            "a=max-message-size:262144"
        )

        normalized = normalize_video_preflight_answer(apple_answer)

        self.assertEqual(
            "v=0\r\n"
            "o=- 1 2 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
            "a=sctp-port:5000\r\n",
            normalized,
        )

    def test_emergency_video_fallback_is_narrowly_bounded(self):
        self.assertTrue(is_emergency_video_fallback(
            width=640,
            height=360,
            fps_milli=5_000,
            bitrate_bps=180_000,
        ))
        self.assertFalse(is_emergency_video_fallback(
            width=960,
            height=540,
            fps_milli=5_000,
            bitrate_bps=180_000,
        ))

    def test_managed_video_quality_policy_preserves_emergency_fallback(self):
        choices = managed_video_quality_choices(
            source_width=1920,
            source_height=1080,
            source_fps=30,
            usable_uplink_bps=100_000,
        )

        self.assertEqual(["High", "Balanced", "Low", "Emergency"], [
            choice["preset"] for choice in choices
        ])
        self.assertEqual("fallback", choices[-1]["capacity"])
        self.assertEqual((640, 360, 5.0, 200_000), (
            choices[-1]["width"],
            choices[-1]["height"],
            choices[-1]["fps"],
            choices[-1]["bitrateBps"],
        ))
        self.assertFalse(is_emergency_video_fallback(
            width=640,
            height=360,
            fps_milli=10_000,
            bitrate_bps=180_000,
        ))
        self.assertFalse(is_emergency_video_fallback(
            width=640,
            height=360,
            fps_milli=5_000,
            bitrate_bps=250_000,
        ))

    def test_platform_admin_uses_hashed_email_login_and_can_change_password(self):
        admin = asyncio.run(
            self.store.ensure_platform_admin(
                email="Platform.Admin@Example.org",
                display_name="Platform Administrator",
                bootstrap_password="initial complex password",
            )
        )

        self.assertEqual("platform.admin@example.org", admin.email)
        authenticated = asyncio.run(
            self.store.authenticate_platform_admin(
                "platform.admin@example.org",
                "initial complex password",
                self.now,
            )
        )
        self.assertEqual(admin.id, authenticated.id)
        asyncio.run(
            self.store.change_platform_admin_password(
                user_id=admin.id,
                current_password="initial complex password",
                new_password="replacement complex password",
            )
        )
        self.assertIsNone(
            asyncio.run(
                self.store.authenticate_platform_admin(
                    admin.email,
                    "initial complex password",
                    self.now + timedelta(minutes=1),
                )
            )
        )
        self.assertIsNotNone(
            asyncio.run(
                self.store.authenticate_platform_admin(
                    admin.email,
                    "replacement complex password",
                    self.now + timedelta(minutes=2),
                )
            )
        )

    def test_infrastructure_identity_rotation_disables_old_admin_without_password_transfer(self):
        old_admin = asyncio.run(
            self.store.ensure_platform_admin(
                email="old@example.org",
                display_name="Old Administrator",
                bootstrap_password="old complex password",
            )
        )
        new_admin = asyncio.run(
            self.store.reconcile_platform_admin_identity(
                email="new@example.org",
                display_name="New Administrator",
            )
        )

        self.assertIsNone(asyncio.run(self.store.get_platform_admin(old_admin.id)))
        self.assertEqual("new@example.org", new_admin.email)
        self.assertIsNone(
            asyncio.run(
                self.store.authenticate_platform_admin(
                    "new@example.org",
                    "old complex password",
                    self.now,
                )
            )
        )
        restored = asyncio.run(
            self.store.reconcile_platform_admin_identity(
                email="old@example.org",
                display_name="Restored Administrator",
            )
        )
        self.assertEqual(old_admin.id, restored.id)
        self.assertIsNone(
            asyncio.run(
                self.store.authenticate_platform_admin(
                    "old@example.org",
                    "old complex password",
                    self.now,
                )
            )
        )

    def test_password_setup_token_is_single_use_and_expires_after_five_minutes(self):
        admin = asyncio.run(
            self.store.reconcile_platform_admin_identity(
                email="setup@example.org",
                display_name="Setup Administrator",
            )
        )
        token = asyncio.run(
            self.store.issue_platform_admin_password_setup(
                email=admin.email,
                identity_generation="7",
                now=self.now,
            )
        )
        configured = asyncio.run(
            self.store.set_platform_admin_password_from_token(
                token=token,
                email=admin.email,
                identity_generation="7",
                new_password="new complex password",
                now=self.now + timedelta(minutes=4, seconds=59),
            )
        )

        self.assertEqual(admin.id, configured.id)
        self.assertIsNone(
            asyncio.run(
                self.store.set_platform_admin_password_from_token(
                    token=token,
                    email=admin.email,
                    identity_generation="7",
                    new_password="another complex password",
                    now=self.now + timedelta(minutes=5),
                )
            )
        )

        later_token = asyncio.run(
            self.store.issue_platform_admin_password_setup(
                email=admin.email,
                identity_generation="7",
                now=self.now + timedelta(minutes=6),
            )
        )
        self.assertIsNone(
            asyncio.run(
                self.store.set_platform_admin_password_from_token(
                    token=later_token,
                    email=admin.email,
                    identity_generation="7",
                    new_password="another complex password",
                    now=self.now + timedelta(minutes=11, seconds=1),
                )
            )
        )

    def test_password_setup_token_is_bound_to_identity_generation(self):
        admin = asyncio.run(
            self.store.reconcile_platform_admin_identity(
                email="setup@example.org",
                display_name="Setup Administrator",
            )
        )
        token = asyncio.run(
            self.store.issue_platform_admin_password_setup(
                email=admin.email,
                identity_generation="7",
                now=self.now,
            )
        )

        self.assertIsNone(
            asyncio.run(
                self.store.set_platform_admin_password_from_token(
                    token=token,
                    email=admin.email,
                    identity_generation="8",
                    new_password="new complex password",
                    now=self.now + timedelta(minutes=1),
                )
            )
        )

    def test_designator_and_hostname_are_unique(self):
        self.create_organization()

        with self.assertRaisesRegex(
            DuplicateOrganizationError,
            "NCSSAR is already in use",
        ):
            self.create_organization(admin_email="second@example.org")

    def test_archiving_disables_site_access_and_keeps_designator_reserved(self):
        organization = self.create_organization()

        archived = asyncio.run(
            self.store.archive_organization(
                designator=organization.designator,
                actor_id="platform-admin",
                administrator_contact="Called Primary Administrator on 30 Jul 2026.",
                now=self.now + timedelta(hours=1),
            )
        )

        self.assertEqual("archived", archived.lifecycle_state)
        self.assertEqual("archived", archived.provisioning_state)
        self.assertIsNone(
            asyncio.run(self.store.get_organization(organization.designator))
        )
        self.assertEqual(
            "archived",
            asyncio.run(
                self.store.get_organization(
                    organization.designator,
                    include_archived=True,
                )
            ).lifecycle_state,
        )
        with self.assertRaises(DuplicateOrganizationError):
            self.create_organization(admin_email="replacement@example.org")
        audit_events = asyncio.run(self.store.list_audit_events())
        self.assertEqual("organization.archived", audit_events[0].event_type)

    def test_unarchive_restores_org_and_requires_fresh_administrator_activation(self):
        organization = self.create_organization(admin_phone="530-555-0102")
        asyncio.run(
            self.store.archive_organization(
                designator=organization.designator,
                actor_id="platform-admin",
                administrator_contact="Called Primary Administrator on 30 Jul 2026.",
                now=self.now + timedelta(hours=1),
            )
        )
        restored = asyncio.run(
            self.store.unarchive_organization(
                designator=organization.designator,
                actor_id="platform-admin",
                now=self.now + timedelta(hours=2),
            )
        )
        self.assertEqual("extended_beta", restored.lifecycle_state)
        self.assertEqual("simulation ready", restored.provisioning_state)
        self.assertEqual("530-555-0102", restored.primary_admin_phone)
        self.assertIsNotNone(
            asyncio.run(
                self.store.get_invitation(
                    restored.designator,
                    restored.primary_admin_email,
                )
            )
        )
        audit_events = asyncio.run(self.store.list_audit_events())
        self.assertEqual("organization.unarchived", audit_events[0].event_type)

    def test_managed_access_requests_are_deduplicated_and_retain_phone(self):
        self.assertEqual("2026-08-07", MANAGED_ACCESS_TERMS_VERSION)
        values = {
            "requester_name": "Jamie Responder",
            "requester_email": "jamie@example.org",
            "requester_phone": "+1 530 555 0100",
            "organization_name": "Foothill Search and Rescue",
            "designator": "FHSAR",
            "source_host": "rid2caltopo.org",
            "terms_acknowledged": True,
            "terms_version": MANAGED_ACCESS_TERMS_VERSION,
            "now": self.now,
        }
        first = asyncio.run(self.store.create_managed_access_request(**values))
        second = asyncio.run(self.store.create_managed_access_request(**values))
        requests = asyncio.run(self.store.list_managed_access_requests())
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(requests))
        self.assertEqual("+1 530 555 0100", requests[0].requester_phone)
        self.assertEqual(MANAGED_ACCESS_TERMS_VERSION, requests[0].terms_version)
        self.assertEqual(self.now, requests[0].terms_acknowledged_at)

        with self.assertRaisesRegex(
            InvalidOrganizationError,
            "best-effort safety terms",
        ):
            asyncio.run(
                self.store.create_managed_access_request(
                    **{
                        **values,
                        "terms_version": "2026-08-06",
                    }
                )
            )

    def test_replacement_administrator_activation_does_not_restart_trial(self):
        organization = self.create_organization(simulation=False)
        first_activation = self.now + timedelta(hours=1)
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                first_activation,
            )
        )
        before = asyncio.run(self.store.get_organization(organization.designator))
        asyncio.run(
            self.store.update_organization_administrator(
                designator=organization.designator,
                legal_name=organization.legal_name,
                admin_name="Replacement Administrator",
                admin_email="replacement@example.org",
                admin_phone="530-555-0199",
                postal_address="200 Rescue Way",
                actor_id="platform-admin",
                now=first_activation + timedelta(days=2),
            )
        )
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                "replacement@example.org",
                "another correct horse battery staple",
                first_activation + timedelta(days=3),
            )
        )
        after = asyncio.run(self.store.get_organization(organization.designator))
        self.assertEqual(before.trial_ends_at, after.trial_ends_at)

    def test_owner_can_activate_and_authenticate(self):
        organization = self.create_organization()

        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
            )
        )
        self.assertEqual("active", owner.state)
        self.assertEqual(set(DEFAULT_OWNER_ROLES), set(owner.roles))
        self.assertIn("config_admin", owner.roles)

        authenticated = asyncio.run(
            self.store.authenticate_user(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
            )
        )
        self.assertIsNotNone(authenticated)
        self.assertEqual(owner.id, authenticated.id)

        rejected = asyncio.run(
            self.store.authenticate_user(
                organization.designator,
                organization.primary_admin_email,
                "incorrect password",
                self.now,
            )
        )
        self.assertIsNone(rejected)

    def test_member_can_be_edited_deleted_and_restored_with_audit_history(self):
        organization = self.create_organization()
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
            )
        )
        member = asyncio.run(
            self.store.add_user(
                organization_id=organization.id,
                display_name="Initial Member",
                email="member@ncssar.example",
                roles=("records_viewer",),
                actor_id=owner.id,
                now=self.now,
            )
        )

        updated = asyncio.run(
            self.store.update_user(
                organization_id=organization.id,
                user_id=member.id,
                display_name="Updated Member",
                email="updated@ncssar.example",
                roles=("records_admin", "video_requester"),
                actor_id=owner.id,
                now=self.now + timedelta(minutes=1),
            )
        )
        self.assertEqual("Updated Member", updated.display_name)
        self.assertEqual("updated@ncssar.example", updated.email)
        self.assertEqual(
            {"records_admin", "video_requester"},
            set(updated.roles),
        )

        deleted = asyncio.run(
            self.store.delete_user(
                organization_id=organization.id,
                user_id=member.id,
                actor_id=owner.id,
                now=self.now + timedelta(minutes=2),
            )
        )
        self.assertEqual("disabled", deleted.state)
        self.assertIsNone(
            asyncio.run(
                self.store.authorize_google_user(
                    organization.designator,
                    updated.email,
                    now=self.now + timedelta(minutes=3),
                )
            )
        )

        restored = asyncio.run(
            self.store.restore_user(
                organization_id=organization.id,
                user_id=member.id,
                actor_id=owner.id,
                now=self.now + timedelta(minutes=4),
            )
        )
        self.assertEqual("invited", restored.state)
        invitation = asyncio.run(
            self.store.get_invitation(organization.designator, restored.email)
        )
        self.assertIsNotNone(invitation)
        audit_events = asyncio.run(self.store.list_audit_events())
        self.assertEqual(
            ["member.restored", "member.deleted", "member.updated"],
            [event.event_type for event in audit_events[:3]],
        )

    def test_r2c_device_role_automatically_includes_records_viewer(self):
        organization = self.create_organization()
        owner = asyncio.run(self.store.activate_owner(
            organization.designator,
            organization.primary_admin_email,
            "correct horse battery staple",
            self.now,
        ))
        member = asyncio.run(self.store.add_user(
            organization_id=organization.id,
            display_name="Tablet operator",
            email="operator@ncssar.example",
            roles=("r2c_device",),
            actor_id=owner.id,
            now=self.now,
        ))
        self.assertEqual({"r2c_device", "records_viewer"}, set(member.roles))

    def test_archived_member_can_be_restored_without_losing_roles(self):
        organization = self.create_organization()
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
            )
        )
        member = asyncio.run(
            self.store.add_user(
                organization_id=organization.id,
                display_name="Archived Member",
                email="archived@ncssar.example",
                roles=("records_admin", "records_viewer", "video_requester"),
                actor_id=owner.id,
                now=self.now,
            )
        )

        async def archive_member():
            async with self.store.sessions() as session:
                stored = await session.get(OrganizationUser, member.id)
                stored.state = "archived"
                stored.password_hash = ""
                await session.commit()

        asyncio.run(archive_member())
        restored = asyncio.run(
            self.store.restore_user(
                organization_id=organization.id,
                user_id=member.id,
                actor_id=owner.id,
                now=self.now + timedelta(minutes=1),
            )
        )

        self.assertEqual("invited", restored.state)
        self.assertEqual(set(member.roles), set(restored.roles))
        invitation = asyncio.run(
            self.store.get_invitation(organization.designator, member.email)
        )
        self.assertIsNotNone(invitation)
        event = asyncio.run(self.store.list_audit_events())[0]
        self.assertEqual("member.restored", event.event_type)
        self.assertEqual("archived", event.details["previous_state"])

    def test_pending_member_invitation_can_be_renewed_and_audited(self):
        organization = self.create_organization()
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
            )
        )
        member = asyncio.run(
            self.store.add_user(
                organization_id=organization.id,
                display_name="Pending Member",
                email="pending@ncssar.example",
                roles=("records_viewer",),
                actor_id=owner.id,
                now=self.now,
            )
        )
        original = asyncio.run(
            self.store.get_invitation(organization.designator, member.email)
        )

        renewed_member = asyncio.run(
            self.store.renew_member_invitation(
                organization_id=organization.id,
                user_id=member.id,
                actor_id=owner.id,
                now=self.now + timedelta(days=1),
            )
        )
        renewed = asyncio.run(
            self.store.get_invitation(organization.designator, member.email)
        )
        self.assertEqual("invited", renewed_member.state)
        self.assertNotEqual(original.activation_nonce, renewed.activation_nonce)
        self.assertEqual(self.now + timedelta(days=8), renewed.expires_at)

        asyncio.run(
            self.store.mark_member_invitation_sent(
                organization_id=organization.id,
                user_id=member.id,
                actor_id=owner.id,
                now=self.now + timedelta(days=1, seconds=1),
            )
        )
        events = asyncio.run(self.store.list_audit_events())[:2]
        self.assertEqual(
            ["member.invitation_sent", "member.invitation_renewed"],
            [event.event_type for event in events],
        )

    def test_organization_owner_cannot_be_deleted_or_restored_as_a_member(self):
        organization = self.create_organization()
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
            )
        )

        with self.assertRaisesRegex(ControlPlaneError, "cannot be deleted"):
            asyncio.run(
                self.store.delete_user(
                    organization_id=organization.id,
                    user_id=owner.id,
                    actor_id=owner.id,
                    now=self.now + timedelta(minutes=1),
                )
            )

        async def archive_owner():
            async with self.store.sessions() as session:
                stored = await session.get(OrganizationUser, owner.id)
                stored.state = "archived"
                await session.commit()

        asyncio.run(archive_owner())
        with self.assertRaisesRegex(ControlPlaneError, "cannot be restored"):
            asyncio.run(
                self.store.restore_user(
                    organization_id=organization.id,
                    user_id=owner.id,
                    actor_id=owner.id,
                    now=self.now + timedelta(minutes=2),
                )
            )

    def test_enrollment_issues_revocable_device_token_stored_as_hash(self):
        organization = self.create_organization()
        invitation = asyncio.run(
            self.store.get_invitation(
                organization.designator,
                organization.primary_admin_email,
            )
        )
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
                activation_nonce=invitation.activation_nonce,
            )
        )
        campaign = asyncio.run(
            self.store.create_enrollment_campaign(
                organization_id=organization.id,
                label="Field tablets",
                created_by_user_id=owner.id,
                expires_in_hours=24,
                max_redemptions=1,
                now=self.now,
            )
        )

        issued = asyncio.run(
            self.store.issue_device_credential(
                campaign_id=campaign.id,
                organization_id=organization.id,
                device_name="Jerry's iPad",
                platform="ios",
                now=self.now,
            )
        )

        self.assertTrue(issued.token.startswith("r2c_dev_"))
        authenticated = asyncio.run(
            self.store.authenticate_device_token(
                issued.token,
                self.now + timedelta(minutes=1),
            )
        )
        self.assertEqual(organization.id, authenticated.organization_id)
        self.assertEqual("NCSSAR", authenticated.designator)
        self.assertIsNone(asyncio.run(
            self.store.authenticate_device_token(
                issued.token,
                self.now + timedelta(days=366),
            )
        ))
        expired = asyncio.run(self.store.list_device_credentials(
            organization.id,
            now=self.now + timedelta(days=366),
        ))
        self.assertEqual("expired", expired[0].state)
        extended = asyncio.run(self.store.extend_device_credential(
            credential_id=issued.id,
            organization_id=organization.id,
            actor_id=owner.id,
            now=self.now + timedelta(days=366),
        ))
        self.assertEqual("active", extended.state)
        self.assertEqual(
            self.now + timedelta(days=731),
            extended.expires_at,
        )
        self.assertIsNotNone(asyncio.run(
            self.store.authenticate_device_token(
                issued.token,
                self.now + timedelta(days=366, minutes=1),
            )
        ))
        self.assertEqual(
            "device.credential_extended",
            asyncio.run(self.store.list_audit_events())[0].event_type,
        )

        async def revoke_credential():
            async with self.store.sessions() as session:
                stored = await session.get(DeviceCredential, issued.id)
                stored.state = "revoked"
                await session.commit()

        asyncio.run(revoke_credential())
        with self.assertRaisesRegex(ControlPlaneError, "revoked"):
            asyncio.run(self.store.extend_device_credential(
                credential_id=issued.id,
                organization_id=organization.id,
                actor_id=owner.id,
                now=self.now + timedelta(days=367),
            ))
        with self.assertRaises(ControlPlaneError):
            asyncio.run(
                self.store.issue_device_credential(
                    campaign_id=campaign.id,
                    organization_id=organization.id,
                    device_name="Second tablet",
                    platform="android",
                    now=self.now,
                )
            )

    def test_admin_can_require_device_reauthentication(self):
        organization = self.create_organization()
        invitation = asyncio.run(self.store.get_invitation(
            organization.designator, organization.primary_admin_email
        ))
        owner = asyncio.run(self.store.activate_owner(
            organization.designator,
            organization.primary_admin_email,
            "correct horse battery staple",
            self.now,
            activation_nonce=invitation.activation_nonce,
        ))
        campaign = asyncio.run(self.store.create_enrollment_campaign(
            organization_id=organization.id,
            label="Field tablets",
            created_by_user_id=owner.id,
            expires_in_hours=24,
            max_redemptions=1,
            now=self.now,
        ))
        issued = asyncio.run(self.store.issue_device_credential(
            campaign_id=campaign.id,
            organization_id=organization.id,
            device_name="Lost tablet",
            platform="android",
            functionality_release=148,
            now=self.now,
        ))

        record = asyncio.run(self.store.require_device_reauthentication(
            credential_id=issued.id,
            organization_id=organization.id,
            actor_id=owner.id,
            now=self.now + timedelta(minutes=1),
        ))

        self.assertEqual("reauth_required", record.state)
        self.assertEqual(148, record.functionality_release)
        self.assertEqual("reauth_required", asyncio.run(
            self.store.device_token_state(issued.token)
        ))
        self.assertIsNone(asyncio.run(
            self.store.authenticate_device_token(issued.token)
        ))
        with self.assertRaisesRegex(ControlPlaneError, "must complete reauthentication"):
            asyncio.run(self.store.extend_device_credential(
                credential_id=issued.id,
                organization_id=organization.id,
                actor_id=owner.id,
                now=self.now + timedelta(minutes=2),
            ))
        event = asyncio.run(self.store.list_audit_events())[0]
        self.assertEqual("device.reauthentication_required", event.event_type)
        self.assertIn("Lost tablet", event.details["message"])

        restored = asyncio.run(self.store.complete_device_reauthentication(
            credential_id=issued.id,
            organization_id=organization.id,
            user_id=owner.id,
            now=self.now + timedelta(minutes=3),
        ))
        self.assertEqual("active", restored.state)
        self.assertEqual(owner.id, restored.authorized_user_id)
        self.assertIsNotNone(asyncio.run(
            self.store.authenticate_device_token(issued.token)
        ))
        event = asyncio.run(self.store.list_audit_events())[0]
        self.assertEqual("device.reauthentication_completed", event.event_type)
        self.assertIn(owner.email, event.details["message"])

    def test_expired_enrollment_campaign_can_be_renewed_with_uses_remaining(self):
        organization = self.create_organization()
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
            )
        )
        campaign = asyncio.run(self.store.create_enrollment_campaign(
            organization_id=organization.id,
            label="Operations QR",
            created_by_user_id=owner.id,
            expires_in_hours=24,
            max_redemptions=5,
            now=self.now,
        ))

        renewed = asyncio.run(self.store.renew_enrollment_campaign(
            campaign_id=campaign.id,
            organization_id=organization.id,
            actor_id=owner.id,
            now=self.now + timedelta(days=2),
        ))

        self.assertEqual("active", renewed.state)
        self.assertEqual(
            self.now + timedelta(days=9),
            renewed.expires_at,
        )
        self.assertTrue(renewed.is_usable(self.now + timedelta(days=8)))
        self.assertNotEqual(campaign.token_generation, renewed.token_generation)
        self.assertEqual(
            "enrollment.renewed",
            asyncio.run(self.store.list_audit_events())[0].event_type,
        )
        asyncio.run(self.store.revoke_enrollment_campaign(
            campaign_id=campaign.id,
            organization_id=organization.id,
            actor_id=owner.id,
            now=self.now + timedelta(days=3),
        ))
        with self.assertRaisesRegex(ControlPlaneError, "revoked"):
            asyncio.run(self.store.renew_enrollment_campaign(
                campaign_id=campaign.id,
                organization_id=organization.id,
                actor_id=owner.id,
                now=self.now + timedelta(days=4),
            ))

    def test_video_streams_are_sorted_tenant_isolated_and_require_consent(self):
        organization = self.create_organization()
        invitation = asyncio.run(
            self.store.get_invitation(
                organization.designator,
                organization.primary_admin_email,
            )
        )
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
                activation_nonce=invitation.activation_nonce,
            )
        )
        campaign = asyncio.run(
            self.store.create_enrollment_campaign(
                organization_id=organization.id,
                label="Video tablets",
                created_by_user_id=owner.id,
                expires_in_hours=24,
                max_redemptions=1,
                now=self.now,
            )
        )
        device = asyncio.run(
            self.store.issue_device_credential(
                campaign_id=campaign.id,
                organization_id=organization.id,
                device_name="Android field tablet",
                platform="android",
                now=self.now,
            )
        )
        for session_id, incident, drone in (
            ("00000000-0000-0000-0000-000000000003", "Zulu", "2B"),
            ("00000000-0000-0000-0000-000000000001", "Alpha", "10A"),
            ("00000000-0000-0000-0000-000000000002", "alpha", "2A"),
        ):
            asyncio.run(
                self.store.advertise_video_stream(
                    organization_id=organization.id,
                    device_credential_id=device.id,
                    device_name="Ken's iPad",
                    session_id=session_id,
                    incident_name=incident,
                    drone_designator=drone,
                    source_width=1920,
                    source_height=1080,
                    source_fps=29.97,
                    source_bitrate_bps=4_000_000,
                    source_codec="H264",
                    timezone_name="America/Los_Angeles",
                    now=self.now,
                )
            )

        streams = asyncio.run(
            self.store.list_active_video_streams(
                organization.id,
                self.now + timedelta(seconds=30),
            )
        )
        self.assertEqual(
            [("Alpha", "10A"), ("alpha", "2A"), ("Zulu", "2B")],
            [(stream.incident_name, stream.drone_designator) for stream in streams],
        )
        self.assertEqual(29.97, streams[0].source_fps)
        self.assertEqual("Ken's iPad", streams[0].device_name)
        self.assertEqual("live", streams[0].media_kind)
        self.assertIsNone(streams[0].recorded_at)
        self.assertEqual(0, streams[0].duration_ms)
        self.assertEqual("", streams[0].thumbnail_revision)
        self.assertFalse(streams[0].remote_control_enabled)

        retired = asyncio.run(
            self.store.reconcile_device_video_streams(
                organization_id=organization.id,
                device_credential_id=device.id,
                active_session_ids=(streams[0].session_id,),
                now=self.now + timedelta(seconds=31),
            )
        )
        self.assertEqual(2, retired)
        remaining_streams = asyncio.run(
            self.store.list_active_video_streams(
                organization.id,
                self.now + timedelta(seconds=31),
            )
        )
        self.assertEqual((streams[0].session_id,), tuple(
            stream.session_id for stream in remaining_streams
        ))

        request = asyncio.run(
            self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id=streams[0].session_id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=30),
            )
        )
        self.assertEqual("Ken's iPad", request.device_name)
        self.assertEqual("pending", request.state)
        activity_details = asyncio.run(
            self.store.deployment_activity_details(
                now=self.now + timedelta(seconds=30),
            )
        )
        self.assertEqual(
            {
                "organization": "NCSSAR",
                "device": "Ken's iPad",
                "platform": "android",
                "stream_count": 1,
                "streams": [{
                    "drone": "10A",
                    "media_kind": "live",
                    "session_id": streams[0].session_id,
                }],
            },
            {
                key: activity_details["active_video_streams"][0][key]
                for key in (
                    "organization",
                    "device",
                    "platform",
                    "stream_count",
                    "streams",
                )
            },
        )
        self.assertEqual(
            organization.primary_admin_email,
            activity_details["active_video_requests"][0]["requester"],
        )
        self.assertEqual(
            "Ken's iPad",
            activity_details["active_video_requests"][0]["device"],
        )
        self.assertEqual("unknown", request.route_kind)
        self.assertEqual(owner.email, request.requester_email)
        self.assertEqual("America/Los_Angeles", request.timezone_name)
        self.assertEqual(
            self.now + timedelta(seconds=90), request.expires_at
        )
        self.assertEqual(2, request.requested_at_local.hour)
        self.assertEqual("PDT", request.requested_at_local.tzname())
        with self.assertRaisesRegex(
            ControlPlaneError,
            "already in progress",
        ):
            asyncio.run(
                self.store.create_video_stream_request(
                    organization_id=organization.id,
                    stream_session_id=streams[0].session_id,
                    requester_user_id=owner.id,
                    now=self.now + timedelta(seconds=31),
                )
            )
        pending_for_device = asyncio.run(
            self.store.list_pending_video_stream_requests_for_device(
                device_credential_id=device.id,
                now=self.now + timedelta(seconds=31),
            )
        )
        self.assertEqual([request.id], [item.id for item in pending_for_device])
        offer_sdp = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        exchange = asyncio.run(
            self.store.start_video_preflight(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                browser_offer_sdp=offer_sdp,
                relay_candidate_ms=321,
                now=self.now + timedelta(seconds=31),
            )
        )
        self.assertEqual("probing", exchange.state)
        self.assertEqual(offer_sdp, exchange.browser_offer_sdp)
        pending_offers = asyncio.run(
            self.store.list_pending_video_preflight_offers_for_device(
                device_credential_id=device.id,
                now=self.now + timedelta(seconds=31),
            )
        )
        self.assertEqual([request.id], [item.request_id for item in pending_offers])
        answer_sdp = "v=0\r\no=- 2 3 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        answered = asyncio.run(
            self.store.record_video_preflight_answer(
                request_id=request.id,
                device_credential_id=device.id,
                device_answer_sdp=answer_sdp,
                now=self.now + timedelta(seconds=31),
            )
        )
        self.assertEqual(answer_sdp, answered.device_answer_sdp)
        preflight = asyncio.run(
            self.store.record_video_preflight_result(
                request_id=request.id,
                device_credential_id=device.id,
                route_kind="Direct",
                estimated_uplink_bps=8_000_000,
                now=self.now + timedelta(seconds=32),
            )
        )
        self.assertEqual("awaiting_approval", preflight.state)
        self.assertEqual("direct", preflight.route_kind)
        self.assertEqual(8_000_000, preflight.estimated_uplink_bps)
        self.assertEqual(
            self.now + timedelta(seconds=90), preflight.expires_at
        )
        approved = asyncio.run(
            self.store.record_video_stream_decision(
                request_id=request.id,
                device_credential_id=device.id,
                decision="approve",
                selected_width=1920,
                selected_height=1080,
                selected_fps=15,
                selected_bitrate_bps=2_000_000,
                now=self.now + timedelta(seconds=33),
            )
        )
        self.assertEqual("approved", approved.state)
        self.assertEqual(1920, approved.selected_width)
        self.assertEqual(15, approved.selected_fps)
        self.assertEqual(2_000_000, approved.selected_bitrate_bps)
        self.assertEqual(
            self.now + timedelta(seconds=633), approved.expires_at
        )
        media_offer_sdp = (
            "v=0\r\n"
            "o=- 3 4 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
            "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
        )
        media = asyncio.run(
            self.store.start_video_media(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                browser_offer_sdp=media_offer_sdp,
                relay_candidate_ms=654,
                now=self.now + timedelta(seconds=34),
            )
        )
        self.assertEqual("approved", media.state)
        self.assertEqual(streams[0].session_id, media.stream_session_id)

        pending_media = asyncio.run(
            self.store.list_pending_video_media_offers_for_device(
                device_credential_id=device.id,
                now=self.now + timedelta(seconds=34),
            )
        )
        self.assertEqual([request.id], [item.request_id for item in pending_media])
        media_answer_sdp = (
            "v=0\r\n"
            "o=- 4 5 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
            "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
        )
        answered_media = asyncio.run(
            self.store.record_video_media_answer(
                request_id=request.id,
                device_credential_id=device.id,
                device_answer_sdp=media_answer_sdp,
                now=self.now + timedelta(seconds=35),
            )
        )
        self.assertEqual(media_answer_sdp, answered_media.device_answer_sdp)
        streaming = asyncio.run(
            self.store.mark_video_streaming(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=36),
            )
        )
        self.assertEqual("streaming", streaming.state)
        first_metrics = asyncio.run(
            self.store.record_video_media_metrics(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                metrics_session_id="browser-session-1",
                audio_bytes_sent=1_000,
                audio_bytes_received=2_000,
                video_bytes_received=3_000_000,
                now=self.now + timedelta(seconds=37),
            )
        )
        self.assertEqual(3_003_000, first_metrics.total_media_bytes)
        updated_metrics = asyncio.run(
            self.store.record_video_media_metrics(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                metrics_session_id="browser-session-1",
                audio_bytes_sent=2_000,
                audio_bytes_received=4_000,
                video_bytes_received=5_000_000,
                now=self.now + timedelta(seconds=38),
            )
        )
        self.assertEqual(5_006_000, updated_metrics.total_media_bytes)
        reconnected_metrics = asyncio.run(
            self.store.record_video_media_metrics(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                metrics_session_id="browser-session-2",
                audio_bytes_sent=500,
                audio_bytes_received=1_000,
                video_bytes_received=1_000_000,
                now=self.now + timedelta(seconds=39),
            )
        )
        self.assertEqual(6_007_500, reconnected_metrics.total_media_bytes)
        usage = asyncio.run(
            self.store.month_to_date_usage_aggregates(
                now=self.now + timedelta(seconds=39),
            )
        )
        self.assertEqual(6_007_500, usage[organization.id].turn_relay_bytes)
        with self.assertRaises(ControlPlaneError):
            asyncio.run(
                self.store.stop_video_stream_from_device(
                    request_id=request.id,
                    device_credential_id="another-device",
                    now=self.now + timedelta(seconds=40),
                )
            )
        stopped = asyncio.run(
            self.store.stop_video_stream_from_device(
                request_id=request.id,
                device_credential_id=device.id,
                reason="source_ended",
                now=self.now + timedelta(seconds=40),
            )
        )
        self.assertEqual("stopped", stopped.state)
        self.assertEqual(4, stopped.duration_seconds)
        self.assertEqual(6_007_500, stopped.total_media_bytes)
        durable_status = asyncio.run(
            self.store.get_video_stream_request_for_requester(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
            )
        )
        self.assertEqual("stopped", durable_status.state)
        self.assertEqual("source_ended", durable_status.status_message)
        with self.assertRaises(ControlPlaneError):
            asyncio.run(
                self.store.get_video_media_exchange_for_requester(
                    request_id=request.id,
                    organization_id=organization.id,
                    requester_user_id=owner.id,
                )
            )
        with self.assertRaises(ControlPlaneError):
            asyncio.run(
                self.store.record_video_preflight_result(
                    request_id=request.id,
                    device_credential_id="another-device",
                    route_kind="direct",
                    estimated_uplink_bps=8_000_000,
                    now=self.now + timedelta(seconds=34),
                )
            )
        audit_events = asyncio.run(self.store.list_audit_events())
        self.assertEqual(
            [
                "video.stopped_by_device",
                "video.streaming",
                "video.media_signaling_started",
                "video.approved",
                "video.preflight_completed",
                "video.preflight_started",
                "video.requested",
            ],
            [event.event_type for event in audit_events[:7]],
        )
        audit_by_type = {
            event.event_type: event for event in audit_events[:7]
        }
        self.assertEqual(
            321,
            audit_by_type["video.preflight_started"].details[
                "browser_relay_candidate_ms"
            ],
        )
        self.assertEqual(
            654,
            audit_by_type["video.media_signaling_started"].details[
                "browser_relay_candidate_ms"
            ],
        )

        failed_low_request = asyncio.run(
            self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id=streams[0].session_id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=34),
            )
        )
        asyncio.run(
            self.store.record_video_preflight_result(
                request_id=failed_low_request.id,
                device_credential_id=device.id,
                route_kind="routed",
                estimated_uplink_bps=100_000,
                now=self.now + timedelta(seconds=35),
            )
        )
        with self.assertRaisesRegex(
            ControlPlaneError,
            "only the 640 px",
        ):
            asyncio.run(
                self.store.record_video_stream_decision(
                    request_id=failed_low_request.id,
                    device_credential_id=device.id,
                    decision="approve",
                    selected_width=1280,
                    selected_height=720,
                    selected_fps=5,
                    selected_bitrate_bps=180_000,
                    now=self.now + timedelta(seconds=36),
                )
            )
        fallback_approved = asyncio.run(
            self.store.record_video_stream_decision(
                request_id=failed_low_request.id,
                device_credential_id=device.id,
                decision="approve",
                selected_width=640,
                selected_height=360,
                selected_fps=5,
                selected_bitrate_bps=180_000,
                now=self.now + timedelta(seconds=36),
            )
        )
        self.assertEqual("approved", fallback_approved.state)
        self.assertEqual(180_000, fallback_approved.selected_bitrate_bps)
        asyncio.run(
            self.store.stop_video_stream(
                request_id=failed_low_request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                reason="Video connection failed before packets arrived.",
                now=self.now + timedelta(seconds=37),
            )
        )

        expiring_request = asyncio.run(
            self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id=streams[0].session_id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=34),
            )
        )
        asyncio.run(
            self.store.start_video_preflight(
                request_id=expiring_request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                browser_offer_sdp=offer_sdp,
                now=self.now + timedelta(seconds=35),
            )
        )
        self.assertEqual(
            0,
            asyncio.run(
                self.store.cleanup_expired_video_preflight_exchanges(
                    now=self.now + timedelta(minutes=11),
                )
            ),
        )
        expired_requests = asyncio.run(
            self.store.list_video_stream_requests(
                organization_id=organization.id,
                requester_user_id=owner.id,
                now=self.now + timedelta(minutes=11),
            )
        )
        expired = next(
            item for item in expired_requests if item.id == expiring_request.id
        )
        self.assertEqual("expired", expired.state)
        with self.assertRaises(ControlPlaneError):
            asyncio.run(
                self.store.get_video_preflight_exchange_for_requester(
                    request_id=expiring_request.id,
                    organization_id=organization.id,
                    requester_user_id=owner.id,
                    now=self.now + timedelta(minutes=11),
                )
            )

        other = self.create_organization(
            designator="EXSAR",
            legal_name="Example SAR",
            admin_email="admin@exsar.example",
        )
        self.assertEqual(
            (),
            asyncio.run(
                self.store.list_active_video_streams(
                    other.id,
                    self.now + timedelta(seconds=30),
                )
            ),
        )
        with self.assertRaises(ControlPlaneError):
            asyncio.run(
                self.store.create_video_stream_request(
                    organization_id=other.id,
                    stream_session_id=streams[0].session_id,
                    requester_user_id=owner.id,
                    now=self.now + timedelta(seconds=30),
                )
            )
        self.assertEqual(
            (),
            asyncio.run(
                self.store.list_active_video_streams(
                    organization.id,
                    self.now + timedelta(seconds=46),
                )
            ),
        )

    def test_remote_control_lets_requester_select_quality_and_locks_tablet(self):
        organization = self.create_organization()
        invitation = asyncio.run(self.store.get_invitation(
            organization.designator, organization.primary_admin_email
        ))
        owner = asyncio.run(self.store.activate_owner(
            organization.designator,
            organization.primary_admin_email,
            "correct horse battery staple",
            self.now,
            activation_nonce=invitation.activation_nonce,
        ))
        campaign = asyncio.run(self.store.create_enrollment_campaign(
            organization_id=organization.id,
            label="Remote video tablet",
            created_by_user_id=owner.id,
            expires_in_hours=24,
            max_redemptions=1,
            now=self.now,
        ))
        device = asyncio.run(self.store.issue_device_credential(
            campaign_id=campaign.id,
            organization_id=organization.id,
            device_name="Ken's iPad",
            platform="ios",
            now=self.now,
        ))
        stream = asyncio.run(self.store.advertise_video_stream(
            organization_id=organization.id,
            device_credential_id=device.id,
            session_id="00000000-0000-0000-0000-000000000010",
            incident_name="Remote control test",
            drone_designator="NCS1",
            source_width=1920,
            source_height=1080,
            source_fps=30,
            source_bitrate_bps=4_000_000,
            remote_control_enabled=True,
            now=self.now,
        ))
        request = asyncio.run(self.store.create_video_stream_request(
            organization_id=organization.id,
            stream_session_id=stream.session_id,
            requester_user_id=owner.id,
            now=self.now + timedelta(seconds=1),
        ))
        asyncio.run(self.store.start_video_preflight(
            request_id=request.id,
            organization_id=organization.id,
            requester_user_id=owner.id,
            browser_offer_sdp="v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n",
            now=self.now + timedelta(seconds=2),
        ))
        asyncio.run(self.store.record_video_preflight_result(
            request_id=request.id,
            device_credential_id=device.id,
            route_kind="routed",
            estimated_uplink_bps=1_500_000,
            now=self.now + timedelta(seconds=3),
        ))
        asyncio.run(self.store.advertise_video_stream(
            organization_id=organization.id,
            device_credential_id=device.id,
            session_id=stream.session_id,
            incident_name="Remote control test",
            drone_designator="NCS1",
            source_width=1920,
            source_height=1080,
            source_fps=14.3,
            source_bitrate_bps=4_000_000,
            remote_control_enabled=True,
            now=self.now + timedelta(milliseconds=3250),
        ))
        preflight = asyncio.run(
            self.store.get_video_preflight_exchange_for_requester(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                now=self.now + timedelta(milliseconds=3400),
            )
        )
        self.assertEqual(30.0, preflight.source_fps)
        with self.assertRaisesRegex(
            ControlPlaneError,
            "bandwidth-qualified video choices",
        ):
            asyncio.run(self.store.record_video_stream_decision(
                request_id=request.id,
                requester_user_id=owner.id,
                organization_id=organization.id,
                decision="approve",
                selected_width=800,
                selected_height=450,
                selected_fps=12,
                selected_bitrate_bps=900_000,
                now=self.now + timedelta(milliseconds=3500),
            ))
        approved = asyncio.run(self.store.record_video_stream_decision(
            request_id=request.id,
            requester_user_id=owner.id,
            organization_id=organization.id,
            decision="approve",
            selected_width=960,
            selected_height=540,
            selected_fps=15,
            selected_bitrate_bps=1_200_000,
            now=self.now + timedelta(seconds=4),
        ))
        self.assertEqual("approved", approved.state)
        with self.assertRaisesRegex(ControlPlaneError, "already sharing"):
            asyncio.run(self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id=stream.session_id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=5),
            ))
        media = asyncio.run(self.store.start_video_media(
            request_id=request.id,
            organization_id=organization.id,
            requester_user_id=owner.id,
            browser_offer_sdp="v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n",
            now=self.now + timedelta(seconds=6),
        ))
        self.assertEqual(owner.email, media.requester_email)
        self.assertEqual((960, 540, 15.0, 1_200_000), (
            media.selected_width,
            media.selected_height,
            media.selected_fps,
            media.selected_bitrate_bps,
        ))

    def test_video_request_decline_and_priority_replacement_are_device_scoped(self):
        organization = self.create_organization()
        invitation = asyncio.run(self.store.get_invitation(
            organization.designator,
            organization.primary_admin_email,
        ))
        owner = asyncio.run(self.store.activate_owner(
            organization.designator,
            organization.primary_admin_email,
            "correct horse battery staple",
            self.now,
            activation_nonce=invitation.activation_nonce,
        ))
        campaign = asyncio.run(self.store.create_enrollment_campaign(
            organization_id=organization.id,
            label="Priority video tablet",
            created_by_user_id=owner.id,
            expires_in_hours=24,
            max_redemptions=1,
            now=self.now,
        ))
        device = asyncio.run(self.store.issue_device_credential(
            campaign_id=campaign.id,
            organization_id=organization.id,
            device_name="Pilot tablet",
            platform="android",
            now=self.now,
        ))
        session_ids = (
            "10000000-0000-0000-0000-000000000001",
            "10000000-0000-0000-0000-000000000002",
        )
        for index, session_id in enumerate(session_ids):
            asyncio.run(self.store.advertise_video_stream(
                organization_id=organization.id,
                device_credential_id=device.id,
                device_name="Pilot tablet",
                session_id=session_id,
                incident_name="Priority test",
                drone_designator=f"DRONE-{index + 1}",
                source_width=1280,
                source_height=720,
                source_fps=15,
                source_bitrate_bps=1_000_000,
                source_codec="H264",
                now=self.now,
            ))

        offer_sdp = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"

        def ready_request(session_id, offset):
            request = asyncio.run(self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id=session_id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=offset),
            ))
            asyncio.run(self.store.start_video_preflight(
                request_id=request.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
                browser_offer_sdp=offer_sdp,
                now=self.now + timedelta(seconds=offset + 1),
            ))
            asyncio.run(self.store.record_video_preflight_result(
                request_id=request.id,
                device_credential_id=device.id,
                route_kind="routed",
                estimated_uplink_bps=2_000_000,
                now=self.now + timedelta(seconds=offset + 2),
            ))
            return request

        initial = ready_request(session_ids[0], 1)
        declined_initial = asyncio.run(self.store.record_video_stream_decision(
            request_id=initial.id,
            device_credential_id=device.id,
            decision="decline",
            now=self.now + timedelta(seconds=4),
        ))
        self.assertEqual("insufficient bandwidth", declined_initial.status_message)

        first_viewer = ready_request(session_ids[0], 5)
        asyncio.run(self.store.record_video_stream_decision(
            request_id=first_viewer.id,
            device_credential_id=device.id,
            decision="approve",
            selected_width=640,
            selected_height=360,
            selected_fps=5,
            selected_bitrate_bps=200_000,
            now=self.now + timedelta(seconds=8),
        ))
        asyncio.run(self.store.start_video_media(
            request_id=first_viewer.id,
            organization_id=organization.id,
            requester_user_id=owner.id,
            browser_offer_sdp=offer_sdp,
            now=self.now + timedelta(seconds=9),
        ))

        lower_priority = asyncio.run(self.store.create_video_stream_request(
            organization_id=organization.id,
            stream_session_id=session_ids[1],
            requester_user_id=owner.id,
            now=self.now + timedelta(seconds=10),
        ))
        with self.assertRaisesRegex(ControlPlaneError, "already in progress"):
            asyncio.run(self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id=session_ids[0],
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=11),
            ))
        asyncio.run(self.store.start_video_preflight(
            request_id=lower_priority.id,
            organization_id=organization.id,
            requester_user_id=owner.id,
            browser_offer_sdp=offer_sdp,
            now=self.now + timedelta(seconds=11),
        ))
        asyncio.run(self.store.record_video_preflight_result(
            request_id=lower_priority.id,
            device_credential_id=device.id,
            route_kind="routed",
            estimated_uplink_bps=2_000_000,
            now=self.now + timedelta(seconds=12),
        ))
        declined_secondary = asyncio.run(
            self.store.record_video_stream_decision(
                request_id=lower_priority.id,
                device_credential_id=device.id,
                decision="decline",
                now=self.now + timedelta(seconds=13),
            )
        )
        self.assertEqual(
            f"App already streaming to {owner.email}",
            declined_secondary.status_message,
        )

        replacement = ready_request(session_ids[1], 14)
        asyncio.run(self.store.record_video_stream_decision(
            request_id=replacement.id,
            device_credential_id=device.id,
            decision="approve",
            selected_width=640,
            selected_height=360,
            selected_fps=5,
            selected_bitrate_bps=200_000,
            now=self.now + timedelta(seconds=17),
        ))
        displaced = asyncio.run(
            self.store.get_video_media_exchange_for_requester(
                request_id=first_viewer.id,
                organization_id=organization.id,
                requester_user_id=owner.id,
            )
        )
        self.assertEqual("redirected", displaced.state)
        self.assertEqual(
            f"Stream redirected to {owner.email}",
            displaced.status_message,
        )

    def test_missing_video_source_closes_pending_request(self):
        organization = self.create_organization()
        invitation = asyncio.run(
            self.store.get_invitation(
                organization.designator,
                organization.primary_admin_email,
            )
        )
        owner = asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
                activation_nonce=invitation.activation_nonce,
            )
        )
        campaign = asyncio.run(
            self.store.create_enrollment_campaign(
                organization_id=organization.id,
                label="Video tablets",
                created_by_user_id=owner.id,
                expires_in_hours=24,
                max_redemptions=1,
                now=self.now,
            )
        )
        device = asyncio.run(
            self.store.issue_device_credential(
                campaign_id=campaign.id,
                organization_id=organization.id,
                device_name="Ken's iPad",
                platform="ios",
                now=self.now,
            )
        )
        stream = asyncio.run(
            self.store.advertise_video_stream(
                organization_id=organization.id,
                device_credential_id=device.id,
                device_name="Ken's iPad",
                session_id="00000000-0000-0000-0000-000000000099",
                incident_name="Training",
                drone_designator="NCS1m3",
                now=self.now,
            )
        )
        request = asyncio.run(
            self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id=stream.session_id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=1),
            )
        )

        unavailable = asyncio.run(
            self.store.record_video_stream_unavailable(
                request_id=request.id,
                device_credential_id=device.id,
                stream_session_id=stream.session_id,
                error_code="e_nosuch_stream",
                now=self.now + timedelta(seconds=2),
            )
        )
        self.assertEqual("e_nosuch_stream", unavailable.state)
        self.assertEqual(
            (),
            asyncio.run(
                self.store.list_pending_video_stream_requests_for_device(
                    device_credential_id=device.id,
                    now=self.now + timedelta(seconds=3),
                )
            ),
        )

        replacement = asyncio.run(
            self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id=stream.session_id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=3),
            )
        )
        retired = asyncio.run(
            self.store.reconcile_device_video_streams(
                organization_id=organization.id,
                device_credential_id=device.id,
                active_session_ids=(),
                now=self.now + timedelta(seconds=4),
            )
        )
        self.assertEqual(1, retired)
        requests = asyncio.run(
            self.store.list_video_stream_requests(
                organization_id=organization.id,
                requester_user_id=owner.id,
                now=self.now + timedelta(seconds=5),
            )
        )
        retired_request = next(item for item in requests if item.id == replacement.id)
        self.assertEqual("e_nosuch_stream", retired_request.state)

    def test_repeated_login_failures_temporarily_lock_account(self):
        organization = self.create_organization()
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now,
            )
        )
        for attempt in range(5):
            rejected = asyncio.run(
                self.store.authenticate_user(
                    organization.designator,
                    organization.primary_admin_email,
                    "incorrect password",
                    self.now + timedelta(seconds=attempt),
                )
            )
            self.assertIsNone(rejected)

        locked = asyncio.run(
            self.store.authenticate_user(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now + timedelta(minutes=1),
            )
        )
        self.assertIsNone(locked)

        unlocked = asyncio.run(
            self.store.authenticate_user(
                organization.designator,
                organization.primary_admin_email,
                "correct horse battery staple",
                self.now + timedelta(minutes=16),
            )
        )
        self.assertIsNotNone(unlocked)

    def test_verified_google_email_activates_exact_pending_organization_user(self):
        organization = self.create_organization()
        expired = asyncio.run(
            self.store.authorize_google_user(
                organization.designator,
                organization.primary_admin_email,
                self.now + timedelta(days=8),
            )
        )
        self.assertIsNone(expired)

        authorized = asyncio.run(
            self.store.authorize_google_user(
                organization.designator.lower(),
                organization.primary_admin_email.upper(),
                self.now + timedelta(minutes=1),
            )
        )

        self.assertIsNotNone(authorized)
        self.assertEqual("active", authorized.state)
        self.assertIsNone(
            asyncio.run(
                self.store.authorize_google_user(
                    organization.designator,
                    "other@example.org",
                    self.now + timedelta(minutes=2),
                )
            )
        )

    def test_organization_password_reset_is_single_use_and_non_enumerating(self):
        organization = self.create_organization()
        asyncio.run(
            self.store.activate_owner(
                organization.designator,
                organization.primary_admin_email,
                "original complex password",
                now=self.now,
            )
        )
        self.assertIsNone(
            asyncio.run(
                self.store.issue_organization_password_reset(
                    designator=organization.designator,
                    email="unknown@example.org",
                    now=self.now,
                )
            )
        )
        token = asyncio.run(
            self.store.issue_organization_password_reset(
                designator=organization.designator,
                email=organization.primary_admin_email,
                now=self.now,
            )
        )
        reset = asyncio.run(
            self.store.set_organization_password_from_reset(
                designator=organization.designator,
                token=token,
                new_password="replacement complex password",
                now=self.now + timedelta(minutes=14, seconds=59),
            )
        )
        self.assertIsNotNone(reset)
        self.assertIsNone(
            asyncio.run(
                self.store.set_organization_password_from_reset(
                    designator=organization.designator,
                    token=token,
                    new_password="replayed complex password",
                    now=self.now + timedelta(minutes=15),
                )
            )
        )
        signed_in = asyncio.run(
            self.store.authenticate_user(
                organization.designator,
                organization.primary_admin_email,
                "replacement complex password",
                self.now + timedelta(minutes=16),
            )
        )
        self.assertIsNotNone(signed_in)

    def test_ledger_is_idempotent_and_balance_is_organization_visible(self):
        organization = self.create_organization()

        first = asyncio.run(
            self.store.append_ledger_entry(
                organization_id=organization.id,
                entry_type="credit",
                amount=Decimal("10.00"),
                description="Simulation account credit",
                idempotency_key="simulation-credit-ncssar-1",
                created_by_type="platform_admin",
                created_by_id="platform-admin",
                now=self.now,
            )
        )
        repeated = asyncio.run(
            self.store.append_ledger_entry(
                organization_id=organization.id,
                entry_type="credit",
                amount=Decimal("10.00"),
                description="Simulation account credit",
                idempotency_key="simulation-credit-ncssar-1",
                created_by_type="platform_admin",
                created_by_id="platform-admin",
                now=self.now,
            )
        )

        self.assertEqual(first.id, repeated.id)
        self.assertEqual(
            Decimal("10.0000"),
            asyncio.run(self.store.list_organizations())[0].credit_balance,
        )
        self.assertEqual(1, len(asyncio.run(self.store.list_ledger(organization.id))))

        with self.assertRaises(ControlPlaneError):
            asyncio.run(
                self.store.append_ledger_entry(
                    organization_id=organization.id,
                    entry_type="charge",
                    amount=Decimal("-10.00"),
                    description="Conflicting retry",
                    idempotency_key="simulation-credit-ncssar-1",
                    created_by_type="platform_admin",
                    created_by_id="platform-admin",
                    now=self.now,
                )
            )

    def test_historical_ledger_does_not_change_extended_beta_lifecycle(self):
        organization = self.create_organization()
        asyncio.run(
            self.store.append_ledger_entry(
                organization_id=organization.id,
                entry_type="credit",
                amount=Decimal("10.00"),
                description="Prepaid account credit",
                idempotency_key="credit-ncssar-funded-1",
                created_by_type="platform_admin",
                created_by_id="platform-admin",
                now=self.now,
            )
        )

        funded = asyncio.run(self.store.get_organization("NCSSAR"))
        self.assertEqual("extended_beta", funded.lifecycle_state)
        self.assertEqual("extended beta", funded.billing_mode)
        self.assertEqual(Decimal("10.0000"), funded.credit_balance)

        asyncio.run(
            self.store.record_daily_usage(
                organization_id=organization.id,
                usage_date="2026-07-30",
                compute_cost=Decimal("4.00"),
                network_cost=Decimal("6.00"),
                now=self.now + timedelta(days=1),
            )
        )

        grace = asyncio.run(self.store.get_organization("NCSSAR"))
        self.assertEqual("extended_beta", grace.lifecycle_state)
        self.assertEqual("extended beta", grace.billing_mode)
        self.assertEqual(Decimal("0.00"), grace.credit_balance)
        notifications = asyncio.run(
            self.store.list_pending_billing_notifications()
        )
        self.assertEqual((), notifications)

        asyncio.run(
            self.store.append_ledger_entry(
                organization_id=organization.id,
                entry_type="credit",
                amount=Decimal("5.00"),
                description="Additional prepaid account credit",
                idempotency_key="credit-ncssar-funded-2",
                created_by_type="platform_admin",
                created_by_id="platform-admin",
                now=self.now + timedelta(days=2),
            )
        )
        refunded = asyncio.run(self.store.get_organization("NCSSAR"))
        self.assertEqual("extended_beta", refunded.lifecycle_state)
        self.assertEqual(Decimal("5.0000"), refunded.credit_balance)
        self.assertIsNone(refunded.trial_ends_at)

    def test_extended_beta_allowance_notices_and_video_cutoff_are_monthly(self):
        organization = self.create_organization()
        billing_admin = asyncio.run(self.store.add_user(
            organization_id=organization.id,
            display_name="Billing Administrator",
            email="billing@ncssar.example",
            roles=("billing_admin",),
            actor_id="platform-admin",
            now=self.now,
        ))

        async def activate_billing_admin():
            async with self.store.sessions() as session:
                user = await session.get(OrganizationUser, billing_admin.id)
                user.state = "active"
                await session.commit()

        asyncio.run(activate_billing_admin())

        reconciled = asyncio.run(self.store.reconcile_extended_beta_allowances(
            billing_month="2026-07",
            billing_data_through=self.now,
            actual_costs={organization.id: Decimal("9.10")},
            forecast_costs={organization.id: Decimal("12.00")},
            now=self.now,
        ))
        self.assertEqual(1, len(reconciled))
        self.assertFalse(reconciled[0].video_streaming_allowed)
        self.assertEqual(Decimal("10.000000"), reconciled[0].allowance_amount)
        notifications = asyncio.run(self.store.list_pending_billing_notifications())
        self.assertEqual(
            ["beta_allowance_on_track", "beta_video_disabled"],
            [item.notification_type for item in notifications],
        )
        self.assertTrue(all(
            item.administrator_email == "billing@ncssar.example"
            for item in notifications
        ))
        with self.assertRaisesRegex(ControlPlaneError, "Remote video streaming is disabled"):
            asyncio.run(self.store.create_video_stream_request(
                organization_id=organization.id,
                stream_session_id="unreachable-stream",
                requester_user_id="unreachable-user",
                now=self.now,
            ))

        asyncio.run(self.store.reconcile_extended_beta_allowances(
            billing_month="2026-07",
            billing_data_through=self.now + timedelta(hours=1),
            actual_costs={organization.id: Decimal("10.01")},
            forecast_costs={organization.id: Decimal("12.50")},
            now=self.now + timedelta(hours=1),
        ))
        notifications = asyncio.run(self.store.list_pending_billing_notifications())
        self.assertEqual(
            [
                "beta_allowance_on_track",
                "beta_video_disabled",
                "beta_allowance_exceeded",
            ],
            [item.notification_type for item in notifications],
        )
        current = asyncio.run(self.store.get_organization(organization.designator))
        self.assertEqual("extended_beta", current.lifecycle_state)
        self.assertEqual("simulation ready", current.provisioning_state)

    def test_allowance_notice_falls_back_to_primary_admin(self):
        organization = self.create_organization()
        asyncio.run(self.store.reconcile_extended_beta_allowances(
            billing_month="2026-07",
            billing_data_through=self.now,
            actual_costs={organization.id: Decimal("1.00")},
            forecast_costs={organization.id: Decimal("11.00")},
            now=self.now,
        ))
        notifications = asyncio.run(self.store.list_pending_billing_notifications())
        self.assertEqual(1, len(notifications))
        self.assertEqual(
            "admin@ncssar.example",
            notifications[0].administrator_email,
        )

    def test_video_cutoff_stops_an_active_request(self):
        organization = self.create_organization()
        invitation = asyncio.run(self.store.get_invitation(
            organization.designator,
            organization.primary_admin_email,
        ))
        owner = asyncio.run(self.store.activate_owner(
            organization.designator,
            organization.primary_admin_email,
            "correct horse battery staple",
            self.now,
            activation_nonce=invitation.activation_nonce,
        ))
        campaign = asyncio.run(self.store.create_enrollment_campaign(
            organization_id=organization.id,
            label="Allowance cutoff tablet",
            created_by_user_id=owner.id,
            expires_in_hours=24,
            max_redemptions=1,
            now=self.now,
        ))
        device = asyncio.run(self.store.issue_device_credential(
            campaign_id=campaign.id,
            organization_id=organization.id,
            device_name="Field tablet",
            platform="android",
            now=self.now,
        ))
        stream = asyncio.run(self.store.advertise_video_stream(
            organization_id=organization.id,
            device_credential_id=device.id,
            device_name="Field tablet",
            session_id="00000000-0000-0000-0000-000000000090",
            incident_name="Training",
            drone_designator="9A",
            source_width=1280,
            source_height=720,
            source_fps=15,
            source_bitrate_bps=1_000_000,
            source_codec="H264",
            timezone_name="UTC",
            now=self.now,
        ))
        request = asyncio.run(self.store.create_video_stream_request(
            organization_id=organization.id,
            stream_session_id=stream.session_id,
            requester_user_id=owner.id,
            now=self.now,
        ))

        asyncio.run(self.store.reconcile_extended_beta_allowances(
            billing_month="2026-07",
            billing_data_through=self.now,
            actual_costs={organization.id: Decimal("9.00")},
            forecast_costs={organization.id: Decimal("9.50")},
            now=self.now,
        ))

        requests = asyncio.run(self.store.list_video_stream_requests(
            organization_id=organization.id,
            requester_user_id=owner.id,
            now=self.now,
        ))
        stopped = next(item for item in requests if item.id == request.id)
        self.assertEqual("stopped", stopped.state)
        self.assertIn("extended beta allowance month", stopped.status_message)

    def test_archive_requires_and_audits_administrator_contact(self):
        organization = self.create_organization()
        with self.assertRaisesRegex(ControlPlaneError, "Record how and when"):
            asyncio.run(
                self.store.archive_organization(
                    designator=organization.designator,
                    actor_id="platform-admin",
                    administrator_contact="",
                    now=self.now + timedelta(hours=1),
                )
            )

        asyncio.run(
            self.store.archive_organization(
                designator=organization.designator,
                actor_id="platform-admin",
                administrator_contact=(
                    "Called Primary Administrator on 30 Jul 2026; export confirmed."
                ),
                now=self.now + timedelta(hours=1),
            )
        )
        audit_events = asyncio.run(self.store.list_audit_events())
        self.assertIn(
            "Called Primary Administrator",
            audit_events[0].details["administrator_contact"],
        )

    def test_prefunded_live_account_does_not_start_trial_on_activation(self):
        organization = self.create_organization(
            designator="FUNDEDSAR",
            admin_email="admin@fundedsar.example",
            simulation=False,
        )
        asyncio.run(
            self.store.append_ledger_entry(
                organization_id=organization.id,
                entry_type="credit",
                amount=Decimal("25.00"),
                description="Prepaid account credit",
                idempotency_key="credit-fundedsar-1",
                created_by_type="platform_admin",
                created_by_id="platform-admin",
                now=self.now,
            )
        )
        asyncio.run(
            self.store.mark_organization_invitation_sent(
                organization_id=organization.id,
                actor_id="platform-admin",
                now=self.now,
            )
        )
        invitation = asyncio.run(
            self.store.get_invitation("FUNDEDSAR", "admin@fundedsar.example")
        )
        asyncio.run(
            self.store.activate_owner(
                "FUNDEDSAR",
                "admin@fundedsar.example",
                "generated complex password",
                now=self.now + timedelta(hours=1),
                activation_nonce=invitation.activation_nonce,
            )
        )

        activated = asyncio.run(self.store.get_organization("FUNDEDSAR"))
        self.assertEqual("extended_beta", activated.lifecycle_state)
        self.assertEqual(Decimal("25.0000"), activated.credit_balance)
        self.assertIsNone(activated.trial_ends_at)

    def test_daily_usage_contains_aggregates_only(self):
        organization = self.create_organization()
        asyncio.run(
            self.store.record_daily_usage(
                organization_id=organization.id,
                usage_date="2026-07-30",
                compute_units=Decimal("3"),
                compute_cost=Decimal("0.15"),
                network_bytes=1024,
                network_cost=Decimal("0.02"),
                faa_proxy_requests=12,
                faa_proxy_cost=Decimal("0.01"),
                turn_relay_bytes=2048,
                turn_relay_cost=Decimal("0.04"),
                now=self.now,
            )
        )

        costs = asyncio.run(self.store.month_to_date_usage_costs(self.now))

        self.assertEqual(Decimal("0.150000"), costs[organization.id].compute)
        self.assertEqual(Decimal("0.020000"), costs[organization.id].network)
        self.assertEqual(Decimal("0.050000"), costs[organization.id].other)

    def test_increment_daily_usage_accumulates_privacy_safe_counters(self):
        organization = self.create_organization()
        for _ in range(2):
            asyncio.run(
                self.store.increment_daily_usage(
                    organization_id=organization.id,
                    usage_date="2026-07-30",
                    compute_units=Decimal("1"),
                    network_bytes=1024,
                    storage_byte_days=256,
                    database_units=Decimal("0.5"),
                    faa_proxy_requests=1,
                    turn_relay_bytes=512,
                    now=self.now,
                )
            )

        usage = asyncio.run(
            self.store.month_to_date_usage_aggregates(self.now)
        )[organization.id]
        self.assertEqual(Decimal("2.000000"), usage.compute_units)
        self.assertEqual(2048, usage.network_bytes)
        self.assertEqual(512, usage.storage_byte_days)
        self.assertEqual(Decimal("1.000000"), usage.database_units)
        self.assertEqual(2, usage.faa_proxy_requests)
        self.assertEqual(1024, usage.turn_relay_bytes)
        column_names = {column.name for column in UsageDaily.__table__.columns}
        for forbidden in (
            "flight_id",
            "incident",
            "remote_id",
            "map_id",
            "user_id",
            "archive_relpath",
        ):
            self.assertNotIn(forbidden, column_names)


class ControlPlaneValidationTest(unittest.TestCase):
    def test_database_pool_recovers_from_idle_server_disconnects(self):
        store = ControlPlaneStore("sqlite+aiosqlite:///:memory:")
        try:
            self.assertTrue(store.engine.pool._pre_ping)
            self.assertEqual(300, store.engine.pool._recycle)
        finally:
            asyncio.run(store.dispose())

    def test_designator_is_normalized_and_restricted(self):
        self.assertEqual("NCSSAR", normalize_designator(" ncssar "))
        for value in ("N", "NC-SAR", "../SAR", "SAR agency", ""):
            with self.subTest(value=value):
                with self.assertRaises(InvalidOrganizationError):
                    normalize_designator(value)

    def test_scrypt_password_hash_is_salted_and_verifiable(self):
        first = hash_password("correct horse battery staple")
        second = hash_password("correct horse battery staple")

        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("incorrect password", first))
        self.assertNotIn("correct horse", first)

    def test_short_password_is_rejected(self):
        with self.assertRaises(ValueError):
            hash_password("too short")

    def test_control_plane_cannot_share_tenant_database_url(self):
        with self.assertRaises(ValueError):
            require_separate_database(
                "postgresql+asyncpg://service/control",
                "postgresql+asyncpg://service/control",
            )
        require_separate_database(
            "postgresql+asyncpg://service/control",
            "postgresql+asyncpg://service/tenant",
        )


if __name__ == "__main__":
    unittest.main()
