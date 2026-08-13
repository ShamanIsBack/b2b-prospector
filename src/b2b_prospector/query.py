"""Search query and prompt construction.

Two strings are built per target and they do different jobs:

* :func:`build_xray_query` produces a literal Google search expression. It is the
  thing whose results we actually want.
* :func:`build_prompt` wraps that expression in an instruction telling the model
  to run it and cite what it finds.

The prompt exists only to steer *which pages get cited*. Its prose answer is
never parsed — see :mod:`b2b_prospector.extract`.
"""

from __future__ import annotations

from collections.abc import Sequence

from b2b_prospector.models import TargetKind

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
    target: str,
    location: str,
    roles: Sequence[str],
    keywords: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> str:
    """Build the literal Google search expression for one target.

    ``target`` is interpolated as a quoted phrase and nothing more. That is the
    whole reason a job-title phrase works in the slot as readily as a company
    name -- see :class:`b2b_prospector.models.TargetKind`.

    Terms are joined by spaces, which Google reads as AND -- equivalent to
    writing ``AND`` explicitly, and easier to read. Roles are OR-ed inside a
    single group so that widening the role list increases recall without costing
    additional queries.

    ``exclude`` terms become negative terms. They are a cheap first filter, not a
    guarantee: given a phrase with few matches a search engine loosens the query,
    and the negative terms loosen with it. Scoring re-checks them for that reason.
    """
    parts = [_SITE_FILTER, f'"{target}"']
    if location:
        parts.append(f'"{location}"')
    if roles:
        parts.append("(" + " OR ".join(f'"{role}"' for role in roles) + ")")
    if keywords:
        parts.append("(" + " OR ".join(f'"{keyword}"' for keyword in keywords) + ")")
    parts.extend(f'-"{term}"' for term in exclude)
    parts.extend(_EXCLUSIONS)
    return " ".join(parts)


def build_prompt(query: str, target: str, kind: TargetKind = TargetKind.COMPANY) -> str:
    """Wrap a search expression in an instruction for the grounding model.

    The closing sentence has to match the kind of target. Telling the model it is
    looking for "decision-makers at konsultant ślubny" describes an employer that
    does not exist, and steers it toward inventing one.
    """
    goal = (
        f"I am looking for people who describe themselves as {target!r}."
        if kind is TargetKind.PHRASE
        else f"I am looking for decision-makers at {target}."
    )
    return (
        f"Run this exact Google search:\n\n{query}\n\n"
        f"List every LinkedIn profile that appears in the results, and cite each "
        f"one. For each person, report only their name and headline exactly as "
        f"the search result shows them. {goal} Do not include anyone who does not "
        f"appear in the search results, and do not describe people you cannot cite."
    )
