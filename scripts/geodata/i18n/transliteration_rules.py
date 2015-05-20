# -*- coding: utf-8 -*-
'''
transliteration.py

Automatically builds rules for transforming other scripts (e.g. Cyrillic, Greek,
Han, Katakana, Devanagari, etc.) into Latin characters.

Uses XML transforms from the CLDR repository.

'''

import argparse
import codecs
import csv
import htmlentitydefs
import itertools
import os
import re
import requests
import sys
import time
import urlparse
import unicodedata

from collections import defaultdict, deque

from lxml import etree

from scanner import Scanner
from unicode_properties import *
from unicode_paths import CLDR_DIR
from geodata.encoding import safe_decode, safe_encode

CLDR_TRANSFORMS_DIR = os.path.join(CLDR_DIR, 'common', 'transforms')

PRE_TRANSFORM = 1
FORWARD_TRANSFORM = 2
BACKWARD_TRANSFORM = 3
BIDIRECTIONAL_TRANSFORM = 4

PRE_TRANSFORM_OP = '::'
BACKWARD_TRANSFORM_OPS = set([u'←', u'<'])
FORWARD_TRANSFORM_OPS = set([u'→', u'>'])
BIDIRECTIONAL_TRANSFORM_OPS = set([u'↔', u'<>'])

ASSIGNMENT_OP = '='

PRE_CONTEXT_INDICATOR = '{'
POST_CONTEXT_INDICATOR = '}'

REVISIT_INDICATOR = '|'

WORD_BOUNDARY_VAR_NAME = 'wordBoundary'
WORD_BOUNDARY_VAR = '${}'.format(WORD_BOUNDARY_VAR_NAME)

START_OF_HAN_VAR_NAME = 'startOfHanMarker'
START_OF_HAN_VAR = '${}'.format(START_OF_HAN_VAR_NAME)

start_of_han_regex = re.compile(START_OF_HAN_VAR.replace('$', '\$'))

word_boundary_var_regex = re.compile(WORD_BOUNDARY_VAR.replace('$', '\$'))

WORD_BOUNDARY_CHAR = u'\u0001'
EMPTY_TRANSITION = u'\u0004'

NAMESPACE_SEPARATOR_CHAR = u"|"

WORD_BOUNDARY_CHAR = u"\x01"
PRE_CONTEXT_CHAR = u"\x02"
POST_CONTEXT_CHAR = u"\x03"
EMPTY_TRANSITION_CHAR = u"\x04"
REPEAT_ZERO_CHAR = u"\x05"
REPEAT_ONE_CHAR = u"\x06"
BEGIN_SET_CHAR = u"\x0e"
END_SET_CHAR = u"\x0f"
GROUP_INDICATOR_CHAR = u"\x10"


EXCLUDE_TRANSLITERATORS = set([
    'hangul-latin',
    'interindic-latin',
    'jamo-latin',
    # Don't care about spaced Han because our tokenizer does it already
    'han-spacedhan',
])

NFD = 'NFD'
NFKD = 'NFKD'
NFC = 'NFC'
NFKC = 'NFKC'
STRIP_MARK = 'STRIP_MARK'

LOWER = 'lower'
UPPER = 'upper'
TITLE = 'title'

UNICODE_NORMALIZATION_TRANSFORMS = set([
    NFD,
    NFKD,
    NFC,
    NFKC,
    STRIP_MARK,
])

unicode_category_aliases = {
    'letter': 'L',
    'lower': 'Ll',
    'lowercase': 'Ll',
    'lowercaseletter': 'Ll',
    'upper': 'Lu',
    'uppercase': 'Lu',
    'uppercaseletter': 'Lu',
    'title': 'Lt',
    'nonspacing mark': 'Mn',
    'mark': 'M',
}

unicode_categories = defaultdict(list)
unicode_blocks = defaultdict(list)
unicode_combining_classes = defaultdict(list)
unicode_general_categories = defaultdict(list)
unicode_scripts = defaultdict(list)
unicode_properties = {}

unicode_script_ids = {}

unicode_blocks = {}
unicode_category_aliases = {}
unicode_property_aliases = {}
unicode_property_value_aliases = {}
unicode_word_breaks = {}

COMBINING_CLASS_PROP = 'canonical_combining_class'
BLOCK_PROP = 'block'
GENERAL_CATEGORY_PROP = 'general_category'
SCRIPT_PROP = 'script'
WORD_BREAK_PROP = 'word_break'


class TransliterationParseError(Exception):
    pass


def init_unicode_categories():
    global unicode_categories, unicode_general_categories, unicode_scripts, unicode_category_aliases
    global unicode_blocks, unicode_combining_classes, unicode_properties, unicode_property_aliases
    global unicode_property_value_aliases, unicode_scripts, unicode_script_ids, unicode_word_breaks

    for i in xrange(NUM_CHARS):
        unicode_categories[unicodedata.category(unichr(i))].append(unichr(i))
        unicode_combining_classes[str(unicodedata.combining(unichr(i)))].append(unichr(i))

    unicode_categories = dict(unicode_categories)
    unicode_combining_classes = dict(unicode_combining_classes)

    for key in unicode_categories.keys():
        unicode_general_categories[key[0]].extend(unicode_categories[key])

    unicode_general_categories = dict(unicode_general_categories)

    script_chars = get_chars_by_script()
    for i, script in enumerate(script_chars):
        if script:
            unicode_scripts[script.lower()].append(unichr(i))

    unicode_scripts = dict(unicode_scripts)

    unicode_script_ids.update(build_master_scripts_list(script_chars))

    unicode_blocks.update(get_unicode_blocks())
    unicode_properties.update(get_unicode_properties())
    unicode_property_aliases.update(get_property_aliases())

    unicode_word_breaks.update(get_word_break_properties())

    for key, value in get_property_value_aliases().iteritems():
        key = unicode_property_aliases.get(key, key)
        if key == GENERAL_CATEGORY_PROP:
            for k, v in value.iteritems():
                k = k.lower()
                unicode_category_aliases[k] = v
                if '_' in k:
                    unicode_category_aliases[k.replace('_', '')] = v

        unicode_property_value_aliases[key] = value


