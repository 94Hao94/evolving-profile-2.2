import tempfile
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from task_state import TaskStateStore


class TaskStateStoreTest(unittest.TestCase):
    def test_metadata_only_ingress_preserves_task_version_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store=TaskStateStore(Path(root))
            initial=store.record('s1','核验原生来源，不要部署','t1','h1',continuation=False)
            state=store.record('s1','<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>','t2','h2',continuation=False)
            self.assertEqual(state,initial)
            self.assertEqual(store.load('s1'),initial)
            empty=store.record('fresh','<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>','t3','h3',continuation=False)
            self.assertEqual(empty,{})
            self.assertIsNone(store.load('fresh'))

    def test_mixed_metadata_keeps_real_user_directive_and_quote_is_a_task(self):
        with tempfile.TemporaryDirectory() as root:
            store=TaskStateStore(Path(root));page='<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'
            first=store.record('s1',page+'\n检查材料附件','t1','h1',continuation=False)
            self.assertEqual(first['current_objective'],'检查材料附件')
            quoted='解释这个示例：'+page
            second=store.record('s1',quoted,'t2','h2',continuation=False)
            self.assertEqual(second['current_objective'],quoted)

    def test_new_human_turn_does_not_reuse_a_legacy_metadata_objective(self):
        with tempfile.TemporaryDirectory() as root:
            store=TaskStateStore(Path(root))
            old=store.record('s1','old human task','t1','h1',continuation=False)
            old['current_objective']='<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'
            store._path('s1').write_text(json.dumps(old))
            resumed=store.record('s1','继续处理','t2','h2',continuation=True)
            self.assertEqual(resumed['current_objective'],'继续处理')
            self.assertIsNone(resumed['continuation_context'])
            self.assertFalse(resumed['continuation'])
    def test_standalone_prompt_does_not_inherit_previous_topic(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStateStore(Path(root))
            store.record("s1", "解释 Lark 是什么", "t1", "h1", continuation=False)
            state = store.record("s1", "检查本机待清理的备份", "t2", "h2", continuation=False)
        self.assertEqual(state["current_objective"], "检查本机待清理的备份")
        self.assertIsNone(state["continuation_context"])

    def test_short_continuation_recovers_active_objective(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStateStore(Path(root))
            store.record("s1", "清理旧版超大备份并验证新备份", "t1", "h1", continuation=False)
            state = store.record("s1", "继续", "t2", "h2", continuation=True)
        self.assertEqual(state["current_objective"], "清理旧版超大备份并验证新备份")
        self.assertEqual(state["current_message"], "继续")
        self.assertIn("清理旧版", state["continuation_context"])

    def test_continuation_preserves_agent_work_projection(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStateStore(Path(root))
            initial = store.record("s1", "升级记忆系统", "t1", "h1", continuation=False)
            store.update("s1", {
                "constraints": ["当前 Prompt 优先"],
                "completed": ["完成目录审查"],
                "unresolved": ["补答案层评测"],
                "objects": ["Evolving Profile 2.1"],
                "source_versions": {"prd": "2.1"},
            }, expected_version=initial["version"])
            state = store.record("s1", "那你建议怎么修？", "t2", "h2", continuation=True)
        self.assertEqual(state["current_objective"], "升级记忆系统")
        self.assertEqual(state["constraints"], ["当前 Prompt 优先"])
        self.assertEqual(state["completed"], ["完成目录审查"])
        self.assertEqual(state["unresolved"], ["补答案层评测"])
        self.assertEqual(state["objects"], ["Evolving Profile 2.1"])
        self.assertEqual(state["source_versions"], {"prd": "2.1"})

    def test_session_state_is_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStateStore(Path(root))
            store.record("s1", "任务一", "t1", "h1", continuation=False)
            state = store.record("s2", "继续", "t2", "h2", continuation=True)
        self.assertEqual(state["current_objective"], "继续")
        self.assertIsNone(state["continuation_context"])

    def test_agent_update_preserves_identity_and_tracks_unresolved_work(self):
        with tempfile.TemporaryDirectory() as root:
            store=TaskStateStore(Path(root));store.record('s1','检查备份','t1','h1',continuation=False)
            state=store.update('s1',{'current_objective':'修复备份并验证','completed':['定位大表'],'unresolved':['生成新备份']},'t1','h1')
        self.assertEqual(state['completed'],['定位大表'])
        self.assertEqual(state['unresolved'],['生成新备份'])
        self.assertEqual(state['source'],'agent_explicit_task_update')

    def test_version_reason_and_expiry_are_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStateStore(Path(root), max_age_seconds=120)
            first = store.record('s1', '检查备份', 't1', 'h1', continuation=False)
            second = store.update('s1', {'completed':['定位大表']}, expected_version=first['version'])
        self.assertEqual(first['version'], 1)
        self.assertEqual(first['update_reason'], 'prompt_ingress')
        self.assertEqual(second['version'], 2)
        self.assertEqual(second['update_reason'], 'agent_update')
        self.assertGreater(second['expires_at_epoch'], second['updated_at_epoch'])
        self.assertIn('expires_at', second)

    def test_stale_expected_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStateStore(Path(root))
            first = store.record('s1', '检查备份', 't1', 'h1', continuation=False)
            store.update('s1', {'completed':['一步']}, expected_version=first['version'])
            with self.assertRaisesRegex(ValueError, 'stale_task_state_version'):
                store.update('s1', {'completed':['陈旧写入']}, expected_version=first['version'])

    def test_concurrent_updates_are_serialized_without_lost_versions(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStateStore(Path(root))
            store.record('s1', '检查备份', 't1', 'h1', continuation=False)

            def update(index):
                return store.update('s1', {'current_message':f'并发更新 {index}'})['version']

            with ThreadPoolExecutor(max_workers=8) as pool:
                versions = list(pool.map(update, range(20)))
            final = store.load('s1')
        self.assertEqual(sorted(versions), list(range(2, 22)))
        self.assertEqual(final['version'], 21)


if __name__ == "__main__":
    unittest.main()
