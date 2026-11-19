"""Narrow YAML text edits; parsing validates locations without reserializing files."""
import json
import re

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, BlockEntryToken, DocumentEndToken

SECRET_FIELDS = (("mysql", "password"), ("ai", "api_key"), ("claude", "api_key"),
                 ("dingtalk", "webhook_url"), ("dingtalk", "secret"), ("web_password",))
KEEP_SECRET = "__KEEP_EXISTING_SECRET__"


class ConfigTextError(ValueError):
    """A safe explanation that contains no input snippets or secret values."""


def _document(text):
    try:
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(text)):
            raise ConfigTextError("网页编辑暂不支持 YAML 锚点或别名；请先在本地私密配置中展开。原文件未修改。")
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        def check(node, depth=0):
            if depth > 64:
                raise ConfigTextError("配置嵌套过深，原文件未修改。")
            if isinstance(node, MappingNode):
                seen = set()
                for key, value in node.value:
                    if not isinstance(key, ScalarNode) or key.tag != 'tag:yaml.org,2002:str':
                        raise ConfigTextError("网页编辑只支持普通字符串配置键，不支持合并键或复杂键。原文件未修改。")
                    if key.value in seen:
                        raise ConfigTextError("配置包含重复字段，原文件未修改。")
                    seen.add(key.value)
                    check(value, depth + 1)
            elif isinstance(node, SequenceNode):
                for value in node.value:
                    check(value, depth + 1)
        if not isinstance(root, MappingNode):
            raise ConfigTextError("配置必须是 YAML 字典，原文件未修改。")
        check(root)
        return root
    except RecursionError:
        raise ConfigTextError("配置嵌套过深，原文件未修改。") from None


def _field(root, keys):
    node = root
    for key in keys:
        if not isinstance(node, MappingNode):
            raise ConfigTextError("配置节必须为字典，原文件未修改。")
        node = next((value for name, value in node.value if name.value == key), None)
        if node is None:
            return None
    return node


def _secret(node):
    if node is None:
        return ''
    if (not isinstance(node, ScalarNode) or node.tag != 'tag:yaml.org,2002:str'
            or node.style in ('|', '>') or node.start_mark.line != node.end_mark.line):
        raise ConfigTextError("网页编辑只支持单行敏感字段；请通过本地私密配置修改此布局。原文件未修改。")
    return node.value


def _replace(text, edits):
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def redact_secrets(text):
    root = _document(text)
    edits = []
    for path in SECRET_FIELDS:
        node = _field(root, path)
        if _secret(node):
            replacement = ("'" + KEEP_SECRET + "'") if node.style == "'" else (
                json.dumps(KEEP_SECRET) if node.style == '"' else KEEP_SECRET)
            edits.append((node.start_mark.index, node.end_mark.index, replacement))
    return _replace(text, edits)


def restore_secrets(text, previous):
    """Preserve submitted comments and restore exact existing secret literals."""
    root, old = _document(text), _document(previous)
    edits = []
    for path in SECRET_FIELDS:
        node, old_node = _field(root, path), _field(old, path)
        value, old_value = _secret(node), _secret(old_node)
        if node is None:
            if old_value:
                raise ConfigTextError("敏感字段占位符不能删除；请保留后重新提交。原文件未修改。")
            continue
        if value not in ('', KEEP_SECRET):
            raise ConfigTextError("敏感字段请保留占位符；请通过本地私密配置或环境变量修改凭据。原文件未修改。")
        if old_value:
            original = previous[old_node.start_mark.index:old_node.end_mark.index]
            edits.append((node.start_mark.index, node.end_mark.index, original))
        elif value == KEEP_SECRET:
            edits.append((node.start_mark.index, node.end_mark.index, '""'))
    restored = _replace(text, edits)
    # Browsers normalize textarea newlines. Keep a consistently CRLF source file
    # in that convention without changing any other submitted text.
    if '\r\n' in previous and '\n' not in previous.replace('\r\n', ''):
        restored = restored.replace('\r\n', '\n').replace('\n', '\r\n')
    return restored


def _line_start(text, index):
    return text.rfind('\n', 0, index) + 1


def _line_end(text, index):
    end = text.find('\n', index)
    return len(text) if end < 0 else end + 1