RULE = 'RULE'
TRANSFORM = 'TRANSFORM'
FILTER = 'FILTER'

UTF8PROC_TRANSFORMS = {
    'Any-NFC': NFC,
    'Any-NFD': NFD,
    'Any-NFKD': NFKD,
    'Any-NFKC': NFKC,
    'Any-Lower': LOWER,
    'Any-Upper': UPPER,
    'Any-Title': TITLE,
}


CONTEXT_TYPE_NONE = 'CONTEXT_TYPE_NONE'
CONTEXT_TYPE_STRING = 'CONTEXT_TYPE_STRING'
CONTEXT_TYPE_WORD_BOUNDARY = 'CONTEXT_TYPE_WORD_BOUNDARY'
CONTEXT_TYPE_REGEX = 'CONTEXT_TYPE_REGEX'

all_transforms = set()

pre_transform_full_regex = re.compile('::[\s]*(.*)[\s]*', re.UNICODE)
pre_transform_regex = re.compile('[\s]*([^\s\(\)]*)[\s]*(?:\(.*\)[\s]*)?', re.UNICODE)
assignment_regex = re.compile(u"(?:[\s]*(\$[^\s\=]+)[\s]*\=[\s]*(?!=[\s])(.*)(?<![\s])[\s]*)", re.UNICODE)
transform_regex = re.compile(u"(?:[\s]*(?!=[\s])(.*?)(?<![\s])[\s]*)((?:<>)|[←<→>↔])(?:[\s]*(?!=[\s])(.*)(?<![\s])[\s]*)", re.UNICODE)

quoted_string_regex = re.compile(r'\'.*?\'', re.UNICODE)

COMMENT_CHAR = '#'
END_CHAR = ';'


def unescape_unicode_char(m):
    return m.group(0).decode('unicode-escape')

escaped_unicode_regex = re.compile(r'\\u[0-9A-Fa-f]{4}')
escaped_wide_unicode_regex = re.compile(r'\\U[0-9A-Fa-f]{8}')

literal_space_regex = re.compile(r'(?:\\u0020|\\U00000020)')

# These are a few unicode property types that were needed by the transforms
unicode_property_regexes = [
    ('ideographic', '[〆〇〡-〩〸-〺㐀-䶵一-鿌豈-舘並-龎 𠀀-𪛖𪜀-𫜴𫝀-𫠝丽-𪘀]'),
    ('logical_order_exception', '[เ-ไ ເ-ໄ ꪵ ꪶ ꪹ ꪻ ꪼ]'),
]

rule_map = {
    u'[:Latin:] { [:Mn:]+ → ;': ':: {}'.format(STRIP_MARK),
    u':: [[[:Greek:][:Mn:][:Me:]] [\:-;?·;·]] ;': u':: [[[:Greek:][́̀᾿᾿˜̑῀¨ͺ´`῀᾿῎῍῏῾῞῝῟΅῭῁ˉ˘]] [\'\:-;?·;·]]',

}

unicode_properties = {}


def replace_literal_space(m):
    return "' '"

regex_char_set_greedy = re.compile(r'\[(.*)\]', re.UNICODE)
regex_char_set = re.compile(r'\[(.*?)(?<!\\)\]', re.UNICODE)

char_class_regex_str = '\[(?:[^\[\]]*\[[^\[\]]*\][^\[\]]*)*[^\[\]]*\]'

nested_char_class_regex = re.compile('\[(?:[^\[\]]*\[[^\[\]]*\][^\[\]]*)+[^\[\]]*\]', re.UNICODE)

range_regex = re.compile(r'[\\]?([^\\])\-[\\]?([^\\])', re.UNICODE)
var_regex = re.compile('[\s]*\$([A-Za-z_\-]+)[\s]*')

context_regex = re.compile(u'(?:[\s]*(?!=[\s])(.*?)(?<![\s])[\s]*{)?(?:[\s]*([^}{]*)[\s]*)(?:}[\s]*(?!=[\s])(.*)(?<![\s])[\s]*)?', re.UNICODE)

paren_regex = re.compile(r'\(.*\)', re.UNICODE)

group_ref_regex_str = '\$[0-9]+'
group_ref_regex = re.compile(group_ref_regex_str)

# Limited subset of regular expressions used in transforms

OPEN_SET = 'OPEN_SET'
CLOSE_SET = 'CLOSE_SET'
OPEN_GROUP = 'OPEN_GROUP'
CLOSE_GROUP = 'CLOSE_GROUP'
GROUP_REF = 'GROUP_REF'
CHAR_SET = 'CHAR_SET'
CHAR_MULTI_SET = 'CHAR_MULTI_SET'
CHAR_CLASS = 'CHAR_CLASS'
OPTIONAL = 'OPTIONAL'
CHARACTER = 'CHARACTER'
WIDE_CHARACTER = 'WIDE_CHARACTER'
REVISIT = 'REVISIT'
REPEAT = 'REPEAT'
LPAREN = 'LPAREN'
RPAREN = 'RPAREN'
WHITESPACE = 'WHITESPACE'
QUOTED_STRING = 'QUOTED_STRING'
SINGLE_QUOTE = 'SINGLE_QUOTE'
HTML_ENTITY = 'HTML_ENTITY'
SINGLE_QUOTE = 'SINGLE_QUOTE'
UNICODE_CHARACTER = 'UNICODE_CHARACTER'
UNICODE_WIDE_CHARACTER = 'UNICODE_WIDE_CHARACTER'
ESCAPED_CHARACTER = 'ESCAPED_CHARACTER'


BEFORE_CONTEXT = 'BEFORE_CONTEXT'
AFTER_CONTEXT = 'AFTER_CONTEXT'

PLUS = 'PLUS'
STAR = 'STAR'

rule_scanner = Scanner([
    (r'[\\].', ESCAPED_CHARACTER),
    ('\[', OPEN_SET),
    ('\]', CLOSE_SET),
    ('\(', OPEN_GROUP),
    ('\)', CLOSE_GROUP),
    ('\{', BEFORE_CONTEXT),
    ('\}', AFTER_CONTEXT),
    ('[\s]+', WHITESPACE),
    (r'[^\s]', CHARACTER),
])


