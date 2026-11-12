"""本地文件数据分支端到端演示。

覆盖：独立分区自动合入、同区分歧与显式放弃、Schema 退役/复活冲突、
删除对修改冲突、两个审核者并发合入的版本校验，以及删分支后的 GC、
生产读者共享对象读取、悬空引用校验与分支恢复入口。

全部状态保存在临时目录的本地文件中，不依赖远端 Git。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'examples' / 'branching'
sys.path.insert(0, str(ROOT))
from warehouse.branching import Repository, StalePreviewError  # noqa: E402


def run(*arguments):
    command = [sys.executable, '-P', '-m', 'warehouse', *arguments]
    print('$ ' + ' '.join(command), flush=True)
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env)
    if result.returncode != 0:
        print(result.stderr, flush=True)
        raise RuntimeError(f'命令失败: {command}')
    print(result.stdout.strip(), flush=True)
    return json.loads(result.stdout)


def heading(title):
    print('\n' + '=' * 78)
    print(title)
    print('=' * 78, flush=True)


def main():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        repo = str(base / 'branch-repo')
        work = base / 'work'
        work.mkdir()

        heading('0. 固定公共基线（仅本地文件，内容寻址对象 + Schema）')
        seed = {
            'day-2026-11-01': json.loads((FIXTURES / 'day-2026-11-01.json').read_text()),
            'day-2026-11-02': json.loads((FIXTURES / 'day-2026-11-02.json').read_text()),
        }
        seed_path = work / 'seed.json'
        seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding='utf-8')
        run('init', '--repo', repo, '--seed', str(seed_path),
            '--message', '固定遥感分类索引公共基线', '--actor', 'data-owner')
        run('branch', '--repo', repo)

        heading('1. 独立分区：分支只改不同分区，预览无冲突并自动合入')
        run('branch', '--repo', repo, '--name', 'exp-disjoint', '--actor', 'algo-a')
        run('commit', '--repo', repo, '--branch', 'exp-disjoint',
            '--partition', 'day-2026-11-03',
            '--input', str(FIXTURES / 'day-2026-11-03-new.json'),
            '--message', '新分类口径补一天分区', '--actor', 'algo-a')
        preview = run('preview', '--repo', repo, '--branch', 'exp-disjoint',
                      '--actor', 'reviewer-1')
        assert preview['clean'], preview
        run('merge', '--repo', repo, '--preview', preview['preview'],
            '--actor', 'reviewer-1')
        run('show', '--repo', repo, '--ref', 'main')

        heading('2. 同区分歧：两侧改同一分区，结构化冲突，禁止最后写入者胜')
        run('branch', '--repo', repo, '--name', 'exp-same', '--actor', 'algo-b')
        run('branch', '--repo', repo, '--name', 'prod-cut', '--actor', 'data-owner')
        run('commit', '--repo', repo, '--branch', 'exp-same',
            '--partition', 'day-2026-11-01',
            '--input', str(FIXTURES / 'day-2026-11-01-branch.json'),
            '--message', '算法口径重分类', '--actor', 'algo-b')
        run('commit', '--repo', repo, '--branch', 'prod-cut',
            '--partition', 'day-2026-11-01',
            '--input', str(FIXTURES / 'day-2026-11-01-main.json'),
            '--message', '生产侧人工修订', '--actor', 'data-owner')
        prod_preview = run('preview', '--repo', repo, '--branch', 'prod-cut',
                           '--actor', 'reviewer-1')
        run('merge', '--repo', repo, '--preview', prod_preview['preview'],
            '--actor', 'reviewer-1')
        conflict_preview = run('preview', '--repo', repo, '--branch', 'exp-same',
                               '--actor', 'reviewer-2')
        assert not conflict_preview['clean']
        assert conflict_preview['conflicts'][0]['type'] == 'partition-divergent'
        # 不带解决记录合入必须失败
        blocked = subprocess.run(
            [sys.executable, '-P', '-m', 'warehouse', 'merge', '--repo', repo,
             '--preview', conflict_preview['preview'], '--actor', 'reviewer-2'],
            cwd=ROOT, text=True, capture_output=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT)})
        assert blocked.returncode != 0
        print('$ 无解决记录合入 -> 被拒绝:', blocked.stderr.strip(), flush=True)
        # 显式放弃生产侧，保留分支口径；解决过程落对象与审计
        resolution = work / 'resolution-same.json'
        resolution.write_text(json.dumps([{
            'scope': 'partition', 'target': 'day-2026-11-01',
            'decision': 'keep-branch', 'type': 'partition-divergent'}]), encoding='utf-8')
        run('merge', '--repo', repo, '--preview', conflict_preview['preview'],
            '--resolutions', str(resolution), '--actor', 'reviewer-2')

        heading('3. Schema 联合判断：生产退役字段，分支不能把它带回')
        # 分支必须在退役合入前创建，才能体现共同祖先上的三方比较
        run('branch', '--repo', repo, '--name', 'exp-schema', '--actor', 'algo-c')
        run('branch', '--repo', repo, '--name', 'prod-retire', '--actor', 'data-owner')
        run('schema', '--repo', repo, '--branch', 'prod-retire',
            '--input', str(FIXTURES / 'schema-retired.json'),
            '--message', '退役 model_version 字段', '--actor', 'data-owner')
        retire_preview = run('preview', '--repo', repo, '--branch', 'prod-retire',
                             '--actor', 'reviewer-1')
        run('merge', '--repo', repo, '--preview', retire_preview['preview'],
            '--actor', 'reviewer-1')
        revive_preview = run('preview', '--repo', repo, '--branch', 'exp-schema',
                             '--actor', 'reviewer-2')
        assert any(c['type'] == 'schema-resurrection' for c in revive_preview['conflicts'])
        revive_resolution = work / 'resolution-schema.json'
        revive_resolution.write_text(json.dumps([{
            'scope': 'schema', 'target': 'model_version',
            'decision': 'keep-main', 'type': 'schema-resurrection'}]), encoding='utf-8')
        run('merge', '--repo', repo, '--preview', revive_preview['preview'],
            '--resolutions', str(revive_resolution), '--actor', 'reviewer-2')
        show = run('show', '--repo', repo, '--ref', 'main')
        assert 'model_version' not in {f['name'] for f in show['schema']['fields']}

        heading('4. 删除对修改冲突：一侧删除、另一侧修改，必须显式裁决')
        run('branch', '--repo', repo, '--name', 'exp-delete', '--actor', 'algo-d')
        run('branch', '--repo', repo, '--name', 'exp-modify', '--actor', 'data-owner')
        run('delete-partition', '--repo', repo, '--branch', 'exp-delete',
            '--partition', 'day-2026-11-02', '--message', '撤销该日分区',
            '--actor', 'algo-d')
        # 退役 model_version 后，新分区数据必须符合当前 Schema（联合校验）
        modified_rows = [{'scene': 's3', 'day': '2026-11-02', 'label': 'urban'}]
        modified_path = work / 'day-2026-11-02-modified.json'
        modified_path.write_text(json.dumps(modified_rows), encoding='utf-8')
        run('commit', '--repo', repo, '--branch', 'exp-modify',
            '--partition', 'day-2026-11-02', '--input', str(modified_path),
            '--message', '修订该日分区', '--actor', 'data-owner')
        modify_preview = run('preview', '--repo', repo, '--branch', 'exp-modify',
                             '--actor', 'reviewer-1')
        run('merge', '--repo', repo, '--preview', modify_preview['preview'],
            '--actor', 'reviewer-1')
        delete_preview = run('preview', '--repo', repo, '--branch', 'exp-delete',
                             '--actor', 'reviewer-2')
        assert any(c['type'] == 'partition-delete-modify'
                   for c in delete_preview['conflicts'])
        delete_resolution = work / 'resolution-delete.json'
        delete_resolution.write_text(json.dumps([{
            'scope': 'partition', 'target': 'day-2026-11-02',
            'decision': 'keep-main', 'type': 'partition-delete-modify'}]),
            encoding='utf-8')
        run('merge', '--repo', repo, '--preview', delete_preview['preview'],
            '--resolutions', str(delete_resolution), '--actor', 'reviewer-2')

        heading('5. 并发合入：两个审核者同时提交，陈旧预览被拒绝并要求重新预览')
        api = Repository(repo)
        for branch, partition, rows in (
                ('exp-conc-1', 'day-2026-11-05',
                 [{'scene': 's8', 'day': '2026-11-05', 'label': 'snow'}]),
                ('exp-conc-2', 'day-2026-11-06',
                 [{'scene': 's9', 'day': '2026-11-06', 'label': 'sand'}])):
            api.create_branch(branch, 'algo-e')
            api.commit_partition(branch, partition, rows, '并发实验分支', 'algo-e')
        preview_1 = api.preview_merge('exp-conc-1', 'reviewer-1')
        preview_2 = api.preview_merge('exp-conc-2', 'reviewer-2')
        outcomes = {}
        barrier = threading.Barrier(2)

        def reviewer(preview, name, slot):
            barrier.wait(timeout=10)
            try:
                outcomes[slot] = ('merged', api.merge(preview['id'], name, [])['merge_commit'])
            except StalePreviewError as exc:
                fresh = api.preview_merge(preview['branch'], name)
                outcomes[slot] = ('re-previewed-then-merged',
                                  api.merge(fresh['id'], name, [])['merge_commit'], str(exc))

        threads = [
            threading.Thread(target=reviewer, args=(preview_1, 'reviewer-1', 1)),
            threading.Thread(target=reviewer, args=(preview_2, 'reviewer-2', 2)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()
        print('并发结果:', json.dumps(outcomes, ensure_ascii=False, indent=2), flush=True)
        assert any(value[0] == 're-previewed-then-merged' for value in outcomes.values())
        final_partitions = api.materialize('main')['partitions']
        assert 'day-2026-11-05' in final_partitions
        assert 'day-2026-11-06' in final_partitions

        heading('6. 删分支只解除引用：软删墓碑保护对象，GC 后无悬空引用')
        throwaway_rows = [{'scene': 's10', 'day': '2026-11-09', 'label': 'cloud'}]
        throwaway_path = work / 'throwaway.json'
        throwaway_path.write_text(json.dumps(throwaway_rows), encoding='utf-8')
        run('branch', '--repo', repo, '--name', 'exp-throwaway', '--actor', 'algo-f')
        run('commit', '--repo', repo, '--branch', 'exp-throwaway',
            '--partition', 'day-2026-11-09', '--input', str(throwaway_path),
            '--message', '未合入草稿', '--actor', 'algo-f')
        tip = run('branch', '--repo', repo)
        throwaway_tip = next(b['commit'] for b in tip['branches'] if b['name'] == 'exp-throwaway')
        # 生产读者在删分支前打开，固定 main 快照
        lease = run('reader', '--repo', repo, '--action', 'open',
                    '--ref', 'main', '--actor', 'downstream-bi')
        run('rm-branch', '--repo', repo, '--name', 'exp-throwaway',
            '--actor', 'data-owner')
        gc_soft = run('gc', '--repo', repo, '--actor', 'data-owner')
        # 软删除墓碑仍是存活根：草稿提交被保留，等待恢复
        assert throwaway_tip in gc_soft['kept']
        verify = run('verify', '--repo', repo)
        assert verify['dangling'] == 0
        # 生产读者仍可读取共享对象，确认没有悬空引用
        fetched = run('read', '--repo', repo, '--lease', lease['id'],
                      '--ref', 'main', '--partition', 'day-2026-11-01')
        assert fetched['rows'], '生产读者在 GC 后读不到共享对象'
        run('reader', '--repo', repo, '--action', 'close',
            '--lease', lease['id'], '--actor', 'downstream-bi')

        heading('7. 按分支查询、恢复入口，以及硬清除后的对象回收')
        deleted = run('deleted', '--repo', repo)
        assert deleted['deleted'][0]['recoverable']
        run('restore', '--repo', repo, '--name', 'exp-throwaway',
            '--actor', 'data-owner')
        run('log', '--repo', repo, '--ref', 'exp-throwaway', '--limit', '3')
        run('diff', '--repo', repo, '--left', 'main', '--right', 'exp-throwaway')
        # 确认不再需要该分支后：再次软删并硬清除墓碑，GC 才真正回收其独占对象
        run('rm-branch', '--repo', repo, '--name', 'exp-throwaway',
            '--actor', 'data-owner')
        run('purge-branch', '--repo', repo, '--name', 'exp-throwaway',
            '--actor', 'data-owner')
        gc_hard = run('gc', '--repo', repo, '--actor', 'data-owner')
        assert throwaway_tip in gc_hard['removed']
        run('verify', '--repo', repo)
        run('audit', '--repo', repo)
        print('\n全部演示场景通过：数据状态仅保存在本地文件，未使用远端 Git。', flush=True)


if __name__ == '__main__':
    main()
