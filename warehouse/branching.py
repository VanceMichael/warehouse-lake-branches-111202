"""轻量数据分支层。

在内容寻址对象存储之上提供文件索引的分区级数据分支：

- 分支引用不可变快照，提交只保存相对父提交的变更分区与 Schema；
- 三方比较（共同祖先 / 生产 main / 分支）产生结构化冲突，禁止最后写入者胜；
- Schema 与分区数据联合判断，分支不能把生产已退役字段带回；
- 差异预览带版本校验，合入时若生产已推进则拒绝并要求重新预览；
- 删除分支只解除引用，对象清理以生产、其他分支与有效读者为存活根；
- 全部状态保存在本地文件，不依赖远端 Git。
"""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .catalog import encode
from .definition import SCHEMA as DDL_SCHEMA


class BranchError(ValueError):
    """分支层可预期错误。"""


class StalePreviewError(BranchError):
    """预览基于的生产版本与当前生产不一致。"""


class ConflictError(BranchError):
    """存在未解决的结构化冲突。"""


_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_MAIN = 'main'


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_ddl(ddl):
    """解析建表列定义（逗号分隔的 ``name TYPE ...``）为结构化字段列表。"""
    fields = []
    for part in ddl.split(','):
        clause = part.strip()
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z]+)(.*)$', clause, re.DOTALL)
        if not match:
            raise BranchError(f'无法解析字段定义: {clause}')
        name, column_type, rest = match.groups()
        required = 'NOT NULL' in rest.upper() or 'PRIMARY KEY' in rest.upper()
        fields.append({'name': name, 'type': column_type.upper(), 'required': required})
    if not fields:
        raise BranchError('Schema 至少需要一个字段')
    return fields


def default_schema():
    fields = []
    for ddl in DDL_SCHEMA.values():
        fields.extend(parse_ddl(ddl))
    return {'fields': fields}


