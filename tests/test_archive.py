import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from agent_relay_archive import (  # noqa: E402
    ARCHIVE_DIRNAME,
    archive_root,
    archive_task,
    infer_project,
    list_projects,
    load_records,
    records_path,
    sanitize_project,
    search_records,
)


class ArchiveStoreTests(unittest.TestCase):
    def setUp(self):
        self._old_root = os.environ.get("AGENTRELAY_SKILL_ROOT")
        self._old_project = os.environ.get("AGENTRELAY_PROJECT")
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["AGENTRELAY_SKILL_ROOT"] = str(self.tmp / "skill")
        os.environ.pop("AGENTRELAY_PROJECT", None)

    def tearDown(self):
        if self._old_root is None:
            os.environ.pop("AGENTRELAY_SKILL_ROOT", None)
        else:
            os.environ["AGENTRELAY_SKILL_ROOT"] = self._old_root
        if self._old_project is None:
            os.environ.pop("AGENTRELAY_PROJECT", None)
        else:
            os.environ["AGENTRELAY_PROJECT"] = self._old_project

    def test_archive_lives_inside_skill_folder_by_project(self):
        record = archive_task(title="MQ 换 Kafka", project="de", summary="切到 Kafka",
                              changed_files=["src/MqSender.java"], verification=["pytest 通过"])
        expected_dir = self.tmp / "skill" / ARCHIVE_DIRNAME / "de"
        self.assertEqual(archive_root().resolve(), (self.tmp / "skill" / ARCHIVE_DIRNAME).resolve())
        self.assertEqual(records_path("de").resolve(), (expected_dir / "已完成任务.jsonl").resolve())
        self.assertTrue(records_path("de").is_file())
        self.assertTrue((expected_dir / "归档.md").is_file())
        self.assertEqual(record.project, "de")
        # 归档写在 Skill 目录内，不能污染项目仓库。
        self.assertIn(ARCHIVE_DIRNAME, str(records_path("de")))

    def test_records_round_trip_and_search(self):
        archive_task(title="Redis 依赖装不上", project="de", summary="换镜像源解决",
                     tags=["依赖"])
        archive_task(title="页面按钮改颜色", project="de", summary="改成主题蓝")
        records = load_records("de")
        self.assertEqual(len(records), 2)
        hits = search_records("de", "Redis")
        self.assertEqual([item.title for item in hits], ["Redis 依赖装不上"])

    def test_rolled_back_task_is_recorded_without_claiming_success(self):
        archive_task(title="Kafka 迁移失败", project="de", status="已回滚",
                     summary="验证失败已回滚", rollback="恢复 MqSender.java")
        record = load_records("de")[0]
        self.assertEqual(record.status, "已回滚")
        self.assertNotEqual(record.status, "已完成")

    def test_archive_markdown_is_human_readable(self):
        archive_task(title="按钮改色", project="de", summary="改成蓝色",
                     archived_by="user", verification=["页面检查通过"])
        markdown = (self.tmp / "skill" / ARCHIVE_DIRNAME / "de" / "归档.md").read_text(encoding="utf-8")
        self.assertIn("按钮改色", markdown)
        self.assertIn("用户要求", markdown)
        self.assertIn("页面检查通过", markdown)

    def test_project_name_cannot_escape_archive_directory(self):
        self.assertEqual(sanitize_project("../../etc/passwd"), "etc-passwd")
        record = archive_task(title="越界测试", project="../../etc/passwd")
        self.assertEqual(record.project, "etc-passwd")
        self.assertTrue(records_path("../../etc/passwd").resolve().is_relative_to(
            (self.tmp / "skill" / ARCHIVE_DIRNAME).resolve()))

    def test_projects_listing(self):
        archive_task(title="A", project="de")
        archive_task(title="B", project="agentrelay")
        self.assertEqual(list_projects(), ["agentrelay", "de"])

    def test_project_is_inferred_from_git_workspace(self):
        workspace = self.tmp / "repo"
        (workspace / ".git").mkdir(parents=True)
        self.assertEqual(infer_project(workspace=workspace), "repo")
        self.assertEqual(infer_project(explicit="custom"), "custom")

    def test_jsonl_records_stay_valid_json(self):
        archive_task(title="A", project="de", changed_files=["a.py", "b.py"])
        lines = records_path("de").read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[0])
        self.assertEqual(payload["changed_files"], ["a.py", "b.py"])


if __name__ == "__main__":
    unittest.main()
