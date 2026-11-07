"""Evidence labels must not turn incomplete facts into recovery claims."""
import unittest

from ui.recovery.presentation import (candidate_status, content_check, deletion_evidence,
    export_guidance, export_status, task_status, task_kind, error_text)


class ResultPresentationTests(unittest.TestCase):
    def test_signature_source_cannot_claim_deleted_even_with_inconsistent_metadata(self):
        result = deletion_evidence({'source': 'computer_signature', 'deletion_state': 'deleted_record'})
        self.assertEqual(result.label, '未知（签名候选）')
        self.assertIn('不能判断是否曾被删除', result.explanation)
        self.assertIn('deleted_record', result.tooltip)

    def test_collection_and_backup_do_not_prove_phone_deletion_state(self):
        android = deletion_evidence({'source': 'android_file', 'deletion_state': 'deleted_record'})
        backup = deletion_evidence({'source': 'ios_backup'})
        self.assertEqual(android.label, '普通文件采集')
        self.assertIn('现存普通文件', android.explanation)
        self.assertIn('不能判断', backup.explanation)

    def test_deleted_record_and_layout_checks_are_separate_evidence(self):
        record = deletion_evidence({'source': 'computer_deleted', 'deletion_state': 'deleted_record'})
        layout = content_check('metadata_checked')
        self.assertEqual(record.label, '文件系统删除记录')
        self.assertIn('可能已被覆盖', record.explanation)
        self.assertEqual(layout.label, '布局检查通过')
        self.assertIn('不证明内容', layout.explanation)

    def test_partial_or_unverified_layout_does_not_present_as_full_file(self):
        for integrity in ['layout_unverified', 'allocation_unknown', 'allocated_clusters', 'unsupported_layout',
                          'invalid_record', 'invalid_extent', 'invalid_metadata', 'size_limit',
                          'overwritten_after_delete', 'shared_with_live']:
            with self.subTest(integrity=integrity):
                self.assertIn(content_check(integrity).tone, ('warning', 'danger'))
                self.assertIn(integrity, content_check(integrity).tooltip)
        self.assertIn('不能作为完整文件导出', candidate_status('partial').explanation)

    def test_structure_check_does_not_imply_full_decode_or_deleted_content(self):
        result = content_check('structure_checked')
        self.assertIn('不等于完整解码', result.explanation)
        self.assertIn('曾被删除', result.explanation)

    def test_future_codes_are_visible_as_unknown_with_original_debug_values(self):
        for display in [candidate_status('future_state'), content_check('future_integrity'),
                        task_status('future_task'), task_kind('future_kind'), error_text('FutureError')]:
            self.assertNotIn('future', display.label)
            self.assertIn(display.raw, display.tooltip)
        self.assertEqual(candidate_status('future_state').label, '状态未知')
        self.assertEqual(content_check('future_integrity').label, '未验证的检查结果')

    def test_exported_marker_requires_consistent_valid_digests_for_pass_label(self):
        digest = 'ab' * 32
        good = export_status({'status': 'exported', 'source_sha256': digest, 'target_sha256': digest})
        self.assertEqual(good.label, '摘要校验通过')
        self.assertIn('历史目标不会自动重新校验', good.explanation)
        for source, target in [(None, None), ('short', 'short'), (digest, 'cd' * 32), (digest, None)]:
            with self.subTest(source=source, target=target):
                result = export_status({'status': 'exported', 'source_sha256': source, 'target_sha256': target})
                self.assertEqual(result.label, '摘要记录不足')
                self.assertEqual(result.tone, 'warning')

    def test_unknown_export_status_cannot_be_promoted_by_equal_hashes(self):
        result = export_status({'status': 'new_status', 'source_sha256': 'a' * 64, 'target_sha256': 'a' * 64})
        self.assertEqual(result.label, '导出状态未知')
        self.assertIn('new_status', result.tooltip)

    def test_export_guidance_does_not_fabricate_an_export_history_state(self):
        result = export_guidance()
        self.assertIn('不判断是否已导出', result.explanation)
        self.assertIn('独立批次记录', result.explanation)
        self.assertIn('不表示已删除数据恢复成功', result.explanation)


if __name__ == '__main__':
    unittest.main()
