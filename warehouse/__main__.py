import argparse
import json
from pathlib import Path

from .branching import (BranchError, Repository, StalePreviewError, default_schema)
from .catalog import connect, load_file
from .domain import overview
from .store import ObjectStore


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def build_parser():
    parser = argparse.ArgumentParser(description='本地数据集合装载、查询与数据分支')
    sub = parser.add_subparsers(dest='command', required=True)

    p_import = sub.add_parser('import', help='向 SQLite 装载输入批次')
    p_import.add_argument('--db', required=True)
    p_import.add_argument('--input', required=True)
    p_import.add_argument('--batch', required=True)

    p_inspect = sub.add_parser('inspect', help='查询标签分布')
    p_inspect.add_argument('--db', required=True)

    p_export = sub.add_parser('export', help='导出内容寻址 JSONL 对象')
    p_export.add_argument('--db', required=True)
    p_export.add_argument('--objects', required=True)

    p_init = sub.add_parser('init', help='初始化分支仓库并固定公共基线')
    p_init.add_argument('--repo', required=True)
    p_init.add_argument('--seed', help='基线分区 JSON 文件；省略则使用空基线')
    p_init.add_argument('--schema', help='结构化 Schema JSON 文件；省略则使用运行时定义')
    p_init.add_argument('--message', default='seed main baseline')
    p_init.add_argument('--actor', default='operator')

    p_branch = sub.add_parser('branch', help='创建或列出数据分支')
    p_branch.add_argument('--repo', required=True)
    p_branch.add_argument('--name')
    p_branch.add_argument('--actor', default='operator')

    p_commit = sub.add_parser('commit', help='在分支上替换/新增分区')
    p_commit.add_argument('--repo', required=True)
    p_commit.add_argument('--branch', required=True)
    p_commit.add_argument('--partition', required=True)
    p_commit.add_argument('--input', required=True)
    p_commit.add_argument('--message', default='replace partition')
    p_commit.add_argument('--actor', default='analyst')

    p_delete_partition = sub.add_parser('delete-partition', help='在分支上删除分区（墓碑）')
    p_delete_partition.add_argument('--repo', required=True)
    p_delete_partition.add_argument('--branch', required=True)
    p_delete_partition.add_argument('--partition', required=True)
    p_delete_partition.add_argument('--message', default='delete partition')
    p_delete_partition.add_argument('--actor', default='analyst')

    p_schema = sub.add_parser('schema', help='在分支上提交结构化 Schema')
    p_schema.add_argument('--repo', required=True)
    p_schema.add_argument('--branch', required=True)
    p_schema.add_argument('--input', required=True)
    p_schema.add_argument('--message', default='change schema')
    p_schema.add_argument('--actor', default='analyst')

    p_preview = sub.add_parser('preview', help='生成三方差异预览')
    p_preview.add_argument('--repo', required=True)
    p_preview.add_argument('--branch', required=True)
    p_preview.add_argument('--actor', default='reviewer')
    p_preview.add_argument('--output', help='把预览 ID 写入该文件')

    p_merge = sub.add_parser('merge', help='按预览合入生产（显式冲突解决）')
    p_merge.add_argument('--repo', required=True)
    p_merge.add_argument('--preview', required=True)
    p_merge.add_argument('--resolutions', help='冲突解决 JSON 文件')
    p_merge.add_argument('--actor', default='reviewer')

    p_log = sub.add_parser('log', help='查看引用的提交链')
    p_log.add_argument('--repo', required=True)
    p_log.add_argument('--ref', default='main')
    p_log.add_argument('--limit', type=int)

    p_diff = sub.add_parser('diff', help='比较两个引用的分区差异')
    p_diff.add_argument('--repo', required=True)
    p_diff.add_argument('--left', required=True)
    p_diff.add_argument('--right', required=True)

    p_show = sub.add_parser('show', help='物化查看引用的分区与 Schema')
    p_show.add_argument('--repo', required=True)
    p_show.add_argument('--ref', default='main')

    p_read = sub.add_parser('read', help='按读者租约读取某个分区')
    p_read.add_argument('--repo', required=True)
    p_read.add_argument('--lease', required=True)
    p_read.add_argument('--ref', default='main')
    p_read.add_argument('--partition', required=True)

    p_reader = sub.add_parser('reader', help='打开/关闭读者租约')
    p_reader.add_argument('--repo', required=True)
    p_reader.add_argument('--action', choices=['open', 'close'], required=True)
    p_reader.add_argument('--lease')
    p_reader.add_argument('--ref', default='main')
    p_reader.add_argument('--actor', default='reader')

    p_gc = sub.add_parser('gc', help='清理不可达对象（生产/分支/读者/预览为根）')
    p_gc.add_argument('--repo', required=True)
    p_gc.add_argument('--actor', default='operator')

    p_verify = sub.add_parser('verify', help='校验无悬空引用')
    p_verify.add_argument('--repo', required=True)

    p_audit = sub.add_parser('audit', help='查看审计日志')
    p_audit.add_argument('--repo', required=True)

    p_rm = sub.add_parser('rm-branch', help='软删除分支引用（写墓碑，对象受保护，可恢复）')
    p_rm.add_argument('--repo', required=True)
    p_rm.add_argument('--name', required=True)
    p_rm.add_argument('--actor', default='operator')

    p_purge = sub.add_parser('purge-branch', help='硬清除墓碑与基线，之后 GC 可回收且不可恢复')
    p_purge.add_argument('--repo', required=True)
    p_purge.add_argument('--name', required=True)
    p_purge.add_argument('--actor', default='operator')

    p_deleted = sub.add_parser('deleted', help='列出已删除分支及最后提交')
    p_deleted.add_argument('--repo', required=True)

    p_restore = sub.add_parser('restore', help='恢复已删除分支')
    p_restore.add_argument('--repo', required=True)
    p_restore.add_argument('--name', required=True)
    p_restore.add_argument('--actor', default='operator')

    p_revert = sub.add_parser('revert', help='回退生产引用到历史提交')
    p_revert.add_argument('--repo', required=True)
    p_revert.add_argument('--commit', required=True)
    p_revert.add_argument('--message', default='revert main')
    p_revert.add_argument('--actor', default='operator')
    return parser


