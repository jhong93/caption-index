#!/usr/bin/env python3

import argparse
import heapq
import math
import pysrt
import os
import shutil
from collections import defaultdict, deque, namedtuple, Counter
from multiprocessing import Pool
from subprocess import check_call
from threading import Lock
from tqdm import tqdm

from util.index import tokenize, Lexicon, Documents, \
    encode_datum, decode_datum, encode_time_int, \
    DATUM_SIZE, TIME_INT_SIZE, MAX_TIME_INT_VALUE


DEFAULT_OUT_DIR = 'out'
DEFAULT_WORKERS = os.cpu_count()


N_WORKERS = None


# Hack to get around sharing args beween processes workers
WORKER_LEXICON = None


def get_args():
    p = argparse.ArgumentParser()
    p.add_argument('doc_dir', type=str, help='Directory containing transcripts')
    p.add_argument('-o', dest='out_dir', type=str, default=DEFAULT_OUT_DIR,
                   help='Output directory. Default: {}'.format(DEFAULT_OUT_DIR))
    p.add_argument('-j', dest='workers', type=int, default=DEFAULT_WORKERS,
                   help='Number of CPU cores to use. Default: {}'.format(DEFAULT_WORKERS))
    p.add_argument('--limit', dest='limit', type=int,
                   help='Number of documents to parse. Default: None')
    return p.parse_args()


def list_subs(dir):
    return [f for f in os.listdir(dir) if f.endswith('.srt')]


def load_srt(doc_path):
    try:
        subs = pysrt.open(doc_path)
    except:
        try:
            subs = pysrt.open(doc_path, encoding='iso-8859-1')
        except:
            raise Exception('Cannot parse {}'.format(doc_path))
    return subs


def get_doc_words(doc_path):
    try:
        subs = load_srt(doc_path)
    except Exception as e:
        print(e)
        return set()

    words = Counter()
    for s in subs:
        tokens = tokenize(s.text)
        words.update(tokens)
    return words


def get_words(doc_dir, docs):
    assert isinstance(doc_dir, str)
    assert isinstance(docs, Documents)

    words = Counter()
    words_lock = Lock()
    with tqdm(total=len(docs)) as pbar, Pool(processes=N_WORKERS) as pool:
        pbar.set_description('Building lexicon')

        def collect(result):
            with words_lock:
                words.update(result)
            pbar.update(1)

        async_results = deque()
        for d in docs:
            doc_path = os.path.join(doc_dir, d)
            async_results.append(
                pool.apply_async(get_doc_words, (doc_path,), callback=collect))

        # Forces exceptions to be rethrown
        for a in async_results:
            a.get()

    print('Lexicon size: {}'.format(len(words)))
    return words


def inv_index_single_doc(doc_path, lexicon):
    assert isinstance(lexicon, Lexicon)

    doc_inv_index = defaultdict(deque)
    try:
        subs = load_srt(doc_path)
        doc_position = 0
        for s in subs:
            start, end = s.start.ordinal, s.end.ordinal
            if start > end:
                print('Warning: start time > end time ({} > {})'.format(start, end))
                end = start
            if end - start > MAX_TIME_INT_VALUE:
                print('Warning: end - start > {}ms'.format(MAX_TIME_INT_VALUE))
                end = start + MAX_TIME_INT_VALUE
            for t in tokenize(s.text):
                try:
                    try:
                        token = lexicon[t]
                    except KeyError:
                        print('Unknown token: {}'.format(t))
                    doc_inv_index[token.id].append((doc_position, start, end))
                finally:
                    doc_position += 1
    except Exception as e:
        print(e)
    return doc_inv_index


def inv_index_batch(doc_batch, out_path):
    lexicon = WORKER_LEXICON

    assert isinstance(out_path, str)
    assert isinstance(lexicon, Lexicon)

    batch_inv_index = defaultdict(deque)     # token -> [(doc, [postings])]
    for doc_id, doc_path in doc_batch:
        doc_index = inv_index_single_doc(doc_path, lexicon)
        for token_id, postings in doc_index.items():
            batch_inv_index[token_id].append((doc_id, postings))

    with open(out_path, 'wb') as f:
        # Order by increasing token_id
        for token_id in sorted(batch_inv_index):
            docs = batch_inv_index[token_id]
            assert len(docs) > 0

            f.write(encode_datum(token_id))    # Token id
            f.write(encode_datum(len(docs)))   # Num of docs in list

            # Order by increasing doc_id
            for doc_id, postings in sorted(docs, key=lambda x: x[0]):
                f.write(encode_datum(doc_id))          # Doc id
                f.write(encode_datum(len(postings)))   # Num postings in doc
                assert len(postings) > 0

                for (position, start, end) in postings:
                    f.write(encode_datum(position))
                    f.write(encode_time_int(start, end))

    return len(doc_batch)