def replace_settings_fields(text, changes, section=None):
    """Patch selected YAML values without regenerating the rest of the file."""
    if not changes:
        return text
    root = _document(text)
    mapping = root if section is None else _field(root, (section,))
    encode = lambda value: json.dumps(value, ensure_ascii=False)
    newline = '\r\n' if '\r\n' in text else '\n'

    def block(rows, indent):
        return ''.join(f"{indent}{key}: {encode(value)}{newline}" for key, value in rows.items())

    if mapping is None:
        if root.flow_style:
            raise ConfigTextError("请在 YAML 编辑器中先添加常规配置节。")
        end = next((token.start_mark.index for token in yaml.scan(text)
                    if isinstance(token, DocumentEndToken)), len(text))
        prefix = '' if not end or text[end - 1] == '\n' else newline
        return _replace(text, [(end, end, prefix + section + ':' + newline + block(changes, '  '))])
    if not isinstance(mapping, MappingNode):
        raise ConfigTextError("配置节必须为字典。")

    if mapping.flow_style:
        values = yaml.safe_load(text[mapping.start_mark.index:mapping.end_mark.index])
        values.update(changes)
        return _replace(text, [(mapping.start_mark.index, mapping.end_mark.index, encode(values))])

    edits = []
    missing = dict(changes)
    for key, node in mapping.value:
        if key.value not in missing:
            continue
        if isinstance(node, ScalarNode) and node.style in ('|', '>'):
            start, end = node.start_mark.index, node.end_mark.index
            edits.append((start, end, encode(missing.pop(key.value)) + newline))
            continue
        edits.append((node.start_mark.index, node.end_mark.index, encode(missing.pop(key.value))))
    if missing:
        # A block mapping ends at the next peer key or at the end of the document.
        end = _line_start(text, mapping.end_mark.index) if mapping.end_mark.column == 0 else _line_end(text, mapping.end_mark.index)
        indentation = ' ' * mapping.value[0][0].start_mark.column
        prefix = '' if not end or text[end - 1] == '\n' else newline
        edits.append((end, end, prefix + block(missing, indentation)))
    return _replace(text, edits)


def _source_layout(text, root, sequence):
    if root.flow_style or (sequence.flow_style and sequence.value):
        raise ConfigTextError("源管理只支持块式配置和块式源列表；请在本地将非空流式列表展开。原文件未修改。")
    entries = []
    dashes = [token for token in yaml.scan(text) if isinstance(token, BlockEntryToken)
              and sequence.start_mark.index <= token.start_mark.index < sequence.end_mark.index
              and token.start_mark.column == sequence.start_mark.column]
    if len(dashes) != len(sequence.value):
        raise ConfigTextError("源列表布局无法安全局部修改，请通过本地私密配置编辑。原文件未修改。")
    for item, dash in zip(sequence.value, dashes):
        if not isinstance(item, MappingNode) or item.flow_style or {key.value for key, _ in item.value} != {'name', 'url'}:
            raise ConfigTextError("源管理只支持由 name/url 组成的普通块式列表项。原文件未修改。")
        for key, value in item.value:
            if (not isinstance(value, ScalarNode) or value.tag != 'tag:yaml.org,2002:str'
                    or value.start_mark.line != value.end_mark.line or key.start_mark.line != value.start_mark.line):
                raise ConfigTextError("源管理只支持单行 name/url 字段。原文件未修改。")
        start = _line_start(text, dash.start_mark.index)
        end = _line_end(text, max(value.end_mark.index for _, value in item.value))
        entries.append((item, start, end, dash.start_mark.column))
    return entries


def _source_header(text, root):
    key = next((key for key, _ in root.value if key.value == 'rss_sources'), None)
    if key is None:
        return None
    match = re.match(r'[ \t]*:', text[key.end_mark.index:_line_end(text, key.end_mark.index)])
    if match is None or text[_line_start(text, key.start_mark.index):key.start_mark.index].strip():
        raise ConfigTextError("源列表标题布局无法安全局部修改。原文件未修改。")
    return key, key.end_mark.index + match.end()


