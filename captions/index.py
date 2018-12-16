"""
Indexes for srt files
"""

import mmap
import msgpack
import spacy
import types
from abc import ABC, abstractmethod, abstractproperty
from collections import namedtuple, deque
from typing import Dict, Iterable, List, Set, Tuple

from .rs_captions import RsCaptionIndex, RsMetadataIndex


MODEL = 'en'
_TOKENIZER = spacy.load(MODEL, disable=['tagger', 'parser', 'ner'])


def tokenize(text):
    return (t.text for t in _TOKENIZER(text))


class Lexicon(object):
    """A map from word to id, and vice versa"""

    UNKNOWN_TOKEN = '<UNKNOWN>'

    Word = namedtuple(
        'Word', [
            'id',       # Token id
            'token',    # String representations
            'count',    # Number of occurrences
        ])

    def __init__(self, words):
        """List of words, where w.id is the index in the list"""
        assert isinstance(words, list)
        self._words = words
        self._inverse = {}
        for i, w in enumerate(words):
            assert w.id == i
            self._inverse[w.token] = w

    def __iter__(self):
        # Iterate lexicon in id order
        return self._words.__iter__()

    def __getitem__(self, key):
        if isinstance(key, int):
            # Get word by id (IndexError)
            return self._words[key]
        elif isinstance(key, str):
            # Get word by token (KeyError)
            return self._inverse[key]
        raise TypeError('Not supported for {}'.format(type(key)))

    def __contains__(self, key):
        try:
            self.__getitem__(key)
        except (KeyError, IndexError):
            return False
        return True

    def __len__(self):
        return len(self._words)

    def decode(self, key):
        try:
            return self.__getitem__(key).token
        except:
            return Lexicon.UNKNOWN_TOKEN

    def store(self, path: str):
        prev_w = None
        for w in self._words:
            if prev_w:
                assert w.id > prev_w.id, 'Bad lexicon, not sorted by id'
                assert w.token > prev_w.token, 'Bad lexicon, not sorted by token'
            prev_w = w

        with open(path, 'wb') as f:
            msgpack.dump([
                (w.id, w.token, w.count)
                for w in self._words
            ], f, use_bin_type=True)

    @staticmethod
    def load(path: str):
        with open(path, 'rb') as f:
            words = [Lexicon.Word(*x) for x in msgpack.load(f, raw=False)]
        return Lexicon(words)


class Documents(object):
    """A mapping from document id to name, and vice versa"""

    Document = namedtuple('Document', ['id', 'name'])

    def __init__(self, docs):
        """List of Documents, where index is the id"""
        assert all(i == d.id for i, d in enumerate(docs))
        self._docs = docs

    def __iter__(self):
        return self._docs.__iter__()

    def __getitem__(self, key):
        if isinstance(key, int):
            # Get doc name by id (IndexError)
            return self._docs[key]
        elif isinstance(key, str):
            # Get doc id by name (KeyError)
            for d in self._docs:
                if d.name == key:
                    return d.id
            else:
                raise KeyError('No document named {}'.format(key))
        raise TypeError('Not supported for {}'.format(type(key)))

    def __contains__(self, key):
        try:
            self.__getitem__(key)
        except (KeyError, IndexError):
            return False
        return True

    def __len__(self):
        return len(self._docs)

    def store(self, path: str):
        with open(path, 'w') as f:
            for d in self._docs:
                f.write('\t'.join([str(d.id), d.name]))
                f.write('\n')

    @staticmethod
    def load(path):
        documents = []
        with open(path, 'r') as f:
            for line in f:
                i, name = line.strip().split('\t', 1)
                documents.append(Documents.Document(id=int(i), name=name))
        return Documents(documents)