def inv_index_all_docs(doc_dir, docs, lexicon, out_dir, batch_size=100):
    assert isinstance(docs, Documents)
    assert isinstance(lexicon, Lexicon)

    global WORKER_LEXICON
    WORKER_LEXICON = lexicon

    with tqdm(total=len(docs)) as pbar, Pool(processes=N_WORKERS) as pool:
        async_results = deque()
        pbar.set_description('Building inverted index')

        def progress(n_indexed):
            pbar.update(n_indexed)

        for base_id in range(0, len(docs), batch_size):
            doc_batch = [
                (i, os.path.join(doc_dir, docs[i]))
                for i in range(base_id, min(base_id + batch_size, len(docs)))
            ]
            out_path = os.path.join(out_dir, '{}.bin'.format(base_id))

            async_results.append(pool.apply_async(
                inv_index_batch,
                (doc_batch, out_path),
                callback=progress))

        # Forces exceptions to be rethrown
        for a in async_results:
            a.get()


class MergeIndexParser(object):

    ParsedDocument = namedtuple('ParsedDocument', ['id', 'n', 'data'])

    def __init__(self, path, min_token, max_token):
        """Min and max are INCLUSIVE"""
        self._path = path
        self._min_token = min_token
        self._max_token = max_token
        self._f = open(path, 'rb')
        self._done = False
        self._curr_token = None
        self._curr_doc = None
        self._curr_n_docs = None
        self._curr_n_docs_left = 0
        self.next_token()

    @property
    def path(self):
        return self._path

    @property
    def token(self):
        return self._curr_token if not self._done else None

    @property
    def ndocs(self):
        assert not self._done, 'EOF already reached'
        return self._curr_n_docs

    @property
    def doc(self):
        assert not self._done, 'EOF already reached'
        return self._curr_doc

    def close(self):
        self._f.close()
        self._done = True
        self._curr_token = None
        self._curr_n_docs = None
        self._curr_n_docs_left = None

    def next_token(self):
        assert not self._done, 'EOF already reached'
        assert self._curr_n_docs_left == 0, 'Not done processing docs'
        while True:
            token_data = self._f.read(DATUM_SIZE)
            if len(token_data) == 0:
                self.close()
                break
            next_token = decode_datum(token_data)
            if next_token > self._max_token:
                self.close()
                break

            self._curr_token = next_token
            self._curr_n_docs = decode_datum(self._f.read(DATUM_SIZE))
            self._curr_n_docs_left = self._curr_n_docs
            assert self._curr_n_docs > 0, 'Token cannot have no docs'
            if self._curr_token < self._min_token:
                self.skip_docs()
            else:
                assert self.next_doc() is not None
                break
        return self.token

    def next_doc(self):
        assert self._curr_n_docs_left >= 0
        if self._curr_n_docs_left == 0:
            self._curr_doc = None
        else:
            self._curr_n_docs_left -= 1
            doc_id = decode_datum(self._f.read(DATUM_SIZE))
            n_postings = decode_datum(self._f.read(DATUM_SIZE))
            assert n_postings > 0, 'Empty postings list'
            postings_len = n_postings * (DATUM_SIZE + TIME_INT_SIZE)
            postings_data = self._f.read(postings_len)
            assert len(postings_data) == postings_len, 'Invalid read'
            self._curr_doc = MergeIndexParser.ParsedDocument(
                id=doc_id, n=n_postings, data=postings_data)
        return self.doc

    def skip_docs(self):
        assert self._curr_n_docs_left >= 0
        while self._curr_n_docs_left > 0:
            self._f.seek(DATUM_SIZE, 1)
            n_postings = decode_datum(self._f.read(DATUM_SIZE))
            postings_len = n_postings * (DATUM_SIZE + TIME_INT_SIZE)
            assert n_postings > 0, 'Empty postings list'
            self._f.seek(postings_len, 1)
            self._curr_n_docs_left -= 1

    def __lt__(self, o):
        # For priority queuing
        if self.token == o.token:
            return self.doc < o.doc
        return self.token < o.token


def merge_inv_indexes(idx_paths, out_path, min_token, max_token):
    lexicon = WORKER_LEXICON

    token_parsers_pq = []
    for path in idx_paths:
        p = MergeIndexParser(path, min_token, max_token - 1)
        if p.token is not None:
            heapq.heappush(token_parsers_pq, p)

    jump_offsets = [-1] * len(lexicon)
    with open(out_path, 'wb') as f:
        for _ in range(len(lexicon)):
            if len(token_parsers_pq) == 0:
                break
            token_id = token_parsers_pq[0].token
            jump_offsets[token_id] = f.tell()

            doc_parsers_pq = []
            doc_count = 0
            while True:
                if (len(token_parsers_pq) == 0 or
                        token_parsers_pq[0].token != token_id):
                    break
                p = heapq.heappop(token_parsers_pq)
                doc_count += p.ndocs
                heapq.heappush(doc_parsers_pq, p)
            assert len(doc_parsers_pq) >= 1

            f.write(encode_datum(token_id))   # Token id
            f.write(encode_datum(doc_count))  # Num docs

            while len(doc_parsers_pq) > 0:
                p = heapq.heappop(doc_parsers_pq)
                f.write(encode_datum(p.doc.id))      # Doc id
                f.write(encode_datum(p.doc.n))       # Num postings
                f.write(p.doc.data)                  # Raw postings data

                if p.next_doc() is None:
                    if p.next_token() is not None:
                        # Return to tokens queue
                        heapq.heappush(token_parsers_pq, p)
                else:
                    # Return to docs queue
                    heapq.heappush(doc_parsers_pq, p)

    assert len(token_parsers_pq) == 0, 'Uh oh... still have lexicons to merge'
    return jump_offsets


