"""
Build a dummy index and run tests on it.
"""

import math
import os
import sys
import shutil
import tempfile
from subprocess import check_call

import pytest
import captions
import captions.util as util

from lib.common import get_docs_and_lexicon

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + '/../tools')
import scan
import search


TMP_DIR = None
TEST_SUBS_SUBDIR = 'subs'
TEST_INDEX_SUBDIR = 'index'
TEST_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'test.tar.gz')

BUILD_INDEX_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', 'scripts', 'build_index.py')


@pytest.fixture(scope="session", autouse=True)
def dummy_data():
    global TMP_DIR
    TMP_DIR = tempfile.mkdtemp(suffix=None, prefix='caption-index-unittest-',
                               dir=None)

    def build_test_index(tmp_dir):
        subs_dir = os.path.join(tmp_dir, TEST_SUBS_SUBDIR)
        idx_dir = os.path.join(tmp_dir, TEST_INDEX_SUBDIR)

        # Unpack the test data
        os.makedirs(subs_dir)
        check_call(['tar', '-xzf', TEST_DATA_PATH, '-C', subs_dir])

        # Build the index
        check_call([BUILD_INDEX_SCRIPT, '-d', subs_dir, '-o', idx_dir])
        assert os.path.isdir(idx_dir)

    try:
        build_test_index(TMP_DIR)
        yield
    finally:
        shutil.rmtree(TMP_DIR, True)


def test_tokenize():
    text = 'I\'m a string! This is a tokenizer test; just a test. (A simple test)'
    tokens = list(captions.default_tokenizer().tokens(text))
    assert isinstance(tokens[0], str)
    assert tokens == [
        'I', "'", 'm', 'a', 'string', '!', 'This', 'is', 'a', 'tokenizer',
        'test', ';', 'just', 'a', 'test', '.', '(', 'A', 'simple', 'test', ')']


def test_lemmatize():
    lemmatizer = captions.default_lemmatizer()
    assert 'tree' in lemmatizer.lemma('tree')
    assert 'tree' in lemmatizer.lemma('trees')
    assert 'duck' in lemmatizer.lemma('duck')
    assert 'duck' in lemmatizer.lemma('ducks')

    # Force lemmatization in the lexicon
    idx_dir = os.path.join(TMP_DIR, TEST_INDEX_SUBDIR)
    _, lexicon = get_docs_and_lexicon(idx_dir)
    assert lexicon['DUCK'].id in lexicon.similar('DUCKS')


def test_inverted_index():
    idx_dir = os.path.join(TMP_DIR, TEST_INDEX_SUBDIR)
    idx_path = os.path.join(idx_dir, 'index.bin')
    documents, lexicon = get_docs_and_lexicon(idx_dir)

    def test_search_and_contains(tokens, doc_ids=None):
        ids = index.contains(tokens, doc_ids)
        search_ids = set()
        for d in index.search(tokens, doc_ids):
            assert len(d.postings) > 0
            for l in d.postings:
                assert l.len == len(tokens)
                assert abs(l.end - l.start) <= 10.0, 'ngram time too large'
            search_ids.add(d.id)
        assert ids == search_ids

    all_doc_ids = [d.id for d in documents]
    with captions.CaptionIndex(idx_path, lexicon, documents) as index:
        # Unigram search
        test_search_and_contains(['THE'])
        test_search_and_contains(['UNITED'])
        test_search_and_contains(['STATES'])
        test_search_and_contains(['AND'])
        test_search_and_contains(['THE'], all_doc_ids)
        test_search_and_contains(['UNITED'], all_doc_ids)
        test_search_and_contains(['STATES'], all_doc_ids)
        test_search_and_contains(['AND'], all_doc_ids)

        # Bigram search
        test_search_and_contains(['UNITED', 'STATES'])
        test_search_and_contains(['UNITED', 'KINGDOM'])
        test_search_and_contains(['UNITED', 'STATES'], all_doc_ids)
        test_search_and_contains(['UNITED', 'KINGDOM'], all_doc_ids)

        # N-gram search
        test_search_and_contains(['UNITED', 'STATES', 'OF', 'AMERICA'])
        test_search_and_contains(['UNITED', 'STATES', 'OF', 'AMERICA'],
                                 all_doc_ids)

        test_search_and_contains(['THE', 'GREAT', 'WAR'])
        test_search_and_contains(['THE', 'GREAT', 'WAR'], all_doc_ids)


def test_token_data():
    idx_dir = os.path.join(TMP_DIR, TEST_INDEX_SUBDIR)
    documents, lexicon = get_docs_and_lexicon(idx_dir)
    for i in range(len(documents)):
        dh = documents.open(i)
        doc_len = dh.length
        tokens = dh.tokens()
        assert len(tokens) == doc_len, \
            '{} has an inconsistent number of tokens'.format(documents[i].name)
        for t in tokens:
            lexicon.decode(t)


def test_intervals_data():
    idx_dir = os.path.join(TMP_DIR, TEST_INDEX_SUBDIR)
    documents, _ = get_docs_and_lexicon(idx_dir)
    for i in range(len(documents)):
        dh = documents.open(i)

        assert len(dh.lines(0, 0)) == 0
        duration = dh.duration
        lines = dh.lines()
        assert len(lines) > 0, \
            '{} has no intervals'.format(documents[i].name)
        length_from_intervals = 0
        for line in lines:
            length_from_intervals += line.len
        assert math.fabs(lines[-1].end - duration) < 1e-6
        assert length_from_intervals == dh.length, \
            '{} has an inconsistent number of tokens'.format(documents[i].name)


def test_util_window():
    values = [0, 1, 2, 3]
    assert list(util.window(values, 2)) == [(0, 1), (1, 2), (2, 3)]
    assert list(util.window(values, 3)) == [(0, 1, 2), (1, 2, 3)]


def test_frequent_words():
    idx_dir = os.path.join(TMP_DIR, TEST_INDEX_SUBDIR)
    _, lexicon = get_docs_and_lexicon(idx_dir)
    assert len(util.frequent_words(lexicon, 100)) == 1
    assert len(util.frequent_words(lexicon, 0)) == len(lexicon)
    assert len(util.frequent_words(lexicon, 99)) > 0


def test_script_scan():
    idx_dir = os.path.join(TMP_DIR, TEST_INDEX_SUBDIR)
    scan.main(idx_dir, os.cpu_count(), None)


def test_script_search():
    idx_dir = os.path.join(TMP_DIR, TEST_INDEX_SUBDIR)
    search.main(idx_dir, ['GOOD', '&', 'MORNING'], False, 3)
    search.main(idx_dir, ['GOOD', '|', 'MORNING'], False, 3)
    search.main(idx_dir, ['UNITED STATES', '\\', 'DONALD TRUMP'], False, 3)
    search.main(idx_dir, ['[STATES]'], False, 3)
    search.main(idx_dir, ['[FIGHT]', '&', '[STATES]'], False, 3)