def edit_source(text, old_name, name, url):
    root = _document(text)
    sequence = _field(root, ("rss_sources",))
    if not isinstance(sequence, SequenceNode):
        raise ConfigTextError("当前配置中没有订阅源列表。")
    entries = _source_layout(text, root, sequence)
    if any(_field(item, ("name",)).value != old_name
           and (_field(item, ("name",)).value == name or _field(item, ("url",)).value == url)
           for item, _, _, _ in entries):
        raise ConfigTextError("源名称或 URL 重复，原文件未修改。")
    for item, _, _, _ in entries:
        fields = {key.value: value for key, value in item.value}
        if fields["name"].value != old_name:
            continue
        return _replace(text, [(fields[key].start_mark.index, fields[key].end_mark.index,
                                json.dumps(value, ensure_ascii=False))
                               for key, value in (("name", name), ("url", url))])
    raise ConfigTextError("订阅源已经被修改或移除，请重新读取。")


def _removed_item_comments(text, item, start, end, indent, newline):
    """Keep standalone groups and turn annotations on removed fields into comments."""
    inline = {}
    for _, value in item.value:
        suffix = text[value.end_mark.index:_line_end(text, value.end_mark.index)]
        comment = suffix.lstrip(' \t')
        if comment.startswith('#'):
            inline[_line_start(text, value.end_mark.index)] = ' ' * indent + comment
    kept, offset = [], start
    for line in text[start:end].splitlines(keepends=True):
        if not line.strip() or line.lstrip().startswith('#'):
            kept.append(line)
        elif offset in inline:
            kept.append(inline[offset])
        offset += len(line)
    result = ''.join(kept)
    return result + newline if result and not result.endswith('\n') else result


def change_source(text, *, add=None, remove_name=None):
    """Change only source-item lines; other values, comments and layout stay intact."""
    root = _document(text)
    if root.flow_style:
        raise ConfigTextError("源管理不支持流式根字典，请在本地展开后重试。原文件未修改。")
    sequence = _field(root, ('rss_sources',))
    if sequence is not None and not isinstance(sequence, SequenceNode):
        raise ConfigTextError("rss_sources 必须为列表，原文件未修改。")
    entries = _source_layout(text, root, sequence) if sequence is not None else []
    header = _source_header(text, root)
    newline = '\r\n' if '\r\n' in text else '\n'
    if remove_name is not None:
        target = next((entry for entry in entries if _field(entry[0], ('name',)).value == remove_name), None)
        if target is None:
            raise ConfigTextError("源不存在，请刷新源列表后重试。原文件未修改。")
        item, start, end, indent = target
        edits = [(start, end, _removed_item_comments(text, item, start, end, indent, newline))]
        if len(entries) == 1:
            edits.append((header[1], header[1], ' []'))
        return _replace(text, edits)
    if not add:
        raise ConfigTextError("缺少源名称或 URL，原文件未修改。")
    name, url = add
    if any(_field(item, ('name',)).value == name or _field(item, ('url',)).value == url
           for item, _, _, _ in entries):
        raise ConfigTextError("源名称或 URL 重复，原文件未修改。")
    indent = entries[0][3] if entries else 2
    block = (' ' * indent + '- name: ' + json.dumps(name, ensure_ascii=False) + newline
             + ' ' * (indent + 2) + 'url: ' + json.dumps(url, ensure_ascii=False) + newline)
    if entries:
        position = entries[-1][2]
        prefix = '' if text[:position].endswith('\n') else newline
        return _replace(text, [(position, position, prefix + block)])
    if sequence is not None:
        if not sequence.flow_style or sequence.start_mark.line != header[0].start_mark.line:
            raise ConfigTextError("空源列表请使用单行 rss_sources: [] 布局后重试。原文件未修改。")
        position = _line_end(text, sequence.end_mark.index)
        prefix = '' if text[:position].endswith('\n') else newline
        return _replace(text, [(sequence.start_mark.index, sequence.end_mark.index, ''),
                               (position, position, prefix + block)])
    position = next((token.start_mark.index for token in yaml.scan(text)
                     if isinstance(token, DocumentEndToken)), len(text))
    prefix = '' if not position or text[:position].endswith('\n') else newline
    return _replace(text, [(position, position, prefix + 'rss_sources:' + newline + block)])
