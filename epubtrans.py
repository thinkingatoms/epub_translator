# -*- coding: utf-8 -*-

# created on: 6/4/22
# author: tom


import abc
from datetime import datetime
from enum import Enum
import json
import os
import re
import time
from typing import List, Set, Tuple, Dict

from bs4 import BeautifulSoup, Tag  # , NavigableString
import ebooklib
from ebooklib import epub as epublib
from google.cloud import translate


__all__ = ['EPUBTranslator', 'GoogleTranslator', 'MockTranslator',
           'InvalidText', 'CannotTranslate']


# Exceptions


class InvalidText(ValueError):
    def __init__(self, msg, text: str):
        super().__init__(msg)
        self.text = text

    def __str__(self):
        msg = super().__str__()
        return (f"{self.__class__.__name__}: {msg}\n"
                f"TEXT:\n{self.text}\n")


class CannotTranslate(ValueError):
    def __init__(self, msg, text: str, exception: Exception = None):
        super().__init__(msg)
        self.text = text
        self.exception = exception

    def __str__(self):
        msg = super().__str__()
        return (f"{self.__class__.__name__}: {msg}\n"
                f"TEXT:\n{self.text}\nEXCEPTION:\n{self.exception}\n")


# Translators


class Translator:
    _CHUNK_SIZE = None  # bytes

    def __init__(self, *, cache_file: str = None, chunk_size=None,
                 source_language='en-US', target_language='zh-CN'):
        """
        :param cache_file: specify False to turn off cache
        :param chunk_size: specify maximum size to send to translate API
        :param source_language: source language
        :param target_language: target language
        """
        if cache_file is None:
            cache_file = (
                    'translator_' +
                    datetime.now().isoformat().replace(':', '-').replace('-', '_') +
                    '.json'
            )
        self._cache_file = cache_file
        self._chunk_size = chunk_size or self._CHUNK_SIZE
        if not self._chunk_size:
            raise ValueError('must specify chunk_size')
        self._source_language = source_language
        self._target_language = target_language
        self._save_cache(self._load_cache())  # test writability
        self._cache = None

    def _load_cache(self):
        if self._cache_file:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, mode='r') as f:
                    cache = json.load(f)
            else:
                cache = {}
        else:
            if self._cache is None:
                self._cache = {}
            cache = self._cache
        return cache

    def _save_cache(self, cache):
        if self._cache_file:
            with open(self._cache_file, mode='w') as f:
                json.dump(cache, f)

    def translate_texts(self, texts: List[str]) -> (Dict[str, str], List[Exception]):
        if isinstance(texts, str):
            texts = [texts]
        elif not isinstance(texts, list) or not all(isinstance(s, str) for s in texts):
            raise ValueError('must specify string or a list of strings')

        errors = []
        cache = self._load_cache()

        distinct_texts = set()
        for text in texts:
            _text = text.strip()
            if not _text:
                cache[text] = ''
                continue
            if text not in cache:
                distinct_texts.add(text)
        if distinct_texts:
            trans = []
            chunks, _errors = self._chunk_texts(distinct_texts)
            errors.extend(_errors)
            for meta, chunk in chunks:
                chunk_trans, _errors = self._translate_chunk(chunk)
                if _errors:
                    errors.extend(_errors)
                else:
                    trans.extend(zip(meta, chunk_trans))
            trans = self._unchunk_trans(trans)
            if trans:
                cache.update(trans)
                self._save_cache(cache)

        return {text: cache.get(text) for text in texts}, errors

    def _chunk_texts(self, texts: Set[str]) -> (List[Tuple[List[Tuple[int, int, str]], List[str]]], List[Exception]):
        last_meta, last_chunk, last_chunk_size = [], [], 0
        chunks, errors = [(last_meta, last_chunk)], []
        for text_id, text in enumerate(texts):
            size = len(text.encode('utf-8'))
            line_id = 0
            if size < self._chunk_size:
                if last_chunk_size + size > self._chunk_size:
                    last_meta, last_chunk, last_chunk_size = [(text_id, line_id, text)], [text.strip()], size
                    chunks.append((last_meta, last_chunk))
                else:
                    last_meta.append((text_id, line_id, text))
                    last_chunk.append(text.strip())
                    last_chunk_size += size
            else:
                lines = text.split('.')
                if any(len(line.encode('utf-8')) > self._chunk_size for line in lines):
                    errors.append(InvalidText('text too long', text))
                    continue
                for line_id, line in enumerate(lines):
                    line = line.strip()
                    if not line:
                        continue
                    size = len(line.encode('utf-8'))
                    if last_chunk_size + size > self._chunk_size:
                        last_meta, last_chunk, last_chunk_size = [(text_id, line_id, text)], [line], size
                        chunks.append((last_meta, last_chunk))
                    else:
                        last_meta.append((text_id, line_id, text))
                        last_chunk.append(line)
                        last_chunk_size += size
        return chunks, errors

    @staticmethod
    def _unchunk_trans(trans: List[Tuple[Tuple[int, int, str], str]]) -> Dict[str, str]:
        trans = sorted(trans)
        texts = {}
        for (text_id, line_id, orig), tran in trans:
            if text_id not in texts:
                texts[text_id] = orig, []
            texts[text_id][1].append((line_id, tran))
        ret = {}
        for orig, trans in texts.values():
            trans = ' '.join([tran for line_id, tran in sorted(trans)])
            ret[orig] = trans
        return ret

    @abc.abstractmethod
    def _translate_chunk(self, chunk: List[str]) -> (List[str], List[Exception]):
        pass


