# =============================================================================
# Copyright (c) 2020. Giuseppe Attardi (attardi@di.unipi.it).
# =============================================================================
# This file is part of Tanl.
#
# Tanl is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License, version 3,
# as published by the Free Software Foundation.
#
# Tanl is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
# =============================================================================

import wikiextractor.extract as _ex
from wikiextractor.extract import Extractor, ignoreTag


def clean_markup(markup, keep_links=False, ignore_headers=True):
    """
    Clean Wikimarkup to produce plaintext.
    :param keep_links: keep literal HTML <a> tags in the output (escaped,
        not live) instead of stripping them. Does not affect [[wikilinks]]
        or [external links], which always reduce to their display text --
        that's controlled separately, by the Extractor's own keepLinks.
    :param ignore_headers: if set to True, the output list will not contain
    headers, only paragraphs.

    Returns a list of paragraphs (unicode strings).
    """
    # Own list, passed straight into the constructor -- no shared,
    # module-level list to save/restore around this call anymore.
    # ignoreTag() is a pure function (returns the compiled pattern
    # rather than appending it anywhere), and Extractor itself no
    # longer has any class- or module-level state of this kind at all.
    ignored_tag_patterns = list(_ex._DEFAULT_IGNORED_TAG_PATTERNS)
    if not keep_links:
        ignored_tag_patterns.append(ignoreTag('a'))
    # id/revid/urlbase/title/page are placeholders: markup is
    # passed directly to clean_text() below rather than via
    # self.page, which only Extractor.extract() reads.
    extractor = Extractor(0, '', '', '', [], ignored_tag_patterns=ignored_tag_patterns)
    paragraphs = extractor.clean_text(markup,
                                       mark_headers=True,
                                       expand_templates=False,
                                       html_safe=True)
    if ignore_headers:
        paragraphs = filter(lambda s: not s.startswith('## '), paragraphs)
    return paragraphs
