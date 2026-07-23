"""Tests for the data-format transform plugin (transforms/data.py).

All fixtures are built programmatically into ``tmp_path`` -- no committed
sample files. Covers CSV/XLSX/JSON/YAML/HTML conversions, the value-typing and
header policies, error handling, and registry wiring.
"""
import csv
import json

import openpyxl
import pytest
import yaml

import transforms
from transforms import TransformError
from transforms import data as data_transforms


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _write_csv(path, rows, delimiter=','):
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh, delimiter=delimiter)
        for row in rows:
            writer.writerow(row)
    return path


def _read_csv(path):
    with open(path, newline='', encoding='utf-8') as fh:
        return [list(r) for r in csv.reader(fh)]


def _write_json(path, obj):
    path.write_text(json.dumps(obj), encoding='utf-8')
    return path


# --------------------------------------------------------------------------
# csv -> xlsx -> csv round trip preserves the grid
# --------------------------------------------------------------------------
def test_csv_xlsx_csv_roundtrip_preserves_grid(tmp_path):
    grid = [
        ['Name', 'Age', 'Code', 'City'],
        ['Alice', '30', '007', 'NYC'],
        ['Bob', '25', '042', 'Boston'],
        ['Carol', '', '100', 'LA'],
    ]
    src = _write_csv(tmp_path / 'in.csv', grid)
    xlsx = tmp_path / 'mid.xlsx'
    out = tmp_path / 'out.csv'

    assert data_transforms.csv_to_xlsx(str(src), str(xlsx)) == str(xlsx)
    data_transforms.xlsx_to_csv(str(xlsx), str(out))

    assert _read_csv(out) == grid


def test_csv_to_xlsx_types_numbers_and_sheet_name(tmp_path):
    src = _write_csv(tmp_path / 'in.csv', [
        ['n', 'f', 'txt', 'zip'],
        ['30', '1.5', 'hello', '007'],
    ])
    xlsx = tmp_path / 'out.xlsx'
    data_transforms.csv_to_xlsx(str(src), str(xlsx))

    wb = openpyxl.load_workbook(xlsx)
    assert wb.sheetnames == ['Sheet1']
    ws = wb['Sheet1']
    row = list(ws.iter_rows(min_row=2, max_row=2, values_only=True))[0]
    assert row[0] == 30 and isinstance(row[0], int)
    assert row[1] == 1.5 and isinstance(row[1], float)
    assert row[2] == 'hello'
    assert row[3] == '007'  # leading zero preserved as text


# --------------------------------------------------------------------------
# csv -> json: header keys, typing, null, duplicate/empty header suffixing
# --------------------------------------------------------------------------
def test_csv_to_json_typing_and_null(tmp_path):
    src = _write_csv(tmp_path / 'in.csv', [
        ['name', 'age', 'score', 'zip', 'note'],
        ['Alice', '30', '9.5', '007', 'hi'],
        ['Bob', '', '3', '012', ''],
    ])
    out = tmp_path / 'out.json'
    data_transforms.csv_to_json(str(src), str(out))
    records = json.loads(out.read_text(encoding='utf-8'))

    assert records == [
        {'name': 'Alice', 'age': 30, 'score': 9.5, 'zip': '007', 'note': 'hi'},
        {'name': 'Bob', 'age': None, 'score': 3, 'zip': '012', 'note': None},
    ]
    assert isinstance(records[0]['age'], int)
    assert isinstance(records[0]['score'], float)


def test_csv_to_json_duplicate_and_empty_headers(tmp_path):
    # headers: id, id (dup), '' (empty), id (dup again)
    src = _write_csv(tmp_path / 'in.csv', [
        ['id', 'id', '', 'id'],
        ['a', 'b', 'c', 'd'],
    ])
    out = tmp_path / 'out.json'
    data_transforms.csv_to_json(str(src), str(out))
    records = json.loads(out.read_text(encoding='utf-8'))

    assert list(records[0].keys()) == ['id', 'id_2', 'col_3', 'id_3']
    assert records[0] == {'id': 'a', 'id_2': 'b', 'col_3': 'c', 'id_3': 'd'}