# Scanner for the lvalue or rvalue of a transform rule

transform_scanner = Scanner([
    (r'\\u[0-9A-Fa-f]{4}', UNICODE_CHARACTER),
    (r'\\U[0-9A-Fa-f]{8}', UNICODE_WIDE_CHARACTER),
    (r'[\\].', ESCAPED_CHARACTER),
    (r'\'\'', SINGLE_QUOTE),
    (r'\'.*?\'', QUOTED_STRING),
    # Char classes only appear to go two levels deep in LDML
    ('\[', OPEN_SET),
    ('\]', CLOSE_SET),
    ('\(', OPEN_GROUP),
    ('\)', CLOSE_GROUP),
    (group_ref_regex_str, GROUP_REF),
    (r'\|', REVISIT),
    (r'&.*?;', HTML_ENTITY),
    (r'(?<![\\])\*', REPEAT),
    (r'(?<![\\])\+', PLUS),
    ('(?<=[^\s])\?', OPTIONAL),
    ('\(', LPAREN),
    ('\)', RPAREN),
    ('\|', REVISIT),
    ('[\s]+', WHITESPACE),
    (ur'[\ud800-\udbff][\udc00-\udfff]', WIDE_CHARACTER),
    (r'[^\s]', CHARACTER),
], re.UNICODE)

CHAR_RANGE = 'CHAR_RANGE'
CHAR_CLASS_PCRE = 'CHAR_CLASS_PCRE'
WORD_BOUNDARY = 'WORD_BOUNDARY'
NEGATION = 'NEGATION'
INTERSECTION = 'INTERSECTION'
DIFFERENCE = 'DIFFERENCE'
BRACKETED_CHARACTER = 'BRACKETED_CHARACTER'

# Scanner for a character set (yes, a regex regex)

char_set_scanner = Scanner([
    ('^\^', NEGATION),
    (r'\\p\{[^\{\}]+\}', CHAR_CLASS_PCRE),
    (r'[\\]?[^\\\s]\-[\\]?[^\s]', CHAR_RANGE),
    (r'\\u[0-9A-Fa-f]{4}', UNICODE_CHARACTER),
    (r'\\U[0-9A-Fa-f]{8}', UNICODE_WIDE_CHARACTER),
    (r'[\\].', ESCAPED_CHARACTER),
    (r'\'\'', SINGLE_QUOTE),
    (r'\'.*?\'', QUOTED_STRING),
    (':[^:]+:', CHAR_CLASS),
    # Char set
    ('\[[^\[\]]+\]', CHAR_SET),
    ('\[.*\]', CHAR_MULTI_SET),
    ('\[', OPEN_SET),
    ('\]', CLOSE_SET),
    ('&', INTERSECTION),
    ('-', DIFFERENCE),
    ('\$', WORD_BOUNDARY),
    (ur'[\ud800-\udbff][\udc00-\udfff]', WIDE_CHARACTER),
    (r'\{[^\s]+\}', BRACKETED_CHARACTER),
    (r'[^\s]', CHARACTER),
])

NUM_CHARS = 65536

all_chars = set([unichr(i) for i in xrange(NUM_CHARS)])

control_chars = set([c for c in all_chars if unicodedata.category(c) in ('Cc', 'Cn', 'Cs')])


def get_transforms():
    return [f for f in os.listdir(CLDR_TRANSFORMS_DIR) if f.endswith('.xml')]


def replace_html_entity(ent):
    name = ent.strip('&;')
    return unichr(htmlentitydefs.name2codepoint[name])


def parse_regex_char_range(regex):
    prev_char = None
    ranges = range_regex.findall(regex)
    regex = range_regex.sub('', regex)
    chars = [ord(c) for c in regex]

    for start, end in ranges:

        if ord(end) > ord(start):
            # Ranges are inclusive
            chars.extend([unichr(c) for c in range(ord(start), ord(end) + 1)])

    return chars


def parse_regex_char_class(c, current_filter=all_chars):
    chars = []
    orig = c
    if c.startswith('\\p'):
        c = c.split('{')[-1].split('}')[0]

    c = c.strip(': ')
    is_negation = False
    if c.startswith('^'):
        is_negation = True
        c = c.strip('^')

    if '=' in c:
        prop, value = c.split('=')
        prop = unicode_property_aliases.get(prop.lower(), prop)

        value = unicode_property_value_aliases.get(prop.lower(), {}).get(value, value)

        if prop == COMBINING_CLASS_PROP:
            chars = unicode_combining_classes[value]
        elif prop == GENERAL_CATEGORY_PROP:
            chars = unicode_categories.get(value, unicode_general_categories[value])
        elif prop == BLOCK_PROP:
            chars = unicode_blocks[value.lower()]
        elif prop == SCRIPT_PROP:
            chars = unicode_scripts[value.lower()]
        elif prop == WORD_BREAK_PROP:
            chars = unicode_word_breaks[value]
        else:
            raise TransliterationParseError(c)
    else:
        c = c.replace('-', '_').replace(' ', '_')

        if c.lower() in unicode_property_aliases:
            c = unicode_property_aliases[c.lower()]
        elif c.lower() in unicode_category_aliases:
            c = unicode_category_aliases[c.lower()]

        if c in unicode_general_categories:
            chars = unicode_general_categories[c]
        elif c in unicode_categories:
            chars = unicode_categories[c]
        elif c.lower() in unicode_properties:
            chars = unicode_properties[c.lower()]

        elif c.lower() in unicode_scripts:
            chars = unicode_scripts[c.lower()]
        elif c.lower() in unicode_properties:
            chars = unicode_properties[c.lower()]
        else:
            raise TransliterationParseError(c)

    if is_negation:
        chars = current_filter - set(chars)

    return sorted((set(chars) & current_filter) - control_chars)


