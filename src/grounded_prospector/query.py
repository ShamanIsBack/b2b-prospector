"""Search query and prompt construction.

Two strings are built per agency and they do different jobs:

* :func:`build_xray_query` produces a literal Google search expression. It is the
  thing whose results we actually want.
* :func:`build_prompt` wraps that expression in an instruction telling the model
  to run it and cite what it finds.

The prompt exists only to steer *which pages get cited*. Its prose answer is
never parsed — see :mod:`grounded_prospector.extract`.
"""

from __future__ import annotations

from collections.abc import Sequence

# Restricting to the member-profile path keeps company pages and job ads out of
# the result set at the source, rather than filtering them afterwards.
_SITE_FILTER = "site:linkedin.com/in/"

# LinkedIn publishes paginated member *directories* under the same path. They
# match the site filter, rank well, and contain dozens of names belonging to
# nobody in particular -- excluding them at the query is far cheaper than
# discarding them after they have consumed result slots we paid for.
_EXCLUSIONS = ('-intitle:"profiles"', '-inurl:"dir/"')

SYSTEM_INSTRUCTION = (
    "You are a search dispatcher for a B2B research tool. Your only job is to run "
    "the Google search you are given and cite the LinkedIn profile pages it "
    "returns. Cite every distinct LinkedIn profile in the results, not just the "
    "most relevant one. Never state a person's name, employer or job title unless "
    "it appears in a page you are citing, and never fill gaps from prior "
    "knowledge. If the search returns no LinkedIn profiles, say so plainly."
)


def build_xray_query(
    agency: str,
    location: str,
    roles: Sequence[str],
    keywords: Sequence[str] = (),
) -> str:
    """Build the literal Google search expression for one agency.

    Terms are joined by spaces, which Google reads as AND -- equivalent to
    writing ``AND`` explicitly, and easier to read. Roles are OR-ed inside a
    single group so that widening the role list increases recall without costing
    additional queries.
    """
    parts = [_SITE_FILTER, f'"{agency}"']
    if location:
        parts.append(f'"{location}"')
    if roles:
        parts.append("(" + " OR ".join(f'"{role}"' for role in roles) + ")")
    if keywords:
        parts.append("(" + " OR ".join(f'"{keyword}"' for keyword in keywords) + ")")
    parts.extend(_EXCLUSIONS)
    return " ".join(parts)


def build_prompt(query: str, agency: str) -> str:
    """Wrap a search expression in an instruction for the grounding model."""
    return (
        f"Run this exact Google search:\n\n{query}\n\n"
        f"List every LinkedIn profile that appears in the results, and cite each "
        f"one. For each person, report only their name and headline exactly as "
        f"the search result shows them. I am looking for decision-makers at "
        f"{agency}. Do not include anyone who does not appear in the search "
        f"results, and do not describe people you cannot cite."
    )