def parallel_merge_inv_indexes(idx_dir, lexicon, out_path, tmp_dir):
    assert isinstance(lexicon, Lexicon)
    global WORKER_LEXICON
    WORKER_LEXICON = lexicon

    if N_WORKERS > 0 and not os.path.exists(tmp_dir):
        os.makedirs(tmp_dir)

    try:
        idx_paths = [
            os.path.join(idx_dir, x)
            for x in os.listdir(idx_dir) if x.endswith('.bin')
        ]

        with tqdm(total=N_WORKERS) as pbar:
            pbar.set_description('Merging indexes')

            def progress(ignored):
                pbar.update(1)

            with Pool(processes=N_WORKERS + 1) as pool:
                tokens_per_worker = math.ceil(len(lexicon) / N_WORKERS)
                new_idx_paths = []
                async_results = deque()

                # Partition across the lexicon
                for file_no, i in enumerate(range(0, len(lexicon),
                                            tokens_per_worker)):
                    new_idx_path = os.path.join(tmp_dir, '{}.bin'.format(file_no))
                    new_idx_paths.append(new_idx_path)
                    async_results.append(pool.apply_async(
                        merge_inv_indexes,
                        (
                            idx_paths, new_idx_path,
                            i, min(i + tokens_per_worker, len(lexicon))
                        ),
                        callback=progress
                    ))

                # Merge the jump offsets
                jump_offsets = [-1] * len(lexicon)
                global_offset = 0
                for i, (new_idx_path, async) in enumerate(
                        zip(new_idx_paths, async_results)):
                    worker_jump_offsets = async.get()
                    min_token = i * tokens_per_worker
                    max_token = min(min_token + tokens_per_worker, len(lexicon))
                    for t in range(min_token, max_token):
                        jump_offsets[t] = global_offset + worker_jump_offsets[t]
                    global_offset += os.path.getsize(new_idx_path)

                # Cat all the files together
                with open(out_path, 'wb') as f:
                    check_call(['cat'] + new_idx_paths, stdout=f)
                pbar.update(1)

            if -1 in jump_offsets:
                print('Warning: not all lexicon words have been indexed')
            return jump_offsets
    finally:
        shutil.rmtree(tmp_dir, True)


def main(doc_dir, out_dir, workers, limit=None):
    global N_WORKERS
    N_WORKERS = workers

    docs = list(sorted(list_subs(doc_dir)))
    if limit is not None:
        docs = docs[:limit]
    docs = Documents(docs)

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    lex_path = os.path.join(out_dir, 'words.lex')
    if os.path.exists(lex_path):
        print('Loading lexicon: {}'.format(lex_path))
        lexicon = Lexicon.load(lex_path)
    else:
        # Save lexicon without offsets initally
        wordcounts = get_words(doc_dir, docs)
        lexicon = Lexicon([
            Lexicon.Word(i, w, wordcounts[w], -1)
            for i, w in enumerate(sorted(wordcounts.keys()))
        ])
        lexicon.store(lex_path)
        del wordcounts

    # Save the document list
    doc_list_path = os.path.join(out_dir, 'docs.list')
    docs.store(doc_list_path)

    chunk_dir = os.path.join(out_dir, 'parts')
    if not os.path.exists(chunk_dir):
        os.makedirs(chunk_dir)
        try:
            inv_index_all_docs(doc_dir, docs, lexicon, chunk_dir)
        except:
            shutil.rmtree(chunk_dir)
            raise
    else:
        print('Found existing indexes: {}'.format(chunk_dir))

    idx_path = os.path.join(out_dir, 'index.bin')
    tmp_dir = os.path.join(out_dir, 'tmp')
    jump_offsets = parallel_merge_inv_indexes(chunk_dir, lexicon, idx_path, tmp_dir)

    # Resave the lexicon with offsets
    lexicon = Lexicon([
        Lexicon.Word(w.id, w.token, w.count, jump_offsets[w.id])
        for w in lexicon
    ])
    lexicon.store(lex_path)


if __name__ == '__main__':
    main(**vars(get_args()))
