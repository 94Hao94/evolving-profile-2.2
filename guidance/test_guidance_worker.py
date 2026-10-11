import sys
from pathlib import Path
import unittest
import json
import tempfile
from unittest.mock import patch
import guidance_worker

sys.path.insert(0, str(Path(__file__).parent))

from guidance_worker import changed_observation_ids


class GuidanceWorkerTests(unittest.TestCase):
    def test_idle_status_recovers_last_successful_publication_from_disk(self):
        with tempfile.TemporaryDirectory() as root:
            run=Path(root)/'run';out=run/'observation-publication';out.mkdir(parents=True)
            (out/'publication-report.json').write_text(json.dumps({'active_published':2,'failed':0,'completed_at':'2026-10-07T00:00:00Z'}))
            (run/'observation-ids.json').write_text(json.dumps(['a','b','c']))
            with patch.object(guidance_worker,'RUNS',Path(root)):
                result=guidance_worker.last_completed_summary({})
            self.assertIsNotNone(result)
            self.assertEqual(result['processed'],3)
            self.assertEqual(result['publication']['active_published'],2)
    def test_legacy_id_only_watermark_processes_only_new_observations_once(self):
        current = {"old": "sha256:old", "new": "sha256:new"}
        state = {"seen_observation_ids": ["old"]}

        self.assertEqual(changed_observation_ids(current, state), ["new"])

    def test_fingerprint_watermark_reprocesses_a_changed_observation(self):
        current = {"unchanged": "sha256:a", "changed": "sha256:new"}
        state = {"seen_observation_fingerprints": {"unchanged": "sha256:a", "changed": "sha256:old"}}

        self.assertEqual(changed_observation_ids(current, state), ["changed"])
