'''Record ordinals follow on-disk row positions and no other layout.'''

from pathlib import Path
from typing import Callable

from yaml_data_editor_kit.schema import load_corpus, load_profile

Writer = Callable[[str, str], Path]


def test_rows_carry_zero_based_ordinals_in_file_order(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/products.yaml',
        '''
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
''',
    )
    write(
        'content/products.yaml',
        '''
- { id: bolt }
- { id: nut }
- { id: washer }
''',
    )

    records = load_corpus(load_profile(profile_dir), tmp_path).of_type('product')

    assert [record.ordinal for record in records] == [0, 1, 2]


def test_a_skipped_row_still_consumes_its_on_disk_ordinal(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/products.yaml',
        '''
dialect: type/1
id: product
fields:
  name: { type: string }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
''',
    )
    write(
        'content/products.yaml',
        '''
- { name: Bolt }
- not-a-record
- { name: Washer }
''',
    )

    corpus = load_corpus(load_profile(profile_dir), tmp_path)

    assert [record.ordinal for record in corpus.records] == [0, 2]
    assert len(corpus.diagnostics) == 1
    assert 'row #1 is not a mapping' in corpus.diagnostics[0].message


def test_single_keyed_map_and_file_per_record_have_no_ordinal(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/types.yaml',
        '''
dialect: type/1
id: settings
fields:
  theme: { type: string }
---
dialect: source/1
of: settings
layout: single
path: content/settings.yaml
---
dialect: type/1
id: category
fields:
  title: { type: string }
---
dialect: source/1
of: category
layout: keyed_map
record_keys: [fastener]
path: content/categories.yaml
---
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: product
layout: file_per_record
path: content/products/*.yaml
''',
    )
    write('content/settings.yaml', 'theme: plain\n')
    write('content/categories.yaml', 'fastener: { title: Fastener }\n')
    write('content/products/bolt.yaml', 'id: bolt\n')

    corpus = load_corpus(load_profile(profile_dir), tmp_path)

    assert [record.ordinal for record in corpus.records] == [None, None, None]


def test_value_shaped_records_and_document_metadata_have_no_ordinal(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/rates.yaml',
        '''
dialect: type/1
id: rate_table
fields:
  note: { type: text }
value:
  type: map
  key: { type: enum, values: [standard] }
  value: { type: int }
---
dialect: source/1
of: rate_table
layout: keyed_map
path: content/rates.yaml
record_keys: [basic]
''',
    )
    write('content/rates.yaml', 'note: reviewed\nbasic: { standard: 10 }\n')

    records = load_corpus(load_profile(profile_dir), tmp_path).of_type('rate_table')

    assert [record.identity for record in records] == ['basic', None]
    assert [record.ordinal for record in records] == [None, None]