def parse_balanced_sets(s):
    open_brackets = 0
    max_nesting = 0

    skip = False

    for i, ch in enumerate(s):
        if ch == '[':
            if open_brackets == 0:
                start = i
            max_nesting
            open_brackets += 1
        elif ch == ']':
            open_brackets -= 1
            if open_brackets == 0:
                skip = False
                yield (s[start:i + 1], CHAR_MULTI_SET)
                (start, i + 1)
        elif open_brackets == 0 and not skip:
            for token, token_class in char_set_scanner.scan(s[i:]):
                if token_class not in (CHAR_SET, CHAR_MULTI_SET, OPEN_SET, CLOSE_SET):
                    yield token, token_class
                else:
                    break
            skip = True


def parse_regex_char_set(s, current_filter=all_chars):
    '''
    Given a regex character set, which may look something like:

    [[:Latin:][:Greek:] & [:Ll:]]
    [A-Za-z_]
    [ $lowerVowel $upperVowel ]

    Parse into a single, flat character set without the unicode properties,
    ranges, unions/intersections, etc.
    '''

    s = s[1:-1]
    is_negation = False
    this_group = set()
    is_intersection = False
    is_difference = False
    is_word_boundary = False

    real_chars = set()

    for token, token_class in parse_balanced_sets(s):
        if token_class == CHAR_RANGE:
            this_char_set = set(parse_regex_char_range(token))
            this_group |= this_char_set
        elif token_class == ESCAPED_CHARACTER:
            token = token.strip('\\')
            this_group.add(token)
            real_chars.add(token)
        elif token_class == SINGLE_QUOTE:
            t = "'"
            this_group.add(t)
            real_chars.add(t)
        elif token_class == QUOTED_STRING:
            t = token.strip("'")
            this_group.add(t)
            real_chars.add(t)
        elif token_class == NEGATION:
            is_negation = True
        elif token_class in (CHAR_CLASS, CHAR_CLASS_PCRE):
            this_group |= set(parse_regex_char_class(token, current_filter=current_filter))
        elif token_class in (CHAR_SET, CHAR_MULTI_SET):
            # Recursive calls, as performance doesn't matter here and nesting is shallow
            this_char_set = set(parse_regex_char_set(token, current_filter=current_filter))
            if is_intersection:
                this_group &= this_char_set
                is_intersection = False
            elif is_difference:
                this_group -= this_char_set
                is_difference = False
            else:
                this_group |= this_char_set
        elif token_class == INTERSECTION:
            is_intersection = True
        elif token_class == DIFFERENCE:
            is_difference = True
        elif token_class == CHARACTER and token not in control_chars:
            this_group.add(token)
            real_chars.add(token)
        elif token_class == UNICODE_CHARACTER:
            token = token.decode('unicode-escape')
            if token not in control_chars:
                this_group.add(token)
                real_chars.add(token)
        elif token_class in (WIDE_CHARACTER, UNICODE_WIDE_CHARACTER):
            continue
        elif token_class == BRACKETED_CHARACTER:
            if token.strip('{{}}') not in control_chars:
                this_group.add(token)
                real_chars.add(token)
        elif token_class == WORD_BOUNDARY:
            is_word_boundary = True

    if is_negation:
        this_group = current_filter - this_group

    return sorted((this_group & (current_filter | real_chars)) - control_chars) + ([WORD_BOUNDARY_CHAR] if is_word_boundary else [])


for name, regex_range in unicode_property_regexes:
    unicode_properties[name] = parse_regex_char_set(regex_range)


def get_source_and_target(xml):
    return xml.xpath('//transform/@source')[0], xml.xpath('//transform/@target')[0]


def get_raw_rules_and_variables(xml):
    '''
    Parse tRule nodes from the transform XML

    At this point we only care about lvalue, op and rvalue
    for parsing forward and two-way transforms.

    Variables are collected in a dictionary in this pass so they can be substituted later
    '''
    rules = []
    variables = {}

    in_compound_rule = False
    compound_rule = []

    for rule in xml.xpath('*//tRule'):
        if not rule.text:
            continue

        rule = safe_decode(rule.text.rsplit(COMMENT_CHAR)[0].strip())
        if rule not in rule_map:
            rule = literal_space_regex.sub(replace_literal_space, rule)
            rule = rule.rstrip(END_CHAR).strip()
        else:
            rule = rule_map[rule]

        if rule.strip().endswith('\\'):
            compound_rule.append(rule.rstrip('\\'))
            in_compound_rule = True
            continue
        elif in_compound_rule:
            compound_rule.append(rule)
            rule = u''.join(compound_rule)
            in_compound_rule = False
            compound_rule = []

        assignment = assignment_regex.match(rule)
        transform = transform_regex.match(rule)
        pre_transform = pre_transform_full_regex.match(rule)

        if pre_transform:
            rules.append((PRE_TRANSFORM, pre_transform.group(1)))
        elif assignment:
            lvalue, rvalue = assignment.groups()
            var_name = lvalue.strip().lstrip('$')
            rvalue = rvalue.strip()
            variables[var_name] = rvalue
        elif transform:
            lvalue, op, rvalue = transform.groups()
            lvalue = lvalue.strip()
            rvalue = rvalue.strip()

            if op in FORWARD_TRANSFORM_OPS:
                rules.append((FORWARD_TRANSFORM, (lvalue, rvalue)))
            elif op in BIDIRECTIONAL_TRANSFORM_OPS:
                rules.append((BIDIRECTIONAL_TRANSFORM, (lvalue, rvalue)))
            elif op in BACKWARD_TRANSFORM_OPS:
                rules.append((BACKWARD_TRANSFORM, (lvalue, rvalue)))

    return rules, variables

CHAR_CLASSES = set([
    ESCAPED_CHARACTER,
    CHAR_CLASS,
    QUOTED_STRING,
    CHARACTER,
    GROUP_REF,
])