class BinaryFormat(object):
    """
    Binary data formatter for writing and reading the indexes

    Supports 4 data types:
        - u32
        - time interval
        - datum
        - byte
    """

    Config = namedtuple(
        'Config', [
            'start_time_bytes',     # Number of bytes to encode start times
            'end_time_bytes',       # Number of bytes to encode end - start
            'datum_bytes',          # Number of bytes to encode other data
        ])

    def __init__(self, config):
        self._endian = 'little'

        assert config.start_time_bytes > 0
        assert config.end_time_bytes > 0
        self._start_time_bytes = config.start_time_bytes
        self._end_time_bytes = config.end_time_bytes

        assert config.datum_bytes > 0
        self._datum_bytes = config.datum_bytes

        # Derived values
        self._time_interval_bytes = (
            config.start_time_bytes + config.end_time_bytes)
        self._max_time_interval = (
            2 ** (8 * (config.start_time_bytes - config.end_time_bytes)) - 1)
        self._max_datum_value = 2 ** (config.datum_bytes * 8) - 1

    @property
    def u32_bytes(self):
        return 4

    @property
    def time_interval_bytes(self):
        return self._time_interval_bytes

    @property
    def datum_bytes(self):
        return self._datum_bytes

    @property
    def max_time_interval(self):
        """Largest number of milliseconds between start and end times"""
        return self._max_time_interval

    @property
    def max_datum_value(self):
        """Largest value that can be serialized"""
        return self._max_datum_value

    def encode_u32(self, data):
        assert isinstance(data, int)
        return (data).to_bytes(4, self._endian)

    def encode_time_interval(self, start, end):
        assert isinstance(start, int)
        assert isinstance(end, int)
        diff = end - start
        if diff < 0:
            raise ValueError(
                'start cannot exceed end: {} > {}'.format(start, end))
        if diff > self.max_time_interval:
            raise ValueError('end - start > {}'.format(self.max_time_interval))
        return (
            (start).to_bytes(self._start_time_bytes, self._endian) +
            (diff).to_bytes(self._end_time_bytes, self._endian))

    def encode_datum(self, i):
        assert isinstance(i, int)
        if i < 0:
            raise ValueError('Out of range: {} < 0'.format(i))
        if i > self._max_datum_value:
            raise ValueError('Out of range: {} > {}'.format(
                             i, self._max_datum_value))
        return (i).to_bytes(self._datum_bytes, self._endian)

    def _decode_u32(self, s):
        assert len(s) == 4, '{} is the wrong length'.format(len(s))
        return int.from_bytes(s, self._endian)

    def _decode_time_interval(self, s):
        assert len(s) == self.time_interval_bytes
        start = int.from_bytes(s[:self._start_time_bytes], self._endian)
        diff = int.from_bytes(s[self._start_time_bytes:], self._endian)
        return start, start + diff

    def _decode_datum(self, s):
        assert len(s) == self._datum_bytes, \
            '{} is the wrong length'.format(len(s))
        return int.from_bytes(s, self._endian)

    @staticmethod
    def default():
        return BinaryFormat(
            BinaryFormat.Config(
                start_time_bytes=4,
                end_time_bytes=2,
                datum_bytes=3))


class CaptionIndex(object):
    """
    Interface to a binary encoded index file.
    """

    Posting = namedtuple(
        'Posting', [
            'start',        # Start time in seconds
            'end',          # End time in seconds
            'idx',          # Start position in document
            'len',          # Number of tokens
        ])

    Document = namedtuple(
        'Document', [
            'id',           # Document ID
            'postings'      # List of locations
        ])

    def __init__(self, path: str, lexicon: Lexicon, documents: Documents,
                 binary_format=None, debug=False):
        assert isinstance(lexicon, Lexicon)
        assert isinstance(documents, Documents)
        self._lexicon = lexicon
        self._documents = documents

        if binary_format is None:
            binary_format = BinaryFormat.default()

        self._rs_index = RsCaptionIndex(
            path, datum_size=binary_format.datum_bytes,
            start_time_size=binary_format._start_time_bytes,
            end_time_size=binary_format._end_time_bytes,
            debug=debug)

    def __require_open_index(f):
        def wrapper(self, *args, **kwargs):
            if self._rs_index is None:
                raise ValueError('I/O on closed CaptionIndex')
            return f(self, *args, **kwargs)
        return wrapper

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.close()

    def close(self):
        self._rs_index = None

    def __get_document_id(self, doc):
        if isinstance(doc, Documents.Document):
            return doc.id
        else:
            return self._documents[doc].id

    def __get_document_ids(self, docs):
        return [] if docs is None else [
            self.__get_document_id(d) for d in docs]

    def __get_word_id(self, word):
        if isinstance(word, Lexicon.Word):
            return word.id
        else:
            return self._lexicon[word].id

    @__require_open_index
    def document_length(self, doc):
        """Get the length of a document in tokens"""
        doc_id = self.__get_document_id(doc)
        return self._rs_index.document_length(doc_id)[0]

    @__require_open_index
    def document_duration(self, doc):
        """Get the duration of a document in seconds"""
        doc_id = self.__get_document_id(doc)
        return self._rs_index.document_length(doc_id)[1]

    def __tokenize_text(self, text):
        if isinstance(text, str):
            tokens = list(tokenize(text.strip()))
            if len(tokens) == 0:
                raise ValueError('No words in input')
            for t in tokens:
                if t not in self._lexicon:
                    raise ValueError('{} is not in the lexicon'.format(t))
        elif isinstance(text, list):
            tokens = text
            if len(tokens) == 0:
                raise ValueError('No words in input')
        elif isinstance(text, types.GeneratorType):
            tokens = list(text)
            if len(tokens) == 0:
                raise ValueError('No words in input')
        else:
            raise TypeError('Unsupported type: {}'.format(type(text)))
        return tokens

    def search(self, text, documents=None) -> Iterable['CaptionIndex.Document']:
        tokens = self.__tokenize_text(text)
        return self.ngram_search(*tokens, documents=documents)

    def __unpack_rs_search(self, result):
        for doc_id, postings in result:
            yield CaptionIndex.Document(
                id=doc_id, postings=[
                    CaptionIndex.Posting(*p) for p in postings])

    @__require_open_index
    def ngram_search(self, first_word, *other_words, documents=None) -> Iterable['CaptionIndex.Document']:
        doc_ids = self.__get_document_ids(documents)
        word_ids = [self.__get_word_id(w) for w in [first_word, *other_words]]
        return self.__unpack_rs_search(
            self._rs_index.ngram_search(word_ids, doc_ids))

    def contains(self, text, documents=None) -> Set[int]:
        tokens = self.__tokenize_text(text)
        return self.ngram_contains(*tokens, documents=documents)

    @__require_open_index
    def ngram_contains(self, first_word, *other_words, documents=None) -> Set[int]:
        doc_ids = self.__get_document_ids(documents)
        word_ids = [self.__get_word_id(w) for w in [first_word, *other_words]]
        return set(self._rs_index.ngram_contains(word_ids, doc_ids))

    @__require_open_index
    def tokens(self, doc, index=0, count=2 ** 31) -> List[int]:
        doc_id = self.__get_document_id(doc)
        return self._rs_index.tokens(doc_id, index, count)

    @__require_open_index
    def intervals(self, doc, start_time=0., end_time=float('inf')) -> Iterable['CaptionIndex.Posting']:
        doc_id = self.__get_document_id(doc)
        return [
            CaptionIndex.Posting(*p)
            for p in self._rs_index.intervals(doc_id, start_time, end_time)]

    @__require_open_index
    def position(self, doc, time_offset):
        doc_id = self.__get_document_id(doc)
        return self._rs_index.position(doc_id, time_offset)