class JsonStore:
    """内容寻址的 JSON 对象存储，与 JSONL 对象存储共用目录但后缀不同。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, value):
        payload = encode(value)
        digest = hashlib.sha256(payload).hexdigest()
        target = self.root / (digest + '.json')
        if target.exists():
            self.read(digest)
            return digest
        descriptor, temporary = tempfile.mkstemp(dir=self.root, prefix='.writing-')
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read(digest)
            _fsync_dir(self.root)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return digest

    def read(self, digest):
        if not _is_digest(digest):
            raise BranchError('对象标识格式错误')
        target = self.root / (digest + '.json')
        if not target.exists():
            raise BranchError(f'对象缺失: {digest}')
        payload = target.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise BranchError('对象内容校验失败')
        return json.loads(payload)

    def has(self, digest):
        return _is_digest(digest) and (self.root / (digest + '.json')).exists()

    def all_digests(self):
        return {path.name[:-5] for path in self.root.glob('*.json') if _is_digest(path.name[:-5])}


def _is_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _fsync_dir(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.writing-')
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def validate_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise BranchError('分支名仅允许字母数字、点、下划线、短横线且长度不超过 64')
    if name in (_MAIN, '.', '..'):
        raise BranchError(f'分支名 {name} 被保留')


def validate_schema(schema):
    if not isinstance(schema, dict) or not isinstance(schema.get('fields'), list) or not schema['fields']:
        raise BranchError('Schema 需要非空 fields 数组')
    names = []
    for field in schema['fields']:
        if set(field) != {'name', 'type', 'required'}:
            raise BranchError('字段只能包含 name/type/required')
        if not isinstance(field['name'], str) or not field['name']:
            raise BranchError('字段名非法')
        if not isinstance(field['type'], str) or not field['type']:
            raise BranchError('字段类型非法')
        if not isinstance(field['required'], bool):
            raise BranchError('required 必须是布尔值')
        names.append(field['name'])
    if len(names) != len(set(names)):
        raise BranchError('字段名不能重复')
    return {'fields': [dict(field) for field in schema['fields']]}


def validate_rows(rows, schema):
    if not isinstance(rows, list):
        raise BranchError('分区内容必须是记录数组')
    field_names = {field['name'] for field in schema['fields']}
    required = {field['name'] for field in schema['fields'] if field['required']}
    allowed_types = {field['name']: field['type'] for field in schema['fields']}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BranchError(f'第 {index} 行不是对象')
        if set(row) != field_names:
            raise BranchError(f'第 {index} 行字段集合与 Schema 不一致')
        for name in required:
            if row[name] is None:
                raise BranchError(f'第 {index} 行必填字段 {name} 为空')
        for name, column_type in allowed_types.items():
            value = row[name]
            if value is None:
                continue
            if column_type == 'TEXT' and not isinstance(value, str):
                raise BranchError(f'第 {index} 行 {name} 应为文本')
            if column_type == 'INTEGER' and (not isinstance(value, int) or isinstance(value, bool)):
                raise BranchError(f'第 {index} 行 {name} 应为整数')
            if column_type == 'REAL' and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                raise BranchError(f'第 {index} 行 {name} 应为数值')
    return rows


class Repository:
    """本地分区数据分支仓库。"""

    def __init__(self, root):
        self.root = Path(root)
        self.objects = JsonStore(self.root / 'objects')
        self.refs = self.root / 'refs' / 'heads'
        self.bases = self.root / 'refs' / 'bases'
        self.tombstones = self.root / 'refs' / 'tombstones'
        self.readers = self.root / 'refs' / 'readers'
        self.previews = self.root / 'previews'
        self.lock_path = self.root / 'locks' / 'repo.lock'
        self.audit_path = self.root / 'audit.log'
        for path in (self.root, self.refs, self.bases, self.tombstones,
                     self.readers, self.previews, self.lock_path.parent):
            path.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        handle = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    # ---------- 引用与审计 ----------

    def _ref_path(self, name):
        return self.refs / name

    def resolve(self, name):
        path = self._ref_path(name)
        if not path.exists():
            raise BranchError(f'分支不存在: {name}')
        record = json.loads(path.read_text(encoding='utf-8'))
        return record['commit']

    def exists(self, name):
        return self._ref_path(name).exists()

    def list_branches(self):
        result = []
        for path in sorted(self.refs.iterdir()):
            if path.is_file():
                record = json.loads(path.read_text(encoding='utf-8'))
                result.append({'name': path.name, 'commit': record['commit'], 'updated_at': record.get('updated_at')})
        return result

    def _write_ref(self, name, commit, actor):
        if name != _MAIN:
            validate_name(name)
        atomic_write(self._ref_path(name), encode({
            'name': name,
            'commit': commit,
            'updated_at': utc_now(),
            'updated_by': actor,
        }))

    def audit(self, actor, action, details=None, record=None):
        event = {'time': utc_now(), 'actor': actor, 'action': action,
                 'details': details or {}, 'record': record}
        with open(self.audit_path, 'ab') as handle:
            handle.write(encode(event) + b'\n')
            handle.flush()
            os.fsync(handle.fileno())

    def read_audit(self):
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding='utf-8').splitlines() if line]

    # ---------- 提交与物化 ----------

    def _commit(self, parent, schema_obj, entries, message, actor, kind='change',
                base=None, parent2=None, resolutions=None):
        record = {
            'type': 'commit',
            'kind': kind,
            'parent': parent,
            'parent2': parent2,
            'base': base,
            'schema': schema_obj,
            'entries': entries,
            'message': message,
            'actor': actor,
            'time': utc_now(),
            'resolutions': resolutions or [],
        }
        return self.objects.put(record)

    def load_commit(self, commit_id):
        commit = self.objects.read(commit_id)
        if commit.get('type') != 'commit':
            raise BranchError(f'{commit_id} 不是提交对象')
        return commit

    def materialize(self, name_or_commit):
        """沿第一父链物化出完整分区映射与 Schema，None 表示已删除分区。"""
        commit_id = name_or_commit if _is_digest(name_or_commit) else self.resolve(name_or_commit)
        chain = []
        current = commit_id
        seen = set()
        while current is not None:
            if current in seen:
                raise BranchError('提交链出现环')
            seen.add(current)
            commit = self.load_commit(current)
            chain.append(commit)
            current = commit['parent']
        partitions = {}
        schema_obj = None
        for commit in reversed(chain):
            partitions.update(commit['entries'])
            if commit.get('schema'):
                schema_obj = commit['schema']
        if schema_obj is None:
            raise BranchError('提交链缺少 Schema')
        return {
            'commit': commit_id,
            'schema': self.objects.read(schema_obj),
            'schema_object': schema_obj,
            'partitions': partitions,
        }

    def seed_main(self, schema, partitions, message, actor):
        """建立公共基线：一次写入初始 Schema 与全量分区。"""
        with self.locked():
            if self.exists(_MAIN):
                raise BranchError('生产基线已存在，不能重复初始化')
            schema = validate_schema(schema)
            entries = {}
            for partition, rows in sorted(partitions.items()):
                validate_name(partition)
                validate_rows(rows, schema)
                entries[partition] = {'object': self.objects.put(rows), 'rows': len(rows)}
            schema_obj = self.objects.put(schema)
            commit_id = self._commit(None, schema_obj, entries, message, actor, kind='base')
            self._write_ref(_MAIN, commit_id, actor)
            self.audit(actor, 'seed-main', {'partitions': sorted(entries)}, commit_id)
            return commit_id

    def create_branch(self, name, actor):
        with self.locked():
            validate_name(name)
            if self.exists(name):
                raise BranchError(f'分支已存在: {name}')
            base_commit = self.resolve(_MAIN)
            self._write_ref(name, base_commit, actor)
            # 固定公共基线：分支创建时刻的生产快照，之后不随生产推进而改变
            atomic_write(self.bases / name, encode(
                {'branch': name, 'base': base_commit, 'created_at': utc_now(),
                 'created_by': actor}))
            self.audit(actor, 'branch-create', {'branch': name, 'base': base_commit}, base_commit)
            return {'name': name, 'commit': base_commit, 'base': base_commit}

    def branch_base(self, name):
        path = self.bases / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding='utf-8'))['base']

    def commit_partition(self, name, partition, rows, message, actor):
        with self.locked():
            validate_name(name)
            if name == _MAIN:
                raise BranchError('生产分区只能通过合入变更，不允许直接写入')
            snapshot = self.materialize(name)
            validate_rows(rows, snapshot['schema'])
            entry = {'object': self.objects.put(rows), 'rows': len(rows)}
            if snapshot['partitions'].get(partition) == entry:
                raise BranchError('分区内容没有变化')
            commit_id = self._commit(snapshot['commit'], snapshot['schema_object'],
                                     {partition: entry}, message, actor)
            self._write_ref(name, commit_id, actor)
            self.audit(actor, 'partition-commit',
                       {'branch': name, 'partition': partition, 'rows': len(rows)}, commit_id)
            return commit_id

    def delete_partition(self, name, partition, message, actor):
        with self.locked():
            validate_name(name)
            if name == _MAIN:
                raise BranchError('生产分区只能通过合入变更，不允许直接删除')
            snapshot = self.materialize(name)
            if partition not in snapshot['partitions']:
                raise BranchError(f'分区不存在或已删除: {partition}')
            commit_id = self._commit(snapshot['commit'], snapshot['schema_object'],
                                     {partition: None}, message, actor)
            self._write_ref(name, commit_id, actor)
            self.audit(actor, 'partition-delete', {'branch': name, 'partition': partition}, commit_id)
            return commit_id

    def commit_schema(self, name, schema, message, actor):
        with self.locked():
            validate_name(name)
            if name == _MAIN:
                raise BranchError('生产 Schema 只能通过合入变更')
            snapshot = self.materialize(name)
            schema = validate_schema(schema)
            schema_obj = self.objects.put(schema)
            if schema_obj == snapshot['schema_object']:
                raise BranchError('Schema 没有变化')
            commit_id = self._commit(snapshot['commit'], schema_obj, {}, message, actor)
            self._write_ref(name, commit_id, actor)
            self.audit(actor, 'schema-commit', {'branch': name}, commit_id)
            return commit_id

    # ---------- 三方比较 ----------

    @staticmethod
    def _field_map(schema):
        return {field['name']: field for field in schema['fields']}

    def _schema_three_way(self, base_schema, main_schema, branch_schema):
        base, main, branch = (self._field_map(s) for s in (base_schema, main_schema, branch_schema))
        conflicts = []
        for field_name in sorted(set(base) | set(main) | set(branch)):
            b, m, h = base.get(field_name), main.get(field_name), branch.get(field_name)
            if b is None:
                if m is not None and h is not None and m != h:
                    conflicts.append({'type': 'schema-divergent', 'field': field_name,
                                      'detail': '两侧新增同一字段但定义不一致'})
                continue
            if m is None and h is not None:
                conflicts.append({'type': 'schema-resurrection', 'field': field_name,
                                  'detail': '生产已退役字段不能由分支带回'})
                continue
            if h is None and m is not None and m != b:
                conflicts.append({'type': 'schema-divergent', 'field': field_name,
                                  'detail': '分支删除字段而生产修改了该字段'})
                continue
            if m is not None and h is not None and m != b and h != b and m != h:
                conflicts.append({'type': 'schema-divergent', 'field': field_name,
                                  'detail': '两侧对同一字段做出不同修改'})
        return conflicts

    def _partition_status(self, partition, base_entry, main_entry, branch_entry):
        if base_entry == main_entry == branch_entry:
            return 'unchanged', None
        if base_entry == main_entry and branch_entry != base_entry:
            action = 'deleted' if branch_entry is None else ('added' if base_entry is None else 'modified')
            return f'branch-{action}', None
        if base_entry == branch_entry and main_entry != base_entry:
            action = 'deleted' if main_entry is None else ('added' if base_entry is None else 'modified')
            return f'main-{action}', None
        if base_entry is None and main_entry is not None and branch_entry is not None:
            if main_entry == branch_entry:
                return 'both-added-identical', None
            return 'conflict-both-added-different', {
                'type': 'partition-divergent', 'partition': partition,
                'detail': '两侧新增同一分区但内容不一致'}
        if main_entry is None or branch_entry is None:
            return 'conflict-delete-modify', {
                'type': 'partition-delete-modify', 'partition': partition,
                'detail': '一侧删除分区而另一侧修改了分区'}
        if main_entry == branch_entry:
            return 'both-changed-identical', None
        return 'conflict-divergent', {
            'type': 'partition-divergent', 'partition': partition,
            'detail': '两侧对同一分区做出不同修改'}

    def three_way(self, branch_name):
        with self.locked():
            return self._three_way(branch_name)

    def _three_way(self, branch_name):
        if branch_name == _MAIN:
            raise BranchError('不能对生产自身做合入比较')
        main_id = self.resolve(_MAIN)
        branch_id = self.resolve(branch_name)
        main_snap = self.materialize(_MAIN)
        branch_snap = self.materialize(branch_name)
        base_id = self._merge_base(main_id, branch_id, branch_name)
        base_snap = self._materialize_commit(base_id)
        partitions = {}
        conflicts = []
        names = set(base_snap['partitions']) | set(main_snap['partitions']) | set(branch_snap['partitions'])
        for partition in sorted(names):
            b = base_snap['partitions'].get(partition)
            m = main_snap['partitions'].get(partition)
            h = branch_snap['partitions'].get(partition)
            status, conflict = self._partition_status(partition, b, m, h)
            partitions[partition] = {
                'status': status,
                'base': b,
                'main': m,
                'branch': h,
                'conflict': conflict,
            }
            if conflict:
                conflicts.append(conflict)
        schema_conflicts = self._schema_three_way(
            base_snap['schema'], main_snap['schema'], branch_snap['schema'])
        conflicts.extend(schema_conflicts)
        tentative_schema, _ = self._apply_schema_decisions(
            {'schema': {'base': base_snap['schema'],
                        'main': main_snap['schema'],
                        'branch': branch_snap['schema']},
             'conflicts': conflicts}, [], strict=False)
        for partition in sorted(partitions):
            detail = partitions[partition]
            if detail['status'].startswith('main-') or detail['status'] == 'unchanged':
                continue
            entry = detail['branch']
            if entry is None:
                continue
            try:
                validate_rows(self.objects.read(entry['object']), tentative_schema)
            except BranchError as exc:
                conflict = {'type': 'data-schema-mismatch', 'partition': partition,
                            'detail': f'分支分区数据与合并后 Schema 联合校验失败: {exc}'}
                detail['conflict'] = conflict
                conflicts.append(conflict)
        return {
            'kind': 'three-way-diff',
            'branch': branch_name,
            'base': base_id,
            'main': main_id,
            'branch_tip': branch_id,
            'partitions': partitions,
            'schema': {
                'base': base_snap['schema'],
                'main': main_snap['schema'],
                'branch': branch_snap['schema'],
            },
            'conflicts': conflicts,
            'clean': not conflicts,
        }

    def _merge_base(self, main_id, branch_id, branch_name=None):
        if branch_name is not None:
            persisted = self.branch_base(branch_name)
            if persisted is not None:
                return persisted
        main_ancestors = set()
        current = main_id
        while current is not None:
            main_ancestors.add(current)
            current = self.load_commit(current)['parent']
        current = branch_id
        while current is not None:
            if current in main_ancestors:
                return current
            current = self.load_commit(current)['parent']
        raise BranchError('分支与生产没有共同祖先')

    def _materialize_commit(self, commit_id):
        return self.materialize(commit_id)

    # ---------- 预览与合入 ----------

    def preview_merge(self, branch_name, actor):
        with self.locked():
            diff = self._three_way(branch_name)
            preview = dict(diff)
            preview['kind'] = 'merge-preview'
            preview['preview_by'] = actor
            preview['created_at'] = utc_now()
            preview['resolutions'] = []
            # 预览 ID 即预览正文（不含 id 字段）的内容哈希；对象与预览文件字节完全一致
            digest = self.objects.put(preview)
            atomic_write(self.previews / f'{digest}.json', encode(preview))
            self.audit(actor, 'merge-preview',
                       {'branch': branch_name, 'preview': digest,
                        'clean': preview['clean'], 'conflicts': len(preview['conflicts'])}, digest)
            returned = dict(preview)
            returned['id'] = digest
            return returned

    def load_preview(self, preview_id):
        path = self.previews / f'{preview_id}.json'
        if not path.exists():
            raise BranchError(f'预览不存在: {preview_id}')
        preview = json.loads(path.read_text(encoding='utf-8'))
        if self.objects.put(preview) != preview_id:
            raise BranchError('预览内容校验失败')
        preview['id'] = preview_id
        return preview

    @staticmethod
    def _resolution_key(resolution):
        conflict_type = resolution.get('type', '')
        if 'type' in resolution and conflict_type:
            if conflict_type.startswith('schema'):
                return ('schema', resolution.get('field') or resolution.get('target'))
            return ('partition', resolution.get('partition') or resolution.get('target'))
        key = (resolution.get('scope'), resolution.get('target'))
        if key[1] is None:
            raise BranchError('冲突解决记录缺少目标')
        return key

    def merge(self, preview_id, actor, resolutions):
        """按预览合入；resolutions 显式声明每个冲突保留哪一侧。"""
        with self.locked():
            preview = self.load_preview(preview_id)
            branch_name = preview['branch']
            current_main = self.resolve(_MAIN)
            if current_main != preview['main']:
                raise StalePreviewError(
                    f'生产已从 {preview["main"][:12]} 推进到 {current_main[:12]}，请重新预览后合入')
            if not self.exists(branch_name):
                raise BranchError(f'分支已删除: {branch_name}')
            if self.resolve(branch_name) != preview['branch_tip']:
                raise StalePreviewError('分支在预览后又有新提交，请重新预览')
            decisions = self._validate_resolutions(preview, resolutions)
            merged_schema, schema_obj = self._apply_schema_decisions(preview, decisions)
            merged_entries = self._apply_partition_decisions(preview, decisions)
            for partition, entry in merged_entries.items():
                if entry is not None:
                    rows = self.objects.read(entry['object'])
                    try:
                        validate_rows(rows, merged_schema)
                    except BranchError as exc:
                        raise ConflictError(
                            f'分区 {partition} 数据与合并后 Schema 联合校验失败: {exc}') from exc
            main_snap = self.materialize(_MAIN)
            delta = {partition: entry for partition, entry in merged_entries.items()
                     if main_snap['partitions'].get(partition) != entry}
            resolution_record = {
                'type': 'resolution-record',
                'preview': preview_id,
                'branch': branch_name,
                'actor': actor,
                'time': utc_now(),
                'decisions': decisions,
            }
            resolution_obj = self.objects.put(resolution_record)
            merge_commit = self._commit(
                current_main, schema_obj, delta,
                f'merge {branch_name} into main', actor,
                kind='merge', base=preview['base'], parent2=preview['branch_tip'],
                resolutions=[resolution_obj])
            self._write_ref(_MAIN, merge_commit, actor)
            self.audit(actor, 'merge-commit',
                       {'branch': branch_name, 'preview': preview_id,
                        'resolution': resolution_obj, 'decisions': decisions}, merge_commit)
            return {'merge_commit': merge_commit, 'resolution': resolution_obj,
                    'main': merge_commit, 'partitions': sorted(delta)}

    def _validate_resolutions(self, preview, resolutions):
        conflicts = preview['conflicts']
        conflict_keys = {self._resolution_key(conflict): conflict for conflict in conflicts}
        decisions = {}
        for resolution in resolutions or []:
            key = self._resolution_key(resolution)
            if key not in conflict_keys:
                raise BranchError(f'解决记录对应不到冲突: {key}')
            decision = resolution.get('decision')
            if decision not in ('keep-branch', 'keep-main'):
                raise BranchError('decision 只能是 keep-branch 或 keep-main')
            conflict = conflict_keys[key]
            if conflict['type'] == 'schema-resurrection' and decision == 'keep-branch':
                raise BranchError('退役字段不能保留分支版本，只能 keep-main')
            if conflict['type'] == 'data-schema-mismatch':
                raise BranchError('数据与 Schema 不匹配不能靠选择一侧解决，需先修正数据或 Schema')
            decisions[key] = decision
        missing = sorted(str(key) for key in conflict_keys if key not in decisions)
        if missing:
            raise ConflictError(f'仍有未解决冲突: {missing}')
        normalized = []
        for key, decision in sorted(decisions.items()):
            scope, target = key
            normalized.append({'scope': scope, 'target': target, 'decision': decision,
                               'conflict': conflict_keys[key]['type']})
        return normalized

    def _apply_schema_decisions(self, preview, decisions, strict=True):
        base_fields = self._field_map(preview['schema']['base'])
        main_fields = self._field_map(preview['schema']['main'])
        branch_fields = self._field_map(preview['schema']['branch'])
        decision_by_field = {d['target']: d['decision']
                             for d in decisions if d['scope'] == 'schema'}
        conflict_fields = {c.get('field') for c in preview.get('conflicts', [])
                           if c.get('type', '').startswith('schema')}
        ordered_names = []
        for field in preview['schema']['main']['fields'] + preview['schema']['branch']['fields']:
            if field['name'] not in ordered_names:
                ordered_names.append(field['name'])
        merged = {}
        for name in ordered_names:
            b, m, h = base_fields.get(name), main_fields.get(name), branch_fields.get(name)
            decision = decision_by_field.get(name)
            if name in conflict_fields:
                # 真正的 Schema 冲突：合入时必须使用显式决策；预览阶段暂取生产侧
                if strict and decision is None:
                    raise ConflictError(f'Schema 字段 {name} 缺少冲突解决记录')
                chosen = h if decision == 'keep-branch' else m
            elif m == h:
                # 两侧一致（含都不存在）
                chosen = m
            elif b == m:
                # 仅分支侧变更（修改/新增/删除），自动接受分支结果
                chosen = h
            elif b == h:
                # 仅生产侧变更，保留生产结果
                chosen = m
            else:
                # 理论上前面的冲突检测应已覆盖，兜底拦截
                raise ConflictError(f'Schema 字段 {name} 存在未识别冲突')
            if chosen is not None:
                merged[name] = chosen
        schema = {'fields': [merged[name] for name in ordered_names if name in merged]}
        validate_schema(schema)
        return schema, self.objects.put(schema)

    def _apply_partition_decisions(self, preview, decisions):
        decision_by_partition = {d['target']: d['decision']
                                 for d in decisions if d['scope'] == 'partition'}
        result = {}
        for partition, detail in preview['partitions'].items():
            status = detail['status']
            if status == 'unchanged':
                continue
            if status.startswith('branch-') or status in ('both-added-identical', 'both-changed-identical'):
                result[partition] = detail['branch']
            elif status.startswith('main-'):
                result[partition] = detail['main']
            else:
                decision = decision_by_partition.get(partition)
                if decision is None:
                    raise ConflictError(f'分区 {partition} 缺少冲突解决记录')
                result[partition] = detail['branch'] if decision == 'keep-branch' else detail['main']
        return result

    # ---------- 查询、日志与恢复 ----------

    def log(self, name=_MAIN, limit=None):
        commit_id = self.resolve(name)
        events = []
        current = commit_id
        while current is not None and (limit is None or len(events) < limit):
            commit = self.load_commit(current)
            events.append({
                'commit': current,
                'kind': commit['kind'],
                'parent': commit['parent'],
                'parent2': commit.get('parent2'),
                'base': commit.get('base'),
                'message': commit['message'],
                'actor': commit['actor'],
                'time': commit['time'],
                'partitions': sorted(commit['entries']),
                'resolutions': commit.get('resolutions', []),
            })
            current = commit['parent']
        return events

    def diff_refs(self, left, right):
        left_snap = self.materialize(left)
        right_snap = self.materialize(right)
        changes = []
        for partition in sorted(set(left_snap['partitions']) | set(right_snap['partitions'])):
            l_entry = left_snap['partitions'].get(partition)
            r_entry = right_snap['partitions'].get(partition)
            if l_entry == r_entry:
                continue
            if l_entry is None:
                kind = 'added'
            elif r_entry is None:
                kind = 'deleted'
            else:
                kind = 'modified'
            changes.append({'partition': partition, 'change': kind,
                            'left': l_entry, 'right': r_entry})
        return {'left': left, 'right': right, 'schema_changed':
                left_snap['schema_object'] != right_snap['schema_object'], 'changes': changes}

    def delete_branch(self, name, actor):
        """软删除：移除 head 引用但写入墓碑，墓碑仍是 GC 存活根，可恢复。"""
        with self.locked():
            validate_name(name)
            if name == _MAIN:
                raise BranchError('生产引用不能删除')
            commit_id = self.resolve(name)
            os.unlink(self._ref_path(name))
            # 墓碑固定最后提交：删除只解除 head 引用，对象仍受墓碑保护
            atomic_write(self.tombstones / name, encode({
                'name': name, 'last_commit': commit_id,
                'deleted_at': utc_now(), 'deleted_by': actor}))
            _fsync_dir(self.refs)
            self.audit(actor, 'branch-delete',
                       {'branch': name, 'last_commit': commit_id}, commit_id)
            return {'name': name, 'last_commit': commit_id}

    def purge_branch(self, name, actor):
        """硬删除：清除墓碑与基线，之后 GC 可回收该分支独占对象且不可恢复。"""
        with self.locked():
            validate_name(name)
            if self.exists(name):
                raise BranchError(f'分支仍存在，请先删除: {name}')
            tombstone = self.tombstones / name
            if not tombstone.exists():
                raise BranchError(f'找不到分支 {name} 的墓碑')
            record = json.loads(tombstone.read_text(encoding='utf-8'))
            tombstone.unlink()
            (self.bases / name).unlink(missing_ok=True)
            _fsync_dir(self.tombstones)
            self.audit(actor, 'branch-purge',
                       {'branch': name, 'last_commit': record['last_commit']},
                       record['last_commit'])
            return {'name': name, 'purged_commit': record['last_commit']}

    def list_deleted_branches(self):
        result = []
        for path in sorted(self.tombstones.iterdir()):
            if not path.is_file():
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            result.append({'name': record['name'], 'last_commit': record['last_commit'],
                           'deleted_at': record['deleted_at'], 'recoverable': True})
        return result

    def restore_branch(self, name, actor):
        with self.locked():
            if self.exists(name):
                raise BranchError(f'分支仍存在: {name}')
            tombstone = self.tombstones / name
            if not tombstone.exists():
                raise BranchError(f'找不到分支 {name} 的墓碑（可能已被清除）')
            record = json.loads(tombstone.read_text(encoding='utf-8'))
            commit_id = record['last_commit']
            self.load_commit(commit_id)
            self._write_ref(name, commit_id, actor)
            tombstone.unlink()
            _fsync_dir(self.tombstones)
            self.audit(actor, 'branch-restore', {'branch': name, 'commit': commit_id}, commit_id)
            return {'name': name, 'commit': commit_id}

    def revert_main(self, commit_id, actor, message):
        """把生产引用回退到历史提交（保留审计，不抹除对象）。"""
        with self.locked():
            self.load_commit(commit_id)
            previous = self.resolve(_MAIN)
            self._write_ref(_MAIN, commit_id, actor)
            self.audit(actor, 'main-revert',
                       {'from': previous, 'to': commit_id, 'message': message}, commit_id)
            return {'main': commit_id, 'previous': previous}

    # ---------- 读者租约 ----------

    def open_reader(self, actor, ref=_MAIN):
        with self.locked():
            head = self.resolve(ref)
            lease_id = hashlib.sha256(encode(
                {'reader': actor, 'time': utc_now(), 'nonce': os.urandom(16).hex()})).hexdigest()
            lease = {'id': lease_id, 'actor': actor, 'opened_at': utc_now(),
                     'ref': ref, 'head': head}
            atomic_write(self.readers / lease_id, encode(lease))
            self.audit(actor, 'reader-open', {'reader': lease_id, 'ref': ref}, head)
            return lease

    def close_reader(self, lease_id, actor):
        with self.locked():
            path = self.readers / lease_id
            if not path.exists():
                raise BranchError(f'读者租约不存在: {lease_id}')
            os.unlink(path)
            _fsync_dir(self.readers)
            self.audit(actor, 'reader-close', {'reader': lease_id})

    def fetch_partition(self, lease_id, ref_name, partition):
        with self.locked():
            lease_path = self.readers / lease_id
            if not lease_path.exists():
                raise BranchError('读者租约无效或已关闭')
            snapshot = self.materialize(ref_name)
            entry = snapshot['partitions'].get(partition)
            if entry is None:
                raise BranchError(f'分区不存在或已删除: {partition}')
            rows = self.objects.read(entry['object'])
            return {'partition': partition, 'ref': ref_name, 'schema': snapshot['schema'],
                    'object': entry['object'], 'rows': rows}

    def _active_reader_commits(self):
        commits = set()
        if not self.readers.exists():
            return commits
        for path in self.readers.iterdir():
            if path.is_file():
                commits.add(json.loads(path.read_text(encoding='utf-8'))['head'])
        return commits

    # ---------- 存活分析、清理与校验 ----------

    def _reachable(self):
        """收集生产、分支、墓碑、读者租约、预览与解决记录引用的全部对象。"""
        roots = set()
        for branch in self.list_branches():
            roots.add(branch['commit'])
        # 已软删除分支的墓碑仍是存活根，保证恢复入口与读者可见对象不被清理
        for path in self.tombstones.iterdir():
            if path.is_file():
                roots.add(json.loads(path.read_text(encoding='utf-8'))['last_commit'])
        roots.update(self._active_reader_commits())
        for path in self.previews.glob('*.json'):
            roots.add(path.name[:-5])
        seen = set()
        stack = list(roots)
        while stack:
            digest = stack.pop()
            if digest in seen or not self.objects.has(digest):
                continue
            seen.add(digest)
            value = self.objects.read(digest)
            if isinstance(value, dict):
                if value.get('type') == 'commit':
                    for key in ('parent', 'parent2', 'base', 'schema'):
                        if value.get(key):
                            stack.append(value[key])
                    for entry in value.get('entries', {}).values():
                        if entry is not None:
                            stack.append(entry['object'])
                    stack.extend(value.get('resolutions', []))
                elif value.get('type') == 'resolution-record':
                    stack.append(value['preview'])
                elif value.get('kind') == 'merge-preview':
                    stack.extend([value['base'], value['main'], value['branch_tip']])
                    stack.extend(conflict_obj_refs(value))
        return seen, roots

    def garbage_collect(self, actor):
        with self.locked():
            reachable, roots = self._reachable()
            before = self.objects.all_digests()
            unreachable = before - reachable
            for digest in unreachable:
                (self.objects.root / f'{digest}.json').unlink()
            if unreachable:
                _fsync_dir(self.objects.root)
            self.audit(actor, 'gc', {'removed': sorted(unreachable),
                                     'roots': sorted(roots), 'kept': sorted(reachable)})
            return {'removed': sorted(unreachable), 'kept': sorted(reachable),
                    'roots': sorted(roots)}

    def verify(self):
        """校验所有引用可达且对象完整，返回统计；发现悬空引用即报错。"""
        reachable, roots = self._reachable()
        missing = sorted(digest for digest in reachable if not self.objects.has(digest))
        if missing:
            raise BranchError(f'存在悬空引用，对象缺失: {missing}')
        for digest in reachable:
            self.objects.read(digest)
        dangling_refs = []
        for branch in self.list_branches():
            if not self.objects.has(branch['commit']):
                dangling_refs.append({'ref': f'heads/{branch["name"]}', 'missing': branch['commit']})
        for lease_commit in self._active_reader_commits():
            if not self.objects.has(lease_commit):
                dangling_refs.append({'ref': 'reader', 'missing': lease_commit})
        if dangling_refs:
            raise BranchError(f'悬空引用: {dangling_refs}')
        return {'reachable_objects': len(reachable), 'roots': len(roots),
                'branches': len(self.list_branches()),
                'active_readers': len(list(self.readers.iterdir())) if self.readers.exists() else 0,
                'dangling': 0}

    def list_readers(self):
        if not self.readers.exists():
            return []
        return [json.loads(path.read_text(encoding='utf-8'))
                for path in sorted(self.readers.iterdir()) if path.is_file()]


def conflict_obj_refs(preview):
    refs = []
    for detail in preview.get('partitions', {}).values():
        for key in ('base', 'main', 'branch'):
            entry = detail.get(key)
            if entry is not None:
                refs.append(entry['object'])
    return refs
