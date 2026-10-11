import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT=Path('/Users/apple/Documents/Codex/2026-09-09/hind/outputs/evolving-profile-2.1/rollback-2.1.zsh')
BASELINE=Path('/Users/apple/.evolving-profile/backups/code/evolving-profile-2.1-pre-20260919T1730')


class RollbackDrillTest(unittest.TestCase):
    def fixture(self, root:Path):
        for name in ('source','state','backup','runtime','launch','release'):
            (root/name).mkdir()
        shutil.copytree(BASELINE,root/'backup',dirs_exist_ok=True)
        (root/'launch/com.evolving-profile.console.plist').write_text(
            '<?xml version="1.0"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0">'
            '<dict><key>WorkingDirectory</key><string>/old</string></dict></plist>'
        )
        for path in BASELINE.rglob('*'):
            if not path.is_file():continue
            relative=path.relative_to(BASELINE)
            target=root/'source'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('changed')
            if relative.parts[0] in {'host-adapter','guidance','status'}:
                runtime=root/'runtime'/relative;runtime.parent.mkdir(parents=True,exist_ok=True);runtime.write_text('changed')
        for relative in ('host-adapter/task_state.py','host-adapter/evidence_gap.py','host-adapter/topic_catalog.py','host-adapter/lib/memory_policy.py','guidance/profile_projection.py','guidance/topic_catalog_worker.py'):
            for lane in ('source','runtime'):
                target=root/lane/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('new')
        for relative in ('bin/evolving-profile-worker.zsh','bin/evolving-profile-topic-catalog.zsh','catalog/topic.txt','task-state/state.json','audit/evidence-decisions/value.json'):
            target=root/'state'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('new')
        for name in ('com.evolving-profile.worker.plist','com.evolving-profile.topic-catalog.plist'):
            (root/'launch'/name).write_text('new')

    def environment(self,root:Path):
        return {**os.environ,**{f'EP_ROLLBACK_{key.upper()}':str(root/key) for key in
                               ('source','state','backup','runtime','launch')},
                'EP_ROLLBACK_PREVIOUS_CONSOLE':str(root/'release'),'EP_ROLLBACK_SKIP_SERVICES':'1'}

    def test_dry_run_targets_only_isolated_roots(self):
        with tempfile.TemporaryDirectory(prefix='ep21-rollback-test-') as directory:
            root=Path(directory)
            self.fixture(root);env=self.environment(root)
            completed=subprocess.run([str(SCRIPT),'--dry-run'],env=env,text=True,capture_output=True)
            self.assertIn(str(root/'source'),completed.stdout)
            self.assertNotIn('launchctl bootout',completed.stdout)
            self.assertNotIn('required path missing: /Users/apple/',completed.stderr)

    def test_apply_restores_isolated_copy_and_keeps_quarantine_paths_unique(self):
        with tempfile.TemporaryDirectory(prefix='ep21-rollback-apply-') as directory:
            root=Path(directory);self.fixture(root);env=self.environment(root)
            completed=subprocess.run([str(SCRIPT),'--apply'],env=env,text=True,capture_output=True)
            self.assertEqual(completed.returncode,0,completed.stderr)
            for relative in ('guidance/mcp_runtime.py','host-adapter/lib/context_coordination.py','host-adapter/recall.py'):
                self.assertEqual((root/'source'/relative).read_bytes(),(root/'backup'/relative).read_bytes())
                self.assertEqual((root/'runtime'/relative).read_bytes(),(root/'backup'/relative).read_bytes())
            quarantined=list((root/'state'/'quarantine').rglob('task_state.py'))
            self.assertEqual(len(quarantined),2)
            self.assertNotEqual(quarantined[0].parent,quarantined[1].parent)


if __name__=='__main__':unittest.main()
