from collections import Counter
from multiprocessing import Pool
import os
import sys
from subprocess import check_call
from typing import List, NamedTuple, Optional

from tqdm import tqdm

from captions import BinaryFormat
from captions.indexer import get_document_word_counts

DEFAULT_PARALLELISM = os.cpu_count()

BINARY_FORMAT = BinaryFormat.default()
MAX_WORD_LEN = 20

STDIN_DELIM = '\t'


class DocumentToIndex(NamedTuple):
    name: str
    path: str


def list_docs(doc_dir: str) -> List[DocumentToIndex]:
    return [DocumentToIndex(d, os.path.join(doc_dir, d))
            for d in os.listdir(doc_dir)]


def read_docs_from_stdin() -> List[DocumentToIndex]:
    # Read in list of "name path" pairs from stdin
    result = []
    for line in sys.stdin:
        line = line.strip()
        if line != '':
            name, path = [t.strip() for t in line.split(STDIN_DELIM, 1)]
            result.append(DocumentToIndex(name, path))
    return result


def merge_files(
        paths: List[str], out_path: str,
        batch_size: int = 1000, keep_tmp_files: bool = False
):
    with open(out_path, 'wb') as f:
        for i in range(0, len(paths), batch_size):
            max_idx = min(i + batch_size, len(paths))
            batch_paths = paths[i:max_idx]
            check_call(['cat'] + batch_paths, stdout=f)
            if not keep_tmp_files:
                for p in batch_paths:
                    os.remove(p)


def _get_batch_word_counts(doc_paths: List[str]):
    words = Counter()
    for doc_path in doc_paths:
        get_document_word_counts(
            doc_path, max_word_len=MAX_WORD_LEN, words=words)
    return len(doc_paths), list(words.items())


def get_word_counts(
        docs_to_index: List[DocumentToIndex],
        parallelism: int,
        batch_size: Optional[int] = None    # use batches to reduce
                                            # communication overhead
) -> Counter:
    words = Counter()
    if batch_size is None:
        batch_size = int(len(docs_to_index) / 10 / os.cpu_count())
        batch_size = min(max(batch_size, 1), 1000)
    assert batch_size > 0

    batch_args = []
    for i in range(0, len(docs_to_index), batch_size):
        batch_args.append([d.path for d in docs_to_index[i:i + batch_size]])

    with Pool(processes=parallelism) as pool, \
            tqdm(desc='Building lexicon', total=len(docs_to_index)) as pbar:
        for n, result in pool.imap_unordered(
                _get_batch_word_counts, batch_args
        ):
            for k, v in result:
                words[k] += v
            pbar.update(n)

    print('Lexicon size: {}'.format(len(words)))
    return words