def char_permutations(s, current_filter=all_chars):
    '''
    char_permutations

    Parses the lvalue or rvalue of a transform rule into
    a list of character permutations, in addition to keeping
    track of revisits and regex groups
    '''
    char_types = []
    move = 0
    in_revisit = False

    in_group = False
    last_token_group_start = False

    start_group = 0
    end_group = 0

    open_brackets = 0
    current_set = []

    groups = []

    for token, token_type in transform_scanner.scan(s):
        if open_brackets > 0 and token_type not in (OPEN_SET, CLOSE_SET):
            current_set.append(token)
            continue

        if token_type == ESCAPED_CHARACTER:
            char_types.append([token.strip('\\')])
        elif token_type == OPEN_GROUP:
            in_group = True
            last_token_group_start = True
        elif token_type == CLOSE_GROUP:
            in_group = False
            end_group = len(char_types)
            groups.append((start_group, end_group))
        elif token_type == OPEN_SET:
            open_brackets += 1
            current_set.append(token)
        elif token_type == CLOSE_SET:
            open_brackets -= 1
            current_set.append(token)
            if open_brackets == 0:
                char_set = parse_regex_char_set(u''.join(current_set), current_filter=current_filter)
                if char_set:
                    char_types.append(char_set)
                current_set = []
        elif token_type == QUOTED_STRING:
            token = token.strip("'")
            for c in token:
                char_types.append([c])
        elif token_type == GROUP_REF:
            char_types.append([token.replace('$', GROUP_INDICATOR_CHAR)])
        elif token_type == REVISIT:
            in_revisit = True
        elif token_type == REPEAT:
            char_types.append([REPEAT_ZERO_CHAR])
        elif token_type == PLUS:
            char_types.append([REPEAT_ONE_CHAR])
        elif token_type == OPTIONAL:
            char_types[-1].append(EMPTY_TRANSITION_CHAR)
        elif token_type == REVISIT:
            in_revisit = True
        elif token_type == HTML_ENTITY:
            char_types.append([replace_html_entity(token)])
        elif token_type == CHARACTER:
            char_types.append([token])
        elif token_type == SINGLE_QUOTE:
            char_types.append(["'"])
        elif token_type == UNICODE_CHARACTER:
            token = token.decode('unicode-escape')
            char_types.append([token])
        elif token_type in (WIDE_CHARACTER, UNICODE_WIDE_CHARACTER):
            continue

        if in_group and last_token_group_start:
            start_group = len(char_types)
            last_token_group_start = False

        if in_revisit and token_type in CHAR_CLASSES:
            move += 1

    return char_types, move, groups

    return list(itertools.product(char_types)), move

string_replacements = {
    u'[': u'\[',
    u']': u'\]',
    u'(': u'\(',
    u')': u'\)',
    u'{': u'\{',
    u'}': u'\{',
    u'$': u'\$',
    u'^': u'\^',
    u'-': u'\-',
    u'\\': u'\\\\',
    u'*': u'\*',
    u'+': u'\+',
}

escape_sequence_long_regex = re.compile(r'(\\x[0-9a-f]{2})([0-9a-f])', re.I)


def replace_long_escape_sequence(s):
    def replace_match(m):
        return u'{}""{}'.format(m.group(1), m.group(2))

    return escape_sequence_long_regex.sub(replace_match, s)


def quote_string(s):
    return u'"{}"'.format(replace_long_escape_sequence(safe_decode(s).replace('"', '\\"')))


def char_types_string(char_types):
    '''
    Transforms the char_permutations output into a string
    suitable for simple parsing in C (characters and character sets only,
    no variables, unicode character properties or unions/intersections)
    '''
    ret = []

    for chars in char_types:
        template = u'{}' if len(chars) == 1 else u'[{}]'
        norm = []
        for c in chars:
            c = string_replacements.get(c, c)
            norm.append(c)

        ret.append(template.format(u''.join(norm)))

    return u''.join(ret)


def format_groups(char_types, groups):
    group_regex = []
    last_end = 0
    for start, end in groups:
        group_regex.append(char_types_string(char_types[last_end:start]))
        group_regex.append(u'(')
        group_regex.append(char_types_string(char_types[start:end]))
        group_regex.append(u')')
        last_end = end
    group_regex.append(char_types_string(char_types[last_end:]))
    return u''.join(group_regex)

charset_regex = re.compile(r'(?<!\\)\[')


def escape_string(s):
    return s.encode('string-escape')


def format_rule(rule):
    '''
    Creates the C literal for a given transliteration rule
    '''
    key = safe_encode(rule[0])
    key_len = len(key)

    pre_context_type = rule[1]
    pre_context = rule[2]
    if pre_context is None:
        pre_context = 'NULL'
        pre_context_len = 0
    else:
        pre_context = safe_encode(pre_context)
        pre_context_len = len(pre_context)
        pre_context = quote_string(escape_string(pre_context))

    pre_context_max_len = rule[3]

    post_context_type = rule[4]
    post_context = rule[5]

    if post_context is None:
        post_context = 'NULL'
        post_context_len = 0
    else:
        post_context = safe_encode(post_context)
        post_context_len = len(post_context)
        post_context = quote_string(escape_string(post_context))

    post_context_max_len = rule[6]

    groups = rule[7]
    if not groups:
        groups = 'NULL'
        groups_len = 0
    else:
        groups = safe_encode(groups)
        groups_len = len(groups)
        groups = quote_string(escape_string(groups))

    replacement = safe_encode(rule[8])
    replacement_len = len(replacement)
    move = rule[9]

    output_rule = (
        quote_string(escape_string(key)),
        str(key_len),
        pre_context_type,
        str(pre_context_max_len),
        pre_context,
        str(pre_context_len),

        post_context_type,
        str(post_context_max_len),
        post_context,
        str(post_context_len),

        quote_string(escape_string(replacement)),
        str(replacement_len),
        str(move),
        groups,
        str(groups_len),
    )

    return output_rule