# --------------------------------------------------------------------------
# json -> csv: union of keys, scalar array, nested rejection
# --------------------------------------------------------------------------
def test_json_to_csv_union_of_keys(tmp_path):
    src = _write_json(tmp_path / 'in.json', [
        {'a': 1, 'b': 2},
        {'b': 3, 'c': 4},
    ])
    out = tmp_path / 'out.csv'
    data_transforms.json_to_csv(str(src), str(out))

    assert _read_csv(out) == [
        ['a', 'b', 'c'],
        ['1', '2', ''],
        ['', '3', '4'],
    ]


def test_json_to_csv_scalar_array(tmp_path):
    src = _write_json(tmp_path / 'in.json', ['x', 2, 3.5, None])
    out = tmp_path / 'out.csv'
    data_transforms.json_to_csv(str(src), str(out))

    assert _read_csv(out) == [['value'], ['x'], ['2'], ['3.5'], ['']]


def test_json_to_csv_rejects_nested(tmp_path):
    src = _write_json(tmp_path / 'in.json', [{'a': 1, 'b': {'nested': True}}])
    out = tmp_path / 'out.csv'
    with pytest.raises(TransformError):
        data_transforms.json_to_csv(str(src), str(out))


def test_json_to_csv_rejects_non_array(tmp_path):
    src = _write_json(tmp_path / 'in.json', {'a': 1})
    out = tmp_path / 'out.csv'
    with pytest.raises(TransformError):
        data_transforms.json_to_csv(str(src), str(out))


def test_json_to_csv_rejects_mixed_shape(tmp_path):
    src = _write_json(tmp_path / 'in.json', [{'a': 1}, 5])
    out = tmp_path / 'out.csv'
    with pytest.raises(TransformError):
        data_transforms.json_to_csv(str(src), str(out))


# --------------------------------------------------------------------------
# xlsx -> json: multi-sheet
# --------------------------------------------------------------------------
def test_xlsx_to_json_multi_sheet(tmp_path):
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = 'People'
    ws1.append(['name', 'age'])
    ws1.append(['Alice', 30])
    ws1.append(['Bob', 25])
    ws2 = wb.create_sheet('Cities')
    ws2.append(['city', 'pop'])
    ws2.append(['NYC', 8000000])
    src = tmp_path / 'book.xlsx'
    wb.save(src)

    out = tmp_path / 'out.json'
    data_transforms.xlsx_to_json(str(src), str(out))
    result = json.loads(out.read_text(encoding='utf-8'))

    assert set(result.keys()) == {'People', 'Cities'}
    assert result['People'] == [
        {'name': 'Alice', 'age': 30},
        {'name': 'Bob', 'age': 25},
    ]
    assert result['Cities'] == [{'city': 'NYC', 'pop': 8000000}]


# --------------------------------------------------------------------------
# json <-> yaml round trip (nested, unicode) + yml alias
# --------------------------------------------------------------------------
def _nested_doc():
    return {
        'title': 'café ünïcöde',
        'count': 3,
        'items': [{'name': 'a', 'tags': ['x', 'y']}, {'name': 'b', 'tags': []}],
        'meta': {'nested': {'deep': True}, 'ratio': 1.5},
    }


def test_json_yaml_roundtrip_nested_unicode(tmp_path):
    doc = _nested_doc()
    j1 = _write_json(tmp_path / 'a.json', doc)
    y = tmp_path / 'a.yaml'
    j2 = tmp_path / 'b.json'

    data_transforms.json_to_yaml(str(j1), str(y))
    yaml_text = y.read_text(encoding='utf-8')
    assert 'café ünïcöde' in yaml_text  # allow_unicode preserved literal chars

    data_transforms.yaml_to_json(str(y), str(j2))
    assert json.loads(j2.read_text(encoding='utf-8')) == doc