class GoogleTranslator(Translator):
    """
    simple class to wrap around google translate
    """

    _CHUNK_SIZE = 1000

    def __init__(self, *, project_id='dad-translations', **kwargs):
        super().__init__(**kwargs)
        self._project_id = project_id

    def _translate_chunk(self, chunk: List[str]) -> (List[str], List[Exception]):
        client = translate.TranslationServiceClient()

        location = "global"
        parent = f"projects/{self._project_id}/locations/{location}"

        # https://cloud.google.com/translate/docs/supported-formats
        response = client.translate_text(
            request={
                "parent": parent,
                "contents": chunk,
                "mime_type": "text/plain",  # mime types: text/plain, text/html
                "source_language_code": self._source_language,
                "target_language_code": self._target_language,
            }
        )
        errors = []
        try:
            trans = list(t.translated_text for t in response.translations)
        except Exception as e:
            trans = []
            for text in chunk:
                errors.append(CannotTranslate('cannot translate', text, e))
        return trans, errors


class MockTranslator(Translator):
    _CHUNK_SIZE = 2000

    def __init__(self, record=True, **kwargs):
        super().__init__(**kwargs)
        self._record = record
        self.chunks = []

    def _translate_chunk(self, chunk: List[str]) -> (List[str], List[Exception]):
        if self._record:
            self.chunks.append(chunk)
        return [''.join(('MOCKED: ', text)) for text in chunk], []


# EPUB translator


class TranslationType(Enum):
    INLINE = 0
    REPLACE = 1