def parse_transform_rules(xml):
    '''
    parse_transform_rules takes a parsed xml document as input
    and generates rules suitable for use in the C code.

    Since we're only concerned with transforming into Latin/ASCII,
    we don't care about backward transforms or two-way contexts.
    Only the lvalue's context needs to be used.
    '''
    rules, variables = get_raw_rules_and_variables(xml)

    def get_var(m):
        return variables.get(m.group(1))

    # Replace variables within variables
    while True:
        num_found = 0
        for k, v in variables.items():
            if var_regex.search(v):
                v = var_regex.sub(get_var, v)
                variables[k] = v
                num_found += 1
        if num_found == 0:
            break

    variables[WORD_BOUNDARY_VAR_NAME] = WORD_BOUNDARY_VAR
    variables[START_OF_HAN_VAR_NAME] = START_OF_HAN_VAR

    current_filter = all_chars

    for rule_type, rule in rules:
        if rule_type in (BIDIRECTIONAL_TRANSFORM, FORWARD_TRANSFORM):
            left, right = rule

            left = var_regex.sub(get_var, left)
            right = var_regex.sub(get_var, right)

            left_pre_context = None
            left_post_context = None
            have_post_context = False
            current_token = []

            in_set = False
            in_group = False
            open_brackets = 0

            for token, token_type in rule_scanner.scan(left):
                if token_type == ESCAPED_CHARACTER:
                    current_token.append(token)
                elif token_type == OPEN_SET:
                    in_set = True
                    open_brackets += 1
                    current_token.append(token)
                elif token_type == CLOSE_SET:
                    open_brackets -= 1
                    current_token.append(token)
                    if open_brackets == 0:
                        in_set = False
                elif token_type == BEFORE_CONTEXT and not in_set:
                    left_pre_context = u''.join(current_token)

                    current_token = []
                elif token_type == AFTER_CONTEXT and not in_set:
                    have_post_context = True
                    left = u''.join(current_token)
                    current_token = []
                else:
                    current_token.append(token)

            if have_post_context:
                left_post_context = u''.join(current_token)
            else:
                left = u''.join(current_token).strip()

            right_pre_context = None
            right_post_context = None
            have_post_context = False
            current_token = []

            in_set = False
            in_group = False
            open_brackets = 0

            for token, token_type in rule_scanner.scan(right):
                if token_type == OPEN_SET:
                    in_set = True
                    open_brackets += 1
                    current_token.append(token)
                elif token_type == CLOSE_SET:
                    open_brackets -= 1
                    current_token.append(token)
                    if open_brackets == 0:
                        in_set = False
                elif token_type == BEFORE_CONTEXT and not in_set:
                    right_pre_context = u''.join(current_token)
                    current_token = []
                elif token_type == AFTER_CONTEXT and not in_set:
                    have_post_context = True
                    right = u''.join(current_token)
                    current_token = []
                else:
                    current_token.append(token)

            if have_post_context:
                right_post_context = u''.join(current_token)
            else:
                right = u''.join(current_token)

            if start_of_han_regex.search(left) or start_of_han_regex.search(right):
                continue

            left_pre_context_max_len = 0
            left_post_context_max_len = 0

            left_pre_context_type = CONTEXT_TYPE_NONE
            left_post_context_type = CONTEXT_TYPE_NONE

            move = 0
            left_groups = []
            right_groups = []

            if left_pre_context:
                if left_pre_context.strip() == WORD_BOUNDARY_VAR:
                    left_pre_context = None
                    left_pre_context_type = CONTEXT_TYPE_WORD_BOUNDARY
                elif left_pre_context.strip() == START_OF_HAN_VAR:
                    left_pre_context = None
                    left_pre_context_type = CONTEXT_TYPE_NONE
                elif left_pre_context.strip():
                    left_pre_context, _, _ = char_permutations(left_pre_context.strip(), current_filter=current_filter)
                    if left_pre_context:
                        left_pre_context_max_len = len(left_pre_context or [])
                        left_pre_context = char_types_string(left_pre_context)

                        if charset_regex.search(left_pre_context):
                            left_pre_context_type = CONTEXT_TYPE_REGEX
                        else:
                            left_pre_context_type = CONTEXT_TYPE_STRING
                    else:
                        left_pre_context = None
                        left_pre_context_type = CONTEXT_TYPE_NONE
            else:
                left_pre_context = None
                left_pre_context_type = CONTEXT_TYPE_NONE

            if left:
                left_chars, _, left_groups = char_permutations(left.strip(), current_filter=current_filter)
                if not left_chars and (left.strip() or not (left_pre_context and left_post_context)):
                    print 'ignoring', rule
                    continue
                if left_groups:
                    left_groups = format_groups(left_chars, left_groups)
                else:
                    left_groups = None
                left = char_types_string(left_chars)

            if left_post_context:
                if left_post_context.strip() == WORD_BOUNDARY_VAR:
                    left_post_context = None
                    left_post_context_type = CONTEXT_TYPE_WORD_BOUNDARY
                elif left_post_context.strip() == START_OF_HAN_VAR:
                    left_pre_context_type = None
                    left_pre_context_type = CONTEXT_TYPE_NONE
                elif left_post_context.strip():
                    left_post_context, _, _ = char_permutations(left_post_context.strip(), current_filter=current_filter)
                    if left_post_context:
                        left_post_context_max_len = len(left_post_context or [])
                        left_post_context = char_types_string(left_post_context)
                        if charset_regex.search(left_post_context):
                            left_post_context_type = CONTEXT_TYPE_REGEX
                        elif left_post_context:
                            left_post_context_type = CONTEXT_TYPE_STRING
                    else:
                        left_post_context = None
                        left_post_context_type = CONTEXT_TYPE_NONE
            else:
                left_post_context = None
                left_post_context_type = CONTEXT_TYPE_NONE

            if right:
                right, move, right_groups = char_permutations(right.strip(), current_filter=current_filter)
                right = char_types_string(right)

            yield RULE, (left, left_pre_context_type, left_pre_context, left_pre_context_max_len,
                         left_post_context_type, left_post_context, left_post_context_max_len, left_groups, right, move)
        elif rule_type == PRE_TRANSFORM and rule.strip(': ').startswith('('):
            continue
        elif rule_type == PRE_TRANSFORM and '[' in rule and ']' in rule:
            filter_rule = regex_char_set_greedy.search(rule)    
            current_filter = set(parse_regex_char_set(filter_rule.group(0)))
        elif rule_type == PRE_TRANSFORM:
            pre_transform = pre_transform_regex.search(rule)
            if pre_transform:
                yield TRANSFORM, pre_transform.group(1)


