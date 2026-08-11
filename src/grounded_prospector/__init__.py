"""Find B2B decision-makers from public search results, without scraping.

The guiding rule of this package: every field about a person is parsed
deterministically from what a search engine returned. The default backend
(``serper``) has no language model in the data path at all; where one is used
(``gemini`` grounding) it is a *search dispatcher*, never a source of facts --
only its citation URLs and titles become data. See ``docs/DECISIONS.md``, ADR-003.
"""

__version__ = "0.1.0"