class EPUBTranslator:
    HTML_TAGS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li']
    IGNORED_ITEM_TYPES = [
        ebooklib.ITEM_AUDIO,
        ebooklib.ITEM_COVER,
        ebooklib.ITEM_FONT,
        ebooklib.ITEM_IMAGE,
        ebooklib.ITEM_NAVIGATION,
        ebooklib.ITEM_STYLE,
    ]
    _replace_newlines = re.compile('\n +')

    def __init__(self, epub_file: str, translator: Translator,
                 html_tags: List[str] = None, ignored_item_types: List[str] = None):
        self._epub_file = epub_file
        if not os.path.isfile(self._epub_file):
            raise ValueError(f'{self._epub_file} is not a file')
        self._translator = translator
        assert isinstance(self._translator, Translator)
        self._html_tags = html_tags or self.HTML_TAGS
        self._ignored_item_types = ignored_item_types or self.IGNORED_ITEM_TYPES

    def translate(self, translation_type: TranslationType = TranslationType.INLINE,
                  output_epub_file: str = None, force: bool = False) -> str:
        if not isinstance(translation_type, TranslationType):
            raise ValueError('translation_type must be a TranslationType')
        if output_epub_file is None:
            output_epub_file = os.path.basename(self._epub_file).lower()
            assert output_epub_file.endswith('.epub')
            output_epub_file = ''.join(c if c.isalnum() else '_' for c in output_epub_file[:-5])
            if translation_type == TranslationType.REPLACE:
                output_epub_file += '.tran.epub'
            else:  # if translation_type == TranslationType.INLINE:
                output_epub_file += '.dual.epub'
            output_epub_file = os.path.join(os.path.dirname(self._epub_file), output_epub_file)
        if os.path.exists(output_epub_file):
            if not force:
                raise ValueError(f'{output_epub_file} already exists')
            print(f'warning: overwriting existing file {output_epub_file}')
            print('sleeping for 15 seconds..')
            time.sleep(15)
        ebook, contents = self._read_epub_file()
        trans, errors = self._translator.translate_texts([r[0] for r in contents])
        if errors:
            print(f'encountered {len(errors)} error(s):\n')
            for e in errors:
                print(e)
            raise RuntimeError('cannot translate, see above')
        files = self._build_files(contents, trans, translation_type)
        for item in ebook.get_items():
            if item.file_name in files:
                soup = files[item.file_name]
                item.set_content(str(soup).encode('utf-8'))
        epublib.write_epub(output_epub_file, ebook)
        return output_epub_file

    def _read_epub_file(self, html_tags=None, ignored_item_types=None)\
            -> (epublib.EpubBook, List[Tuple[str, Tag, epublib.EpubHtml]]):
        if html_tags is None:
            html_tags = self._html_tags
        if ignored_item_types is None:
            ignored_item_types = self._ignored_item_types
        ebook = epublib.read_epub(self._epub_file)
        contents = []
        for item in ebook.get_items():
            if item.get_type() in ignored_item_types:
                continue
            # soup = BeautifulSoup(item.get_content().decode('utf-8'), 'html.parser')
            soup = BeautifulSoup(item.get_content().decode('utf-8'))
            for p in soup.find_all(html_tags):
                line = self._replace_newlines.sub(' ', p.text.strip())
                if not line:
                    continue
                contents.append((line, p, item))
        return ebook, contents

    def _build_files(self, contents: List[Tuple[str, Tag, epublib.EpubHtml]],
                     trans: Dict[str, str], translation_type: TranslationType) -> Dict[str, BeautifulSoup]:
        files = {}
        soups = {}
        for text, tag, item in contents:
            if item.file_name not in files:
                files[item.file_name] = {'rows': [], 'item': item}
            files[item.file_name]['rows'].append((tag, text, trans.get(text)))
        for k, f in files.items():
            soup = BeautifulSoup(f['item'].get_content())
            soups[k] = soup
            tags = []
            for p in soup.find_all(self._html_tags):
                line = self._replace_newlines.sub(' ', p.text.strip())
                if not line:
                    continue
                tags.append(p)
            for t, r in zip(tags, f['rows']):
                tag = r[0]
                assert t == tag
                if translation_type == TranslationType.REPLACE:
                    t.string = r[-1]
                    # if t.string:
                    #     t.string = r[-1]
                    # else:
                    #     for c in t.children:
                    #         if isinstance(c, NavigableString):
                    #             c.replace_with(r[-1])
                    #             break
                    #     else:
                    #         t.string = r[-1]
                else:
                    new_tag = soup.new_tag(name=tag.name, **tag.attrs)
                    new_tag.append(r[-1])
                    t.insert_after(new_tag)
        return soups


if __name__ == '__main__':
    pass
