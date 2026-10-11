import io
import json
import unittest

from lib import scenario_model


def source():
    return {'status': 'complete', 'thread_id': 'session', 'source_revision': 'revision',
            'source': 'codex_thread_history', 'source_files': ['source.jsonl'],
            'messages': [{'evidence_id': f'id{i}', 'model_ref': f'm{i}', 'role': 'user',
                          'text': chr(64 + i) * 100, 'turn_id': f'turn{i}', 'at': None}
                         for i in range(1, 10)]}


class ScenarioChunksTest(unittest.TestCase):
    def test_every_message_is_submitted_once_without_clipping(self):
        chunks = scenario_model.source_chunks(source(), max_chars=350)
        self.assertEqual([[m['evidence_id'] for m in c['messages']] for c in chunks],
                         [['id1', 'id2', 'id3'], ['id4', 'id5', 'id6'], ['id7', 'id8', 'id9']])
        self.assertTrue(all(len(m['text']) == 100 for c in chunks for m in c['messages']))

    def test_oversized_message_is_rejected_not_clipped(self):
        with self.assertRaisesRegex(ValueError, 'scenario_source_chunk_too_large'):
            scenario_model.source_chunks(source(), max_chars=99)

    def test_review_stops_with_recorded_rejection_without_retrying_other_chunks(self):
        data = source()
        draft = scenario_model.validate_session_state(data, {'source_revision': 'revision', 'events': [
            {'kind': 'user_goal', 'message_id': 'id1', 'quote': 'A' * 50},
            {'kind': 'user_correction', 'message_id': 'id9', 'quote': 'I' * 50}]}, model='test')
        seen = []
        def opener(request, timeout):
            content = json.loads(request.data)['messages'][0]['content']
            packet = json.loads(content.split('输入：', 1)[1])
            ids = [m['id'] for m in packet['messages']];seen.extend(ids)
            issues = ([{'tier': 'standard', 'code': 'missing_correction', 'detail': 'middle correction omitted'}]
                      if 'id4' in ids else [])
            result = {'source_revision': 'revision', 'accept': not issues, 'issues': issues}
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(result)}}]}).encode())
        review = scenario_model.request_session_review(data, draft, base_url='https://example.invalid',
                                                       api_key='test', model='test', opener=opener,
                                                       max_input_chars=350)
        self.assertEqual(seen, [f'id{i}' for i in range(1, 7)])
        self.assertEqual(review['status'], 'model_review_rejected')
        self.assertEqual(review['draft_sha256'], scenario_model.fingerprint_draft(draft))
        self.assertEqual(review['review_coverage']['source_chunk_count'], 3)
        self.assertEqual(review['review_coverage']['reviewed_chunk_count'], 2)

    def test_recognized_credential_crossing_segment_boundary_is_never_sent(self):
        data = source()
        secret = 'ak-' + 'K' * 40
        data['messages'][0]['text'] = 'A' * 213 + ' ' + secret + '\n请检查工具'
        outbound = []
        def opener(request, timeout):
            packet = json.loads(json.loads(request.data)['messages'][0]['content'].split('输入：', 1)[1])
            outbound.append(packet)
            result = {'source_revision': 'revision', 'events': [
                {'kind': 'user_goal', 'message_id': 'm1', 'quote': '请检查工具'},
                {'kind': 'user_goal', 'message_id': 'm9', 'quote': 'I' * 50}]}
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(result)}}]}).encode())
        scenario_model.request_session_draft(data, base_url='https://example.invalid',
                                             api_key='test', model='test', opener=opener)
        fragments = ''.join(s['text'] for m in outbound[0]['messages'] for s in m['segments'])
        self.assertNotIn(secret, fragments)
        self.assertNotIn('ak-', fragments)

    def test_rejection_is_not_erased_by_a_later_transport_error(self):
        data = source()
        draft = scenario_model.validate_session_state(data, {'source_revision': 'revision', 'events': [
            {'kind': 'user_goal', 'message_id': 'id1', 'quote': 'A' * 50},
            {'kind': 'user_correction', 'message_id': 'id9', 'quote': 'I' * 50}]}, model='test')
        def opener(request, timeout):
            packet = json.loads(json.loads(request.data)['messages'][0]['content'].split('输入：', 1)[1])
            if packet['messages'][0]['id'] != 'id1':
                raise TimeoutError('later chunk failed')
            result = {'source_revision': 'revision', 'accept': False,
                      'issues': [{'tier': 'compact', 'code': 'missing_scope', 'detail': 'source scope omitted'}]}
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(result)}}]}).encode())
        review = scenario_model.request_session_review(data, draft, base_url='https://example.invalid',
                                                       api_key='test', model='test', opener=opener,
                                                       max_input_chars=350)
        self.assertEqual(review['status'], 'model_review_rejected')
        self.assertEqual(review['review_coverage']['reviewed_chunk_count'], 1)

    def test_credentials_are_not_sent_to_summary_model_or_retained_in_quotes(self):
        data = source()
        secret = 'ak-' + 'S' * 40
        data['messages'][0]['text'] = secret + '\n请研究工具说明'
        sent = []
        def opener(request, timeout):
            sent.append(request.data.decode())
            content = json.dumps({'source_revision': 'revision', 'events': [
                {'kind': 'user_goal', 'message_id': 'id1', 'quote': '请研究工具说明'},
                {'kind': 'user_goal', 'message_id': 'id9', 'quote': 'I' * 50}]})
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                                                       'message': {'content': content}}]}).encode())
        draft = scenario_model.request_session_draft(data, base_url='https://example.invalid',
                                                     api_key='test', model='test', opener=opener)
        self.assertNotIn(secret, ''.join(sent))
        self.assertNotIn(secret, json.dumps(draft))
        draft['events'][0]['quote'] = secret
        with self.assertRaisesRegex(ValueError, 'scenario_state_quote_invalid'):
            scenario_model.validate_session_state(data, draft, model='test')

    def test_model_selects_source_segment_ids_without_rewriting_quotes(self):
        data = source()
        packets = []
        def opener(request, timeout):
            packet = json.loads(json.loads(request.data)['messages'][0]['content'].split('输入：', 1)[1].split('\n上次事件结构', 1)[0])
            packets.append(packet)
            response = {'source_revision': 'revision', 'events': [
                {'kind': 'user_goal', 'message_id': 'm1', 'segment_id': 'm1:s1'},
                {'kind': 'user_correction', 'message_id': 'm9', 'segment_id': 'm9:s1'}]}
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(response)}}]}).encode())
        draft = scenario_model.request_session_draft(data, base_url='https://example.invalid',
                                                     api_key='test', model='test', opener=opener)
        self.assertEqual(draft['events'][0]['quote'], 'A' * 100)
        self.assertEqual(draft['events'][-1]['quote'], 'I' * 100)
        self.assertEqual(packets[0]['messages'][0]['segments'][0]['id'], 'm1:s1')

    def test_short_message_preserves_exception_with_its_positive_request(self):
        message = {'model_ref': 'm1', 'text': '回顾全部工作。记忆系统维护不算工作。'}
        segments = scenario_model._source_segments(message)
        self.assertEqual(segments, [{'id': 'm1:s1', 'text': '回顾全部工作。记忆系统维护不算工作。'}])

    def test_long_file_locator_does_not_hide_the_following_correction(self):
        data = source()
        text = '/Users/apple/Projects/' + 'long-directory/' * 7 + 'proposal.docx 现在删除GB指标，其他内容保留。'
        data['messages'][-1]['text'] = text
        result = {'source_revision': 'revision', 'events': [
            {'kind': 'user_goal', 'message_id': 'id1', 'quote': 'A' * 80},
            {'kind': 'user_correction', 'message_id': 'id9', 'quote': text}]}
        draft = scenario_model.validate_session_state(data, result, model='test')
        self.assertEqual(draft['events'][-1]['quote'], text)
        for summary in draft['summaries'].values():
            self.assertIn('现在删除GB指标，其他内容保留', summary)
        self.assertNotIn('long-directory', draft['summaries']['compact'])

    def test_long_request_returns_source_exact_events_and_bounded_coverage(self):
        submitted = []
        def opener(request, timeout):
            prompt = json.loads(request.data)['messages'][0]['content']
            data = json.loads(prompt.split('输入：', 1)[1].split('\n上次事件结构', 1)[0])
            submitted.append(data['messages'])
            events = [{'kind': 'user_goal', 'message_id': m['id'], 'quote': m['text'][:50]}
                      for m in (data['messages'][0], data['messages'][-1])]
            content = json.dumps({'source_revision': 'revision', 'events': events})
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                                                       'message': {'content': content}}]}).encode())
        draft = scenario_model.request_session_draft(source(), base_url='https://example.invalid',
                                                     api_key='test', model='test', opener=opener,
                                                     max_input_chars=350)
        self.assertEqual([e['message_id'] for e in draft['events']], ['id1', 'id9'])
        self.assertEqual(draft['selection_coverage']['source_message_count'], 9)
        self.assertEqual(draft['selection_coverage']['source_chunk_count'], 3)
        self.assertFalse(draft['selection_coverage']['semantic_completeness_proven'])
        self.assertEqual(len(submitted), 4)
        rebuilt = scenario_model.validate_session_draft(source(), draft, model='test')
        self.assertEqual(rebuilt['selection_coverage'], draft['selection_coverage'])
        original_hash = scenario_model.fingerprint_draft(draft)
        draft['selection_coverage']['source_message_count'] = 1
        self.assertNotEqual(original_hash, scenario_model.fingerprint_draft(draft))
        with self.assertRaisesRegex(ValueError, 'scenario_state_invalid'):
            scenario_model.validate_session_draft(source(), draft, model='test')


if __name__ == '__main__':
    unittest.main()
