import pathlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from unittest import mock


class GuardedReleaseTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(__file__).resolve().parents[1]

    def test_app_exposes_health_and_protected_activity_gate(self):
        source = (self.root / "main.py").read_text()
        self.assertIn('@app.get("/livez")', source)
        self.assertIn('@app.get("/readyz")', source)
        self.assertIn('@app.get("/deployment-readiness")', source)
        self.assertIn("secrets.compare_digest(candidate, DEPLOYMENT_GATE_KEY)", source)
        self.assertIn("recent_coordination_zones", source)
        self.assertIn("active_video_streams", source)
        self.assertIn("active_video_requests", source)
        self.assertIn('"activity_details": activity_details', source)
        self.assertIn("deployment_connection_details", source)
        self.assertIn("probe_flightlog_storage", source)

    def test_cloud_deploy_wires_probe_and_dedicated_gate_secret(self):
        deploy = (self.root / "deploy.sh").read_text()
        pilot = (self.root / "deploy_pilot.sh").read_text()
        self.assertIn("DEPLOYMENT_GATE_KEY=${DEPLOYMENT_GATE_KEY_SECRET_NAME}:latest", deploy)
        self.assertIn('--startup-probe "httpGet.path=/livez', deploy)
        self.assertIn('--liveness-probe "httpGet.path=/livez', deploy)
        self.assertIn("r2c-deployment-gate-key", pilot)
        self.assertIn('"FLIGHTLOGS_STORAGE_REQUIRED"', deploy)
        self.assertIn('FAST_UI_DEPLOY="${FAST_UI_DEPLOY:-0}"', deploy)
        self.assertIn("reusing the production service's existing secrets and IAM bindings", deploy)

    def test_candidate_promotion_and_rollback_are_separate_commands(self):
        guard = (self.root / "scripts" / "release_guard.py").read_text()
        self.assertIn("refresh_staging_databases.sh", guard)
        self.assertIn("setup_pilot_staging.sh", guard)
        self.assertIn("deploy_staging.sh", guard)
        self.assertIn("staging_regression(staging_url", guard)
        self.assertIn('"CONTAINER_IMAGE": image_digest', guard)
        self.assertIn("candidate_digest != image_digest", guard)
        self.assertIn('setup_command.append("--reuse-existing")', guard)
        self.assertIn('"reused_staging": reuse_staging', guard)
        self.assertIn('"--reuse-staging"', guard)
        self.assertIn("validate_staging_reuse_state()", guard)
        self.assertIn("save_staging_refresh_state()", guard)
        self.assertIn("committed_source_snapshot()", guard)
        self.assertIn('staging_env["DEPLOY_SOURCE_DIR"]', guard)
        self.assertIn("websocket_smoke", guard)
        self.assertIn("Candidate preparation failed; removing ephemeral staging resources.", guard)
        self.assertIn("cleanup_pilot_staging.sh", guard)
        self.assertIn('"ACTIVATE_LATEST_REVISION": "0"', guard)
        self.assertIn('"REVISION_TAG": "candidate"', guard)
        self.assertIn("deployment_readiness(PUBLIC_URL", guard)
        self.assertIn('result.get("activity_details", {})', guard)
        self.assertIn("regression(candidate_url", guard)
        self.assertIn("--to-revisions=", guard)
        for name in (
            "deploy_candidate.sh",
            "test_candidate.sh",
            "promote_candidate.sh",
            "rollback_release.sh",
        ):
            self.assertTrue((self.root / name).exists())
            self.assertIn(".venv/bin/python", (self.root / name).read_text())

    def test_presentation_bypass_is_explicit_scoped_and_repeatable(self):
        guard = (self.root / "scripts" / "release_guard.py").read_text()
        publisher = self.root / "publish_release.sh"

        self.assertTrue(publisher.is_file())
        self.assertIn("--bypass-safety-checks", publisher.read_text())
        self.assertIn("qualification-current", publisher.read_text())
        self.assertIn("--allow-non-presentation-changes", publisher.read_text())
        self.assertIn("./qualify_release.sh", publisher.read_text())
        self.assertIn(
            "Complete local qualification and candidate/public health checks remain required.",
            publisher.read_text(),
        )
        self.assertNotIn("./test_candidate.sh", publisher.read_text())
        self.assertIn('BYPASS_ALLOWED_PATH_PREFIXES = ("static/", "templates/", "tests/")', guard)
        self.assertIn('BYPASS_ALLOWED_PATHS = {"changes.txt"}', guard)
        self.assertIn("secret_baseline_is_semantically_unchanged", guard)
        self.assertIn('"FAST_UI_DEPLOY": "1"', guard)
        self.assertIn("candidate_regression_passed_at_epoch", guard)
        self.assertIn("Non-presentation fast publication requires", guard)
        self.assertIn("validate_bypass_change_scope(", guard)
        self.assertIn('require_activity_gate=False', guard)
        self.assertIn('deploy_env.pop("CONTAINER_IMAGE", None)', guard)
        self.assertIn("Promotion must repeat --bypass-safety-checks", guard)
        self.assertIn('"bypassed_safety_checks": True', guard)

        rejected = subprocess.run(
            ["sh", str(publisher), "--ui-test", "133"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, rejected.returncode)
        self.assertIn("--bypass-safety-checks", rejected.stderr)

    def test_presentation_bypass_change_scope_rejects_backend_files(self):
        spec = importlib.util.spec_from_file_location(
            "release_guard_bypass_test",
            self.root / "scripts" / "release_guard.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def allowed_run(*args, capture=False, env=None):
            if args[1] == "describe":
                return "v1.4.41"
            return "templates/organization_streams.html\nstatic/video_media.js\ntests/test_organization_routes.py\nchanges.txt"

        with mock.patch.object(module, "run", side_effect=allowed_run):
            previous_tag, paths = module.validate_bypass_change_scope()
        self.assertEqual("v1.4.41", previous_tag)
        self.assertIn("static/video_media.js", paths)

        def unsafe_run(*args, capture=False, env=None):
            if args[1] == "describe":
                return "v1.4.41"
            return "templates/organization_streams.html\nmain.py"

        with mock.patch.object(module, "run", side_effect=unsafe_run):
            with self.assertRaisesRegex(RuntimeError, "normal guarded release.*main.py"):
                module.validate_bypass_change_scope()

        def baseline_run(*args, capture=False, env=None):
            if args[1] == "describe":
                return "v1.4.43"
            return "static/video_media.js\n.secrets.baseline"

        with (
            mock.patch.object(module, "run", side_effect=baseline_run),
            mock.patch.object(
                module,
                "secret_baseline_is_semantically_unchanged",
                return_value=True,
            ),
        ):
            _previous_tag, paths = module.validate_bypass_change_scope()
        self.assertIn(".secrets.baseline", paths)

        with (
            mock.patch.object(module, "run", side_effect=baseline_run),
            mock.patch.object(
                module,
                "secret_baseline_is_semantically_unchanged",
                return_value=False,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, r"\.secrets\.baseline"):
                module.validate_bypass_change_scope()

    def test_secret_baseline_location_changes_are_semantically_ignored(self):
        spec = importlib.util.spec_from_file_location(
            "release_guard_baseline_test",
            self.root / "scripts" / "release_guard.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        previous = {
            "generated_at": "earlier",
            "results": {"tests/example.py": [{
                "type": "Secret Keyword",
                "hashed_secret": "same-fingerprint",  # pragma: allowlist secret
                "line_number": 40,
            }]},
        }
        current = {
            "generated_at": "later",
            "results": {"tests/example.py": [{
                "type": "Secret Keyword",
                "hashed_secret": "same-fingerprint",  # pragma: allowlist secret
                "line_number": 57,
            }]},
        }
        changed = json.loads(json.dumps(current))
        changed["results"]["tests/example.py"][0]["hashed_secret"] = (  # pragma: allowlist secret
            "different"
        )

        self.assertEqual(
            module._normalized_secret_baseline(previous),
            module._normalized_secret_baseline(current),
        )
        self.assertNotEqual(
            module._normalized_secret_baseline(previous),
            module._normalized_secret_baseline(changed),
        )

    def test_explicit_bypass_scope_override_keeps_changes_auditable(self):
        spec = importlib.util.spec_from_file_location(
            "release_guard_scope_override_test",
            self.root / "scripts" / "release_guard.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def fake_run(*args, capture=False, env=None):
            if args[1] == "describe":
                return "v1.4.43"
            return "main.py\ncontrol_plane.py"

        with mock.patch.object(module, "run", side_effect=fake_run):
            previous_tag, paths = module.validate_bypass_change_scope(
                allow_non_presentation_changes=True
            )
        self.assertEqual("v1.4.43", previous_tag)
        self.assertEqual(("main.py", "control_plane.py"), paths)

    def test_qualification_receipt_is_wired_into_full_and_fast_release(self):
        qualification = (self.root / "qualify_release.sh").read_text()
        publisher = (self.root / "publish_release.sh").read_text()
        guard = (self.root / "scripts" / "release_guard.py").read_text()

        self.assertIn("record-qualification", qualification)
        self.assertIn("qualification-current", publisher)
        self.assertIn("QUALIFICATION_RECEIPT_MAX_AGE_SECONDS", guard)
        self.assertIn('run("git", "status", "--porcelain"', guard)

    def test_qualification_receipt_matches_only_the_clean_exact_commit(self):
        spec = importlib.util.spec_from_file_location(
            "release_guard_receipt_test",
            self.root / "scripts" / "release_guard.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        identity = {"commit": "commit-a", "tree": "tree-a"}

        def fake_run(*args, capture=False, env=None):
            if args[1:3] == ("status", "--porcelain"):
                return ""
            if args[1:3] == ("rev-parse", "HEAD"):
                return identity["commit"]
            if args[1:3] == ("rev-parse", "HEAD^{tree}"):
                return identity["tree"]
            raise AssertionError(args)

        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt = pathlib.Path(temporary_directory) / "qualification.json"
            with (
                mock.patch.object(module, "QUALIFICATION_RECEIPT_PATH", receipt),
                mock.patch.object(module, "run", side_effect=fake_run),
                mock.patch.object(module.time, "time", return_value=1_000),
            ):
                self.assertTrue(module.record_qualification_receipt())
                self.assertTrue(module.qualification_receipt_is_current())
                identity["commit"] = "commit-b"
                self.assertFalse(module.qualification_receipt_is_current())

    def test_local_release_gate_checks_migration_rollback_compatibility(self):
        release_check = (self.root / "release_check.sh").read_text()
        self.assertIn("scripts/check_migration_compatibility.py", release_check)

    def test_staging_clone_lifecycle_is_explicitly_scoped(self):
        refresh = (self.root / "scripts" / "refresh_staging_databases.sh").read_text()
        cleanup = (self.root / "cleanup_pilot_staging.sh").read_text()
        setup = (self.root / "setup_pilot_staging.sh").read_text()

        self.assertIn("r2c_pilot_tracker", refresh)
        self.assertIn("r2c_stage_tracker", refresh)
        self.assertIn('"${PG_DUMP}" --format=custom', refresh)
        self.assertIn('tracker_dump_pid="$!"', refresh)
        self.assertIn('control_dump_pid="$!"', refresh)
        self.assertIn('wait "${tracker_dump_pid}"', refresh)
        self.assertIn('wait "${control_dump_pid}"', refresh)
        self.assertIn('tracker_restore_pid="$!"', refresh)
        self.assertIn('control_restore_pid="$!"', refresh)
        self.assertIn('wait "${tracker_restore_pid}"', refresh)
        self.assertIn('wait "${control_restore_pid}"', refresh)
        self.assertIn('if [ "${tracker_dump_status}" -ne 0 ]', refresh)
        self.assertIn('if [ "${tracker_restore_status}" -ne 0 ]', refresh)
        self.assertIn("pg_dump_major", refresh)
        self.assertIn("compute start-iap-tunnel", refresh)
        self.assertIn("cloud-sql-proxy", refresh)
        self.assertIn("allow-iap-postgres-r2c-staging", refresh)
        self.assertIn("r2c-release-staging", cleanup)
        self.assertIn('.release-state/staging.json', cleanup)
        self.assertNotIn("DROP DATABASE IF EXISTS r2c_pilot_tracker", cleanup)
        self.assertIn("r2c_stage_tracker_user", setup)
        self.assertIn("r2c_stage_control_user", setup)
        self.assertIn("POSTGRES_15", setup)
        self.assertIn("db-f1-micro", setup)
        self.assertIn('if [ "${1:-}" = "--reuse-existing" ]', setup)
        self.assertIn('instance_state', setup)
        self.assertIn('"${instance_state}" != "RUNNABLE"', setup)
        self.assertIn("Validated reusable isolated staging resources.", setup)

    def test_staging_reuse_receipt_is_scoped_and_time_limited(self):
        spec = importlib.util.spec_from_file_location(
            "release_guard_reuse_test",
            self.root / "scripts" / "release_guard.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        now = 2_000_000_000
        with tempfile.TemporaryDirectory() as temporary_dir:
            receipt = pathlib.Path(temporary_dir) / "staging.json"
            receipt.write_text(json.dumps({
                "instance": "r2c-release-staging",
                "project": module.PROJECT,
                "refreshed_at_epoch": now - 60,
                "region": module.REGION,
            }))
            with mock.patch.object(module, "STAGING_STATE_PATH", receipt), \
                    mock.patch.object(module.time, "time", return_value=now):
                self.assertEqual(
                    now - 60,
                    module.validate_staging_reuse_state()["refreshed_at_epoch"],
                )
                receipt.write_text(json.dumps({
                    "instance": "r2c-release-staging",
                    "project": module.PROJECT,
                    "refreshed_at_epoch": now - module.STAGING_REUSE_MAX_AGE_SECONDS - 1,
                    "region": module.REGION,
                }))
                with self.assertRaisesRegex(RuntimeError, "outside its 24-hour reuse window"):
                    module.validate_staging_reuse_state()

    def test_committed_source_snapshot_excludes_untracked_editor_artifacts(self):
        spec = importlib.util.spec_from_file_location(
            "release_guard_source_test",
            self.root / "scripts" / "release_guard.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with module.committed_source_snapshot() as source_path:
            self.assertTrue((source_path / "Dockerfile").is_file())
            self.assertFalse((source_path / ".#tracker2.txt").exists())

    def test_bootstrap_recognizes_old_revision_http_status_wording(self):
        spec = importlib.util.spec_from_file_location(
            "release_guard",
            self.root / "scripts" / "release_guard.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.is_bootstrap_gate_unavailable(
            RuntimeError("Expected HTTP 200 from URL; received 404: body")
        ))
        self.assertTrue(module.is_bootstrap_gate_unavailable(
            RuntimeError("Expected HTTP 200 from URL; received 503: body")
        ))
        self.assertFalse(module.is_bootstrap_gate_unavailable(
            RuntimeError("Deployment blocked by active use")
        ))


if __name__ == "__main__":
    unittest.main()