STEP_RULESET = 'STEP_RULESET'
STEP_TRANSFORM = 'STEP_TRANSFORM'
STEP_UNICODE_NORMALIZATION = 'STEP_UNICODE_NORMALIZATION'


NEW_STEP = 'NEW_STEP'
EXISTING_STEP = 'EXISTING_STEP'

# Extra rules defined here
supplemental_transliterations = {
    'latin-ascii': (EXISTING_STEP, [
        # German transliterations not handled by standard NFD normalization
        # ä => ae
        (u'"\\xc3\\xa4"', '2', CONTEXT_TYPE_NONE, '0', 'NULL', '0', CONTEXT_TYPE_NONE, '0', 'NULL', '0', u'"ae"', '2', '0', 'NULL', '0'),
        # ö => oe
        (u'"\\xc3\\xb6"', '2', CONTEXT_TYPE_NONE, '0', 'NULL', '0', CONTEXT_TYPE_NONE, '0', 'NULL', '0', u'"oe"', '2', '0', 'NULL', '0'),
        # ü => ue
        (u'"\\xc3\\xbc"', '2', CONTEXT_TYPE_NONE, '0', 'NULL', '0', CONTEXT_TYPE_NONE, '0', 'NULL', '0', u'"ue"', '2', '0', 'NULL', '0'),
        # ß => ss
        (u'"\\xc3\\x9f"', '2', CONTEXT_TYPE_NONE, '0', 'NULL', '0', CONTEXT_TYPE_NONE, '0', 'NULL', '0', u'"ss"', '2', '0', 'NULL', '0'),

    ]),
}


def get_all_transform_rules():
    transforms = {}
    to_latin = set()

    retain_transforms = set()

    init_unicode_categories()

    all_transforms = set([name.split('.xml')[0].lower() for name in get_transforms()])

    name_aliases = {}

    for filename in get_transforms():
        name = name = filename.split('.xml')[0].lower()

        f = open(os.path.join(CLDR_TRANSFORMS_DIR, filename))
        xml = etree.parse(f)
        source, target = get_source_and_target(xml)
        name_alias = '-'.join([source.lower(), target.lower()])
        if name_alias not in name_aliases:
            name_aliases[name_alias] = name

    dependencies = defaultdict(list)

    for filename in get_transforms():
        name = filename.split('.xml')[0].lower()

        f = open(os.path.join(CLDR_TRANSFORMS_DIR, filename))
        xml = etree.parse(f)
        source, target = get_source_and_target(xml)

        if name in EXCLUDE_TRANSLITERATORS:
            continue

        if (target.lower() == 'latin' or name == 'latin-ascii'):
            to_latin.add(name)
            retain_transforms.add(name)

        print 'doing', filename

        steps = []
        rule_set = []

        for rule_type, rule in parse_transform_rules(xml):
            if rule_type == RULE:
                rule = format_rule(rule)
                rule_set.append(rule)
            elif rule_type == TRANSFORM:
                if rule_set:
                    steps.append((STEP_RULESET, rule_set))
                    rule_set = []

                if rule.lower() in all_transforms and rule.lower() not in EXCLUDE_TRANSLITERATORS:
                    dependencies[name].append(rule.lower())
                    steps.append((STEP_TRANSFORM, rule.lower()))
                elif rule.lower() in name_aliases and rule.lower() not in EXCLUDE_TRANSLITERATORS:
                    dep = name_aliases[rule.lower()]
                    dependencies[name].append(dep)
                    steps.append((STEP_TRANSFORM, dep))
                elif rule.split('-')[0].lower() in all_transforms and rule.split('-')[0].lower() not in EXCLUDE_TRANSLITERATORS:
                    dependencies[name].append(rule.split('-')[0].lower())
                    steps.append((STEP_TRANSFORM, rule.split('-')[0].lower()))

                rule = UTF8PROC_TRANSFORMS.get(rule, rule)
                if rule in UNICODE_NORMALIZATION_TRANSFORMS:
                    steps.append((STEP_UNICODE_NORMALIZATION, rule))

        if rule_set:
            steps.append((STEP_RULESET, rule_set))

        transforms[name] = steps

    dependency_queue = deque(to_latin)
    retain_transforms |= to_latin

    seen = set()

    while dependency_queue:
        name = dependency_queue.popleft()
        for dep in dependencies.get(name, []):
            retain_transforms.add(dep)
            if dep not in seen:
                dependency_queue.append(dep)
                seen.add(dep)

    all_rules = []
    all_steps = []
    all_transforms = []

    for name, steps in transforms.iteritems():
        if name in supplemental_transliterations:
            step_type, rules = supplemental_transliterations[name]
            if step_type == EXISTING_STEP:
                steps[-1][1].extend(rules)
            else:
                steps[-1].append((STEP_RULESET, rules))
        # Only care if it's a transform to Latin/ASCII or a dependency
        # for a transform to Latin/ASCII
        elif name not in retain_transforms:
            continue
        step_index = len(all_steps)
        num_steps = len(steps)
        for i, (step_type, data) in enumerate(steps):
            if step_type == STEP_RULESET:
                rule_index = len(all_rules)
                num_rules = len(data)
                step = (STEP_RULESET, str(rule_index), str(num_rules), quote_string(str(i)))
                all_rules.extend(data)
            elif step_type == STEP_TRANSFORM:
                step = (STEP_TRANSFORM, '-1', '-1', quote_string(data))
            elif step_type == STEP_UNICODE_NORMALIZATION:
                step = (STEP_UNICODE_NORMALIZATION, '-1', '-1', quote_string(data))
            all_steps.append(step)

        internal = int(name not in to_latin)

        transliterator = (quote_string(name), str(internal), str(step_index), str(num_steps))
        all_transforms.append(transliterator)

    return all_transforms, all_steps, all_rules


transliteration_data_template = u'''#include <stdlib.h>

transliteration_rule_source_t rules_source[] = {{
    {all_rules}
}};

transliteration_step_source_t steps_source[] = {{
    {all_steps}
}};

transliterator_source_t transliterators_source[] = {{
    {all_transforms}
}};

'''

