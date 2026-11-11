import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from scripts import skill_catalog as package_catalog_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = [
    "create-adaptable-composable",
    "vue-best-practices",
    "vue-debug-guides",
    "vue-jsx-best-practices",
    "vue-options-api-best-practices",
    "vue-pinia-best-practices",
    "vue-router-best-practices",
    "vue-testing-best-practices",
]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


catalog_module = load_module(
    "skill_catalog", PROJECT_ROOT / "scripts" / "skill_catalog.py"
)
targets = load_module(
    "skill_catalog_agent_targets", PROJECT_ROOT / "scripts" / "agent_targets.py"
)


def build_official_archive(
    path,
    skill_id="docx",
    files=None,
    extra_members=None,
    archive_root=None,
):
    files = files or {
        "SKILL.md": (
            "---\n"
            f"name: {skill_id}\n"
            "description: Official fixture\n"
            "---\n\n"
            "# Official fixture\n"
        ),
        "LICENSE.txt": (
            "Apache License\n"
            "Version 2.0, January 2004\n"
        ),
        "scripts/render.py": "print('official')\n",
    }
    root = Path(archive_root or catalog_module.OFFICIAL_SKILL_ARCHIVE_ROOT) / "skills" / skill_id
    with tarfile.open(path, "w:gz") as archive:
        for relative, content in sorted(files.items()):
            data = content.encode("utf-8") if isinstance(content, str) else content
            member = tarfile.TarInfo(str(root / relative))
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
        for member in extra_members or []:
            archive.addfile(member)

    digest = hashlib.sha256()
    total_bytes = 0
    for relative, content in sorted(files.items()):
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
        total_bytes += len(data)
    return {
        "files": len(files),
        "bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


class SkillCatalogTests(unittest.TestCase):
    def setUp(self):
        self.test_temp = Path(
            tempfile.mkdtemp(prefix="agentrelay-catalog-official-")
        )
        self.addCleanup(shutil.rmtree, self.test_temp, True)
        self.catalog = catalog_module.load_catalog(PROJECT_ROOT)

    def test_catalog_is_fixed_upstream_snapshot_with_license(self):
        self.assertEqual(
            self.catalog["source"]["commit"],
            "198f0cd8f7f9c0a3d9b566ccc16de6677519ee78",
        )
        self.assertEqual(
            [entry["id"] for entry in self.catalog["skills"]],
            EXPECTED_SKILLS,
        )
        self.assertEqual(self.catalog["content_files"], 205)
        license_file = catalog_module.license_file(PROJECT_ROOT, self.catalog)
        self.assertIn("MIT License", license_file.read_text(encoding="utf-8"))
        self.assertEqual(
            catalog_module.default_skill_ids(self.catalog),
            ["vue-best-practices"],
        )
        for skill_id in EXPECTED_SKILLS:
            source = catalog_module.skill_source_dir(
                PROJECT_ROOT, self.catalog, skill_id
            )
            self.assertTrue((source / "SKILL.md").is_file())

    def test_vue_best_practices_and_debug_guides_keep_reference_trees(self):
        for skill_id, reference_dir in (
            ("vue-best-practices", "references"),
            ("vue-debug-guides", "reference"),
        ):
            source = catalog_module.skill_source_dir(
                PROJECT_ROOT, self.catalog, skill_id
            )
            references = list((source / reference_dir).glob("*.md"))
            self.assertGreater(len(references), 10)

    def test_unknown_and_path_traversal_ids_are_rejected(self):
        with self.assertRaises(catalog_module.CatalogError):
            catalog_module.selection_from_cli(self.catalog, "../outside")
        with self.assertRaises(catalog_module.CatalogError):
            catalog_module.skill_source_dir(
                PROJECT_ROOT, self.catalog, "../../outside"
            )
        with self.assertRaises(catalog_module.CatalogError):
            catalog_module.normalize_skill_ids(self.catalog, ["unknown"])

    def test_cli_selection_defaults_deduplicates_and_can_disable(self):
        self.assertEqual(
            catalog_module.selection_from_cli(self.catalog),
            ["vue-best-practices"],
        )
        self.assertEqual(
            catalog_module.selection_from_cli(
                self.catalog,
                "vue-router-best-practices,vue-best-practices,"
                "vue-router-best-practices",
            ),
            ["vue-router-best-practices", "vue-best-practices"],
        )
        self.assertEqual(
            catalog_module.selection_from_cli(self.catalog, no_skills=True),
            [],
        )
        with self.assertRaises(catalog_module.CatalogError):
            catalog_module.selection_from_cli(
                self.catalog, "vue-best-practices", no_skills=True
            )

    def test_manifest_checksum_detects_tampering(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-catalog-") as directory:
            project = Path(directory)
            target = project / "skills" / "catalog" / "vuejs-ai"
            shutil.copytree(
                PROJECT_ROOT / "skills" / "catalog" / "vuejs-ai",
                target,
            )
            manifest_path = target / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["content_sha256"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaises(catalog_module.CatalogError):
                catalog_module.load_catalog(project)

    def test_windows_crlf_checkout_preserves_bundled_checksums(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-catalog-crlf-") as directory:
            project = Path(directory)
            catalog_target = project / "skills" / "catalog" / "vuejs-ai"
            bundled_target = project / "skills" / "catalog" / "agentrelay"
            shutil.copytree(
                PROJECT_ROOT / "skills" / "catalog" / "vuejs-ai",
                catalog_target,
            )
            shutil.copytree(
                PROJECT_ROOT / "skills" / "catalog" / "agentrelay",
                bundled_target,
            )
            for path in [*catalog_target.rglob("*"), *bundled_target.rglob("*")]:
                if not path.is_file():
                    continue
                content = path.read_bytes()
                normalized = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                path.write_bytes(normalized.replace(b"\n", b"\r\n"))

            catalog = catalog_module.load_catalog(project)
            self.assertEqual(catalog["content_files"], 205)
            browser = catalog_module.agentrelay_bundled_skill_entry(
                project,
                "browser-automatic",
            )
            self.assertEqual(browser["id"], "browser-automatic")

            install_root = project / "codex"
            installed_browser = install_root / "browser-automatic"
            shutil.copytree(bundled_target / "browser-automatic", installed_browser)
            for path in installed_browser.rglob("*"):
                if path.is_file():
                    content = path.read_bytes()
                    path.write_bytes(content.replace(b"\r\n", b"\n"))
            installed = catalog_module.discover_installed_skills(
                {"Codex": install_root},
                project_root=project,
            )
            self.assertEqual(
                [entry["id"] for entry in installed],
                ["browser-automatic"],
            )

    def test_installed_skills_are_discovered_deduplicated_and_safe(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-local-skills-") as directory:
            root = Path(directory)
            codex = root / "codex"
            claude = root / "claude"
            codex.mkdir()
            claude.mkdir()

            def add_skill(parent, skill_id, name, license_name=""):
                skill = parent / skill_id
                skill.mkdir()
                (skill / "SKILL.md").write_text(
                    "---\n"
                    f"name: {name}\n"
                    f"description: {name} 的本机说明\n"
                    "---\n\n"
                    f"# {name}\n",
                    encoding="utf-8",
                )
                (skill / "reference.md").write_text(
                    "reference", encoding="utf-8"
                )
                if license_name:
                    (skill / license_name).write_text(
                        "MIT License", encoding="utf-8"
                    )
                return skill

            add_skill(codex, "docx", "DOCX", "LICENSE.txt")
            add_skill(claude, "docx", "DOCX")
            restricted = add_skill(codex, "restricted", "Restricted", "LICENSE.txt")
            (restricted / "LICENSE.txt").write_text(
                "You may not reproduce or copy these materials.",
                encoding="utf-8",
            )
            shutil.copytree(
                PROJECT_ROOT
                / "skills"
                / "catalog"
                / "agentrelay"
                / "browser-automatic",
                codex / "browser-automatic",
            )
            add_skill(codex, "unsigned-local", "Unsigned")
            add_skill(codex, "agent-relay", "AgentRelay")
            add_skill(codex, "bitch-skill", "Private")
            add_skill(codex, "Bad_ID", "Invalid")
            (codex / "missing").mkdir()
            (codex / "linked").symlink_to(claude / "docx")

            installed = catalog_module.discover_installed_skills({
                "Codex": codex,
                "Claude Code": claude,
            })

        self.assertEqual(
            [entry["id"] for entry in installed],
            ["browser-automatic", "docx", "restricted", "unsigned-local"],
        )
        docx = next(entry for entry in installed if entry["id"] == "docx")
        self.assertEqual(docx["name"], "DOCX")
        self.assertEqual(docx["installed_in"], ["Codex", "Claude Code"])
        self.assertTrue(docx["installable"])
        restricted = next(
            entry for entry in installed if entry["id"] == "restricted"
        )
        self.assertFalse(restricted["installable"])
        self.assertIn("禁止复制", restricted["restriction"])
        unsigned = next(
            entry for entry in installed if entry["id"] == "unsigned-local"
        )
        self.assertFalse(unsigned["installable"])
        self.assertEqual(unsigned["copy_policy"], "unknown")
        self.assertIn("未找到可确认的许可证", unsigned["restriction"])
        browser = next(
            entry for entry in installed if entry["id"] == "browser-automatic"
        )
        self.assertTrue(browser["installable"])
        self.assertEqual(browser["copy_policy"], "allowed")
        self.assertNotIn("local_source", browser)
        self.assertNotIn("agent-relay", {entry["id"] for entry in installed})
        self.assertNotIn("bitch-skill", {entry["id"] for entry in installed})
        self.assertNotIn("linked", {entry["id"] for entry in installed})

    def test_browser_bundle_ignores_generated_cache_files(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-browser-cache-") as directory:
            root = Path(directory)
            browser = root / "browser-automatic"
            shutil.copytree(
                PROJECT_ROOT
                / "skills"
                / "catalog"
                / "agentrelay"
                / "browser-automatic",
                browser,
            )
            cache = browser / "__pycache__"
            cache.mkdir()
            (cache / "browse.cpython-310.pyc").write_bytes(b"generated")

            installed = catalog_module.discover_installed_skills(
                {"Codex": root},
                project_root=PROJECT_ROOT,
            )

        self.assertEqual([entry["id"] for entry in installed], ["browser-automatic"])

    def test_browser_bundle_rejects_a_mismatched_local_copy(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-browser-mismatch-") as directory:
            root = Path(directory)
            browser = root / "browser-automatic"
            browser.mkdir()
            (browser / "SKILL.md").write_text(
                "---\nname: browser-automatic\ndescription: fake\n---\n",
                encoding="utf-8",
            )
            (browser / "LICENSE").write_text("MIT License", encoding="utf-8")
            (browser / "scripts").mkdir()
            (browser / "scripts" / "browse.py").write_text("fake", encoding="utf-8")

            installed = catalog_module.discover_installed_skills(
                {"Codex": root},
                project_root=PROJECT_ROOT,
            )

        self.assertEqual(installed, [])

    def test_unreadable_local_skill_is_ignored(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-unreadable-skill-") as directory:
            root = Path(directory)
            skill = root / "unreadable"
            skill.mkdir()
            skill_file = skill / "SKILL.md"
            skill_file.write_text("# unreadable\n", encoding="utf-8")
            original_is_file = Path.is_file

            def guarded_is_file(path):
                if path == skill_file:
                    raise PermissionError(path)
                return original_is_file(path)

            with patch.object(Path, "is_file", guarded_is_file):
                installed = catalog_module.discover_installed_skills(
                    {"Codex": root},
                    project_root=PROJECT_ROOT,
                )

        self.assertEqual(installed, [])

    def test_merged_catalog_adds_official_downloads_and_vue_fallback(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-merged-skills-") as directory:
            root = Path(directory)
            skill = root / "docx"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: DOCX\n"
                "description: 创建和编辑 Word 文档。\n"
                "---\n",
                encoding="utf-8",
            )
            (skill / "LICENSE.txt").write_text("license", encoding="utf-8")
            merged = catalog_module.catalog_with_installed_skills(
                PROJECT_ROOT, {"Codex": root}
            )
            public = catalog_module.public_catalog(merged)

        self.assertEqual(
            merged["defaults"],
            [
                "browser-automatic",
                "docx",
                "pdf",
                "pptx",
                "xlsx",
                "vue-best-practices",
                "web-artifacts-builder",
            ],
        )
        self.assertEqual(merged["installed_count"], 1)
        docx = next(
            entry for entry in merged["skills"] if entry["id"] == "docx"
        )
        self.assertEqual(docx["source_type"], "official")
        self.assertEqual(docx["install_method"], "official-download")
        self.assertNotIn("local_source", docx)
        self.assertNotIn(str(root), json.dumps(public, ensure_ascii=False))
        browser = next(
            entry
            for entry in merged["skills"]
            if entry["id"] == "browser-automatic"
        )
        self.assertEqual(browser["source_label"], "AgentRelay 内置")
        self.assertEqual(browser["license_spdx"], "MIT")
        self.assertNotIn("local_source", browser)
        self.assertIn(
            "vue-best-practices",
            {entry["id"] for entry in merged["skills"]},
        )

    def test_merged_catalog_uses_only_preferred_defaults(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-preferred-defaults-") as directory:
            root = Path(directory)
            for skill_id in (*catalog_module.PREFERRED_DEFAULT_SKILL_IDS, "mysql"):
                skill = root / skill_id
                skill.mkdir()
                (skill / "SKILL.md").write_text(
                    "---\n"
                    f"name: {skill_id}\n"
                    f"description: {skill_id}\n"
                    "---\n",
                    encoding="utf-8",
                )
                (skill / "LICENSE.txt").write_text(
                    "MIT License", encoding="utf-8"
                )
            merged = catalog_module.catalog_with_installed_skills(
                PROJECT_ROOT, {"Codex": root}
            )

        self.assertEqual(
            merged["defaults"],
            list(catalog_module.PREFERRED_DEFAULT_SKILL_IDS),
        )
        self.assertNotIn("mysql", merged["defaults"])

    def test_nonofficial_restricted_skills_are_visible_but_not_selectable(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-restricted-skills-") as directory:
            root = Path(directory)
            restricted = root / "restricted-report"
            restricted.mkdir()
            (restricted / "SKILL.md").write_text(
                "---\nname: Restricted\ndescription: Local only\n---\n",
                encoding="utf-8",
            )
            (restricted / "LICENSE.txt").write_text(
                "You may not reproduce or copy these materials.",
                encoding="utf-8",
            )
            merged = catalog_module.catalog_with_installed_skills(
                PROJECT_ROOT, {"Codex": root}
            )
            public = catalog_module.public_catalog(merged)

        self.assertNotIn("restricted-report", merged["defaults"])
        self.assertIn("docx", merged["defaults"])
        self.assertEqual(merged["restricted_count"], 1)
        self.assertEqual(merged["selectable_installed_count"], 0)
        restricted_public = next(
            entry
            for entry in public["skills"]
            if entry["id"] == "restricted-report"
        )
        self.assertFalse(restricted_public["installable"])
        with self.assertRaises(catalog_module.CatalogError):
            catalog_module.normalize_skill_ids(merged, ["restricted-report"])
        self.assertEqual(
            catalog_module.normalize_skill_ids(merged, ["all"]),
            [
                "create-adaptable-composable",
                "vue-best-practices",
                "vue-debug-guides",
                "vue-jsx-best-practices",
                "vue-options-api-best-practices",
                "vue-pinia-best-practices",
                "vue-router-best-practices",
                "vue-testing-best-practices",
                "docx",
                "pdf",
                "pptx",
                "web-artifacts-builder",
                "xlsx",
                "browser-automatic",
            ],
        )
        self.assertEqual(
            catalog_module.reconcile_skill_ids(
                merged, ["restricted-report", "docx", "removed-skill"]
            ),
            ["docx"],
        )

    def test_private_skills_are_hidden_and_cannot_be_selected(self):
        with tempfile.TemporaryDirectory(prefix="agentrelay-private-skill-") as directory:
            root = Path(directory)
            private = root / "bitch-skill"
            private.mkdir()
            (private / "SKILL.md").write_text(
                "---\nname: Private\ndescription: local only\n---\n",
                encoding="utf-8",
            )
            merged = catalog_module.catalog_with_installed_skills(
                PROJECT_ROOT, {"Codex": root}
            )
            public = catalog_module.public_catalog(merged)

        visible_ids = {entry["id"] for entry in public["skills"]}
        self.assertNotIn("bitch-skill", visible_ids)
        self.assertNotIn("bitch-skill", merged["defaults"])
        with self.assertRaises(catalog_module.CatalogError):
            catalog_module.normalize_skill_ids(merged, ["bitch-skill"])

    def test_official_catalog_includes_web_artifacts_builder(self):
        self.assertEqual(
            catalog_module.OFFICIAL_SKILL_IDS,
            {"docx", "pdf", "pptx", "xlsx", "web-artifacts-builder"},
        )
        entry = catalog_module.official_skill_entry("web-artifacts-builder")
        self.assertEqual(entry["official_commit"], catalog_module.OFFICIAL_SKILL_COMMIT)
        self.assertEqual(entry["official_path"], "skills/web-artifacts-builder")
        self.assertEqual(entry["license_spdx"], "Apache-2.0")
        metadata = catalog_module.OFFICIAL_SKILLS["web-artifacts-builder"]
        self.assertEqual(metadata["files"], 5)
        self.assertEqual(metadata["bytes"], 45_840)
        self.assertEqual(
            metadata["sha256"],
            "0f102ba19f13b4deb19f2248a5014952d2af1521129a5a0e4f3d805f74632b75",
        )

    def test_agentrelay_browser_bundle_is_mit_and_self_contained(self):
        entry = catalog_module.agentrelay_bundled_skill_entry(
            PROJECT_ROOT,
            "browser-automatic",
        )
        source = catalog_module.skill_source_dir(
            PROJECT_ROOT,
            {"skills": [entry], "defaults": ["browser-automatic"]},
            "browser-automatic",
        )
        self.assertEqual(entry["source_type"], "bundled")
        self.assertEqual(entry["license_spdx"], "MIT")
        self.assertTrue((source / "SKILL.md").is_file())
        self.assertTrue((source / "scripts" / "browse.py").is_file())
        self.assertIn(
            "MIT License",
            (source / "LICENSE").read_text(encoding="utf-8"),
        )

    def test_publish_tree_rejects_private_skill_directory(self):
        installer = load_module(
            "skill_catalog_publish_guard", PROJECT_ROOT / "install.py"
        )
        with tempfile.TemporaryDirectory(prefix="agentrelay-publish-guard-") as directory:
            root = Path(directory)
            private = root / "skills" / "bitch-skill"
            private.mkdir(parents=True)
            (private / "SKILL.md").write_text("private", encoding="utf-8")
            with patch.object(installer, "PROJECT_ROOT", root):
                with self.assertRaises(installer.InstallError):
                    installer.validate_publish_tree()

    def test_publish_tree_allows_managed_skill_directories(self):
        installer = load_module(
            "skill_catalog_publish_guard_managed", PROJECT_ROOT / "install.py"
        )
        with tempfile.TemporaryDirectory(prefix="agentrelay-publish-managed-") as directory:
            root = Path(directory)
            for skill_id in ("agent-relay", "agent-fusion", "agent-image"):
                skill = root / "skills" / skill_id
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("managed", encoding="utf-8")
            with patch.object(installer, "PROJECT_ROOT", root):
                installer.validate_publish_tree()

    def test_official_archive_rejects_unsafe_paths_links_and_duplicates(self):
        root = (
            Path(catalog_module.OFFICIAL_SKILL_ARCHIVE_ROOT)
            / "skills"
            / "docx"
        )
        unsafe_members = {
            "traversal": tarfile.TarInfo("../escape.txt"),
            "absolute": tarfile.TarInfo("/tmp/escape.txt"),
        }
        symlink = tarfile.TarInfo(str(root / "linked"))
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/tmp/outside"
        unsafe_members["symlink"] = symlink

        for label, member in unsafe_members.items():
            with self.subTest(label=label):
                archive = self.tmp_archive(label)
                with tarfile.open(archive, "w:gz") as handle:
                    data = b"unsafe"
                    member.size = len(data) if member.isfile() else 0
                    handle.addfile(member, io.BytesIO(data) if member.isfile() else None)
                with self.assertRaises(catalog_module.CatalogError):
                    with catalog_module.official_skill_source(
                        "docx", archive_source=archive
                    ):
                        pass

        duplicate = self.tmp_archive("duplicate")
        with tarfile.open(duplicate, "w:gz") as handle:
            for _ in range(2):
                data = b"duplicate"
                member = tarfile.TarInfo(str(root / "SKILL.md"))
                member.size = len(data)
                handle.addfile(member, io.BytesIO(data))
        with self.assertRaises(catalog_module.CatalogError):
            with catalog_module.official_skill_source(
                "docx", archive_source=duplicate
            ):
                pass

    def test_official_archive_accepts_both_codeload_root_names(self):
        roots = (
            f"anthropics-skills-{catalog_module.OFFICIAL_SKILL_COMMIT[:7]}",
            f"skills-{catalog_module.OFFICIAL_SKILL_COMMIT}",
        )
        for index, archive_root in enumerate(roots):
            with self.subTest(archive_root=archive_root):
                archive = self.tmp_archive(f"root-{index}")
                fixture = build_official_archive(
                    archive,
                    archive_root=archive_root,
                )
                metadata = {
                    **catalog_module.OFFICIAL_SKILLS["docx"],
                    **fixture,
                }
                with patch.dict(
                    catalog_module.OFFICIAL_SKILLS,
                    {"docx": metadata},
                    clear=False,
                ):
                    with catalog_module.official_skill_source(
                        "docx", archive_source=archive
                    ) as source:
                        self.assertTrue((source / "SKILL.md").is_file())
                        self.assertTrue((source / "LICENSE.txt").is_file())

    def test_official_skill_rejects_bad_license_and_content_checksum(self):
        archive = self.tmp_archive("bad-license")
        fixture = build_official_archive(
            archive,
            files={
                "SKILL.md": (
                    "---\nname: docx\ndescription: fixture\n---\n"
                ),
                "LICENSE.txt": "Not an Apache license",
            },
        )
        metadata = {
            **catalog_module.OFFICIAL_SKILLS["docx"],
            **fixture,
        }
        with patch.dict(
            catalog_module.OFFICIAL_SKILLS,
            {"docx": metadata},
            clear=False,
        ):
            with self.assertRaises(catalog_module.CatalogError):
                with catalog_module.official_skill_source(
                    "docx", archive_source=archive
                ):
                    pass

        archive = self.tmp_archive("bad-digest")
        fixture = build_official_archive(archive)
        metadata = {
            **catalog_module.OFFICIAL_SKILLS["docx"],
            **fixture,
            "sha256": "0" * 64,
        }
        with patch.dict(
            catalog_module.OFFICIAL_SKILLS,
            {"docx": metadata},
            clear=False,
        ):
            with self.assertRaises(catalog_module.CatalogError):
                with catalog_module.official_skill_source(
                    "docx", archive_source=archive
                ):
                    pass

    def test_official_download_rejects_oversized_or_failed_responses(self):
        class Response(io.BytesIO):
            def __init__(self, data, length):
                super().__init__(data)
                self.headers = {"Content-Length": str(length)}

        destination = self.tmp_archive("download")
        with patch.object(
            catalog_module.urllib.request,
            "urlopen",
            return_value=Response(
                b"",
                catalog_module.OFFICIAL_SKILL_DOWNLOAD_MAX_BYTES + 1,
            ),
        ):
            with self.assertRaises(catalog_module.CatalogError):
                catalog_module.download_official_archive(destination)
        self.assertFalse(destination.exists())

        with patch.object(
            catalog_module.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            with self.assertRaises(catalog_module.CatalogError):
                catalog_module.download_official_archive(destination)
        self.assertFalse(destination.exists())

    def tmp_archive(self, label):
        return self.test_temp / f"{label}.tar.gz"


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentrelay-skill-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.catalog = catalog_module.load_catalog(PROJECT_ROOT)

    def test_install_copies_nested_references_and_license(self):
        source = catalog_module.skill_source_dir(
            PROJECT_ROOT, self.catalog, "vue-best-practices"
        )
        destination = self.tmp / "skills" / "vue-best-practices"
        result = targets.install_skill_tree_to(
            source,
            destination,
            catalog_module.license_file(PROJECT_ROOT, self.catalog),
        )
        source_files = sum(path.is_file() for path in source.rglob("*"))
        self.assertEqual(result["installed"], source_files + 1)
        self.assertEqual(
            (destination / "references" / "reactivity.md").read_bytes(),
            (source / "references" / "reactivity.md").read_bytes(),
        )
        self.assertTrue((destination / "LICENSE").is_file())

    def test_reinstall_backs_up_changed_files(self):
        source = self.tmp / "source"
        source.mkdir()
        (source / "SKILL.md").write_text("new", encoding="utf-8")
        destination = self.tmp / "destination"
        targets.install_skill_tree_to(source, destination)
        (destination / "SKILL.md").write_text("old", encoding="utf-8")
        targets.install_skill_tree_to(source, destination)
        self.assertEqual(
            (destination / "SKILL.md").read_text(encoding="utf-8"), "new"
        )
        backups = list(destination.glob("SKILL.md.before-agentrelay-update.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "old")

    def test_rapid_reinstalls_keep_each_changed_backup(self):
        source = self.tmp / "source"
        source.mkdir()
        (source / "SKILL.md").write_text("new", encoding="utf-8")
        destination = self.tmp / "destination"
        targets.install_skill_tree_to(source, destination)

        (destination / "SKILL.md").write_text("middle", encoding="utf-8")
        targets.install_skill_tree_to(source, destination)
        (destination / "SKILL.md").write_text("old", encoding="utf-8")
        targets.install_skill_tree_to(source, destination)

        backups = sorted(
            destination.glob("SKILL.md.before-agentrelay-update.*")
        )
        self.assertEqual(len(backups), 2)
        self.assertEqual(
            sorted(path.read_text(encoding="utf-8") for path in backups),
            ["middle", "old"],
        )

    def test_source_and_destination_symlinks_are_rejected(self):
        source = self.tmp / "source-link"
        source.mkdir()
        outside = self.tmp / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (source / "reference").mkdir()
        (source / "reference" / "link.md").symlink_to(outside)
        with self.assertRaises(targets.TargetError):
            targets.install_skill_tree_to(source, self.tmp / "destination")

        safe_source = self.tmp / "safe-source"
        safe_source.mkdir()
        (safe_source / "SKILL.md").write_text("safe", encoding="utf-8")
        destination = self.tmp / "linked-destination"
        destination.symlink_to(self.tmp / "outside-destination")
        with self.assertRaises(targets.TargetError):
            targets.install_skill_tree_to(safe_source, destination)

    def test_codex_claude_and_pi_receive_skill_in_their_skill_roots(self):
        home = self.tmp / "home"
        with patch.dict(os.environ, {"CODEX_HOME": str(home / ".codex")}), \
                patch.object(targets.Path, "home", return_value=home):
            registry = targets.agent_targets()
        for target_id in ("codex", "claude", "pi"):
            destination = targets.install_catalog_skill_to(
                registry[target_id], PROJECT_ROOT, "vue-best-practices"
            )
            self.assertEqual(
                destination,
                Path(registry[target_id]["skill_dir"]).parent
                / "vue-best-practices",
            )
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertTrue((destination / "references" / "reactivity.md").is_file())
            self.assertTrue((destination / "LICENSE").is_file())

    def test_browser_skill_installs_independently_for_all_agents(self):
        home = self.tmp / "home"
        codex_root = home / ".codex" / "skills"
        local_browser = codex_root / "browser-automatic"
        local_browser.mkdir(parents=True)
        (local_browser / "SKILL.md").write_text(
            "---\nname: browser-automation\ndescription: unlicensed local copy\n---\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CODEX_HOME": str(home / ".codex")}), \
                patch.object(targets.Path, "home", return_value=home):
            registry = targets.agent_targets()
        catalog = catalog_module.catalog_with_installed_skills(
            PROJECT_ROOT,
            {"Codex": codex_root},
        )
        destinations = {}
        for target_id in ("codex", "claude", "pi"):
            destinations[target_id] = targets.install_catalog_skill_to(
                registry[target_id],
                PROJECT_ROOT,
                "browser-automatic",
                catalog=catalog,
            )

        self.assertEqual(len(set(destinations.values())), 3)
        for destination in destinations.values():
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertTrue((destination / "scripts" / "browse.py").is_file())
            self.assertIn(
                "MIT License",
                (destination / "LICENSE").read_text(encoding="utf-8"),
            )

    def test_official_skill_downloads_independently_for_codex_claude_and_pi(self):
        archive = self.tmp / "anthropics-skills.tar.gz"
        fixture = build_official_archive(archive)
        metadata = {
            **catalog_module.OFFICIAL_SKILLS["docx"],
            **fixture,
        }
        codex_root = self.tmp / "home" / ".codex" / "skills"
        local_docx = codex_root / "docx"
        local_docx.mkdir(parents=True)
        (local_docx / "SKILL.md").write_text("LOCAL COPY", encoding="utf-8")
        (local_docx / "LICENSE.txt").write_text(
            "You may not reproduce or copy these materials.",
            encoding="utf-8",
        )
        with patch.dict(
            catalog_module.OFFICIAL_SKILLS,
            {"docx": metadata},
            clear=False,
        ):
            catalog = catalog_module.catalog_with_installed_skills(
                PROJECT_ROOT, {"Codex": codex_root}
            )
            registry = {
                "codex": {
                    "skill_dir": str(codex_root / "agent-relay"),
                },
                "claude": {
                    "skill_dir": str(
                        self.tmp / "home" / ".claude" / "skills" / "agent-relay"
                    ),
                },
                "pi": {
                    "skill_dir": str(
                        self.tmp / "home" / ".pi" / "agent" / "skills" / "agent-relay"
                    ),
                },
            }
            destinations = {}
            for target_id, target in registry.items():
                destinations[target_id] = targets.install_catalog_skill_to(
                    target,
                    PROJECT_ROOT,
                    "docx",
                    catalog=catalog,
                    archive_source=archive,
                )

        self.assertEqual(len(set(destinations.values())), 3)
        for destination in destinations.values():
            self.assertIn(
                "Official fixture",
                (destination / "SKILL.md").read_text(encoding="utf-8"),
            )
            self.assertTrue((destination / "scripts" / "render.py").is_file())
            self.assertIn(
                "Apache License",
                (destination / "LICENSE.txt").read_text(encoding="utf-8"),
            )
        self.assertEqual(
            destinations["codex"],
            local_docx,
        )

    def test_official_reinstall_replaces_stale_files_after_backup(self):
        archive = self.tmp / "anthropics-skills.tar.gz"
        fixture = build_official_archive(archive)
        metadata = {
            **catalog_module.OFFICIAL_SKILLS["docx"],
            **fixture,
        }
        destination = self.tmp / "skills" / "docx"
        destination.mkdir(parents=True)
        (destination / "stale.txt").write_text("stale", encoding="utf-8")
        with patch.dict(
            catalog_module.OFFICIAL_SKILLS,
            {"docx": metadata},
            clear=False,
        ):
            with catalog_module.official_skill_source(
                "docx", archive_source=archive
            ) as source:
                targets.replace_skill_tree_to(source, destination)

        self.assertFalse((destination / "stale.txt").exists())
        backups = list(
            destination.parent.glob("docx.before-agentrelay-update.*")
        )
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "stale.txt").is_file())

    def test_installed_skill_copies_to_another_agent_and_allows_same_source(self):
        codex_root = self.tmp / "codex-skills"
        sample = codex_root / "sample-skill"
        sample.mkdir(parents=True)
        (sample / "SKILL.md").write_text(
            "---\n"
            "name: sample-skill\n"
            "description: Sample documents\n"
            "---\n",
            encoding="utf-8",
        )
        (sample / "LICENSE.txt").write_text("MIT License", encoding="utf-8")
        (sample / "scripts").mkdir()
        (sample / "scripts" / "render.py").write_text("print('ok')", encoding="utf-8")
        catalog = catalog_module.catalog_with_installed_skills(
            PROJECT_ROOT, {"Codex": codex_root}
        )
        codex_target = {
            "skill_dir": str(codex_root / "agent-relay"),
        }
        claude_target = {
            "skill_dir": str(self.tmp / "claude-skills" / "agent-relay"),
        }

        same = targets.install_catalog_skill_to(
            codex_target,
            PROJECT_ROOT,
            "sample-skill",
            catalog=catalog,
        )
        copied = targets.install_catalog_skill_to(
            claude_target,
            PROJECT_ROOT,
            "sample-skill",
            catalog=catalog,
        )

        self.assertEqual(same, sample)
        self.assertTrue((copied / "SKILL.md").is_file())
        self.assertTrue((copied / "LICENSE.txt").is_file())
        self.assertTrue((copied / "scripts" / "render.py").is_file())

    def test_restricted_installed_skill_is_rejected_before_copy(self):
        source_root = self.tmp / "source-skills"
        restricted = source_root / "restricted-report"
        restricted.mkdir(parents=True)
        (restricted / "SKILL.md").write_text(
            "---\nname: restricted-report\ndescription: Local only\n---\n",
            encoding="utf-8",
        )
        (restricted / "LICENSE.txt").write_text(
            "You may not reproduce or copy these materials.",
            encoding="utf-8",
        )
        catalog = catalog_module.catalog_with_installed_skills(
            PROJECT_ROOT, {"Codex": source_root}
        )
        target = {"skill_dir": str(self.tmp / "claude-skills" / "agent-relay")}

        with self.assertRaises(targets.TargetError):
            targets.install_catalog_skill_to(
                target,
                PROJECT_ROOT,
                "restricted-report",
                catalog=catalog,
            )
        self.assertFalse(
            (self.tmp / "claude-skills" / "restricted-report").exists()
        )


class InstallerIntegrationTests(unittest.TestCase):
    def test_install_manifest_includes_skill_catalog_runtime(self):
        installer = load_module(
            "skill_catalog_install_manifest", PROJECT_ROOT / "install.py"
        )
        self.assertIn("skill_catalog.py", installer.SCRIPT_FILES)

    def test_terminal_skill_checklist_defaults_to_installable_items(self):
        installer = load_module(
            "skill_catalog_installer_checklist", PROJECT_ROOT / "install.py"
        )
        with tempfile.TemporaryDirectory(prefix="agentrelay-checklist-") as directory:
            root = Path(directory)
            docx = root / "docx"
            docx.mkdir()
            (docx / "SKILL.md").write_text(
                "---\nname: docx\ndescription: Word documents\n---\n",
                encoding="utf-8",
            )
            catalog = catalog_module.catalog_with_installed_skills(
                PROJECT_ROOT, {"Codex": root}
            )
            with patch("builtins.input", return_value=""):
                selected = installer.choose_install_skills(catalog)

        self.assertEqual(
            selected,
            [
                "docx",
                "vue-best-practices",
                "pdf",
                "pptx",
                "web-artifacts-builder",
                "xlsx",
                "browser-automatic",
            ],
        )

    def test_apply_agent_targets_records_selected_vue_skill_check(self):
        installer = load_module(
            "skill_catalog_installer", PROJECT_ROOT / "install.py"
        )
        from scripts import agent_targets as package_targets

        with tempfile.TemporaryDirectory(prefix="agentrelay-installer-skill-") as directory:
            tmp = Path(directory)
            meta = {
                "id": "codex",
                "label": "Codex CLI",
                "skill_dir": str(tmp / "target" / "skills" / "agent-relay"),
                "level": "skill-only",
            }
            destination = tmp / "bundled" / "vue-best-practices"
            destination.mkdir(parents=True)
            (destination / "SKILL.md").write_text("skill", encoding="utf-8")
            (destination / "LICENSE").write_text("MIT", encoding="utf-8")
            with patch.object(
                package_targets, "agent_targets", return_value={"codex": meta}
            ), patch.object(
                package_targets, "install_skill_to"
            ), patch.object(
                package_targets,
                "install_catalog_skill_to",
                return_value=destination,
            ), patch.object(
                package_targets, "verify_target", return_value=[]
            ), patch.object(
                installer, "merge_hooks_json"
            ):
                results, errors = installer.apply_agent_targets(
                    ["codex"], ["vue-best-practices"]
                )
        self.assertEqual(errors, {})
        self.assertTrue(
            any(check["name"] == "内置 Skill：vue-best-practices"
                for check in results["codex"])
        )

    def test_official_download_failure_short_circuits_before_target_changes(self):
        installer = load_module(
            "skill_catalog_installer_download_failure", PROJECT_ROOT / "install.py"
        )
        catalog = catalog_module.catalog_with_installed_skills(
            PROJECT_ROOT, {"Codex": self.tmp_dir()}
        )
        with patch.object(
            installer,
            "load_install_skill_catalog",
            return_value=catalog,
        ), patch.object(
            package_catalog_module,
            "download_official_archive",
            side_effect=package_catalog_module.CatalogError("offline"),
        ), patch.object(
            installer,
            "_apply_agent_targets_loop",
        ) as apply_loop:
            results, errors = installer.apply_agent_targets(
                ["codex"], ["docx"]
            )

        self.assertEqual(results, {})
        self.assertIn("官方 Skill 下载失败：offline", errors["codex"])
        apply_loop.assert_not_called()

    def test_official_archive_downloads_once_for_all_selected_skills(self):
        installer = load_module(
            "skill_catalog_installer_single_download", PROJECT_ROOT / "install.py"
        )
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-single-download-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        archive = tmp / "anthropics-skills.tar.gz"
        archive.write_bytes(b"archive fixture")
        catalog = {
            "skills": [
                catalog_module.official_skill_entry("docx"),
                catalog_module.official_skill_entry("pdf"),
            ],
            "defaults": ["docx", "pdf"],
        }
        with patch.object(
            installer,
            "load_install_skill_catalog",
            return_value=catalog,
        ), patch.object(
            package_catalog_module,
            "download_official_archive",
            return_value=archive,
        ) as download, patch.object(
            package_catalog_module,
            "official_skill_source",
            return_value=nullcontext(archive),
        ) as official_source, patch.object(
            installer,
            "_apply_agent_targets_loop",
            return_value=({}, {}),
        ) as apply_loop:
            results, errors = installer.apply_agent_targets(
                ["codex", "claude"], ["docx", "pdf"]
            )

        self.assertEqual((results, errors), ({}, {}))
        download.assert_called_once()
        self.assertEqual(official_source.call_count, 2)
        apply_loop.assert_called_once()

    def tmp_dir(self):
        directory = Path(tempfile.mkdtemp(prefix="agentrelay-installer-catalog-"))
        self.addCleanup(shutil.rmtree, directory, True)
        return directory

    def test_main_accepts_skills_and_no_skills_flags(self):
        installer = load_module(
            "skill_catalog_installer_cli", PROJECT_ROOT / "install.py"
        )
        common_patches = (
            patch.object(installer, "validate_publish_tree"),
            patch.object(installer, "check_base_environment"),
            patch.object(installer, "ensure_virtualenv"),
            patch.object(installer, "install_dependencies"),
            patch.object(installer, "install_and_verify_chromium"),
            patch.object(installer, "install_files"),
            patch.object(installer, "merge_hooks_json"),
            patch.object(
                installer, "_load_local_config", return_value=({}, lambda *_: None)
            ),
            patch.object(installer, "write_daemon_service_config", return_value=None),
            patch.object(installer, "print_next_steps"),
            patch.object(installer, "print_verify_results"),
        )
        with common_patches[0], common_patches[1], common_patches[2], \
                common_patches[3], common_patches[4], common_patches[5], \
                common_patches[6], common_patches[7], common_patches[8], \
                common_patches[9], common_patches[10], patch.object(
                    installer,
                    "apply_agent_targets",
                    return_value=({}, {}),
                ) as apply:
            self.assertEqual(
                installer.main([
                    "--targets", "codex",
                    "--skills", "vue-router-best-practices,vue-best-practices",
                    "--no-menu",
                ]),
                0,
            )
            apply.assert_called_once_with(
                ["codex"],
                ["vue-router-best-practices", "vue-best-practices"],
            )

        with common_patches[0], common_patches[1], common_patches[2], \
                common_patches[3], common_patches[4], common_patches[5], \
                common_patches[6], common_patches[7], common_patches[8], \
                common_patches[9], common_patches[10], patch.object(
                    installer,
                    "apply_agent_targets",
                    return_value=({}, {}),
                ) as apply:
            self.assertEqual(
                installer.main([
                    "--targets", "codex", "--no-skills", "--no-menu",
                ]),
                0,
            )
            apply.assert_called_once_with(["codex"], [])

    def test_config_page_exposes_skill_boxes_and_submits_ids(self):
        config_web = load_module(
            "skill_catalog_config_web", PROJECT_ROOT / "scripts" / "config_web.py"
        )
        page = config_web.build_config_page({
            "install_skills": ["vue-best-practices"],
            "skill_catalog": catalog_module.public_catalog(
                catalog_module.catalog_with_installed_skills(PROJECT_ROOT, {})
            ),
        })
        self.assertIn('class="skill-box${installable', page)
        self.assertIn("开发 Skills", page)
        self.assertIn("本机已安装 Skill", page)
        self.assertIn("不可复制", page)
        self.assertIn("LOCAL + TRUSTED SOURCES", page)
        self.assertIn("Anthropic 官方下载", page)
        self.assertIn("AgentRelay 内置", page)
        self.assertIn("skills:skillIds", page)


if __name__ == "__main__":
    unittest.main()
