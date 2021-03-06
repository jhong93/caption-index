"""
Decode SRT/VTT from CaptionIndex
"""

from io import StringIO
import math
import re
from typing import Iterable

from .index import Lexicon, DocumentData


def _format_time(t: float, is_vtt: bool) -> str:
    millis = math.floor(t * 1000) % 1000
    seconds = math.floor(t) % 60
    minutes = math.floor(t / 60) % 60
    hours = math.floor(t / 3600)
    return '{:02}:{:02}:{:02}{}{:03}'.format(
        hours, minutes, seconds, '.' if is_vtt else ',', millis)


def _untokenize(words: Iterable[str]) -> str:
    text = ' '.join(words)
    step1 = text.replace("`` ", '"').replace(" ''", '"').replace('. . .', '...')
    step2 = step1.replace(" ( ", " (").replace(" ) ", ") ")
    step3 = re.sub(r' ([.,:;?!%>]+)([ \'"`])', r"\1\2", step2)
    step4 = re.sub(r' ([.,:;?!%>]+)$', r"\1", step3)
    step5 = step4.replace(" '", "'").replace(" n't", "n't").replace("can not", "cannot")
    step6 = step5.replace(" ` ", " '")
    return step6.strip()


def _decode_helper(
        lexicon: Lexicon, document_data: DocumentData,
        unknown_token: str, is_vtt: bool
):
    out = StringIO()
    if is_vtt:
        out.write('WEBVTT\r\n\r\n')
    for i, line in enumerate(document_data.lines()):
        if line.len > 0:
            if not is_vtt:
                out.write(str(i))
                out.write('\r\n')
            tokens = [lexicon.decode(t, unknown_token)
                      for t in document_data.tokens(line.idx, line.len)]
            out.write('{} --> {}\r\n'.format(
                _format_time(line.start, is_vtt),
                _format_time(line.end, is_vtt)))
            out.write(_untokenize(tokens))
            out.write('\r\n\r\n')
    return out.getvalue()


def get_vtt(
        lexicon: Lexicon,
        document_data: DocumentData,
        unknown_token: str = 'UNKNOWN'
) -> str:
    """Get document as a VTT string"""
    return _decode_helper(lexicon, document_data, unknown_token, True)


def get_srt(
        lexicon: Lexicon,
        document_data: DocumentData,
        unknown_token: str = 'UNKNOWN'
) -> str:
    """Get document as a SRT string"""
    return _decode_helper(lexicon, document_data, unknown_token, False)
