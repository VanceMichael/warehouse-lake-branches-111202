import json
import multiprocessing as mp
import tempfile
import threading
import unittest
from pathlib import Path

from warehouse.branching import (
    BranchError, ConflictError, Repository, StalePreviewError,
    default_schema, parse_ddl, validate_rows,
)


def schema_with(extra=(), dropped=(), changed=None):
    fields = [dict(field) for field in default_schema()['fields']
              if field['name'] not in dropped]
    if changed:
        for field in fields:
            if field['name'] in changed:
                field.update(changed[field['name']])
    fields.extend(dict(field) for field in extra)
    return {'fields': fields}


ROW = {'scene': 's1', 'day': '2026-10-01', 'label': 'forest', 'model_version': 'v1'}
ROW2 = {'scene': 's2', 'day': '2026-10-02', 'label': 'water', 'model_version': 'v1'}
CONFIDENCE = {'name': 'confidence', 'type': 'REAL', 'required': False}


def _cross_process_worker(repo_path, preview_id, queue):
    """子进程入口：独立 Repository 实例上尝试合入同一生产引用。"""
    try:
        Repository(repo_path).merge(preview_id, 'separate-process', [])
        queue.put(('ok', None))
    except StalePreviewError as exc:
        queue.put(('stale', str(exc)))
    except Exception as exc:  # pragma: no cover - 暴露非预期异常
        queue.put(('error', repr(exc)))


class BranchingTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temporary.name) / 'repo')
        self.schema = default_schema()
        self.repo.seed_main(
            self.schema,
            {'day-2026-10-01': [ROW], 'day-2026-10-02': [ROW2]},
            'seed baseline', 'ops')

    def tearDown(self):
        self.temporary.cleanup()

    def branch(self, name):
        return self.repo.create_branch(name, 'ops')['commit']

    def partitions(self, ref='main'):
        return self.repo.materialize(ref)['partitions']

    def resolve_conflicts(self, preview):
        decisions = []
        for conflict in preview['conflicts']:
            if conflict['type'] == 'schema-resurrection':
                decisions.append({'scope': 'schema', 'target': conflict['field'],
                                  'decision': 'keep-main', 'type': conflict['type']})
            else:
                key = conflict.get('partition') or conflict.get('field')
                scope = 'partition' if 'partition' in conflict else 'schema'
                decisions.append({'scope': scope, 'target': key,
                                  'decision': 'keep-main', 'type': conflict['type']})
        return decisions


class BaselineTests(BranchingTestBase):
    def test_branch_references_immutable_snapshot(self):
        base = self.repo.resolve('main')
        tip = self.branch('exp-a')
        self.assertEqual(tip, base)
        # 分支提交后，公共基线提交对象内容不变
        self.repo.commit_partition('exp-a', 'day-2026-10-01', [ROW2], 'try', 'algo')
        self.assertEqual(self.repo.resolve('main'), base)
        self.assertNotEqual(self.repo.resolve('exp-a'), base)
        self.assertEqual(self.partitions('main'), self.partitions(base))

    def test_commit_only_stores_changed_partition(self):
        self.branch('exp-a')
        before = len(list((self.repo.objects.root).glob('*.json')))
        self.repo.commit_partition('exp-a', 'day-2026-10-01', [ROW2], 'try', 'algo')
        commit = self.repo.load_commit(self.repo.resolve('exp-a'))
        self.assertEqual(list(commit['entries']), ['day-2026-10-01'])
        # 仅新增：行对象、提交对象，其他分区对象沿用
        after = len(list((self.repo.objects.root).glob('*.json')))
        self.assertLessEqual(after - before, 2)
        # 物化仍能看到全部两个分区
        self.assertEqual(set(self.partitions('exp-a')), {'day-2026-10-01', 'day-2026-10-02'})

    def test_cannot_commit_directly_to_main(self):
        with self.assertRaises(BranchError):
            self.repo.commit_partition('main', 'day-2026-10-01', [ROW2], 'x', 'algo')
        with self.assertRaises(BranchError):
            self.repo.delete_partition('main', 'day-2026-10-01', 'x', 'algo')

    def test_parse_ddl_and_validate_rows(self):
        parsed = parse_ddl('scene TEXT PRIMARY KEY, day TEXT NOT NULL')
        self.assertEqual(parsed[0], {'name': 'scene', 'type': 'TEXT', 'required': True})
        self.assertEqual(parsed[1]['name'], 'day')
        with self.assertRaises(BranchError):
            validate_rows([{'scene': 1}], self.schema)
        with self.assertRaises(BranchError):
            validate_rows([{**ROW, 'label': None}], self.schema)

    def test_branch_name_validation(self):
        for bad in ('', '../x', 'main', 'a' * 65, 'has space'):
            with self.assertRaises(BranchError):
                self.repo.create_branch(bad, 'ops')