def run_branch_command(args):
    repo = Repository(args.repo)
    command = args.command
    if command == 'init':
        seed = load_json(args.seed) if args.seed else {}
        schema = load_json(args.schema) if args.schema else default_schema()
        commit_id = repo.seed_main(schema, seed, args.message, args.actor)
        return {'main': commit_id, 'partitions': sorted(seed)}
    if command == 'branch':
        if args.name:
            return repo.create_branch(args.name, args.actor)
        return {'branches': repo.list_branches()}
    if command == 'commit':
        return {'commit': repo.commit_partition(
            args.branch, args.partition, load_json(args.input), args.message, args.actor)}
    if command == 'delete-partition':
        return {'commit': repo.delete_partition(
            args.branch, args.partition, args.message, args.actor)}
    if command == 'schema':
        return {'commit': repo.commit_schema(
            args.branch, load_json(args.input), args.message, args.actor)}
    if command == 'preview':
        preview = repo.preview_merge(args.branch, args.actor)
        preview_id = preview['id']
        if args.output:
            Path(args.output).write_text(preview_id, encoding='utf-8')
        return _preview_summary(preview_id, preview)
    if command == 'merge':
        if Path(args.preview).exists() and not _is_hex(args.preview):
            preview_id = Path(args.preview).read_text(encoding='utf-8').strip()
        else:
            preview_id = args.preview
        resolutions = load_json(args.resolutions) if args.resolutions else []
        return repo.merge(preview_id, args.actor, resolutions)
    if command == 'log':
        return {'commits': repo.log(args.ref, args.limit)}
    if command == 'diff':
        return repo.diff_refs(args.left, args.right)
    if command == 'show':
        snapshot = repo.materialize(args.ref)
        return {'ref': args.ref, 'commit': snapshot['commit'], 'schema': snapshot['schema'],
                'partitions': {name: entry for name, entry in snapshot['partitions'].items()}}
    if command == 'reader':
        if args.action == 'open':
            return repo.open_reader(args.actor, args.ref)
        if not args.lease:
            raise BranchError('关闭读者租约需要 --lease')
        repo.close_reader(args.lease, args.actor)
        return {'closed': args.lease}
    if command == 'read':
        return repo.fetch_partition(args.lease, args.ref, args.partition)
    if command == 'gc':
        return repo.garbage_collect(args.actor)
    if command == 'verify':
        return repo.verify()
    if command == 'audit':
        return {'events': repo.read_audit()}
    if command == 'rm-branch':
        return repo.delete_branch(args.name, args.actor)
    if command == 'purge-branch':
        return repo.purge_branch(args.name, args.actor)
    if command == 'deleted':
        return {'deleted': repo.list_deleted_branches()}
    if command == 'restore':
        return repo.restore_branch(args.name, args.actor)
    if command == 'revert':
        return repo.revert_main(args.commit, args.actor, args.message)
    raise BranchError(f'未知命令: {command}')


def _is_hex(value):
    return len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _preview_summary(preview_id, preview):
    return {
        'preview': preview_id,
        'branch': preview['branch'],
        'base': preview['base'],
        'main': preview['main'],
        'branch_tip': preview['branch_tip'],
        'clean': preview['clean'],
        'conflicts': preview['conflicts'],
        'partitions': {name: detail['status'] for name, detail in preview['partitions'].items()},
    }


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'import':
            Path(args.db).parent.mkdir(parents=True, exist_ok=True)
            connection = connect(args.db)
            try:
                emit({'changed': load_file(connection, args.input, args.batch)})
            finally:
                connection.close()
        elif args.command == 'inspect':
            connection = connect(args.db)
            try:
                emit(overview(connection))
            finally:
                connection.close()
        elif args.command == 'export':
            connection = connect(args.db)
            try:
                records = overview(connection)
                emit({'object': ObjectStore(args.objects).put(records), 'rows': len(records)})
            finally:
                connection.close()
        else:
            emit(run_branch_command(args))
    except (BranchError, StalePreviewError) as exc:
        parser.exit(2, f'错误: {exc}\n')


if __name__ == '__main__':
    main()
