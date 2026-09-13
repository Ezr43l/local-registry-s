import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).parents[1]


class DistributionContractTests(unittest.TestCase):
    def test_distribution_secure_rebuild_is_pinned(self):
        dockerfile = (ROOT / "docker/local-registry/Dockerfile").read_text(
            encoding="utf-8"
        )
        required = (
            "golang:1.27.0-alpine3.24@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc",
            "DISTRIBUTION_COMMIT=9a8d98b679740cd514aa7e7d84d23d442a5ef54c",
            "DISTRIBUTION_VERSION=3.1.1-secure.2",
            "golang.org/x/crypto@v0.55.0",
            "golang.org/x/net@v0.58.0",
            "golang.org/x/text@v0.41.0",
            "google.golang.org/grpc@v1.83.2",
            "COPY docker/local-registry/patches/0001-inmemory-find-consumes-final-component.patch /tmp/distribution.patch",
            "git -C /src apply --check /tmp/distribution.patch",
            "go test -p 1 -short ./...",
            "OTEL_TRACES_EXPORTER=none",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, dockerfile)
        self.assertNotIn("ARG REGISTRY_IMAGE=registry:3.1.1", dockerfile)
        self.assertNotIn("    MAINTENANCE_ADMIN_TOKEN= \\", dockerfile)
        self.assertNotIn("    MAINTENANCE_CLUSTER_TOKEN= \\", dockerfile)
        entrypoint = (ROOT / "docker/local-registry/entrypoint.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 /app/server.py --check-config", entrypoint)
        self.assertIn("env -u REGISTRY_CONFIG -u REGISTRY_URL -u REGISTRY_DATA", entrypoint)

    def test_distribution_patch_is_exact_and_does_not_weaken_the_suite(self):
        dockerfile = (ROOT / "docker/local-registry/Dockerfile").read_text(
            encoding="utf-8"
        )
        patch_path = (
            ROOT
            / "docker/local-registry/patches/0001-inmemory-find-consumes-final-component.patch"
        )
        patch = patch_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        pinned = re.search(
            r"^ARG DISTRIBUTION_PATCH_SHA256=([0-9a-f]{64})$",
            dockerfile,
            re.MULTILINE,
        )
        self.assertIsNotNone(pinned)
        self.assertEqual(pinned.group(1), digest)
        self.assertEqual(patch.count("diff --git "), 2)
        self.assertIn(
            "a/registry/storage/driver/inmemory/mfs.go "
            "b/registry/storage/driver/inmemory/mfs.go",
            patch,
        )
        self.assertIn(
            "a/registry/storage/driver/inmemory/driver_test.go "
            "b/registry/storage/driver/inmemory/driver_test.go",
            patch,
        )
        self.assertIn("+\tif i < 0 || !child.isdir() {", patch)
        self.assertIn("+func TestDeleteDirectoryWithAdjacentEqualComponents", patch)
        self.assertIn("+func TestDeleteOnlyDeletesExactSelfNestedSubpath", patch)
        self.assertNotIn("t.Skip", patch)
        self.assertNotIn("suite.T().Skip", patch)
        self.assertEqual(dockerfile.count("go test -p 1 -short ./..."), 1)
        self.assertNotIn("|| true", dockerfile)
        self.assertNotIn("go test -short -run", dockerfile)

    def test_third_party_notices_and_copyleft_source_gate_are_complete(self):
        dockerfile = (ROOT / "docker/local-registry/Dockerfile").read_text(
            encoding="utf-8"
        )
        collector = (
            ROOT / "scripts/collect-alpine-copyleft-sources.sh"
        ).read_text(encoding="utf-8")
        release = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for path in (
            ROOT / "third_party/spdx/GPL-2.0-only.txt",
            ROOT / "third_party/spdx/GPL-3.0-or-later.txt",
            ROOT / "third_party/spdx/LGPL-2.1-or-later.txt",
            ROOT / "third_party/spdx/MPL-2.0.txt",
            ROOT / "third_party/spdx/SOURCE.txt",
        ):
            with self.subTest(path=path):
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
        for required in (
            "npm ls --all --json",
            "go version -m /out/registry",
            "/usr/local/go/PATENTS",
            "APK-PACKAGES.tsv",
            "RUNTIME-PACKAGES.lock",
            "THIRD_PARTY_NOTICES.md",
            "io.ezr43l.licenses.notices",
        ):
            with self.subTest(required=required):
                self.assertIn(required, dockerfile)
        runtime_lock = ROOT / "third_party/alpine-runtime-packages.lock"
        locked_packages = runtime_lock.read_text(encoding="utf-8").splitlines()
        self.assertEqual(locked_packages, sorted(locked_packages))
        self.assertEqual(len(locked_packages), len(set(locked_packages)))
        self.assertGreater(len(locked_packages), 30)
        self.assertFalse(any(package.startswith(".") for package in locked_packages))
        self.assertIn("apk info -v | sed '/^\\./d' | sort", dockerfile)
        self.assertIn(
            "cmp /usr/share/licenses/local-registry/alpine/RUNTIME-PACKAGES.lock",
            dockerfile,
        )
        for required in (
            "abuild fetch verify",
            "git -C \"$GIT_DIR\" cat-file -e \"$commit^{commit}\"",
            "--filter=blob:none",
            "fetch.fsckObjects true",
            "for attempt in 1 2 3",
            "--mtime='UTC 1970-01-01'",
            "gzip -n -9",
            "DISTFILES.sha512",
            '! -L "$OUTPUT_DIR"',
            '! -L "$installed_db"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, collector)
        self.assertNotIn("|| true", collector)
        tar_verifier = (ROOT / "scripts/verify-tar-members.py").read_text(
            encoding="utf-8"
        )
        archive_verifier = (
            ROOT / "scripts/verify-alpine-copyleft-archive.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("verify-tar-members.py", archive_verifier)
        for required in (
            "tarfile.SYMTYPE",
            "archive.extractall(path=extract_root, members=members, filter=\"data\")",
            "MAX_TOTAL_SIZE",
            "MAX_ARCHIVE_SIZE",
            "MAX_PATH_LENGTH",
            "for member in archive:",
            "el enlace escapa del archivo",
        ):
            with self.subTest(required=required):
                self.assertIn(required, tar_verifier)
        self.assertIn("alpine-copyleft-sources.tar.gz", release)
        self.assertIn("alpine-copyleft-sources-manifest.tsv", release)
        self.assertIn(".assets | length == 6", release)
        self.assertIn("child_digest=", release)
        self.assertIn('"${IMAGE_NAME}@${child_digest}"', release)
        self.assertNotRegex(
            release,
            r'docker create --platform "\$platform"\s+'
            r'"\$\{IMAGE_NAME\}@\$\{DIGEST\}"',
        )
        self.assertIn("abuild fetch verify", notices)
        self.assertNotIn("oferta", notices.lower())

        fixture = ROOT / "tests/fixtures/apk-installed-license.db"
        parsed = subprocess.run(
            [
                "awk",
                "-v",
                "platform=amd64",
                "-v",
                "copyleft=1",
                "-f",
                str(ROOT / "scripts/apk-installed-manifest.awk"),
                str(fixture),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("\tbusybox\t", parsed)
        self.assertIn("\tca-certificates\t", parsed)
        self.assertIn("\texample-epl\t", parsed)
        self.assertNotIn("\tzlib\t", parsed)
        self.assertNotIn("\texample-apache\t", parsed)

    def test_version_is_consistent(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        package = json.loads(
            (ROOT / "docker/local-registry/web/package.json").read_text(encoding="utf-8")
        )
        template = ET.parse(ROOT / "unraid/my-Local-Registry.xml").getroot()
        self.assertEqual(version, "v1.2.14")
        self.assertEqual(package["version"], version.removeprefix("v"))
        self.assertEqual(
            template.findtext("Repository"),
            "ghcr.io/ezr43l/local-registry-s:stable",
        )
        self.assertEqual(
            template.findtext("Registry"),
            "https://github.com/Ezr43l/local-registry-s/pkgs/container/local-registry-s",
        )
        self.assertEqual(
            template.findtext("Icon"),
            "https://www.docker.com/wp-content/uploads/2022/03/Moby-logo.png",
        )

    def test_public_template_has_no_markers_or_private_defaults(self):
        text = (ROOT / "unraid/my-Local-Registry.xml").read_text(encoding="utf-8")
        for forbidden in ("__IMAGE__", "__NODE__", "192.168."):
            self.assertNotIn(forbidden, text)
        root = ET.fromstring(text)
        configs = {item.get("Target"): item for item in root.findall("Config")}
        self.assertEqual(set(configs), {"5000", "5001", "/var/lib/registry"})
        self.assertNotIn('Type="Variable"', text)
        self.assertNotIn("/run/secrets", text)
        extra_params = root.findtext("ExtraParams") or ""
        self.assertIn(
            "--tmpfs /run:rw,nosuid,noexec,size=16m,mode=0755", extra_params
        )
        self.assertIn(
            "--tmpfs /tmp:rw,nosuid,noexec,size=32m,mode=1777", extra_params
        )

        dockerfile = (ROOT / "docker/local-registry/Dockerfile").read_text(
            encoding="utf-8"
        )
        for label in (
            'net.unraid.docker.managed="dockerman"',
            'net.unraid.docker.webui="http://[IP]:[PORT:5001]/"',
            'net.unraid.docker.icon="https://www.docker.com/wp-content/uploads/2022/03/Moby-logo.png"',
        ):
            with self.subTest(label=label):
                self.assertIn(label, dockerfile)

    def test_public_install_surfaces_are_minimal_and_deploy_is_transactional(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        deploy = (ROOT / "deploy-registry.sh").read_text(encoding="utf-8")
        mirror = (ROOT / "mirror-registries.sh").read_text(encoding="utf-8")
        boot_hook = (ROOT / "docker-registry.sh").read_text(encoding="utf-8")
        for direct in (
            "MAINTENANCE_ADMIN_TOKEN:",
            "MAINTENANCE_CLUSTER_TOKEN:",
            "MAINTENANCE_ADMIN_TOKEN=",
            "MAINTENANCE_CLUSTER_TOKEN=",
        ):
            with self.subTest(direct=direct):
                self.assertNotIn(direct, compose)
                self.assertNotIn(direct, env_example)
        self.assertNotIn("/run/secrets", compose)
        self.assertNotIn("REGISTRY_SECRETS_DIR", env_example)
        for tmpfs_contract in (
            "/run:rw,nosuid,noexec,size=16m,mode=0755",
            "/tmp:rw,nosuid,noexec,size=32m,mode=1777",
        ):
            with self.subTest(tmpfs_contract=tmpfs_contract):
                self.assertIn(tmpfs_contract, compose)
                self.assertIn(tmpfs_contract, deploy)
        self.assertNotIn('--set "MAINTENANCE_ADMIN_TOKEN=', deploy)
        self.assertNotIn('--set "MAINTENANCE_CLUSTER_TOKEN=', deploy)
        self.assertNotIn("-e MAINTENANCE_ADMIN_TOKEN='", deploy)
        self.assertNotIn("-e MAINTENANCE_CLUSTER_TOKEN='", deploy)
        self.assertGreaterEqual(deploy.count("StrictHostKeyChecking=yes"), 3)
        self.assertNotIn("StrictHostKeyChecking=no", deploy)
        self.assertIn('if "<!DOCTYPE" in upper or "<!ENTITY" in upper:', deploy)
        self.assertIn("StrictHostKeyChecking=yes", mirror)
        self.assertNotIn("StrictHostKeyChecking=no", mirror)
        self.assertIn("valid_repository", mirror)
        self.assertIn("valid_port", boot_hook)
        self.assertIn("endpoint inválido", boot_hook)
        api_client = (ROOT / "docker/local-registry/web/src/api.ts").read_text(
            encoding="utf-8"
        )
        maintenance_panel = (
            ROOT / "docker/local-registry/web/src/components/MaintenancePanel.tsx"
        ).read_text(encoding="utf-8")
        auth_panel = (
            ROOT / "docker/local-registry/web/src/components/AuthPanel.tsx"
        ).read_text(encoding="utf-8")
        setup_panel = (
            ROOT / "docker/local-registry/web/src/components/SetupPanel.tsx"
        ).read_text(encoding="utf-8")
        styles = (ROOT / "docker/local-registry/web/src/styles.css").read_text(
            encoding="utf-8"
        )
        app = (ROOT / "docker/local-registry/web/src/App.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("api/auth", api_client)
        self.assertIn("api/settings", api_client)
        self.assertIn("X-CSRF-Token", api_client)
        self.assertNotIn("X-Registry-Admin-Token", api_client)
        self.assertNotIn("Token administrativo", maintenance_panel)
        self.assertNotIn("DialogoCoordinador", maintenance_panel)
        self.assertIn("void cambiarCoordinador(coordinadorElegido)", maintenance_panel)
        self.assertIn("coordinator-error", maintenance_panel)
        self.assertIn("mostrarProgreso", maintenance_panel)
        self.assertIn("incidencia?.detail", maintenance_panel)
        self.assertIn("Crear cuenta y entrar", auth_panel)
        self.assertIn("Configuración", app)
        for unknown_css_variable in ("var(--line)", "var(--text)", "var(--surface)"):
            with self.subTest(unknown_css_variable=unknown_css_variable):
                self.assertNotIn(unknown_css_variable, styles)
        self.assertIn(".setup-form .panel:focus-within", styles)
        self.assertIn("background: var(--hundido)", styles)
        self.assertIn("color: var(--tinta)", styles)
        for field in (
            "cache_ttl", "cors_allowed_origins", "lease_ttl",
            "require_all_nodes", "branch_pattern",
        ):
            with self.subTest(field=field):
                self.assertIn(field, api_client)
        for label in (
            "Nodos del clúster", "Código de incorporación",
            "Mantenimiento y retención", "Opciones avanzadas",
        ):
            with self.subTest(label=label):
                self.assertIn(label, setup_panel)
        self.assertIn("configuredNodeNames", setup_panel)
        self.assertIn("Automático (primer nodo por nombre)", setup_panel)
        self.assertIn("Usar el coordinador (recomendado)", setup_panel)
        self.assertIn("Coordinador inicial", setup_panel)
        self.assertIn("Se cambia únicamente desde la página principal", setup_panel)
        self.assertIn("Fuente autoritativa en conflictos", setup_panel)
        self.assertNotIn("Coordinador<input", setup_panel)
        self.assertNotIn("Origen de sincronización<input", setup_panel)
        for required in (
            "stage_remote_secret",
            "printf '%s\\n' \"$secret_value\" | ssh_node",
            "chmod 0400",
            "[ ! -L",
            "rollback_container",
            "old_present=0",
            "docker info >/dev/null",
            'elif [ \\"\\$old_present\\" = 0 ]',
            "secrets_activated",
            "template_touched",
            "NODE_REGISTRY_PORT",
            "Punto de commit",
            'exit "$fallos"',
            "MAINTENANCE_ADMIN_TOKEN_FILE=$admin_file_value",
            "MAINTENANCE_CLUSTER_TOKEN_FILE=$cluster_file_value",
            "index .HostConfig.Tmpfs",
        ):
            with self.subTest(required=required):
                self.assertIn(required, deploy)
        for target in (
            "CACHE_TTL",
            "STATS_ENABLED",
            "KEEP_LAST",
            "PROTECTED_TAGS",
            "MAINTENANCE_GC",
            "MAINTENANCE_LEASE_TTL",
            "RETENTION_REQUIRE_ALL_NODES",
            "TZ",
        ):
            with self.subTest(target=target):
                self.assertIn(f'--set "{target}=', deploy)
                self.assertIn(f"grep -Fxq '{target}=", deploy)
                self.assertNotIn(f"grep -q 'Target=\\\"{target}\\\"'", deploy)
        self.assertIn("grep -c '<Config '", deploy)
        self.assertIn("! grep -Fq 'Type=\\\"Variable\\\"'", deploy)
        commit = deploy.index("# Punto de commit:")
        rollback_removal = deploy.index(
            'docker rm -f \\"\\$rollback_container\\"', commit
        )
        trap_disabled = deploy.index("trap - EXIT HUP INT TERM", commit)
        self.assertLess(trap_disabled, rollback_removal)

    def test_renderer_changes_only_the_requested_copy(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "render-unraid-template.py"),
                str(ROOT / "unraid/my-Local-Registry.xml"),
                "--repository",
                "registry.example/local-registry:v1.2.0",
                "--set",
                "NODE_NAME=node-z",
                "--set",
                "5000=5500",
            ],
            check=True,
            capture_output=True,
        )
        rendered = ET.fromstring(result.stdout)
        self.assertEqual(
            rendered.findtext("Repository"), "registry.example/local-registry:v1.2.0"
        )
        configs = {item.get("Target"): item.text or "" for item in rendered.findall("Config")}
        self.assertNotIn("NODE_NAME", configs)
        self.assertEqual(configs["5000"], "5500")
        self.assertEqual(
            ET.parse(ROOT / "unraid/my-Local-Registry.xml").getroot().findtext("Repository"),
            "ghcr.io/ezr43l/local-registry-s:stable",
        )

    def test_renderer_rejects_ambiguous_or_nonportable_inputs(self):
        common = [
            sys.executable,
            str(ROOT / "render-unraid-template.py"),
            str(ROOT / "unraid/my-Local-Registry.xml"),
        ]
        duplicate = subprocess.run(
            common
            + [
                "--repository",
                "registry.example/app:v1",
                "--set",
                "NODE_NAME=node-a",
                "--set",
                "NODE_NAME=node-b",
            ],
            capture_output=True,
        )
        self.assertNotEqual(duplicate.returncode, 0)
        nonportable = subprocess.run(
            common + ["--repository", "registry.example/bad image:v1"],
            capture_output=True,
        )
        self.assertNotEqual(nonportable.returncode, 0)
        with tempfile.TemporaryDirectory() as temporary:
            unsafe = Path(temporary) / "unsafe.xml"
            unsafe.write_text(
                '<!DOCTYPE Container [<!ENTITY x "value">]>'
                '<Container><Repository>&x;</Repository></Container>',
                encoding="utf-8",
            )
            dtd = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "render-unraid-template.py"),
                    str(unsafe),
                    "--repository",
                    "registry.example/app:v1",
                ],
                capture_output=True,
            )
            self.assertNotEqual(dtd.returncode, 0)
            linked = Path(temporary) / "linked.xml"
            linked.symlink_to(ROOT / "unraid/my-Local-Registry.xml")
            symlink = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "render-unraid-template.py"),
                    str(linked),
                    "--repository",
                    "registry.example/app:v1",
                ],
                capture_output=True,
            )
            self.assertNotEqual(symlink.returncode, 0)


if __name__ == "__main__":
    unittest.main()