class IndependentPartitionTests(BranchingTestBase):
    def test_disjoint_partitions_merge_without_conflict(self):
        self.branch('exp-a')
        new_rows = [{'scene': 's3', 'day': '2026-10-03', 'label': 'crop', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-03', new_rows, 'new day', 'algo')
        preview = self.repo.preview_merge('exp-a', 'reviewer-1')
        self.assertTrue(preview['clean'])
        self.assertEqual(preview['partitions']['day-2026-10-03']['status'], 'branch-added')
        result = self.repo.merge(preview['id'], 'reviewer-1', [])
        self.assertIn('day-2026-10-03', self.partitions('main'))
        merge_commit = self.repo.load_commit(result['merge_commit'])
        self.assertEqual(merge_commit['kind'], 'merge')
        self.assertEqual(merge_commit['parent2'], preview['branch_tip'])

    def test_identical_change_on_both_sides_is_clean(self):
        self.branch('exp-a')
        rows = [{'scene': 's9', 'day': '2026-10-01', 'label': 'cloud', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-01', rows, 'b', 'algo')
        # 生产通过另一分支先合入同样内容
        self.branch('exp-b')
        self.repo.commit_partition('exp-b', 'day-2026-10-01', rows, 'b', 'algo2')
        preview_b = self.repo.preview_merge('exp-b', 'r')
        self.assertTrue(preview_b['clean'])
        self.repo.merge(preview_b['id'], 'r', [])
        preview_a = self.repo.preview_merge('exp-a', 'r')
        self.assertTrue(preview_a['clean'])
        self.assertEqual(preview_a['partitions']['day-2026-10-01']['status'],
                         'both-changed-identical')


class SamePartitionConflictTests(BranchingTestBase):
    def _divergent_setup(self):
        self.branch('exp-a')
        self.branch('exp-b')
        branch_rows = [{'scene': 's1', 'day': '2026-10-01', 'label': 'wetland',
                        'model_version': 'v2'}]
        main_rows = [{'scene': 's1', 'day': '2026-10-01', 'label': 'forest',
                      'model_version': 'v3'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-01', branch_rows, 'algo cut', 'algo')
        self.repo.commit_partition('exp-b', 'day-2026-10-01', main_rows, 'prod cut', 'data')
        preview_b = self.repo.preview_merge('exp-b', 'r')
        self.repo.merge(preview_b['id'], 'r', [])
        return branch_rows

    def test_structured_conflict_blocks_merge(self):
        self._divergent_setup()
        preview = self.repo.preview_merge('exp-a', 'reviewer-2')
        self.assertFalse(preview['clean'])
        conflict = preview['conflicts'][0]
        self.assertEqual(conflict['type'], 'partition-divergent')
        self.assertEqual(conflict['partition'], 'day-2026-10-01')
        with self.assertRaises(ConflictError):
            self.repo.merge(preview['id'], 'reviewer-2', [])
        # 不允许最后写入者胜：分支新提交不会自动覆盖生产
        self.assertEqual(
            self.partitions('main')['day-2026-10-01']['object'],
            self.partitions('exp-b')['day-2026-10-01']['object'])

    def test_explicit_discard_resolves_and_is_recorded(self):
        branch_rows = self._divergent_setup()
        preview = self.repo.preview_merge('exp-a', 'reviewer-2')
        # 放弃分支侧（keep-main），也演示显式放弃某侧变更
        resolutions = [{'scope': 'partition', 'target': 'day-2026-10-01',
                        'decision': 'keep-main', 'type': 'partition-divergent'}]
        result = self.repo.merge(preview['id'], 'reviewer-2', resolutions)
        self.assertNotEqual(
            self.partitions('main')['day-2026-10-01']['object'],
            self.partitions('exp-a')['day-2026-10-01']['object'])
        # 解决记录对象可追溯
        record = self.repo.objects.read(result['resolution'])
        self.assertEqual(record['decisions'][0]['decision'], 'keep-main')
        self.assertEqual(record['preview'], preview['id'])
        audit_actions = [event['action'] for event in self.repo.read_audit()]
        self.assertIn('merge-preview', audit_actions)
        self.assertIn('merge-commit', audit_actions)
        # 反过来：同一分支重新预览，这次保留分支侧（放弃生产侧修改）
        preview_c = self.repo.preview_merge('exp-a', 'r')
        self.assertTrue(preview_c['conflicts'])
        result_c = self.repo.merge(preview_c['id'], 'r', [{
            'scope': 'partition', 'target': 'day-2026-10-01',
            'decision': 'keep-branch', 'type': 'partition-divergent'}])
        self.assertEqual(
            self.partitions('main')['day-2026-10-01']['object'],
            self.partitions('exp-a')['day-2026-10-01']['object'])
        self.assertTrue(self.repo.objects.read(result_c['resolution']))

    def test_unknown_resolution_target_rejected(self):
        self._divergent_setup()
        preview = self.repo.preview_merge('exp-a', 'r')
        with self.assertRaises(BranchError):
            self.repo.merge(preview['id'], 'r', [{
                'scope': 'partition', 'target': 'no-such-partition',
                'decision': 'keep-main', 'type': 'partition-divergent'}])


class DeleteModifyConflictTests(BranchingTestBase):
    def test_delete_against_modify_conflicts_both_directions(self):
        # 分支删除，生产修改 -> 冲突
        self.branch('exp-del')
        self.branch('exp-mod')
        self.repo.delete_partition('exp-del', 'day-2026-10-01', 'drop', 'algo')
        modified = [{'scene': 's1', 'day': '2026-10-01', 'label': 'wetland',
                     'model_version': 'v9'}]
        self.repo.commit_partition('exp-mod', 'day-2026-10-01', modified, 'fix', 'data')
        preview_mod = self.repo.preview_merge('exp-mod', 'r')
        self.repo.merge(preview_mod['id'], 'r', [])
        preview_del = self.repo.preview_merge('exp-del', 'r')
        self.assertEqual(preview_del['conflicts'][0]['type'], 'partition-delete-modify')
        with self.assertRaises(ConflictError):
            self.repo.merge(preview_del['id'], 'r', [])
        # 显式决定保留删除（放弃生产修改）
        result = self.repo.merge(preview_del['id'], 'r', [{
            'scope': 'partition', 'target': 'day-2026-10-01',
            'decision': 'keep-branch', 'type': 'partition-delete-modify'}])
        self.assertIsNone(self.partitions('main')['day-2026-10-01'])
        self.assertTrue(self.repo.objects.read(result['resolution']))

    def test_modify_against_delete_also_conflicts(self):
        # 分支在生产删除前已存在（共同祖先里分区还在）：生产删除、分支修改 -> 冲突
        self.branch('exp-existing')
        self.branch('del-main')
        self.repo.delete_partition('del-main', 'day-2026-10-01', 'drop', 'data')
        preview = self.repo.preview_merge('del-main', 'r')
        self.repo.merge(preview['id'], 'r', [])
        late = [{'scene': 's1', 'day': '2026-10-01', 'label': 'x', 'model_version': 'v1'}]
        self.repo.commit_partition('exp-existing', 'day-2026-10-01', late, 'late', 'algo')
        preview_late = self.repo.preview_merge('exp-existing', 'r')
        types = {c['type'] for c in preview_late['conflicts']}
        self.assertIn('partition-delete-modify', types)


class SchemaTests(BranchingTestBase):
    def test_branch_cannot_revive_retired_field(self):
        # 实验分支在退役发生前就已存在，其固定基线仍含 model_version
        self.branch('exp-old')
        self.branch('prod-retire')
        retired = schema_with(dropped={'model_version'})
        self.repo.commit_schema('prod-retire', retired, 'retire model_version', 'data')
        preview = self.repo.preview_merge('prod-retire', 'r')
        resolutions = self.resolve_conflicts(preview)
        self.repo.merge(preview['id'], 'r', resolutions)
        self.assertNotIn('model_version',
                         {f['name'] for f in self.repo.materialize('main')['schema']['fields']})

        # exp-old 基于退役前的祖先，仍带 model_version
        revived = schema_with(extra=[CONFIDENCE])
        self.repo.commit_schema('exp-old', revived, 'add confidence', 'algo')
        preview_old = self.repo.preview_merge('exp-old', 'r')
        conflict_types = {c['type'] for c in preview_old['conflicts']}
        self.assertIn('schema-resurrection', conflict_types)
        # 禁止保留分支版本把退役字段带回
        with self.assertRaises(BranchError):
            self.repo.merge(preview_old['id'], 'r', [{
                'scope': 'schema', 'target': 'model_version',
                'decision': 'keep-branch', 'type': 'schema-resurrection'}])
        # 只能 keep-main；同时新增 confidence 与退役字段共存于同一 Schema 提交
        result = self.repo.merge(preview_old['id'], 'r', [{
            'scope': 'schema', 'target': 'model_version',
            'decision': 'keep-main', 'type': 'schema-resurrection'}])
        merged_fields = {f['name'] for f in self.repo.materialize('main')['schema']['fields']}
        self.assertNotIn('model_version', merged_fields)
        self.assertIn('confidence', merged_fields)
        self.assertTrue(self.repo.objects.read(result['resolution']))

    def test_schema_divergent_field_conflict(self):
        self.branch('exp-x')
        self.branch('exp-y')
        # 两侧从同一基线出发，对 label 做出不同修改：x 放宽必填，y 改类型为 INTEGER
        schema_x = schema_with(changed={'label': {'name': 'label', 'type': 'TEXT', 'required': False}})
        schema_y = schema_with(changed={'label': {'name': 'label', 'type': 'INTEGER', 'required': True}})
        self.repo.commit_schema('exp-x', schema_x, 'loosen label', 'algo')
        self.repo.commit_schema('exp-y', schema_y, 'label as integer', 'data')
        preview_y = self.repo.preview_merge('exp-y', 'r')
        self.repo.merge(preview_y['id'], 'r', self.resolve_conflicts(preview_y))
        preview_x = self.repo.preview_merge('exp-x', 'r')
        divergent = [c for c in preview_x['conflicts'] if c['type'] == 'schema-divergent']
        self.assertTrue(any(c['field'] == 'label' for c in divergent))
        # 显式保留生产侧定义即可解决
        result = self.repo.merge(preview_x['id'], 'r', [{
            'scope': 'schema', 'target': 'label',
            'decision': 'keep-main', 'type': 'schema-divergent'}])
        fields = {f['name']: f for f in self.repo.materialize('main')['schema']['fields']}
        self.assertEqual(fields['label']['type'], 'INTEGER')
        self.assertTrue(self.repo.objects.read(result['resolution']))

    def test_data_schema_mismatch_is_joint_conflict(self):
        # 实验分支在退役前创建，之后生产退役 model_version 并改造分区
        self.branch('stale-data')
        self.branch('retire')
        new_rows = [{'scene': 's1', 'day': '2026-10-01', 'label': 'forest'},
                    {'scene': 's2', 'day': '2026-10-02', 'label': 'water'}]
        self.repo.commit_schema('retire', schema_with(dropped={'model_version'}), 'retire', 'data')
        self.repo.commit_partition('retire', 'day-2026-10-01', new_rows[:1], 'fix', 'data')
        self.repo.commit_partition('retire', 'day-2026-10-02', new_rows[1:], 'fix', 'data')
        preview = self.repo.preview_merge('retire', 'r')
        self.repo.merge(preview['id'], 'r', self.resolve_conflicts(preview))

        stale_rows = [{'scene': 's7', 'day': '2026-10-04', 'label': 'crop',
                       'model_version': 'v1'}]
        self.repo.commit_partition('stale-data', 'day-2026-10-04', stale_rows, 'stale', 'algo')
        preview_stale = self.repo.preview_merge('stale-data', 'r')
        self.assertTrue(any(c['type'] == 'data-schema-mismatch'
                            for c in preview_stale['conflicts']))
        with self.assertRaises(BranchError):
            self.repo.merge(preview_stale['id'], 'r', [{
                'scope': 'partition', 'target': 'day-2026-10-04',
                'decision': 'keep-branch', 'type': 'data-schema-mismatch'}])


class ConcurrentMergeTests(BranchingTestBase):
    def test_stale_preview_rejected_after_main_advances(self):
        self.branch('exp-a')
        self.branch('exp-b')
        rows_a = [{'scene': 's3', 'day': '2026-10-03', 'label': 'crop', 'model_version': 'v2'}]
        rows_b = [{'scene': 's4', 'day': '2026-10-04', 'label': 'urban', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-03', rows_a, 'a', 'algo')
        self.repo.commit_partition('exp-b', 'day-2026-10-04', rows_b, 'b', 'algo')
        preview_a = self.repo.preview_merge('exp-a', 'reviewer-1')
        preview_b = self.repo.preview_merge('exp-b', 'reviewer-2')
        # reviewer-1 先合入，生产推进
        self.repo.merge(preview_a['id'], 'reviewer-1', [])
        # reviewer-2 的预览基于旧版本，必须失败而不是覆盖
        with self.assertRaises(StalePreviewError):
            self.repo.merge(preview_b['id'], 'reviewer-2', [])
        fresh = self.repo.preview_merge('exp-b', 'reviewer-2')
        # 共同祖先仍是分支创建时固定的基线，但 main 已指向新合入提交
        self.assertEqual(fresh['main'], self.repo.resolve('main'))
        self.assertEqual(fresh['base'], self.repo.branch_base('exp-b'))
        self.assertTrue(fresh['clean'])
        self.repo.merge(fresh['id'], 'reviewer-2', [])
        self.assertIn('day-2026-10-03', self.partitions('main'))
        self.assertIn('day-2026-10-04', self.partitions('main'))

    def test_parallel_merges_one_wins_other_re_previews(self):
        errors = []

        def reviewer(branch_name, partition, rows, barrier, results, slot):
            try:
                self.repo.create_branch(branch_name, 'ops')
                self.repo.commit_partition(branch_name, partition, rows, 'work', 'algo')
                preview = self.repo.preview_merge(branch_name, f'reviewer-{slot}')
                barrier.wait(timeout=10)
                try:
                    results[slot] = self.repo.merge(preview['id'], f'reviewer-{slot}', [])
                except StalePreviewError as exc:
                    errors.append(str(exc))
                    fresh = self.repo.preview_merge(branch_name, f'reviewer-{slot}')
                    results[slot] = self.repo.merge(fresh['id'], f'reviewer-{slot}', [])
            except BaseException as exc:  # pragma: no cover - 测试失败可见
                errors.append(repr(exc))

        barrier = threading.Barrier(2)
        results = {}
        threads = [
            threading.Thread(target=reviewer, args=(
                'exp-t1', 'day-2026-10-05',
                [{'scene': 's5', 'day': '2026-10-05', 'label': 'sand', 'model_version': 'v2'}],
                barrier, results, 1)),
            threading.Thread(target=reviewer, args=(
                'exp-t2', 'day-2026-10-06',
                [{'scene': 's6', 'day': '2026-10-06', 'label': 'snow', 'model_version': 'v2'}],
                barrier, results, 2)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(set(results), {1, 2})
        self.assertTrue(errors, '应当至少有一次陈旧预览被拒绝')
        self.assertIn('day-2026-10-05', self.partitions('main'))
        self.assertIn('day-2026-10-06', self.partitions('main'))


class GcAndReaderTests(BranchingTestBase):
    def test_delete_branch_keeps_shared_objects_for_main(self):
        self.branch('exp-a')
        new_rows = [{'scene': 's3', 'day': '2026-10-03', 'label': 'crop', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-03', new_rows, 'new', 'algo')
        preview = self.repo.preview_merge('exp-a', 'r')
        self.repo.merge(preview['id'], 'r', [])
        main_partitions = self.partitions('main')

        lease = self.repo.open_reader('downstream')
        # 读者在分支删除前已持有生产快照
        fetched = self.repo.fetch_partition(lease['id'], 'main', 'day-2026-10-03')
        self.assertEqual(fetched['rows'], new_rows)

        deleted = self.repo.delete_branch('exp-a', 'ops')
        self.assertFalse(self.repo.exists('exp-a'))
        gc_result = self.repo.garbage_collect('ops')
        # 生产引用与读者租约引用的对象全部保留
        for partition, entry in main_partitions.items():
            self.assertIn(entry['object'], gc_result['kept'])
        # 读者仍可读取共享对象，没有悬空引用
        again = self.repo.fetch_partition(lease['id'], 'main', 'day-2026-10-03')
        self.assertEqual(again['rows'], new_rows)
        self.repo.close_reader(lease['id'], 'downstream')
        verify = self.repo.verify()
        self.assertEqual(verify['dangling'], 0)
        # 删除只解除引用，可追溯且可恢复
        listed = self.repo.list_deleted_branches()
        self.assertEqual([item['name'] for item in listed], ['exp-a'])
        self.assertEqual(listed[0]['last_commit'], deleted['last_commit'])

    def test_unmerged_branch_kept_by_tombstone_then_collected_after_purge(self):
        self.branch('exp-throwaway')
        throwaway = [{'scene': 's8', 'day': '2026-10-08', 'label': 'cloud',
                      'model_version': 'vX'}]
        self.repo.commit_partition('exp-throwaway', 'day-2026-10-08', throwaway,
                                   'draft', 'algo')
        tip = self.repo.resolve('exp-throwaway')
        self.repo.delete_branch('exp-throwaway', 'ops')
        # 软删除：墓碑仍是存活根，对象保留且可恢复
        soft = self.repo.garbage_collect('ops')
        self.assertNotIn(tip, soft['removed'])
        self.assertTrue(self.repo.objects.has(tip))
        self.assertEqual(self.repo.list_deleted_branches()[0]['last_commit'], tip)
        # 硬清除墓碑后，独占对象才被 GC 回收，且没有悬空引用
        self.repo.purge_branch('exp-throwaway', 'ops')
        hard = self.repo.garbage_collect('ops')
        self.assertIn(tip, hard['removed'])
        self.repo.verify()
        with self.assertRaises(BranchError):
            self.repo.restore_branch('exp-throwaway', 'ops')

    def test_active_reader_blocks_collection(self):
        self.branch('exp-a')
        rows = [{'scene': 's3', 'day': '2026-10-03', 'label': 'crop', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-03', rows, 'new', 'algo')
        # 读者在分支删除前打开，租约固定在分支当前快照
        lease = self.repo.open_reader('downstream', ref='exp-a')
        self.assertEqual(lease['head'], self.repo.resolve('exp-a'))
        self.repo.delete_branch('exp-a', 'ops')
        # 分支未合入，但读者租约指向其快照提交 -> 仍是存活根
        gc_result = self.repo.garbage_collect('ops')
        self.assertIn(lease['head'], gc_result['kept'])
        fetched = self.repo.fetch_partition(lease['id'], lease['head'], 'day-2026-10-03')
        self.assertEqual(fetched['rows'], rows)
        self.repo.close_reader(lease['id'], 'downstream')
        # 读者关闭后墓碑仍保护对象；硬清除墓碑，GC 才回收分支独占快照
        gc_tombstone = self.repo.garbage_collect('ops')
        self.assertIn(lease['head'], gc_tombstone['kept'])
        self.repo.purge_branch('exp-a', 'ops')
        gc_after = self.repo.garbage_collect('ops')
        self.assertIn(lease['head'], gc_after['removed'])

    def test_restore_deleted_branch_and_revert_main(self):
        self.branch('exp-a')
        rows = [{'scene': 's3', 'day': '2026-10-03', 'label': 'crop', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-03', rows, 'new', 'algo')
        tip = self.repo.resolve('exp-a')
        self.repo.delete_branch('exp-a', 'ops')
        restored = self.repo.restore_branch('exp-a', 'ops')
        self.assertEqual(restored['commit'], tip)
        self.assertEqual(self.repo.resolve('exp-a'), tip)

        previous_main = self.repo.resolve('main')
        preview = self.repo.preview_merge('exp-a', 'r')
        merged = self.repo.merge(preview['id'], 'r', [])
        reverted = self.repo.revert_main(previous_main, 'ops', 'rollback demo')
        self.assertEqual(reverted['main'], previous_main)
        self.assertNotIn('day-2026-10-03', self.partitions('main'))
        # 回退不销毁对象，恢复入口仍可前进
        self.repo.revert_main(merged['merge_commit'], 'ops', 'redo')
        self.assertIn('day-2026-10-03', self.partitions('main'))
        self.repo.verify()

    def test_diff_and_log_helpers(self):
        self.branch('exp-a')
        rows = [{'scene': 's3', 'day': '2026-10-03', 'label': 'crop', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-03', rows, 'new', 'algo')
        diff = self.repo.diff_refs('main', 'exp-a')
        self.assertEqual(diff['changes'][0]['change'], 'added')
        log = self.repo.log('exp-a')
        self.assertEqual([entry['kind'] for entry in log][:2], ['change', 'base'])

    def test_cross_process_merge_uses_file_lock_and_version_check(self):
        # 两个独立进程基于同一版本预览合入；文件锁串行化，后合入者必须收到陈旧预览错误
        self.branch('exp-proc')
        rows = [{'scene': 's3', 'day': '2026-10-03', 'label': 'crop', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-proc', 'day-2026-10-03', rows, 'a', 'algo')
        preview = self.repo.preview_merge('exp-proc', 'reviewer-1')
        before_main = self.repo.resolve('main')
        queue = mp.Queue()
        process = mp.Process(target=_cross_process_worker,
                             args=(str(self.repo.root), preview['id'], queue))
        process.start()
        process.join(timeout=30)
        self.assertFalse(process.is_alive())
        status, message = queue.get(timeout=5)
        # 子进程先拿到锁并成功（生产推进），本进程再合入同一预览即陈旧
        if status == 'ok':
            self.assertNotEqual(self.repo.resolve('main'), before_main)
            with self.assertRaises(StalePreviewError):
                self.repo.merge(preview['id'], 'reviewer-1', [])
        else:
            # 子进程输给本进程：本进程成功，子进程拿到陈旧错误
            self.assertEqual(status, 'stale', message)
            self.repo.merge(preview['id'], 'reviewer-1', [])
            self.assertNotEqual(self.repo.resolve('main'), before_main)
        # 无论谁先，最终生产只包含一次该分区变更，未被覆盖
        self.assertEqual(len(self.repo.log('main', limit=2)), 2)

    def test_preview_rejects_advanced_branch_tip(self):
        self.branch('exp-a')
        rows = [{'scene': 's3', 'day': '2026-10-03', 'label': 'crop', 'model_version': 'v2'}]
        self.repo.commit_partition('exp-a', 'day-2026-10-03', rows, 'v1', 'algo')
        preview = self.repo.preview_merge('exp-a', 'r')
        self.repo.commit_partition('exp-a', 'day-2026-10-03',
                                   [{**rows[0], 'label': 'urban'}], 'v2', 'algo')
        with self.assertRaises(StalePreviewError):
            self.repo.merge(preview['id'], 'r', [])


if __name__ == '__main__':
    unittest.main()