transliterator_script_data_template = u'''
#ifndef TRANSLITERATION_SCRIPTS_H
#define TRANSLITERATION_SCRIPTS_H

#include <stdlib.h>
#include "unicode_scripts.h"
#include "transliterate.h"

typedef struct script_transliteration_rule {{
    script_type_t script;
    char *language;
    uint32_t index;
    uint32_t len;
}} script_transliteration_rule_t;

script_transliteration_rule_t script_transliteration_rules[] = {{
    {rules}
}};

char *script_transliterators[] = {{
    {transliterators}
}}

#endif
'''


script_transliterators = {
    'arabic': {None: ['arabic-latin', 'arabic-latin-bgn'],
               'fa': ['persian-latin-bgn'],
               'ps': ['pashto-latin-bgn'],
               },
    'armenian': {None: ['armenian-latin-bgn']},
    'balinese': None,
    'bamum': None,
    'batak': None,
    'bengali': {None: ['bengali-latin']},
    'bopomofo': None,
    'braille': None,
    'buginese': None,
    'buhid': None,
    'canadian_aboriginal': {None: ['canadianaboriginal-latin']},
    'cham': None,
    'cherokee': None,
    'common': {None: ['latin-ascii']},
    'coptic': None,
    'cyrillic': {None: ['cyrillic-latin'],
                 'be': ['belarusian-latin-bgn'],
                 'ru': ['russian-latin-bgn'],
                 'bg': ['bulgarian-latin-bgn'],
                 'kk': ['kazakh-latin-bgn'],
                 'ky': ['kirghiz-latin-bgn'],
                 'mk': ['macedonian-latin-bgn'],
                 'mn': ['mongolian-latin-bgn'],
                 'sr': ['serbian-latin-bgn'],
                 'uk': ['ukrainian-latin-bgn'],
                 'uz': ['uzbek-latin-bgn'],
                 },
    'devanagari': {None: ['devanagari-latin']},
    'ethiopic': None,
    'georgian': {None: ['georgian-latin', 'georgian-latin-bgn']},
    'glagolitic': None,
    'greek': {None: ['greek-latin', 'greek-latin-bgn', 'greek_latin_ungegn']},
    'gujarati': {None: ['gujarati-latin']},
    'gurmukhi': {None: ['gurmukhi-latin']},
    'han': {None: ['han-latin']},
    'hangul': {None: ['korean-latin-bgn']},
    'hanunoo': None,
    'hebrew': {None: ['hebrew-latin', 'hebrew-latin-bgn']},
    'hiragana': {None: ['hiragana-latin']},
    'inherited': None,
    'javanese': None,
    'kannada': {None: ['kannada-latin']},
    'katakana': {None: ['katakana-latin-bgn']},
    'kayah_li': None,
    'khmer': None,
    'lao': None,
    'latin': {None: ['latin-ascii']},
    'lepcha': None,
    'limbu': None,
    'lisu': None,
    'malayalam': {None: ['malayam-latin']},
    'mandaic': None,
    'meetei_mayek': None,
    'mongolian': None,
    'myanmar': None,
    'new_tai_lue': None,
    'nko': None,
    'ogham': None,
    'ol_chiki': None,
    'oriya': {None: ['oriya-latin']},
    'phags_pa': None,
    'rejang': None,
    'runic': None,
    'samaritan': None,
    'saurashtra': None,
    'sinhala': None,
    'sundanese': None,
    'syloti_nagri': None,
    'syriac': None,
    'tagalog': None,
    'tagbanwa': None,
    'tai_le': None,
    'tai_tham': None,
    'tai_viet': None,
    'tamil': {None: ['tamil-latin']},
    'telugu': {None: ['telugu-latin']},
    'thaana': None,
    'thai': {None: ['thai-latin']},
    'tibetan': None,
    'tifinagh': None,
    'unknown': None,
    'vai': None,
    'yi': None
}


def write_transliterator_scripts_file(filename):
    transliterator_rule_template = '''{{{script_type}, {lang}, {start}, {length}}}'''
    rules = []
    all_transliterators = []
    index = 0
    for script, i in unicode_script_ids.iteritems():
        spec = script_transliterators.get(script.lower())
        if not spec:
            continue
        script_type = 'SCRIPT_{}'.format(script.upper())
        for lang, transliterators in spec.iteritems():
            lang = 'NULL' if not lang else quote_string(lang)
            num_transliterators = len(transliterators)
            rules.append(transliterator_rule_template.format(script_type=script_type,
                         lang=lang, start=index, length=num_transliterators))
            for trans in transliterators:
                all_transliterators.append(quote_string(trans))

            index += num_transliterators

    template = transliterator_script_data_template.format(rules=''',
    '''.join(rules), transliterators=''',
    '''.join(all_transliterators))

    f = open(filename, 'w')
    f.write(safe_encode(template))


def write_transliteration_data_file(filename):
    transforms, steps, rules = get_all_transform_rules()

    all_transforms = u''',
    '''.join([u'{{{}}}'.format(u','.join(t)) for t in transforms])

    all_steps = u''',
    '''.join([u'{{{}}}'.format(u','.join(s)) for s in steps])

    all_rules = u''',
    '''.join([u'{{{}}}'.format(u','.join(r)) for r in rules])

    template = transliteration_data_template.format(
        all_transforms=all_transforms,
        all_steps=all_steps,
        all_rules=all_rules
    )

    f = open(filename, 'w')
    f.write(safe_encode(template))


TRANSLITERATION_DATA_FILENAME = 'transliteration_data.c'
TRANSLITERATION_SCRIPTS_FILENAME = 'transliteration_scripts.h'


def main(out_dir):
    write_transliteration_data_file(os.path.join(out_dir, TRANSLITERATION_DATA_FILENAME))

    write_transliterator_scripts_file(os.path.join(out_dir, TRANSLITERATION_SCRIPTS_FILENAME))

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print 'Usage: python transliteration_rules.py out_dir'
        exit(1)
    main(sys.argv[1])