# =============================================================================
#  Copyright (c) 2020. Giuseppe Attardi (attardi@di.unipi.it).
# =============================================================================
#  This file is part of Tanl.
#
#  Tanl is free software; you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License, version 3,
#  as published by the Free Software Foundation.
#
#  Tanl is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
# =============================================================================

from wikiextractor.extract import Extractor, ignoreTag, resetIgnoredTags


def clean_markup(markup, keep_links=False, ignore_headers=True):
    """
    Clean Wikimarkup to produce plaintext.

    :param keep_links: Set to True to keep internal and external links
    :param ignore_headers: if set to True, the output list will not contain
    headers, only 

    Returns a list of paragraphs (unicode strings).
    """

    if not keep_links:
        ignoreTag('a')

    extractor = Extractor(0, '', [])

    # returns a list of strings
    paragraphs = extractor.clean_text(markup,
                                      mark_headers=True,
                                      expand_templates=False,
                                      escape_doc=True)
    resetIgnoredTags()

    if ignore_headers:
        paragraphs = filter(lambda s: not s.startswith('## '), paragraphs)

    return paragraphs
