import json
import subprocess
import sys
import tempfile
import threading
import unittest
import inspect
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import io

from lib.retention_queue import RetentionQueue


class RetentionWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.posts=[]; self.status="completed"; self.reject=False; self.lost_ack=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({"status":owner.status}).encode())
            def do_POST(self):
                if self.path.endswith('/retry'):
                    owner.status='pending';self.send_response(200);self.end_headers();self.wfile.write(b'{"success":true}');return
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.posts.append(payload)
                self.send_response(503 if owner.reject else 200);self.end_headers()
                self.wfile.write(b'broken acknowledgement' if owner.lost_ack else json.dumps({"success":not owner.reject,"operation_id":"op1"}).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.queue=RetentionQueue(self.root/'queue.json',threshold_tokens=12000)
        self.config={"autoRetain":True,"retainQueuePath":str(self.queue.path),"retainTokenThreshold":12000,"retainTailMinAgeSeconds":1800,"evolvingProfileApiUrl":f"http://127.0.0.1:{self.server.server_port}","retainStrategy":"conversations"}
        (self.root/'config.json').write_text(json.dumps(self.config))
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def capture(self,project,at):
        self.queue.capture(session_id=project,message_count=1,bank_id="test-bank",project=project,content="真实用户原话",metadata={"source":"codex-hook-token-batch"},captured_at=at)
    def run_worker(self):
        p=subprocess.run([sys.executable,str(Path(__file__).parent/'retention_worker.py'),'--once','--config',str(self.root/'config.json'),'--state-root',str(self.root)],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        return json.loads(p.stdout)
    def test_old_subthreshold_tail_is_submitted_and_reconciled_without_new_stop(self):
        self.capture('old','2020-01-01T00:00:00Z')
        out=self.run_worker()
        self.assertEqual(out['status'],'submitted')
        self.assertEqual(len(self.posts),1)
        self.assertTrue(self.posts[0]['async'])
        self.assertEqual(self.posts[0]['items'][0]['strategy'],'conversations')
        self.assertEqual(self.queue._read()['items'][0]['operation_id'],'op1')
        self.run_worker()
        self.assertEqual(self.queue._read()['items'],[])
        self.assertEqual(len(self.posts),1)
    def test_recent_tail_waits_without_spending_model_calls(self):
        self.capture('fresh',datetime.now(timezone.utc))
        self.assertEqual(self.run_worker()['status'],'idle')
        self.assertEqual(self.posts,[])
        self.assertEqual(len(self.queue._read()['items']),1)
    def test_corrupt_queue_is_never_replaced_by_an_empty_queue_on_capture(self):
        original='{"items": [truncated raw state'
        self.queue.path.write_text(original)
        with self.assertRaises(ValueError):self.capture('new','2020-01-01T00:00:00Z')
        self.assertEqual(self.queue.path.read_text(),original)
    def test_unfinished_operation_prevents_another_model_request(self):
        self.capture('first','2020-01-01T00:00:00Z'); self.capture('second','2020-01-01T00:00:00Z')
        self.run_worker();self.status='processing'
        self.assertEqual(self.run_worker()['status'],'processing')
        self.assertEqual(len(self.posts),1)
    def test_failed_submit_keeps_raw_text_for_retry(self):
        self.capture('old','2020-01-01T00:00:00Z');self.reject=True
        self.assertEqual(self.run_worker()['status'],'retry_pending')
        rows=self.queue._read()['items']
        self.assertEqual(rows[0]['content'],'真实用户原话')
        self.assertNotIn('operation_id',rows[0])
    def test_lost_ack_is_recovered_without_a_second_model_submission(self):
        self.capture('old','2020-01-01T00:00:00Z');self.lost_ack=True
        self.assertEqual(self.run_worker()['status'],'retry_pending')
        path=self.root/'retention/worker-state.json';d=json.loads(path.read_text());d['next_retry_at_epoch']=0;path.write_text(json.dumps(d))
        self.assertEqual(self.run_worker()['status'],'recovered_acknowledgement')
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.queue._read()['items'][0]['operation_id'],self.posts[0]['operation_id'])
    def test_failed_extraction_retries_same_operation_without_resubmitting_content(self):
        self.capture('old','2020-01-01T00:00:00Z');self.run_worker();self.status='failed'
        self.assertEqual(self.run_worker()['status'],'retry_requested')
        self.assertEqual(self.queue._read()['items'][0]['operation_id'],'op1')
        self.assertEqual(len(self.posts),1)
    def test_configured_priority_tail_uses_the_shorter_age(self):
        self.queue.capture(session_id='priority',message_count=1,bank_id='test-bank',project='priority',content='已完成的有意义任务',metadata={'priority_tail':'true'},captured_at=datetime.now(timezone.utc)-timedelta(seconds=1200))
        self.assertEqual(self.run_worker()['status'],'submitted')
    def test_failed_forbidden_legacy_operation_is_not_retried(self):
        content='[role: user]\n这次不要写入记忆。\n[user:end]\n\n[role: assistant]\n完成。\n[assistant:end]'
        self.queue.capture('blocked',2,'test-bank','/project',content,{},'2020-01-01T00:00:00Z')
        batch=self.queue._select_batches(self.queue._read(),force_tail=True)[0]
        self.queue.mark_submitted(batch['batch_id'],'op1',item_ids=batch['item_ids'])
        self.status='failed'
        self.assertEqual(self.run_worker()['status'],'retry_policy_held')
        self.assertEqual(self.status,'failed')
        self.assertEqual(self.queue.snapshot()['items'][0]['content'],content)
    def test_lost_ack_missing_operation_cannot_resend_a_forbidden_intent(self):
        import retention_worker
        content='[role: user]\n不要保存到长期记忆。\n[user:end]\n\n[role: assistant]\n完成。\n[assistant:end]'
        self.queue.capture('blocked',2,'test-bank','/project',content,{},'2020-01-01T00:00:00Z')
        batch=self.queue._select_batches(self.queue._read(),force_tail=True)[0]
        path=self.root/'retention/worker-state.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'intent':{'batch_id':batch['batch_id'],'bank_id':'test-bank','operation_id':'missing','item_ids':batch['item_ids']}}))
        with patch.object(retention_worker.HindsightClient,'operation_status',side_effect=RuntimeError('HTTP 404 missing')):
            self.assertEqual(retention_worker.run_once(self.config,self.root)['status'],'intent_policy_held')
        self.assertEqual(self.posts,[])
    def test_selected_items_can_be_acknowledged_after_a_new_capture(self):
        self.capture('old','2020-01-01T00:00:00Z')
        selected=self.queue._select_batches(self.queue._read(),force_tail=True)[0]
        self.queue.capture(session_id='old',message_count=2,bank_id='test-bank',project='old',content='后来的新消息',metadata={})
        self.assertIn('item_ids',inspect.signature(self.queue.mark_submitted).parameters)
        self.assertTrue(self.queue.mark_submitted(selected['batch_id'],'op1',item_ids=selected['item_ids']))
        rows=self.queue._read()['items']
        self.assertEqual(rows[0]['operation_id'],'op1')
        self.assertNotIn('operation_id',rows[1])
    def test_stop_persists_raw_messages_before_a_failing_feedback_request(self):
        import retain
        c={**self.config,'scenarioCompletionGateEnabled':False,'assistantSelectiveRetentionShadow':False,'taskHandoffEnabled':False}
        messages=[{'role':'user','content':'必须保留下来的原始消息'}]
        with patch.object(retain,'load_config',return_value=c),patch.object(retain,'read_transcript',return_value=messages),patch.object(retain,'ham_emit'),patch('memory_turn_check.observe_stop'),patch.object(retain,'record_provisional_timeline_events'),patch.object(retain,'record_semantic_lifecycle_events'),patch.object(retain,'snapshot_journal',return_value={'text':''}),patch.object(retain,'archive_tool_journal',return_value={'durable':True}),patch.object(retain,'clear_checkpoint'),patch.object(retain,'submit_answer_feedback',side_effect=RuntimeError('offline')),patch('sys.stdin',io.StringIO(json.dumps({'session_id':'real-user','cwd':'/project','transcript_path':'fixture'}))):
            with self.assertRaises(RuntimeError):retain.main()
        self.assertEqual(len(self.queue._read()['items']),1)
        self.assertIn('必须保留下来的原始消息',self.queue._read()['items'][0]['content'])
    def test_stop_turn_policy_survives_later_continuation_after_cursor(self):
        import retain
        c={**self.config,'scenarioCompletionGateEnabled':False,'assistantSelectiveRetentionShadow':False,
           'taskHandoffEnabled':False,'backgroundRetainWorkerEnabled':True}
        messages=[{'role':'user','content':'这次不要写入记忆，只改一句。'},
                  {'role':'assistant','content':'改好了。'}]
        def stop():
            with patch.object(retain,'load_config',return_value=c),patch.object(retain,'read_transcript',return_value=messages),patch.object(retain,'ham_emit'),patch('memory_turn_check.observe_stop'),patch.object(retain,'record_provisional_timeline_events'),patch.object(retain,'record_semantic_lifecycle_events'),patch.object(retain,'snapshot_journal',return_value={'text':''}),patch.object(retain,'archive_tool_journal',return_value={'durable':True}),patch.object(retain,'clear_checkpoint'),patch.object(retain,'submit_answer_feedback'),patch('sys.stdin',io.StringIO(json.dumps({'session_id':'real-user','cwd':'/project','transcript_path':'fixture'}))):
                retain.main()
        stop()
        messages.extend([{'role':'user','content':'继续，换成更短的。'}, {'role':'assistant','content':'短句。'}])
        stop()
        messages.extend([{'role':'user','content':'新任务：请记住我的新地址是天津。'}])
        stop()
        rows=self.queue.snapshot()['items']
        self.assertEqual([r['write_policy']['knowledge_allowed'] for r in rows],[False,False,True])
        batches=self.queue.ready_batches(force_tail=True,config=c)
        self.assertEqual(len(batches),1)
        self.assertNotIn('换成更短',batches[0]['content'])
    def test_no_write_turn_does_not_enter_semantic_side_ledgers(self):
        import retain
        c={**self.config,'scenarioCompletionGateEnabled':False,'assistantSelectiveRetentionShadow':False,
           'taskHandoffEnabled':False,'backgroundRetainWorkerEnabled':True}
        messages=[{'role':'user','content':'不要修改记忆，也不要把这个要求记为长期规则。'}]
        recorded=[]
        with patch.object(retain,'load_config',return_value=c),patch.object(retain,'read_transcript',return_value=messages),patch.object(retain,'ham_emit'),patch('memory_turn_check.observe_stop'),patch.object(retain,'record_provisional_timeline_events',side_effect=lambda rows,_:recorded.extend(rows)),patch.object(retain,'record_semantic_lifecycle_events',side_effect=lambda rows,_:recorded.extend(rows)),patch.object(retain,'snapshot_journal',return_value={'text':''}),patch.object(retain,'archive_tool_journal',return_value={'durable':True}),patch.object(retain,'clear_checkpoint'),patch.object(retain,'submit_answer_feedback'),patch('sys.stdin',io.StringIO(json.dumps({'session_id':'real-user','cwd':'/project','transcript_path':'fixture'}))):
            retain.main()
        self.assertEqual(recorded,[])

if __name__=='__main__': unittest.main()