class MetadataFormat(ABC):

    @staticmethod
    def header(doc_id: int, n: int):
        return doc_id.to_bytes(4, 'little') + n.to_bytes(4, 'little')

    @abstractmethod
    def decode(self, s):
        """Return decoded metadata"""
        pass

    @abstractproperty
    def size(self):
        """Number of bytes of metadata"""
        pass


class MetadataIndex(object):
    """
    Interface to binary encoded metadata files for efficient iteration
    """

    def __init__(self, path: str, documents: Documents,
                 metadata_format: MetadataFormat, debug=False):
        assert isinstance(metadata_format, MetadataFormat)
        assert metadata_format.size > 0, \
            'Invalid metadata size: {}'.format(metadata_format.size)
        self._documents = documents
        self._meta_fmt = metadata_format
        self._rs_meta = RsMetadataIndex(path, metadata_format.size, debug)

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.close()

    def close(self):
        self._rs_meta = None

    def __require_open_index(f):
        def wrapper(self, *args, **kwargs):
            if self._rs_meta is None:
                raise ValueError('I/O on closed MetadataIndex')
            return f(self, *args, **kwargs)
        return wrapper

    def __get_document_id(self, doc):
        if isinstance(doc, Documents.Document):
            return doc.id
        else:
            return self._documents[doc].id

    @__require_open_index
    def metadata(self, doc, position=0, count=2 ** 31) -> List:
        """
        Generator over metadata returned by the MetadataFormat's decode method.
        """
        doc_id = self.__get_document_id(doc)
        if position < 0:
            raise ValueError('Position cannot be negative')
        if count < 0:
            raise ValueError('Count cannot be negative')
        return [
            self._meta_fmt.decode(b)
            for b in self._rs_meta.metadata(doc_id, position, count)]


class NgramFrequency(object):
    """A map from ngrams to their frequencies"""

    def __init__(self, path, lexicon):
        """Dictionary of ngram to frequency"""
        assert isinstance(path, str)
        assert isinstance(lexicon, Lexicon)
        self._lexicon = lexicon
        with open(path, 'rb') as f:
            self._counts, self._totals = msgpack.load(f, use_list=False)

    def __iter__(self):
        return self._counts.__iter__()

    def __getitem__(self, key):
        if isinstance(key, str):
            key = tuple(tokenize(key.strip()))
        denom = self._totals[len(key) - 1]
        if isinstance(key[0], int):
            return self._counts[key] / denom
        elif isinstance(key[0], str):
            key = tuple(self._lexicon[k].id for k in key)
            return self._counts[key] / denom
        raise TypeError('Not supported for {}'.format(type(key)))

    def __contains__(self, key):
        try:
            self.__getitem__(key)
        except (KeyError, IndexError):
            return False
        return True

    def __len__(self):
        return len(self._counts)