def test_yaml_to_json_via_yml_alias(tmp_path):
    doc = {'k': [1, 2, 3], 'nested': {'v': 'ünï'}}
    src = tmp_path / 'in.yml'
    src.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')
    out = tmp_path / 'out.json'

    data_transforms.yaml_to_json(str(src), str(out))
    assert json.loads(out.read_text(encoding='utf-8')) == doc

    # And the .yml alias is wired into the registry both ways.
    assert transforms.get_transform('.yml', '.json') is not None
    assert transforms.get_transform('.json', '.yml') is not None


# --------------------------------------------------------------------------
# csv -> html: table structure + escaping
# --------------------------------------------------------------------------
def test_csv_to_html_table_and_escaping(tmp_path):
    src = _write_csv(tmp_path / 'in.csv', [
        ['name', 'payload'],
        ['danger', '<script>alert(1)</script>'],
    ])
    out = tmp_path / 'out.html'
    data_transforms.csv_to_html(str(src), str(out))
    html_text = out.read_text(encoding='utf-8')

    assert '<table>' in html_text
    assert '<th>name</th>' in html_text
    # cell markup is escaped, not emitted raw
    assert '<script>alert(1)</script>' not in html_text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html_text


# --------------------------------------------------------------------------
# CSV delimiter sniffing
# --------------------------------------------------------------------------
def test_semicolon_delimited_csv_is_sniffed(tmp_path):
    src = _write_csv(
        tmp_path / 'in.csv',
        [['a', 'b', 'c'], ['1', '2', '3'], ['4', '5', '6']],
        delimiter=';',
    )
    out = tmp_path / 'out.json'
    data_transforms.csv_to_json(str(src), str(out))
    records = json.loads(out.read_text(encoding='utf-8'))

    assert records == [
        {'a': 1, 'b': 2, 'c': 3},
        {'a': 4, 'b': 5, 'c': 6},
    ]


def test_ragged_rows_are_padded(tmp_path):
    # Hand-write a ragged CSV (short + long rows).
    (tmp_path / 'in.csv').write_text('a,b,c\n1,2\n4,5,6,7\n', encoding='utf-8')
    out = tmp_path / 'out.json'
    data_transforms.csv_to_json(str(tmp_path / 'in.csv'), str(out))
    records = json.loads(out.read_text(encoding='utf-8'))

    # 4th column has an empty header -> col_4; short row -> null.
    assert records[0] == {'a': 1, 'b': 2, 'c': None, 'col_4': None}
    assert records[1] == {'a': 4, 'b': 5, 'c': 6, 'col_4': 7}


# --------------------------------------------------------------------------
# Malformed input -> TransformError
# --------------------------------------------------------------------------
def test_malformed_json_raises(tmp_path):
    src = tmp_path / 'bad.json'
    src.write_text('{not valid json,,', encoding='utf-8')
    out = tmp_path / 'out.yaml'
    with pytest.raises(TransformError):
        data_transforms.json_to_yaml(str(src), str(out))


def test_malformed_yaml_raises(tmp_path):
    src = tmp_path / 'bad.yaml'
    # unbalanced/broken flow mapping -> YAMLError
    src.write_text('key: [1, 2\n  bad: : :\n', encoding='utf-8')
    out = tmp_path / 'out.json'
    with pytest.raises(TransformError):
        data_transforms.yaml_to_json(str(src), str(out))


# --------------------------------------------------------------------------
# Registry wiring
# --------------------------------------------------------------------------
def test_registry_get_transform():
    assert transforms.get_transform('.csv', '.xlsx') is not None
    assert transforms.get_transform('.xlsx', '.csv') is not None
    assert transforms.get_transform('.json', '.yaml') is not None


def test_registry_targets_for_csv():
    targets = transforms.targets_for('.csv')
    for ext in ('.xlsx', '.json', '.html'):
        assert ext in targets
